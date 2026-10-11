"""Completion execution shares authentication without caching responses or listing."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from api_agent.providers.contracts import ContractError, ProviderAccessError, SessionClosedError, SetupPrerequisiteError
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupClient, SetupServer
from api_agent.providers.sessions import LLMSessionOwner
from api_agent.testing.provider_setup import IntegrationLLM, RemoteIntegrationLLM

CAP = 'synthetic-inference-capability-value-000'
REQUEST = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('Ok'),)),), generation=GenerationOptions(max_output_tokens=128)))
RESULT = LLMResult(Message('assistant', (TextContent('Ok'),)), Usage(input_tokens=2, output_tokens=1))


def test_shared_completion_uses_one_auth_and_never_caches_or_lists():
    async def scenario():
        session = AsyncMock(return_value='synthetic-secret')
        adapter = SimpleNamespace(asetup=AsyncMock(return_value=session))
        complete = AsyncMock(return_value=RESULT)
        subject = SharedSetupOwner('op','selected',lambda c: LLMSessionOwner(adapter), complete=complete)
        try:
            results = await asyncio.gather(*(subject.complete(REQUEST,selected_identity='selected') for _ in range(3)))
            assert results == [RESULT]*3 and not subject._calls
            adapter.asetup.assert_awaited_once()
            assert complete.await_count == 3
            assert all(call.args == (REQUEST,) and call.kwargs == {'auth':'synthetic-secret'} for call in complete.await_args_list)
            with pytest.raises(ContractError, match='outside shared'):
                await subject.complete(REQUEST,selected_identity='other')
        finally:
            await subject.aclose()
        session.aclose.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('method,expected', [(None,SetupPrerequisiteError),(AsyncMock(return_value='secret'),ContractError)])
def test_missing_capability_or_invalid_reply(method, expected):
    async def scenario():
        adapter = SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='secret')))
        owner = SharedSetupOwner('op','selected',lambda c: LLMSessionOwner(adapter),complete=method)
        try:
            with pytest.raises(expected):
                await owner.complete(REQUEST)
        finally:
            await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [None, ProviderAccessError('authorization','Request permission denied',status=403,code='denied',request_id='req-safe')])
def test_real_ipc_returns_complete_result_or_safe_error_and_runs_each_call(failure):
    async def scenario():
        owner = SimpleNamespace(ensure_ready=AsyncMock(),complete=AsyncMock(return_value=RESULT,side_effect=failure),aclose=AsyncMock())
        registry = SetupRegistry('op','selected',lambda:owner)
        server = SetupServer(registry,CAP)
        endpoint = await server.start()
        clients = [RemoteIntegrationLLM(endpoint),RemoteIntegrationLLM(endpoint)]
        try:
            if failure is None:
                assert await asyncio.gather(*(asyncio.to_thread(c.complete,REQUEST) for c in clients)) == [RESULT]*2
            else:
                for client in clients:
                    with pytest.raises(ProviderAccessError) as error:
                        await asyncio.to_thread(client.complete,REQUEST)
                    assert error.value.status == 403 and error.value.code == 'denied' and error.value.request_id == 'req-safe'
            assert owner.complete.await_count == 2
        finally:
            for client in clients:
                await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
        owner.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_registry_no_completion_skip_and_revocation():
    async def scenario():
        owner = SimpleNamespace(ensure_ready=AsyncMock(),aclose=AsyncMock())
        registry = SetupRegistry('op','selected',lambda:owner)
        binding = registry.register('worker')
        try:
            with pytest.raises(SetupPrerequisiteError):
                await registry.complete(binding,REQUEST)
            await registry.revoke(binding)
            with pytest.raises(SessionClosedError):
                await registry.complete(binding,REQUEST)
        finally:
            await registry.aclose()
    asyncio.run(scenario())


def test_cancellation_stops_only_its_inference_keeps_auth_for_next_call():
    async def scenario():
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def complete(request,*,auth):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        adapter = SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='secret')))
        owner = SharedSetupOwner('op','selected',lambda c:LLMSessionOwner(adapter),complete=complete)
        task = asyncio.create_task(owner.complete(REQUEST))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set() and not owner._calls
        owner._complete = AsyncMock(return_value=RESULT)
        assert await owner.complete(REQUEST) == RESULT
        adapter.asetup.assert_awaited_once()
        await owner.aclose()
    asyncio.run(scenario())


def test_worker_local_complete_closed_and_prerequisite(tmp_path, monkeypatch):
    from api_agent.testing import provider_setup
    implementation = SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='secret')), acomplete=AsyncMock(return_value=RESULT))
    monkeypatch.setattr(provider_setup,'aload_provider',AsyncMock(return_value=SimpleNamespace(implementation=implementation,aclose=AsyncMock())))
    local = IntegrationLLM({'LLM_PROVIDER':'synthetic'},tmp_path)
    assert local.complete(REQUEST) == RESULT
    implementation.acomplete.assert_awaited_once_with(REQUEST,auth='secret')
    local.close()
    with pytest.raises(ContractError,match='closed'):
        local.complete(REQUEST)
    del implementation.acomplete
    local = IntegrationLLM({'LLM_PROVIDER':'synthetic'},tmp_path)
    try:
        with pytest.raises(pytest.skip.Exception,match='completion capability'):
            local.complete(REQUEST)
    finally:
        local.close()


@pytest.mark.parametrize('available', [True, False])
def test_selection_fixture_resolves_catalog_or_missing_capability(monkeypatch, available):
    from api_agent.testing import provider_setup
    from api_agent.providers.llm import AvailableSelection
    from unittest.mock import Mock
    config = {'LLM_DEFAULT_MODEL': 'family-.*'}
    monkeypatch.setattr(provider_setup, '_read_snapshot', lambda: {'config': config})
    provider = SimpleNamespace(describe=Mock(return_value=(AvailableSelection('family-2'),)))
    if not available:
        provider.describe.side_effect = pytest.skip.Exception('no catalog capability')
        with pytest.raises(pytest.skip.Exception, match='no catalog capability'):
            provider_setup.llm_selection.__wrapped__(provider)
    else:
        assert provider_setup.llm_selection.__wrapped__(provider) == 'family-2'
    provider.describe.assert_called_once_with()


@pytest.mark.parametrize('grouped', [False, True])
def test_local_failure_preserves_stack_and_error_chain_without_credential_locals(tmp_path, grouped):
    credential = 'synthetic-truncated-credential-' * 100
    failures = []
    async def raises_with_credentials():
        access_token = credential
        try:
            raise ValueError('safe inner failure')
        except ValueError as inner:
            failures.append(inner)
            error = ProviderAccessError('authorization', 'safe upstream rejection', status=401)
            error.__cause__ = inner
            inner.__cause__ = error  # A diagnostic chain may contain a cycle.
            raise error
    async def raises_group():
        try:
            await raises_with_credentials()
        except ProviderAccessError as error:
            children = [error]
        # Raise outside the except block so group context cannot mask missing
        # traversal of children.
        raise ExceptionGroup('safe grouped failure', children)
    local = IntegrationLLM({}, tmp_path)
    try:
        with pytest.raises(ExceptionGroup if grouped else ProviderAccessError) as result:
            local._run_private(raises_group() if grouped else raises_with_credentials())
        error = result.value.exceptions[0] if grouped else result.value
        if grouped:
            assert result.value.__cause__ is None and result.value.__context__ is None
        assert error.status == 401 and error.message == 'safe upstream rejection'
        assert error.__cause__ is failures[0]
        locations = []
        for error in (error, failures[0]):
            trace = error.__traceback__
            while trace is not None:
                if trace.tb_frame.f_code.co_name == 'raises_with_credentials':
                    locations.append(trace.tb_frame.f_code.co_name)
                    assert not trace.tb_frame.f_locals
                trace = trace.tb_next
        assert locations == ['raises_with_credentials', 'raises_with_credentials']
    finally:
        local.close()


@pytest.mark.parametrize('kind',['shared','registry'])
def test_repeated_cancellation_waits_for_cleanup_and_releases_result_tracking(kind):
    async def scenario():
        entered,cleaning,release,stopped = (asyncio.Event() for _ in range(4))
        async def complete(request,**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                stopped.set()
                return RESULT
        if kind == 'shared':
            adapter = SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='secret')))
            subject = SharedSetupOwner('op','selected',lambda c:LLMSessionOwner(adapter),complete=complete)
            operation=lambda:subject.complete(REQUEST)
        else:
            owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
            subject=SetupRegistry('op','selected',lambda:owner)
            binding=subject.register('worker')
            operation=lambda:subject.complete(binding,REQUEST)
        task=asyncio.create_task(operation())
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done() and not stopped.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set() and not subject._cancel_requested
        if kind == 'shared':
            assert not subject._calls
        else:
            assert not subject._lease(binding)['waiters']
        await subject.aclose()
    asyncio.run(scenario())


def test_registry_completed_reply_cannot_survive_lease_revocation(monkeypatch):
    from api_agent.providers import setup_registry as module
    async def scenario():
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=AsyncMock(return_value=RESULT),aclose=AsyncMock())
        registry=SetupRegistry('op','selected',lambda:owner)
        binding=registry.register('worker')
        original=module._spawn
        count=0
        def spawn(awaitable):
            nonlocal count
            count+=1
            task=original(awaitable)
            if count==3:
                task.add_done_callback(lambda _: registry._lease(binding).__setitem__('revoked',True))
            return task
        monkeypatch.setattr(module,'_spawn',spawn)
        try:
            with pytest.raises(SessionClosedError,match='revoked'):
                await registry.complete(binding,REQUEST)
        finally:
            await registry.aclose()
    asyncio.run(scenario())


def test_client_rejects_extra_fields_in_completed_result():
    import json
    class Writer:
        def __init__(self): self.writes=[]
        def write(self,data): self.writes.append(json.loads(data))
        async def drain(self): pass
    async def scenario():
        client=SetupClient({'version':1,'host':'127.0.0.1','port':43123,'capability':CAP})
        client._writer=Writer()
        client._reader=asyncio.StreamReader()
        client._reader.feed_data(b'{"version":1,"status":"completed","result":{},"extra":"not allowed"}\n')
        with pytest.raises(ContractError,match='completion response'):
            await client._exchange('complete',{})
        assert client._writer.writes[0]['action']=='complete'
    asyncio.run(scenario())
