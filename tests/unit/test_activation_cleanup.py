"""Operation activation and retained parent ownership use synthetic owners only."""
import asyncio
from types import SimpleNamespace

import pytest

from api_agent.api.application import Runtime
from api_agent.api.composition import activation_kinds, compose_runtime
from api_agent.providers.control import CleanupPendingError, OperationalError
from api_agent.runtime.operation import OperationCleanup


@pytest.mark.parametrize('mode', [None, 'help', 'unit', 'collect', 'setup'])
def test_inert_modes_do_not_execute_selected_factories(mode, tmp_path):
    selected = tmp_path / 'broken.py'
    selected.write_text('raise AssertionError("executed unrelated selection")')
    values = dict(TUI_MODE=mode, RUN_UI=True, API_PROVIDER=str(selected)+':create',
                  UI_PROVIDER=str(selected)+':create', LLM_PROVIDER=str(selected)+':create',
                  ACCESS_PROVIDER=str(selected)+':create')
    runtime = compose_runtime(SimpleNamespace(pack_id='synthetic'), values,
                              callback_origin='https://example.test')
    async def scenario():
        await runtime.astart()
        assert runtime.identity_adapter is None
        assert runtime.factory_plan is None
        await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('values,ui', [({'TUI_MODE':'integration','RUN_UI':True},False),
    ({'TUI_MODE':'run-local'},False), ({'TUI_MODE':'run-local','RUN_UI':True},True),
    ({'TUI_MODE':'e2e'},True), ({'TUI_MODE':'e2e','COLLECT_ONLY':True},False)])
def test_mode_ui_activation(values, ui):
    assert ('ui' in activation_kinds(values)) is ui


def test_explicit_narrow_activation_does_not_resolve_unrelated_identity(tmp_path):
    location = tmp_path / 'llm.py'
    location.write_text('class Adapter: version=1\ndef create(config, services): return Adapter()')
    runtime = compose_runtime(SimpleNamespace(pack_id='synthetic', identity_factory_path='missing.py'),
        {'LLM_PROVIDER':str(location)+':create', 'API_PROVIDER':'missing.py:create',
         'UI_PROVIDER':'missing.py:create', 'ACCESS_PROVIDER':'missing.py:create'},
        callback_origin='https://example.test', required_kinds={'llm'})
    async def scenario():
        await runtime.astart()
        assert runtime.llm_provider is not None
        assert runtime.api_provider is runtime.ui_provider is runtime.access_provider is None
        await runtime.aclose()
    asyncio.run(scenario())
    assert activation_kinds({}, {'api'}) == {'api','upstream'}
    assert activation_kinds({}, {'access'}) == {'access'}
    with pytest.raises(ValueError, match='invalid_activation_kind'):
        activation_kinds({}, {'unknown'})


def test_runtime_pending_api_retains_upstream_tracing_but_closes_sibling():
    async def scenario():
        calls = []
        release = asyncio.Event()
        async def api_close():
            calls.append('api')
            await release.wait()
        def owner(name):
            async def close(): calls.append(name)
            return SimpleNamespace(aclose=close)
        api = SimpleNamespace(aclose=api_close)
        upstream, tracing, llm = owner('upstream'), owner('trace'), owner('llm')
        runtime = Runtime('synthetic', api_provider=api, upstream_provider=upstream,
                          tracing=tracing, llm_provider=llm, timeout=.01)
        with pytest.raises(CleanupPendingError) as error:
            await runtime.aclose()
        assert error.value.owners == (api, upstream, tracing)
        assert calls == ['llm','api']
        release.set()
        await asyncio.gather(error.value.retry(), error.value.retry())
        await runtime.aclose()
        assert calls == ['llm','api','upstream','trace']
        assert runtime.pending_cleanup is None
    asyncio.run(scenario())


def test_operation_pending_listener_retains_runtime_and_lease_retry_once():
    async def scenario():
        calls=[]
        fails=True
        async def stop():
            calls.append('stop')
            if fails: raise RuntimeError('private')
        async def close(): calls.append('runtime')
        def release(**kwargs): calls.append('lease')
        listener, runtime, lease = SimpleNamespace(stop=stop), SimpleNamespace(aclose=close), object()
        cleanup = OperationCleanup(SimpleNamespace(release=release),runtime,listener,lease,False,.01)
        with pytest.raises(CleanupPendingError) as error:
            await cleanup.retry()
        assert error.value.owners == (listener,runtime,lease)
        assert calls == ['stop']
        fails=False
        await asyncio.gather(error.value.retry(),error.value.retry())
        await cleanup.retry()
        assert calls == ['stop','stop','runtime','lease']
    asyncio.run(scenario())


def test_operation_runtime_pending_and_release_failure_are_retryable():
    async def scenario():
        calls=[]
        failed=True
        async def close():
            calls.append('runtime')
            if failed: raise CleanupPendingError((),close)
        releases=0
        def release(**kwargs):
            nonlocal releases
            releases+=1
            if releases==1: raise ValueError('private')
        runtime,lease = SimpleNamespace(aclose=close),object()
        cleanup=OperationCleanup(SimpleNamespace(release=release),runtime,None,lease,False,.01)
        with pytest.raises(CleanupPendingError) as error: await cleanup.retry()
        assert error.value.owners == (runtime,lease)
        assert releases==0
        failed=False
        retry = error.value.retry
        with pytest.raises(CleanupPendingError) as error: await retry()
        assert error.value.owners == (lease,)
        await error.value.retry()
        await cleanup.retry()
        assert calls==['runtime','runtime'] and releases==2
    asyncio.run(scenario())


def test_runtime_preserves_approved_operation_error():
    async def scenario():
        async def start(): raise OperationalError('unavailable')
        runtime=Runtime('synthetic',identity_adapter=SimpleNamespace(astart=start))
        with pytest.raises(OperationalError,match='unavailable'):
            await runtime.astart()
    asyncio.run(scenario())


def test_started_runtime_handler_uses_frozen_routes(monkeypatch):
    from api_agent.api.application import RuntimeHandler
    from django.core.handlers.asgi import ASGIHandler
    async def response(handler, request):
        return request.urlconf
    monkeypatch.setattr(ASGIHandler, 'get_response_async', response)
    async def scenario():
        runtime=Runtime('synthetic')
        await asyncio.gather(runtime.astart(),runtime.astart())
        request=SimpleNamespace(scope={'api_agent_runtime':runtime})
        result=await object.__new__(RuntimeHandler).get_response_async(request)
        assert result is runtime.urlconf
        await runtime.aclose()
    asyncio.run(scenario())


def test_operation_cancellation_resistant_pending_child_retains_prerequisites():
    async def scenario():
        calls=[]
        entered, finish=asyncio.Event(),asyncio.Event()
        async def close():
            calls.append('runtime')
            entered.set()
            try:
                await finish.wait()
            except asyncio.CancelledError:
                await finish.wait()
        def release(**kwargs): calls.append('lease')
        runtime,lease=SimpleNamespace(aclose=close),object()
        cleanup=OperationCleanup(SimpleNamespace(release=release),runtime,None,lease,False,.01)
        with pytest.raises(CleanupPendingError) as error: await cleanup.retry()
        assert calls==['runtime']
        with pytest.raises(CleanupPendingError): await error.value.retry()
        cleanup.tasks['runtime'].cancel()
        await asyncio.sleep(0)
        assert not cleanup.tasks['runtime'].done()
        finish.set()
        await asyncio.gather(error.value.retry(),error.value.retry())
        assert calls==['runtime','lease']
    asyncio.run(scenario())


def test_access_only_callback_routes_do_not_launch_ui_or_inference(tmp_path):
    provider=tmp_path/'access.py'
    provider.write_text('''from api_agent.providers.contracts import ProviderCapabilities
class Adapter:
    version=1
    capabilities=ProviderCapabilities('access')
    def setup_routes(self, registrar, services):
        registrar.add('/owned/callback', ('GET',), lambda request: None)
    async def aclose(self): pass
def create(config, services): return Adapter()
''')
    runtime=compose_runtime(SimpleNamespace(pack_id='synthetic', identity_factory_path='missing.py'),
        dict(ACCESS_PROVIDER=str(provider)+':create',UI_PROVIDER='missing.py:create',
             LLM_PROVIDER='missing.py:create', API_PROVIDER='missing.py:create'),
        callback_origin='https://example.test',required_kinds={'access'})
    async def scenario():
        await runtime.astart()
        assert runtime.access_provider is not None
        assert runtime.ui_provider is runtime.llm_provider is runtime.api_provider is None
        assert sum(str(route.pattern)=='^owned/callback$' for route in runtime.urlconf.urlpatterns)==1
        await runtime.aclose()
    asyncio.run(scenario())


def test_pending_ui_retains_api_upstream_and_tracing_while_llm_disposes():
    async def scenario():
        calls=[]
        failed=True
        async def ui_close():
            calls.append('ui')
            if failed: raise RuntimeError('private')
        def owner(name):
            async def close(): calls.append(name)
            return SimpleNamespace(aclose=close)
        ui=SimpleNamespace(aclose=ui_close)
        api,upstream,tracing=owner('api'),owner('upstream'),owner('trace')
        runtime=Runtime('synthetic',ui_provider=ui,api_provider=api,
                        upstream_provider=upstream,tracing=tracing,llm_provider=owner('llm'))
        with pytest.raises(CleanupPendingError) as error: await runtime.aclose()
        assert error.value.owners==(ui,api,upstream,tracing)
        assert calls==['ui','llm']
        failed=False
        await error.value.retry()
        assert calls==['ui','llm','ui','api','upstream','trace']
    asyncio.run(scenario())


@pytest.mark.parametrize('mode,run_ui',[('integration',True),('run-local',False)])
def test_ui_free_operations_never_import_unusable_ui(mode,run_ui,tmp_path):
    selected=tmp_path/'broken.py'
    selected.write_text('raise AssertionError("must not import")')
    runtime=compose_runtime(SimpleNamespace(pack_id='synthetic',identity_factory_path=None),
        dict(TUI_MODE=mode,RUN_UI=run_ui,UI_PROVIDER=str(selected)+':create'),
        callback_origin='https://example.test')
    async def scenario():
        await runtime.astart()
        assert runtime.ui_provider is None
        await runtime.aclose()
    asyncio.run(scenario())


def test_retry_cancellation_is_shielded_and_preserved_after_disposal():
    async def scenario():
        entered,finish=asyncio.Event(),asyncio.Event()
        calls=[]
        async def close():
            entered.set()
            await finish.wait()
            calls.append('runtime')
        def release(**kwargs): calls.append('lease')
        cleanup=OperationCleanup(SimpleNamespace(release=release),SimpleNamespace(aclose=close),None,object(),False,1)
        retry=asyncio.create_task(cleanup.retry())
        await entered.wait()
        retry.cancel()
        await asyncio.sleep(0)
        retry.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError): await retry
        assert calls==['runtime','lease']
        await cleanup.retry()
        assert calls==['runtime','lease']
    asyncio.run(scenario())


def test_cancelled_retry_retains_pending_handle_until_child_confirms():
    async def scenario():
        entered,finish=asyncio.Event(),asyncio.Event()
        releases=[]
        async def close():
            entered.set()
            await finish.wait()
        cleanup=OperationCleanup(SimpleNamespace(release=lambda **kwargs:releases.append('lease')),
            SimpleNamespace(aclose=close),None,object(),False,.01)
        retry=asyncio.create_task(cleanup.retry())
        await entered.wait()
        retry.cancel()
        with pytest.raises(asyncio.CancelledError) as error: await retry
        assert isinstance(error.value.cleanup_pending,CleanupPendingError)
        assert releases==[]
        finish.set()
        await error.value.cleanup_pending.retry()
        assert releases==['lease']
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid_kind',['api','ui'])
def test_invalid_factory_pending_retains_its_required_clients(invalid_kind,tmp_path):
    from unittest.mock import AsyncMock
    from api_agent.api.application import RuntimeLifecycleError
    upstream=tmp_path/'upstream.py'
    upstream.write_text('class Adapter: version=1\ndef create(config,services): return Adapter()')
    api=tmp_path/'api.py'
    api.write_text('class Adapter: version=1\ndef create(config,services): return Adapter()')
    invalid=tmp_path/'invalid.py'
    invalid.write_text('class Adapter:\n    version=2\n    async def aclose(self): raise ValueError("private")\ndef create(config,services): return Adapter()')
    values=dict(API_PROVIDER=str(invalid if invalid_kind=='api' else api)+':create',
                UPSTREAM_API_PROVIDER=str(upstream)+':create')
    if invalid_kind=='ui': values['UI_PROVIDER']=str(invalid)+':create'
    runtime=compose_runtime(SimpleNamespace(pack_id='synthetic',identity_factory_path=None),values,
        callback_origin='https://example.test',required_kinds={'api','ui'})
    async def scenario():
        with pytest.raises(RuntimeLifecycleError) as error: await runtime.astart()
        assert isinstance(error.value.cleanup_pending,CleanupPendingError)
        failed=runtime._retained_owners[0]
        assert not failed.disposed and not runtime.upstream_provider.disposed
        if invalid_kind=='ui': assert not runtime.api_provider.disposed
        with pytest.raises(CleanupPendingError): await runtime.aclose()
        assert not runtime.upstream_provider.disposed
        failed.implementation.aclose=AsyncMock()
        await error.value.cleanup_pending.retry()
        assert failed.disposed and runtime.upstream_provider.disposed
        if invalid_kind=='ui': assert runtime.api_provider.disposed
    asyncio.run(scenario())


@pytest.mark.parametrize('flag', ['HELP', 'COLLECT_ONLY'])
@pytest.mark.parametrize('required', [None, {'llm', 'access', 'api', 'ui'}])
def test_help_and_collection_flags_prevent_execution_even_with_mode(flag, required):
    assert activation_kinds({'TUI_MODE': 'run-local', 'RUN_UI': True, flag: True}, required) == frozenset()
