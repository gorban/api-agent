"""Local inference controls use synthetic providers without browsers or I/O."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderCapabilities, SessionClosedError, ProviderAccessError, SetupPrerequisiteError
from api_agent.providers.control import CleanupPendingError, OperationContext, OperationalError
from api_agent.providers.llm import AvailableSelection, CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.runtime.local_llm import LocalLLM


def request(model='sample'):
    return LLMRequest(model, CompletionRequest((Message('user', (TextContent('hello'),)),)))


@pytest.fixture
def selected(tmp_path):
    events = []
    class Auth:
        async def __call__(self): return 'secret'
        async def aclose(self): events.append('auth-close')
    class Provider:
        capabilities = ProviderCapabilities(kind='llm')
        async def asetup(self, **options):
            events.append(('setup', options))
            return Auth()
        async def adescribe(self, *, auth):
            events.append('describe')
            return (AvailableSelection('sample'), AvailableSelection('other'))
        async def acomplete(self, value, *, auth):
            events.append(('complete', value, auth))
            return LLMResult(Message('assistant', (TextContent('answer'),)), Usage(2, 3))
    implementation = Provider()
    loaded = SimpleNamespace(implementation=implementation, astart=AsyncMock(), aclose=AsyncMock())
    loader = AsyncMock(return_value=loaded)
    owner = LocalLLM({'LLM_PROVIDER': 'synthetic', 'LLM_ALLOWED_MODELS': 'sample'}, tmp_path, loader=loader)
    return owner, implementation, loaded, loader, events


def test_construction_and_studio_callback_are_lazy(selected):
    owner, _, loaded, loader, events = selected
    async def run():
        assert await owner.call(AsyncMock(return_value='status')) == 'status'
        loader.assert_not_called()
        assert events == [] and owner.usage == ()
        await owner.aclose()
        await owner.aclose()
        loaded.aclose.assert_not_called()
        with pytest.raises(SessionClosedError): await owner.describe()
    asyncio.run(run())


def test_shared_auth_catalog_usage_and_fresh_requests(selected):
    owner, _, loaded, loader, events = selected
    async def run():
        await asyncio.gather(owner.ensure_ready(), owner.ensure_ready())
        assert [event[0] for event in events if isinstance(event, tuple)] == ['setup']
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert (await owner.describe())[0].selected_llm == 'sample'
        assert events.count('describe') == 1
        assert (await owner.complete(request())).usage == Usage(2, 3)
        assert (await owner.complete(request())).payload.content == (TextContent('answer'),)
        assert len(owner.usage) == 2 and owner.usage[0]['usage']['input_tokens'] == 2
        copy = owner.usage
        copy[0]['outcome'] = 'changed'
        assert owner.usage[0]['outcome'] == 'completed'
        assert loader.call_args.args[2]['browser_inspection'] is owner.inspection
        assert owner.emissions.content_export('secret') != 'secret'
        await owner.aclose()
        loaded.aclose.assert_awaited_once()
        assert events.count('auth-close') == 1
    asyncio.run(run())


def test_studio_http_catalog_recovers_after_transient_provider_failure(selected):
    import httpx
    from api_agent.api import Runtime, create_application
    from api_agent.api.studio import StudioBackend
    from api_agent.providers.selection import ModelSelectionPolicy

    owner, implementation, loaded, loader, events = selected
    implementation.adescribe = AsyncMock(side_effect=[
        ProviderAccessError('unavailable', 'Synthetic catalog failure.'),
        (AvailableSelection('sample'),),
    ])

    async def describe(*, auth):
        return await owner.describe()

    backend = StudioBackend(owner=owner, describe=describe,
        complete=lambda *args, **kwargs: None,
        policy=ModelSelectionPolicy({'LLM_DEFAULT_MODEL': 'sample'}),
        origin='http://127.0.0.1:9443', nonce='s' * 32, scrub=lambda text: text)
    application = create_application(Runtime('synthetic', services={'studio_backend': backend}))

    async def run():
        transport = httpx.ASGITransport(app=application, client=('127.0.0.1', 41337))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8443',
            headers={'Origin': 'http://127.0.0.1:9443', 'X-API-Agent-Session': 's' * 32},
            trust_env=False) as client:
            first = await client.get('/app/agent/models')
            second = await client.get('/app/agent/models')
            third = await client.get('/app/agent/models')
        assert first.status_code == 502 and first.json()['code'] == 'unavailable'
        assert second.status_code == third.status_code == 200
        assert second.json()['default'] == third.json()['default'] == 'sample'
        assert implementation.adescribe.await_count == 2
        assert [event[0] for event in events if isinstance(event, tuple)] == ['setup']
        loader.assert_awaited_once()
        await backend.aclose()
        loaded.aclose.assert_awaited_once()
        assert events.count('auth-close') == 1
    asyncio.run(run())


@pytest.mark.parametrize('configured, expected', [(None, 3600), (0, 0), (12.5, 12.5)])
def test_local_llm_passes_catalog_ttl_to_shared_owner(selected, configured, expected):
    owner, _, _, _, _ = selected
    if configured is not None:
        owner.config = {**owner.config, 'LLM_CATALOG_CACHE_TTL_SECONDS': configured}
    async def run():
        await owner.ensure_ready()
        assert owner.shared._catalog_ttl == expected
        await owner.aclose()
    asyncio.run(run())


def test_admission_rejects_before_authentication(selected):
    owner, _, _, loader, _ = selected
    async def run():
        with pytest.raises(ProviderAccessError): await owner.complete(request('forbidden'))
        with pytest.raises(OperationalError, match='deadline'):
            await owner.complete(request(), context=OperationContext('expired', 0))
        loader.assert_not_called()
        assert await owner.complete(request())
        await owner.aclose()
    asyncio.run(run())


def test_failed_setup_retries_once_per_explicit_action_without_fallback(selected):
    owner, implementation, _, loader, events = selected
    implementation.asetup = AsyncMock(side_effect=ProviderAccessError('authentication', 'Selected authentication failed.'))
    async def run():
        for _ in range(2):
            with pytest.raises(ProviderAccessError): await owner.ensure_ready()
        assert implementation.asetup.await_count == 2
        assert loader.await_count == 2
        assert loader.await_args_list[0] == loader.await_args_list[1]
        assert events == []
        await owner.aclose()
    asyncio.run(run())


def test_cancelled_waiter_does_not_cancel_shared_setup(selected):
    owner, implementation, _, _, _ = selected
    original = implementation.asetup
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def setup(**options):
            entered.set()
            await release.wait()
            return await original(**options)
        implementation.asetup = setup
        waiter = asyncio.create_task(owner.ensure_ready())
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
        release.set()
        await owner.ensure_ready()
        await owner.aclose()
    asyncio.run(run())


def test_retryable_cleanup_retains_inspection_until_provider_acknowledges(selected):
    owner, _, loaded, _, _ = selected
    loaded.aclose.side_effect = [RuntimeError('blocked'), None]
    async def run():
        await owner.ensure_ready()
        with pytest.raises(CleanupPendingError) as failure: await owner.aclose()
        assert not owner.inspection._closed
        await failure.value.retry()
        assert owner.inspection._closed
        assert loaded.aclose.await_count == 2
    asyncio.run(run())


def test_missing_selection_and_bad_timeout(tmp_path):
    with pytest.raises(ValueError): LocalLLM({}, tmp_path, timeout=0)
    async def run():
        owner = LocalLLM({}, tmp_path)
        with pytest.raises(SetupPrerequisiteError): await owner.ensure_ready()
        await owner.aclose()
    asyncio.run(run())


def test_load_failure_retains_owned_resources(selected):
    owner, _, _, loader, _ = selected
    retained = SimpleNamespace(aclose=AsyncMock())
    error = RuntimeError('load failed')
    error.retained_owners = (retained,)
    loader.side_effect = error
    async def run():
        with pytest.raises(RuntimeError): await owner.ensure_ready()
        await owner.aclose()
        retained.aclose.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('registry', [None, object()])
def test_selected_credentials_are_scoped_and_lazy(selected, monkeypatch, registry):
    from api_agent.runtime import local_llm
    owner, _, _, _, events = selected
    owner.config = {**owner.config, 'UNATTENDED_USER_INDEX': 2, 'LLM_CREDENTIAL_ROLE': 'personal'}
    owner.registry = registry
    bindings = []
    from api_agent.providers.sessions import ScopedRoleCredentials
    def bind(config, roles, index, **options):
        bindings.append((roles, index, options))
        return ScopedRoleCredentials(('personal',), 2, AsyncMock(return_value='username'))
    monkeypatch.setattr(local_llm, 'bind_role_credentials', bind)
    monkeypatch.setattr(local_llm, 'registry_for_owner', lambda root: 'registry')
    async def run():
        assert bindings == []
        await owner.ensure_ready()
        assert bindings[0][:2] == (('personal',), 2)
        assert bindings[0][2]['registry'] == (registry or 'registry')
        setup = events[0][1]
        assert setup['user_index'] == 2 and setup['credentials'] is not None
        await owner.aclose()
    asyncio.run(run())


def test_provider_without_start_and_non_string_auth(selected):
    owner, implementation, loaded, _, _ = selected
    del loaded.astart
    class Auth:
        async def __call__(self): return object()
        async def aclose(self): pass
    implementation.asetup = AsyncMock(return_value=Auth())
    async def run():
        await owner.complete(request())
        await owner.aclose()
    asyncio.run(run())


def test_failed_completion_records_usage_and_quote_failure_preserves_result(selected):
    owner, implementation, _, _, _ = selected
    implementation.acomplete = AsyncMock(side_effect=ProviderAccessError('unavailable', 'Unavailable.'))
    async def run():
        with pytest.raises(ProviderAccessError): await owner.complete(request())
        assert owner.usage[0]['outcome'] == 'failed' and owner.usage[0]['usage'] is None
        implementation.acomplete.side_effect = None
        implementation.acomplete.return_value = LLMResult(Message('assistant', (TextContent('answer'),)))
        implementation.quote_cost = lambda *args: None
        assert await owner.complete(request())
        assert 'quote' not in owner.usage[1]
        await owner.aclose()
    asyncio.run(run())


def test_helpers_pending_cleanup_is_retryable(selected):
    owner, _, _, _, _ = selected
    owner._timeout = .01
    async def run():
        gate = asyncio.Event()
        helper = asyncio.create_task(gate.wait())
        owner._helpers.add(helper)
        with pytest.raises(CleanupPendingError) as pending: await owner.aclose()
        assert not helper.cancelled() and not owner.inspection._closed
        gate.set()
        await helper
        await pending.value.retry()
        assert owner.inspection._closed
    asyncio.run(run())


def test_readiness_and_catalog_register_each_refreshed_string_auth(selected):
    owner, implementation, _, _, _ = selected
    tokens = iter(('readiness-credential', 'refresh-credential', 'catalog-credential'))
    class Auth:
        async def __call__(self): return next(tokens)
        async def aclose(self): pass
    implementation.asetup = AsyncMock(return_value=Auth())
    async def describe(*, auth):
        assert auth == 'catalog-credential'
        raise ProviderAccessError('unavailable', 'Catalog failed: ' + auth)
    implementation.adescribe = describe
    async def run():
        await owner.ensure_ready()
        assert 'readiness-credential' not in owner.emissions.content_export({'text': 'readiness-credential'})['text']
        with pytest.raises(ProviderAccessError) as failure: await owner.describe()
        safe = owner.emissions.content_export({'text': str(failure.value)})['text']
        assert 'catalog-credential' not in safe and 'Catalog failed' in safe
        assert 'refresh-credential' not in owner.emissions.content_export({'text': 'refresh-credential'})['text']
        await owner.aclose()
    asyncio.run(run())


def test_local_status_records_are_content_free_and_do_not_change_usage(selected):
    owner, _, _, _, _ = selected
    statuses = []
    owner.on_status = statuses.append
    async def run():
        await owner.ensure_ready()
        await owner.describe()
        await owner.complete(request())
        assert len(owner.usage) == 1
        await owner.aclose()
    asyncio.run(run())
    assert [(row['category'], row['outcome']) for row in statuses] == [
        ('setup', 'started'), ('setup', 'ready'),
        ('authentication', 'started'), ('authentication', 'ready'),
        ('catalog', 'started'), ('catalog', 'ready'),
        ('request', 'accepted'), ('request', 'executing'), ('request', 'completed')]
    assert all(set(row) == {'event', 'category', 'outcome', 'operation_id'} for row in statuses)
    assert 'hello' not in repr(statuses) and 'secret' not in repr(statuses)
    with pytest.raises(TypeError):
        statuses[0]['outcome'] = 'changed'


@pytest.mark.parametrize('category', ['authentication', 'catalog', 'request', 'setup'])
def test_local_failure_status_never_contains_provider_errors(selected, category):
    owner, implementation, _, loader, _ = selected
    statuses = []
    owner.on_status = statuses.append
    failure = ValueError('synthetic-secret-error')
    async def run():
        if category == 'setup':
            loader.side_effect = failure
        elif category == 'authentication':
            implementation.asetup = AsyncMock(side_effect=failure)
        elif category == 'catalog':
            implementation.adescribe = AsyncMock(side_effect=failure)
        else:
            implementation.acomplete = AsyncMock(side_effect=failure)
        with pytest.raises(Exception):
            if category in ('authentication', 'setup'):
                await owner.ensure_ready()
            elif category == 'catalog':
                await owner.describe()
            else:
                await owner.complete(request())
        await owner.aclose()
    asyncio.run(run())
    assert any(row['category'] == category and row['outcome'] == 'failed' for row in statuses)
    assert 'synthetic-secret-error' not in repr(statuses)


def test_local_cancelled_request_status(selected):
    owner, implementation, _, _, _ = selected
    statuses = []
    owner.on_status = statuses.append
    implementation.acomplete = AsyncMock(side_effect=asyncio.CancelledError())
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await owner.complete(request())
        await owner.aclose()
    asyncio.run(run())
    assert statuses[-1]['outcome'] == 'cancelled'


def test_concurrent_local_calls_share_usage_identity_with_own_visit(selected):
    from api_agent.workflows.attribution import bind_workflow_visit
    from unittest.mock import Mock
    owner, implementation, _, _, _ = selected
    original = implementation.acomplete
    async def overlapping(value, *, auth):
        await asyncio.sleep(0)
        return await original(value, auth=auth)
    implementation.acomplete = overlapping
    journals = [Mock(), Mock()]
    async def call(index):
        with bind_workflow_visit(journals[index], f'visit-{index}'):
            await owner.complete(request())
    async def run():
        await asyncio.gather(call(0), call(1))
        receipts = [journal.record_call.call_args.kwargs for journal in journals]
        assert {row['call_id'] for row in receipts} == {row['call_id'] for row in owner.usage}
        assert len({row['call_id'] for row in receipts}) == 2
        assert [row['visit_id'] for row in receipts] == ['visit-0', 'visit-1']
        assert all(journal.record_call.call_count == 1 for journal in journals)
        await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('failure', [ValueError('sink failed'), asyncio.CancelledError()])
def test_status_sink_failure_cannot_change_success_or_mask_cancellation(selected, failure):
    owner, implementation, _, _, _ = selected
    def broken_status(row):
        raise failure
    owner.on_status = broken_status
    async def run():
        assert (await owner.complete(request())).payload.content == (TextContent('answer'),)
        implementation.acomplete = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await owner.complete(request())
        await owner.aclose()
    asyncio.run(run())


def test_cancelled_setup_waiter_does_not_report_shared_setup_failed(selected):
    owner, _, loaded, _, _ = selected
    statuses = []
    owner.on_status = statuses.append
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()
        async def start():
            started.set()
            await finish.wait()
        loaded.astart.side_effect = start
        waiter = asyncio.create_task(owner.ensure_ready())
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        finish.set()
        await owner.ensure_ready()
        await owner.aclose()
    asyncio.run(run())
    assert ('setup', 'failed') not in [(row['category'], row['outcome']) for row in statuses]
    assert sum(row['category'] == 'setup' and row['outcome'] == 'ready' for row in statuses) == 1


@pytest.mark.parametrize('central_calls',[1,2])
def test_central_terminal_receipts_are_reused_without_duplicate_local_charge(selected,central_calls):
    from uuid import uuid4
    from api_agent.workflows.attribution import bind_workflow_visit,current_workflow_visit
    from api_agent.workflows.journal import WorkflowJournal
    from test_workflow_observations import definition
    owner, implementation, _, _, _ = selected
    issued = []
    async def published(value, *, auth):
        journal,visit,attempt = current_workflow_visit()
        for _ in range(central_calls):
            identifier = str(uuid4())
            issued.append(identifier)
            journal.record_call(visit_id=visit,attempt_id=attempt,call_id=identifier,
                usage={'input_tokens':2,'output_tokens':3},selection='sample',outcome='completed')
            await asyncio.sleep(0)
        return LLMResult(Message('assistant',(TextContent('answer'),)),Usage(2,3))
    implementation.acomplete = published
    journals = [WorkflowJournal(definition()),WorkflowJournal(definition())]
    async def call(index):
        journal = journals[index]
        journal.observe(dict(event='scope_start',scope_id=f'visit-{index}',kind='node',owner='Make'))
        with bind_workflow_visit(journal,f'visit-{index}'):
            await owner.complete(request())
    async def run():
        await asyncio.gather(call(0),call(1))
        assert {row['call_id'] for row in owner.usage} == set(issued)
        assert len(owner.usage) == central_calls*2
        for journal in journals:
            calls = [row for row in journal.finalize()['events'] if row['event']=='call']
            assert len(calls) == central_calls
        await owner.aclose()
    asyncio.run(run())


def test_local_cancelled_call_retains_cancelled_terminal_outcome(selected):
    from api_agent.workflows.attribution import bind_workflow_visit
    from unittest.mock import Mock
    owner, implementation, _, _, _ = selected
    async def cancelled(value, *, auth):
        raise asyncio.CancelledError()
    implementation.acomplete = cancelled
    journal = Mock()
    async def run():
        with bind_workflow_visit(journal,'visit'):
            with pytest.raises(asyncio.CancelledError):
                await owner.complete(request())
        assert journal.record_call.call_args.kwargs['outcome'] == 'cancelled'
        assert owner.usage[0]['outcome'] == 'cancelled'
        await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('model',['synthetic@revision','synthetic model (v1)','模型@版本'])
@pytest.mark.parametrize('failing',[False,True])
def test_optional_accounting_preserves_contract_valid_provider_result_and_identifier(selected,model,failing):
    from pathlib import Path
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.local_application import LocalChatApplication
    from api_agent.workflows.observations import WorkflowObservationFactory
    from api_agent.config.resolve import ConfigResult
    owner,implementation,_,_,_ = selected
    owner.policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS':'.*'})
    implementation.adescribe = AsyncMock(return_value=(AvailableSelection(model),))
    expected = LLMResult(Message('assistant',(TextContent('answer'),)),Usage(2,3))
    error = ProviderAccessError('unavailable','Synthetic provider failure')
    async def complete(value,*,auth):
        if failing:
            raise error
        return expected
    implementation.acomplete = complete
    root = Path(__file__).resolve().parents[2]
    definition = load_definition(root/'flow.md.html',base_root=root,pack_root=root)
    rows = []
    from api_agent.telemetry.tracing import TraceRuntime
    trace = TraceRuntime(ConfigResult({}, {}, (), (), ()))
    owner.trace = trace
    factory = WorkflowObservationFactory(ConfigResult({}, {}, (), (), ()),boundary=EmissionBoundary(),sink=rows.append,trace=trace)
    app = LocalChatApplication(definition,observations=factory)
    value = request(model)
    async def run():
        try:
            await owner.describe()
            if failing:
                with pytest.raises(ProviderAccessError) as caught:
                    await app.complete(value,llm=owner,context=OperationContext('request'))
                assert caught.value is error
            else:
                assert await app.complete(value,llm=owner,context=OperationContext('request')) is expected
            assert owner.usage[0]['selection'] == model
        finally:
            await owner.aclose()
    asyncio.run(run())
    call = next(row for row in rows if row['event']=='call')
    if len(model)>200 or not model.isprintable():
        assert call['reason']=='selection_unavailable' and call['selection'] is None
    else:
        assert call['selection']==model


def test_catalog_trace_binding_is_bounded_and_cannot_change_catalog_behavior(selected):
    from unittest.mock import Mock
    llm, implementation, _, _, _ = selected
    from api_agent.providers.selection import ModelSelectionPolicy
    llm.policy = ModelSelectionPolicy({'LLM_ALLOWED_MODELS': '.*'})
    catalog = tuple(AvailableSelection(f'model-{number}') for number in range(256))
    implementation.adescribe = AsyncMock(return_value=catalog)
    trace = Mock()
    trace.bind_model_identities.side_effect = RuntimeError('metadata unavailable')
    llm.trace = trace
    async def run():
        try:
            result = await llm.describe()
            assert result == catalog
            trace.bind_model_identities.assert_called_once_with(
                tuple(item.selected_llm for item in catalog[:256]))
        finally:
            await llm.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('explicit_expired_request, expected_status, expected_body, catalog_calls', [
    (False, 200, {'default': 'sample'}, 1),
    (True, 408, {'code': 'deadline'}, 0),
])
def test_listener_catalog_uses_request_budget_after_startup_expires(
        selected, monkeypatch, explicit_expired_request, expected_status, expected_body, catalog_calls):
    import sys
    import time
    from contextlib import nullcontext
    import httpx
    from api_agent.api import Runtime, create_application
    from api_agent.api.studio import StudioBackend
    from api_agent.providers import progress
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.runtime.listener import UvicornListener

    owner, _, _, loader, events = selected
    clock = [time.monotonic()]
    monkeypatch.setattr(progress, '_clock', lambda: clock[0])

    async def describe(*, auth):
        return await owner.describe()

    backend = StudioBackend(owner=owner, describe=describe, complete=AsyncMock(),
        policy=ModelSelectionPolicy({'LLM_DEFAULT_MODEL': 'sample'}),
        origin='http://127.0.0.1:9443', nonce='s' * 32, scrub=lambda text: text)
    application = create_application(Runtime('synthetic', services={'studio_backend': backend}))

    async def delayed_application(scope, receive, send):
        async def delayed_receive():
            await asyncio.sleep(0)
            return await receive()
        await application(scope, delayed_receive, send)

    async def run():
        admitted = asyncio.Event()
        responses = []
        class Server:
            def __init__(self, config):
                self.started, self.should_exit = True, False
            async def serve(self):
                await admitted.wait()
                # A request can still carry an explicit caller deadline after
                # the listener has detached its unrelated startup deadline.
                request_scope = progress.bind_timeout_budget(progress.TimeoutBudget(
                    'request', None, clock[0] - 2, clock[0] - 1, 1)) if explicit_expired_request else nullcontext()
                with request_scope:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delayed_application),
                        base_url='http://127.0.0.1:8443', headers={
                            'Origin': 'http://127.0.0.1:9443', 'X-API-Agent-Session': 's' * 32},
                        trust_env=False) as client:
                        responses.append(await client.get('/app/agent/models'))
        monkeypatch.setitem(sys.modules, 'uvicorn', SimpleNamespace(Config=lambda *args, **kwargs: kwargs, Server=Server))
        listener = UvicornListener()
        async with progress.report_timeout('Local startup', 1):
            await listener.start(delayed_application, SimpleNamespace(cert_file='cert', key_file='key'),
                SimpleNamespace(port=8443))
        clock[0] += 2
        admitted.set()
        try:
            await listener.task
            response = responses[0]
            assert response.status_code == expected_status
            assert expected_body.items() <= response.json().items()
            assert events.count('describe') == catalog_calls
            assert loader.await_count == catalog_calls
        finally:
            await listener.stop()
            await backend.aclose()
    asyncio.run(run())
