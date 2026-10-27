"""Actual provider joins for selectable acquisition and retained cleanup owners."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.llm_providers.bedrock.factory import factory
from api_agent.llm_providers.bedrock.auth import BedrockAuthSession
from api_agent.providers.contracts import ContractError, InteractionRequiredError
from api_agent.providers.contracts import SessionClosedError


ADAPTER = '''
import time
from api_agent.llm_providers.bedrock.acquisition import SessionMaterial

class Adapter:
    version = 1
    def __init__(self, config, services):
        self.config, self.services = config, services
        self.closes = 0
    async def acquire(self, request, *, previous=None):
        self.services['requests'].append(request)
        if self.config.get('fail_acquire'):
            raise RuntimeError('synthetic-sensitive-adapter-detail')
        if request.credentials is not None and request.user_index is not None:
            await request.credentials(request.user_index, 'username')
        return SessionMaterial('temporary', user_index=request.user_index,
            access_key='synthetic-key', secret_key='synthetic-secret',
            session_token='synthetic-token', expires_at=time.time() + 120)
    async def aclose(self):
        self.closes += 1
        self.services['closes'].append(self.closes)
        if self.config.get('fail_first_close') and self.closes == 1:
            raise RuntimeError('synthetic-sensitive-cleanup-detail')

def factory(config, services):
    return Adapter(config, services)
'''


@pytest.mark.asyncio
async def test_static_access_key_session_is_opaque_callable_and_disposable():
    session = BedrockAuthSession({'mode': 'access_key', 'secret_key': 'synthetic-secret'})
    assert await session() is session
    assert 'synthetic-secret' not in repr(session)
    await session.aclose()
    with pytest.raises(SessionClosedError):
        await session()


def custom_provider(tmp_path, **config):
    (tmp_path / 'adapter.py').write_text(ADAPTER)
    services = {'owner_root': tmp_path, 'requests': [], 'closes': [],
                'bedrock_interaction': object()}
    return factory({'BEDROCK_SESSION_PROVIDER': 'adapter.py:factory',
                    'BEDROCK_REGION': 'us-east-1', **config}, services), services


@pytest.mark.asyncio
@pytest.mark.parametrize('interactive,no_input', [(True, False), (True, True), (False, False)])
async def test_custom_setup_preserves_context_and_interaction_permission(tmp_path, interactive, no_input):
    provider, services = custom_provider(tmp_path, NO_STD_IN=no_input)
    calls = []
    async def credentials(index, kind):
        calls.append((index, kind))
        return 'synthetic-value'
    session = await provider.asetup(interactive=interactive, user_index=2, credentials=credentials)
    request = services['requests'][0]
    assert request.user_index == 2
    assert request.interactive is (interactive and not no_input)
    assert request.interaction is (services['bedrock_interaction'] if interactive and not no_input else None)
    assert calls == [(2, 'username')]
    assert session.worker_config()['session_token'] == 'synthetic-token'
    assert 'synthetic-token' not in repr(session)
    await provider.aclose()
    assert services['closes'] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize('index,kind', [(True, 'username'), (3, 'username'), (2, 'other')])
async def test_acquisition_reader_rejects_different_index_or_field(tmp_path, index, kind):
    provider, services = custom_provider(tmp_path)
    credentials = AsyncMock(return_value='synthetic-value')
    await provider.asetup(interactive=False, user_index=2, credentials=credentials)
    credentials.reset_mock()
    with pytest.raises(InteractionRequiredError, match='selected account'):
        await services['requests'][0].credentials(index, kind)
    credentials.assert_not_awaited()
    await provider.aclose()


@pytest.mark.asyncio
async def test_unindexed_acquisition_cannot_read_an_indexed_credential(tmp_path):
    provider, services = custom_provider(tmp_path)
    provider.config['fail_acquire'] = True
    credentials = AsyncMock()
    with pytest.raises(ContractError, match='acquisition failed'):
        await provider.asetup(interactive=False, user_index=None, credentials=credentials)
    with pytest.raises(SessionClosedError, match='reader is closed'):
        await services['requests'][0].credentials(0, 'username')
    credentials.assert_not_awaited()
    assert services['closes'] == [1]
    await provider.aclose()


@pytest.mark.asyncio
async def test_reader_errors_are_safe_and_failed_setup_disposes_adapter(tmp_path):
    provider, services = custom_provider(tmp_path)
    credentials = AsyncMock(side_effect=RuntimeError('synthetic-sensitive-reader-detail'))
    with pytest.raises(ContractError) as caught:
        await provider.asetup(interactive=False, user_index=2, credentials=credentials)
    assert 'synthetic-sensitive' not in str(caught.value)
    assert services['closes'] == [1]
    await provider.aclose()


@pytest.mark.asyncio
async def test_failed_initial_cleanup_retains_owner_for_provider_retry(tmp_path):
    provider, services = custom_provider(tmp_path, fail_acquire=True, fail_first_close=True)
    with pytest.raises(ContractError, match='cleanup failed') as caught:
        await provider.asetup(interactive=False, user_index=None, credentials=None)
    assert provider.sessions == list(caught.value.retained_owners)
    assert len(provider.sessions) == 1
    await provider.aclose()
    assert provider.sessions == []
    assert services['closes'] == [1, 2]


@pytest.mark.asyncio
async def test_provider_cleanup_attempts_all_owners_and_retries_only_failures():
    provider = factory({}, {})
    first = SimpleNamespace(aclose=AsyncMock(side_effect=[RuntimeError('synthetic-detail'), None]))
    second = SimpleNamespace(aclose=AsyncMock())
    provider.sessions.extend([first, second])
    with pytest.raises(ContractError, match='retry disposal'):
        await provider.aclose()
    second.aclose.assert_awaited_once()
    assert provider.sessions == [first]
    await provider.aclose()
    assert first.aclose.await_count == 2
    assert provider.sessions == []


@pytest.mark.asyncio
async def test_concurrent_provider_close_is_idempotent():
    provider = factory({}, {})
    provider.sessions.append(SimpleNamespace(aclose=AsyncMock()))
    await asyncio.gather(provider.aclose(), provider.aclose())
    assert provider.sessions == []


def test_new_acquisition_setting_is_inert_in_real_schema():
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    root = Path(__file__).resolve().parents[2]
    override = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory'}
    schema = bootstrap_schema(load_shared_schema(), ['--help'], {}, {}, override, owner_root=root)
    result = resolve_config(schema, environ={}, overrides=override)
    assert result.values['BEDROCK_SESSION_PROVIDER'] == ''
    assert 'aws_login.py:factory' in schema.fields['BEDROCK_SESSION_PROVIDER'].help


@pytest.mark.asyncio
async def test_custom_acquirer_joins_parsed_flow_and_actual_local_llm(tmp_path, monkeypatch):
    import re
    import time
    from api_agent.providers.control import OperationContext
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.runtime.local_llm import LocalLLM
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.local_application import LocalChatApplication

    root = Path(__file__).resolve().parents[2]
    model = 'zai.glm-4.7-flash'
    adapter = tmp_path / 'adapter.py'
    adapter.write_text(ADAPTER.replace("self.services['requests']", "self.services.get('requests', [])")
                       .replace("self.services['closes']", "self.services.get('closes', [])"))
    config = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory',
              'BEDROCK_SESSION_PROVIDER': str(adapter) + ':factory', 'BEDROCK_API_MODE': 'completions', 'NO_STD_IN': True,
              'LLM_ALLOWED_MODELS': re.escape(model), 'LLM_DEFAULT_MODEL': re.escape(model)}
    async def forbidden(*args, **kwargs):
        raise AssertionError('Offline acquisition join cannot launch an AWS worker')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)
    owner = LocalLLM(config, root)
    try:
        await owner.ensure_ready()
        calls = []
        async def sdk(operation, payload, *, auth):
            assert auth.worker_config()['mode'] == 'temporary'
            assert auth.worker_config()['session_token'] == 'synthetic-token'
            calls.append(operation)
            if operation == 'models':
                return {'data': [{'id': model}]}
            return {'model': model, 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Ok'},
                'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}}
        owner.provider.implementation.transport.call = sdk
        definition = load_definition(root / 'flow.md.html', base_root=root, pack_root=root)
        application = LocalChatApplication(definition, config=config)
        context = OperationContext('offline-acquirer-flow', time.monotonic() + 30)
        try:
            result = await application.complete(LLMRequest(model, CompletionRequest((
                Message('user', (TextContent('Return Ok.'),)),))), llm=owner, context=context)
        finally:
            await context.aclose()
        assert result.payload.content == (TextContent('Ok'),)
        assert calls == ['completions']
    finally:
        await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('index', [-1, True, 1.0])
async def test_invalid_selected_index_precedes_adapter_and_credentials(tmp_path, index):
    provider, services = custom_provider(tmp_path)
    credentials = AsyncMock()
    with pytest.raises(InteractionRequiredError, match='selected account'):
        await provider.asetup(interactive=False, user_index=index, credentials=credentials)
    assert services['requests'] == []
    assert services['closes'] == []
    credentials.assert_not_awaited()
    await provider.aclose()


BLOCKING_ADAPTER = ADAPTER.replace(
    "self.services['requests'].append(request)",
    """self.services['requests'].append(request)
        self.services['entered'].set()
        try:
            await self.services['release'].wait()
        except __import__('asyncio').CancelledError:
            self.services['cancelled'].set()
            if not self.config.get('suppress_cancel'):
                raise
            await self.services['release'].wait()"""
).replace('self.closes += 1', """self.services['cleanup_entered'].set()
        await self.services['cleanup_release'].wait()
        self.closes += 1""")


def blocking_provider(tmp_path, **config):
    provider, services = custom_provider(tmp_path, **config)
    (tmp_path / 'adapter.py').write_text(BLOCKING_ADAPTER)
    services.update({name: asyncio.Event() for name in (
        'entered', 'release', 'cancelled', 'cleanup_entered', 'cleanup_release')})
    provider.services.update(services)
    return provider, services


@pytest.mark.asyncio
@pytest.mark.parametrize('suppress_cancel', [False, True])
async def test_close_drains_loaded_adapter_before_acknowledging(tmp_path, suppress_cancel):
    provider, services = blocking_provider(tmp_path, suppress_cancel=suppress_cancel)
    setup = asyncio.create_task(provider.asetup(interactive=False, user_index=None, credentials=None))
    await services['entered'].wait()
    closes = [asyncio.create_task(provider.aclose()) for _ in range(2)]
    await asyncio.wait_for(services['cancelled'].wait(), 1)
    if suppress_cancel:
        assert all(not close.done() for close in closes)
        services['release'].set()
    await asyncio.wait_for(services['cleanup_entered'].wait(), 1)
    assert all(not close.done() for close in closes)
    with pytest.raises(ContractError, match='closed'):
        await provider.asetup(interactive=False, user_index=None, credentials=None)
    services['cleanup_release'].set()
    await asyncio.gather(*closes)
    with pytest.raises((asyncio.CancelledError, ContractError, SessionClosedError)):
        await setup
    assert services['closes'] == [1]
    assert provider.sessions == []


@pytest.mark.asyncio
async def test_cancelled_setup_drains_adapter_and_preserves_unrelated_caller_work(tmp_path):
    provider, services = blocking_provider(tmp_path)
    setup = asyncio.create_task(provider.asetup(interactive=False, user_index=None, credentials=None))
    await services['entered'].wait()
    setup.cancel()
    await asyncio.wait_for(services['cleanup_entered'].wait(), 1)
    assert not setup.done()
    services['cleanup_release'].set()
    with pytest.raises(asyncio.CancelledError):
        await setup
    assert services['closes'] == [1]
    await provider.aclose()


@pytest.mark.asyncio
async def test_close_joins_already_cancelled_setup_cleanup(tmp_path):
    provider, services = blocking_provider(tmp_path)
    setup = asyncio.create_task(provider.asetup(interactive=False, user_index=None, credentials=None))
    await services['entered'].wait()
    setup.cancel()
    await services['cleanup_entered'].wait()
    close = asyncio.create_task(provider.aclose())
    await asyncio.sleep(.01)
    assert not close.done()
    services['cleanup_release'].set()
    with pytest.raises(asyncio.CancelledError):
        await setup
    await close
    assert services['closes'] == [1]


@pytest.mark.asyncio
async def test_cancelled_setup_disposes_late_returned_session(monkeypatch):
    from api_agent.llm_providers.bedrock import auth
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    session = SimpleNamespace(aclose=AsyncMock())
    async def create(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return session
    monkeypatch.setattr(auth, 'create_session', create)
    provider = factory({}, {})
    setup = asyncio.create_task(provider.asetup(interactive=False, user_index=None, credentials=None))
    await entered.wait()
    setup.cancel()
    await cancelled.wait()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await setup
    session.aclose.assert_awaited_once()
    assert provider.sessions == []
    await provider.aclose()


@pytest.mark.asyncio
async def test_closed_before_setup_child_runs_prevents_acquisition(monkeypatch):
    from api_agent.llm_providers.bedrock import auth
    provider = factory({}, {})
    create = AsyncMock()
    monkeypatch.setattr(auth, 'create_session', create)
    original = asyncio.create_task
    def schedule(work, *args, **kwargs):
        if work.cr_code.co_name == 'setup':
            provider.closed = True
        return original(work, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'create_task', schedule)
    with pytest.raises(ContractError, match='closed'):
        await provider.asetup(interactive=False, user_index=None, credentials=None)
    create.assert_not_awaited()
    await provider.aclose()


@pytest.mark.asyncio
async def test_closed_before_setup_waiter_resumes_prevents_publication(monkeypatch):
    from api_agent.llm_providers.bedrock import auth
    provider = factory({}, {})
    session = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(auth, 'create_session', AsyncMock(return_value=session))
    original = asyncio.create_task
    def schedule(work, *args, **kwargs):
        task = original(work, *args, **kwargs)
        if work.cr_code.co_name == 'setup':
            task.add_done_callback(lambda task: setattr(provider, 'closed', True))
        return task
    monkeypatch.setattr(asyncio, 'create_task', schedule)
    with pytest.raises(ContractError, match='closed'):
        await provider.asetup(interactive=False, user_index=None, credentials=None)
    await provider.aclose()
    session.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_close_does_not_cancel_unrelated_setup_caller_work():
    provider = factory({}, {})
    ready, release = asyncio.Event(), asyncio.Event()
    async def caller():
        await provider.asetup(interactive=False, user_index=None, credentials=None)
        ready.set()
        await release.wait()
        return 'unrelated work completed'
    task = asyncio.create_task(caller())
    await ready.wait()
    await provider.aclose()
    assert not task.done()
    release.set()
    assert await task == 'unrelated work completed'


@pytest.mark.asyncio
async def test_cancelled_close_still_drains_pending_setup_adapter(tmp_path):
    provider, services = blocking_provider(tmp_path)
    setup = asyncio.create_task(provider.asetup(interactive=False, user_index=None, credentials=None))
    await services['entered'].wait()
    close = asyncio.create_task(provider.aclose())
    await services['cleanup_entered'].wait()
    close.cancel()
    await asyncio.sleep(.01)
    assert not close.done()
    services['cleanup_release'].set()
    with pytest.raises(asyncio.CancelledError):
        await close
    with pytest.raises(asyncio.CancelledError):
        await setup
    assert services['closes'] == [1]
    assert provider.sessions == []
    await provider.aclose()


@pytest.fixture
def cancel_setup_handoff(monkeypatch):
    """Cancel only the setup waiter after its successful child has finished."""
    original = asyncio.create_task
    def schedule(work, *args, **kwargs):
        caller = asyncio.current_task()
        task = original(work, *args, **kwargs)
        if work.cr_code.co_name == 'setup':
            task.add_done_callback(lambda finished: caller.cancel())
        return task
    monkeypatch.setattr(asyncio, 'create_task', schedule)


@pytest.mark.asyncio
async def test_cancelled_completed_setup_handoff_disposes_each_adapter(tmp_path, cancel_setup_handoff):
    provider, services = custom_provider(tmp_path)
    for _ in range(3):
        setup = asyncio.create_task(provider.asetup(interactive=False, user_index=None, credentials=None))
        with pytest.raises(asyncio.CancelledError):
            await setup
        assert provider.sessions == []
    assert services['closes'] == [1, 1, 1]
    await provider.aclose()
    assert services['closes'] == [1, 1, 1]


@pytest.mark.asyncio
async def test_cancelled_completed_handoff_retains_failed_cleanup_for_retry(tmp_path, cancel_setup_handoff):
    provider, services = custom_provider(tmp_path, fail_first_close=True)
    setup = asyncio.create_task(provider.asetup(interactive=False, user_index=None, credentials=None))
    with pytest.raises(ContractError, match='cleanup failed'):
        await setup
    assert len(provider.sessions) == 1
    assert services['closes'] == [1]
    await provider.aclose()
    assert provider.sessions == []
    assert services['closes'] == [1, 2]


@pytest.mark.asyncio
async def test_cancelled_completed_handoff_shares_cleanup_with_provider_close(tmp_path, cancel_setup_handoff):
    provider, services = blocking_provider(tmp_path)
    services['release'].set()
    setup = asyncio.create_task(provider.asetup(interactive=False, user_index=None, credentials=None))
    await asyncio.wait_for(services['cleanup_entered'].wait(), 1)
    close = asyncio.create_task(provider.aclose())
    await asyncio.sleep(.01)
    assert not setup.done()
    assert not close.done()
    services['cleanup_release'].set()
    with pytest.raises(asyncio.CancelledError):
        await setup
    await close
    assert provider.sessions == []
    assert services['closes'] == [1]


@pytest.mark.asyncio
async def test_session_cleanup_remains_idempotent_after_shared_owner_finishes(tmp_path):
    provider, services = custom_provider(tmp_path)
    session = await provider.asetup(interactive=False, user_index=None, credentials=None)
    await asyncio.gather(provider._close_session(session), provider._close_session(session))
    await provider._close_session(session)
    assert services['closes'] == [1]
    assert provider.sessions == []
    assert provider._session_closes == {}
    await provider.aclose()


@pytest.fixture(autouse=True)
def frozen_ambient(monkeypatch):
    from api_agent.llm_providers.bedrock.ambient import AmbientSession
    from api_agent.llm_providers.bedrock.acquisition import SessionMaterial
    async def acquire(self, request, **kwargs):
        return SessionMaterial('frozen', access_key='synthetic-key', secret_key='synthetic-secret'), {}
    monkeypatch.setattr(AmbientSession, 'acquire', acquire)
