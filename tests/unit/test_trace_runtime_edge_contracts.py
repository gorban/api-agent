"""Boundary, cancellation and optional-startup cases for runtime trace owners."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock

import pytest

from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.telemetry.worker_trace import WorkerTraceOffer
from api_agent.testing.studio_tracing import StudioTraceOwner,_TraceSession,StudioTraceCleanupPending
from api_agent.testing.studio_retained import RetainedStudio
from api_agent.testing.workflow_capture import capture_sources
from test_workflow_observations import config
from test_studio_retained_owner import Automated
from test_passive_inventory import source


def scope():
    return {'client':('127.0.0.1',1234),'headers':[(b'origin',b'http://localhost:9443'),(b'x-api-agent-session',b'x'*32)]}


class Runtime:
    def __init__(self,config,*,boundary,**kwargs):
        self.boundary=boundary
        self.astart=AsyncMock()
        self.aclose=AsyncMock()
        self.bind_model_identities=Mock()


def trace_owner(**kwargs):
    settings=config(False,False)
    owner=StudioTraceOwner(settings.values,capture_sources(settings.values,settings.fields),kwargs.pop('provider',Automated()),runtime_factory=kwargs.pop('runtime_factory',Runtime),**kwargs)
    owner.bind_http('http://localhost:9443','x'*32)
    return owner


@pytest.mark.parametrize('candidate,result',[(object(),object()),(None,None)])
def test_structural_provider_facts_reject_unvalidated_values(candidate,result):
    from api_agent.telemetry.genai_fields import provider_fields
    with pytest.raises(TypeError,match='validated request'):provider_fields(candidate,result)


def test_duplicate_deferred_terminal_is_rejected_without_overwriting_first():
    loss=Mock()
    owner=SimpleNamespace(trace_losses=SimpleNamespace(record=loss))
    receipt=WorkerTraceOffer(owner,'call',{},local=True)
    first,duplicate=Mock(),Mock()
    assert receipt.defer(first)
    assert not receipt.defer(duplicate)
    receipt.finalize_evidence(lambda:None)
    first.assert_called_once()
    duplicate.assert_not_called()
    loss.assert_called_once()


@pytest.mark.parametrize('state',['granted',None])
def test_nonlocal_finalization_never_captures_worker_payload(state):
    receipt=WorkerTraceOffer(SimpleNamespace(trace_losses=SimpleNamespace(record=Mock())),'call',{})
    receipt.receipt(state)
    capture=Mock()
    receipt.finalize_evidence(capture)
    capture.assert_not_called()


@pytest.mark.parametrize('case',['unbound','shape','external','duplicate','invalid_ip','invalid_header'])
def test_invalid_http_authority_is_inert_before_runtime_admission(case):
    factory=Mock(side_effect=AssertionError('unauthorized allocation'))
    owner=trace_owner(runtime_factory=factory)
    request=scope()
    if case=='unbound':owner._http=None
    elif case=='shape':request=[]
    elif case=='external':request['client']=('192.0.2.1',1)
    elif case=='duplicate':request['headers'].append(request['headers'][0])
    elif case=='invalid_ip':request['client']=('invalid address',1)
    elif case=='invalid_header':request['headers']=[(b'\xff',b'value')]
    async def run():
        async with owner.request(request) as runtime:assert runtime is None
        await owner.aclose()
    asyncio.run(run())
    factory.assert_not_called()


@pytest.mark.parametrize('state',['closed','disabled','manual_waiting'])
def test_nonadmitted_session_states_never_create_sdk(state):
    owner=trace_owner(runtime_factory=Mock(side_effect=AssertionError('session allocation')))
    if state=='closed':owner._closed=True
    elif state=='disabled':owner.config.values['TRACE_MODE']=''
    else:
        retained=RetainedStudio(Automated(),lambda _:None,lambda:False)
        retained.retire_test();owner.provider=retained
    owner.bind_catalog(())
    async def run():
        async with owner.request(scope()) as runtime:assert runtime is None
    asyncio.run(run())
    owner.runtime_factory.assert_not_called()


def test_failed_admission_warns_once_and_does_not_retry_factory(caplog):
    factory=Mock(side_effect=ValueError('private detail'))
    owner=trace_owner(runtime_factory=factory)
    async def run():
        for _ in range(2):
            async with owner.request(scope()) as runtime:assert runtime is None
        await owner.aclose()
    asyncio.run(run())
    factory.assert_called_once()
    assert caplog.text.count('Studio tracing setup failed')==1
    assert 'private detail' not in caplog.text


def test_startup_failure_is_nonfatal_and_does_not_restart_failed_sdk(caplog):
    runtime=None
    def factory(*args,**kwargs):
        nonlocal runtime
        runtime=Runtime(*args,**kwargs)
        runtime.astart.side_effect=ValueError('private detail')
        return runtime
    owner=trace_owner(runtime_factory=factory)
    async def run():
        for _ in range(2):
            async with owner.request(scope()) as selected:assert selected is runtime
        await owner.aclose()
    asyncio.run(run())
    runtime.astart.assert_awaited_once()
    assert caplog.text.count('Studio tracing setup failed')==1
    assert 'private detail' not in caplog.text


def test_cancelled_startup_drops_active_request_count_and_preserves_cancellation():
    cancellation=asyncio.CancelledError()
    runtime=Runtime(None,boundary=EmissionBoundary())
    runtime.astart.side_effect=cancellation
    session=_TraceSession(runtime,None)
    async def run():
        with pytest.raises(asyncio.CancelledError) as caught:await session.enter()
        assert caught.value is cancellation and session._active==0
        await session.aclose()
    asyncio.run(run())


def test_diagnostic_failure_is_fatal_and_cannot_be_classified_trace_only():
    failure=OSError('diagnostic unavailable')
    runtime=Runtime(None,boundary=EmissionBoundary())
    runtime.losses=SimpleNamespace(_diagnostic_failed=True)
    runtime.aclose.side_effect=failure
    owner=trace_owner()
    owner._sessions['automated']=_TraceSession(runtime,None)
    async def run():
        with pytest.raises(OSError) as caught:await owner.aclose()
        assert caught.value is failure and owner._cleanup_fatal
    asyncio.run(run())


def test_acknowledged_delivery_loss_releases_collector_with_safe_warning(caplog):
    from api_agent.telemetry.tracing import TraceDeliveryError,TRACE_DELIVERY_WARNING
    runtime=Runtime(None,boundary=EmissionBoundary())
    runtime.aclose.side_effect=TraceDeliveryError('private detail')
    manager=SimpleNamespace(release=Mock())
    session=_TraceSession(runtime,manager)
    asyncio.run(session.aclose())
    assert session.closed
    manager.release.assert_called_once_with(retain=False)
    assert TRACE_DELIVERY_WARNING in caplog.text and 'private detail' not in caplog.text


def test_manual_retry_keeps_failed_automated_cleanup_and_warns_only_once(caplog):
    from api_agent.telemetry.privacy import EmissionBoundary
    attempts=0
    def manual_factory(_):
        nonlocal attempts
        attempts+=1
        if attempts==1:raise asyncio.CancelledError()
        return SimpleNamespace(emissions=EmissionBoundary())
    retained=RetainedStudio(Automated(),manual_factory,lambda:True)
    owner=trace_owner(provider=retained)
    async def run():
        async with owner.request(scope()):pass
        automated=owner._sessions['automated']
        automated.runtime.aclose.side_effect=RuntimeError('pending exporter')
        retained.retire_test()
        with pytest.raises(asyncio.CancelledError):
            async with owner.request(scope()):pass
        async with owner.request(scope()) as selected:
            assert selected is owner._sessions['manual'].runtime
        assert caplog.text.count('Studio tracing cleanup is incomplete')==1
        automated.runtime.aclose.side_effect=None
        await owner.aclose()
        assert automated.closed and owner._sessions['manual'].closed
    asyncio.run(run())


@pytest.mark.parametrize('invalid',['wrong_backend','wrong_origin','wrong_registry','nontrace_live','trace_settled'])
def test_optional_cleanup_admission_rejects_unqualified_owner(tmp_path,invalid):
    from api_agent.testing.setup_backend import SetupBackend,_SetupOwner
    from api_agent.testing.trace_cleanup import admit_optional_trace_receipt
    backend=SetupBackend({},tmp_path)
    origin=object.__new__(_SetupOwner)
    origin._nontrace_settled=True;origin._trace_cleanup_failed=True
    backend.registry=SimpleNamespace(_owner=origin)
    if invalid=='wrong_backend':backend=object()
    elif invalid=='wrong_origin':origin=object()
    elif invalid=='wrong_registry':backend.registry._owner=object()
    elif invalid=='nontrace_live':origin._nontrace_settled=False
    else:origin._trace_cleanup_failed=False
    with pytest.raises(TypeError,match='stopped provider'):admit_optional_trace_receipt(backend,origin)


def test_builtin_collector_snapshot_restores_only_captured_inventory(source,monkeypatch):
    from api_agent.testing.studio_tracing import capture_collector_plan,collector_from_snapshot
    from api_agent.runtime.prerequisites import PrerequisiteManager
    from api_agent.runtime.inventory import InventoryCatalog
    from api_agent.telemetry.tracing_providers import effective_selection
    from api_agent.runtime.tracing_context import capture_trace_context
    settings=config()
    selection=effective_selection(settings,environ={})
    packet=capture_collector_plan(source.context,settings,trace_context={'environ':{}})
    assert packet['inventory']
    before=packet['inventory']
    source.context.pack_root.joinpath('normal.toml').write_text('not parsed after capture')
    manager=collector_from_snapshot({'trace_collector_plan':packet,'owner_root':str(source.base),
        'trace_context':capture_trace_context(selection)},selection.values)
    assert isinstance(manager,PrerequisiteManager)
    assert manager.plan.inventory.to_wire()==before
    remote=config(TRACE_EXPORT_ENDPOINT='https://example.test/v1/traces')
    remote_selection=effective_selection(remote,environ={})
    remote_packet=capture_collector_plan(source.context,remote,trace_context={'environ':{}})
    assert collector_from_snapshot({'trace_collector_plan':remote_packet,'owner_root':str(source.base),
        'trace_context':capture_trace_context(remote_selection)},remote_selection.values) is None
    assert capture_collector_plan(source.context,config(mode='')) is None



@pytest.mark.parametrize('packet',[None,{}, {'version':1,'base_root':'relative','pack_root':'relative','inventory':{}}, {'version':False,'base_root':'/base','pack_root':'/pack','inventory':{}}])
def test_managed_collector_rejects_missing_or_invalid_captured_authority(packet,monkeypatch,tmp_path):
    from api_agent.testing.studio_tracing import collector_from_snapshot
    manager = Mock(side_effect=AssertionError('Invalid receipt must not allocate a manager'))
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',manager)
    from api_agent.telemetry.tracing_providers import effective_selection
    from api_agent.runtime.tracing_context import capture_trace_context
    settings=config()
    selection=effective_selection(settings,environ={})
    snapshot={'trace_collector_plan':packet,'owner_root':str(tmp_path),'trace_context':capture_trace_context(selection)}
    reason='Captured tracing collector is unavailable' if packet=={} else 'collector receipt'
    with pytest.raises(ValueError,match=reason):
        collector_from_snapshot(snapshot,selection.values)
    manager.assert_not_called()
