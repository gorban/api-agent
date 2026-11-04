"""Offline retained-work and cleanup policies at real operation boundaries."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.telemetry.privacy import EmissionBoundary
from test_inference_shared_join import REQUEST, RESULT
from test_studio_retained_owner import manual, Automated
from test_workflow_observations import collector, definition

PROVIDER = str(Path(__file__).resolve().parents[2] / 'src/api_agent/telemetry/trace_adapters/provider.py') + ':create_tracing_provider'


def test_manual_phase_cannot_append_finalized_content_or_usage(tmp_path,monkeypatch):
    from api_agent.testing.inference_content import publish_content
    from api_agent.testing.inference_usage import publish
    from api_agent.workflows.run_context import test_evidence
    events=tmp_path/'finalized-events'
    events.write_bytes(b'completed suite\n')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','suite.py::test_prompt (call)')
    with test_evidence(False):
        assert publish_content(({'phase':'request','content':{'text':'manual only'}},),str(uuid4())) is None
        assert publish(REQUEST,RESULT) is None
    assert events.read_bytes()==b'completed suite\n'


@pytest.mark.parametrize('packet',[None,[],True,'private-receipt'])
def test_protected_snapshot_rejects_nonmapping_trace_receipt_before_worker_admission(tmp_path,monkeypatch,packet):
    from test_integration_provider_setup import snapshot
    from api_agent.testing import provider_setup
    path=snapshot(tmp_path,monkeypatch)
    data=json.loads(path.read_text());data['trace_collector_plan']=packet
    path.write_text(json.dumps(data))
    from api_agent.providers.contracts import ContractError
    with pytest.raises(ContractError,match='Integration provider context is unavailable'):
        provider_setup._read_snapshot()


def test_failed_remote_offer_returns_unknown_owner_without_replaying_or_retaining_payload(monkeypatch):
    from api_agent.testing.provider_setup import _trace_offer
    from api_agent.workflows.observations import WorkflowObservationFactory
    from api_agent.telemetry.operation import TraceLoss
    from api_agent.telemetry.worker_trace import ownership
    trace=SimpleNamespace(is_model_identity=lambda value:False,boundary=EmissionBoundary(),losses=TraceLoss())
    owner=WorkflowObservationFactory({},boundary=trace.boundary,trace=trace).create(definition())
    failure=RuntimeError('unpublishable offer detail')
    callback=Mock(side_effect=failure)
    monkeypatch.setattr(owner,'remote_offer',callback)
    call_id=str(uuid4())
    offer=_trace_offer((owner,str(uuid4()),None),call_id)
    assert offer is not None and offer.state is None
    assert owner.trace_losses.count==1
    assert not owner._call_payloads and owner._remote_pending==0
    callback.assert_called_once()


def test_legacy_remote_offer_failure_without_loss_owner_preserves_exact_failure():
    from api_agent.testing.provider_setup import _trace_offer
    failure=OSError('offer unavailable')
    class LegacyOwner:
        def remote_offer(self,*args):raise failure
    with pytest.raises(OSError) as caught:_trace_offer((LegacyOwner(),'visit',None),str(uuid4()))
    assert caught.value is failure


def test_setup_trace_disposal_retains_exact_sdk_and_warns_once_until_acknowledged(tmp_path,monkeypatch,capsys):
    from api_agent.testing.setup_backend import SetupBackend,_SetupOwner
    from api_agent.testing.trace_cleanup import optional_trace_cleanup
    with collector() as (endpoint,received):
        async def run():
            backend=SetupBackend({'TRACE_PROVIDER':PROVIDER,'TRACE_MODE':'OTEL','TRACE_EXPORT_ENDPOINT':endpoint,'JAEGER_VIEWER_PORT':16686},tmp_path)
            owner=_SetupOwner(backend)
            await owner.trace.astart()
            runtime=owner.trace.runtime
            sdk=runtime._provider
            original=sdk.shutdown
            monkeypatch.setattr(sdk,'shutdown',Mock(side_effect=RuntimeError('sensitive failure')))
            for _ in range(2):
                with pytest.raises(CleanupPendingError) as caught:await owner._close_tracing()
                assert caught.value.owners==(owner,)
                assert runtime._provider is sdk and not runtime.disposal_confirmed
            diagnostic=capsys.readouterr().err
            assert diagnostic.count('Tracing cleanup remains pending')==1
            assert 'sensitive failure' not in diagnostic
            monkeypatch.setattr(sdk,'shutdown',original)
            await caught.value.retry()
            assert runtime.disposal_confirmed
            await owner.aclose()
        asyncio.run(run())


def test_unrelated_runtime_cleanup_receipt_is_never_optional_trace(tmp_path,monkeypatch):
    from api_agent.api import Runtime
    from api_agent.testing.studio_http import _close_runtime,studio_trace_pending
    from api_agent.testing.studio_tracing import StudioTraceOwner
    from api_agent.testing.studio_retained import RetainedStudio
    trace=StudioTraceOwner({}, {},RetainedStudio(Automated(),lambda _:None,lambda:False))
    class OtherOwner:
        blocked=True
        async def aclose(self):
            if self.blocked:raise RuntimeError('provider cleanup pending')
    other=OtherOwner()
    async def run():
        runtime=Runtime('synthetic',tracing=trace,identity_adapter=other)
        with pytest.raises(CleanupPendingError) as caught:await _close_runtime(runtime)
        assert not studio_trace_pending(caught.value)
        assert caught.value.owners==(other,trace)
        other.blocked=False
        await caught.value.retry()
    asyncio.run(run())


@pytest.mark.parametrize('quote_fails',[False,True])
def test_retained_manual_terminal_accounting_survives_optional_quote_failure(manual,monkeypatch,quote_fails):
    from api_agent.testing.studio_retained import RetainedStudio
    from api_agent.testing import studio_retained
    factory,seen,_=manual
    async def run():
        retained=RetainedStudio(Automated(),factory,lambda:True)
        retained.retire_test()
        await retained.describe()
        quote=Mock(side_effect=ValueError('private pricing detail')) if quote_fails else Mock(return_value={'list_cost_usd':None,'payment':None})
        monkeypatch.setattr(studio_retained,'quote_cost',quote)
        try:
            identifier=str(uuid4())
            retained._record(REQUEST,identifier,'2026-01-01T00:00:00Z')
            retained._record(REQUEST,identifier,'2026-01-01T00:00:00Z',result=RESULT,finished=True)
            assert retained.usage.report()['prompt_count']==1
            assert retained.usage.rows()[0]['total_tokens']==3
            quote.assert_called_once()
        finally:await retained.aclose()
    asyncio.run(run())


def test_synchronous_debug_close_drains_disconnected_inspection_before_closing_loop(tmp_path,capsys):
    from api_agent.testing.provider_setup import IntegrationLLM
    from api_agent.providers.contracts import BrowserSession
    worker=IntegrationLLM({'DEBUG':True},tmp_path)
    async def admit():
        worker.inspection.observe(BrowserSession(browser=SimpleNamespace(is_connected=lambda:False),
            context=SimpleNamespace(pages=[]),page=object()))
    worker.loop.run_until_complete(admit())
    worker.close()
    assert worker.closed and worker.loop.is_closed() and worker.inspection._closed
    assert 'Debug browser retained' in capsys.readouterr().out


def test_finalized_success_does_not_waive_genuine_debug_backend_finish_failure(tmp_path,monkeypatch):
    from test_integration_runner import fixture_repo,context
    from api_agent.testing.integration import run_integration_tests
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.setup_pool import SetupPool
    base=fixture_repo(tmp_path/'base','def test_ok(llm_provider):\n    assert llm_provider is not None\n')
    ctx=context(base)
    selected=collect_tests(ctx,'integration')
    failure=OSError('genuine final backend failure')
    original=SetupPool.finish_run
    pools=[]
    def finish(pool):
        pools.append(pool)
        original(pool)
        raise failure
    monkeypatch.setattr(SetupPool,'finish_run',finish)
    published=[]
    with pytest.raises(OSError) as caught:
        run_integration_tests(ctx,selected,accepted=True,config={'DEBUG':True},after_run=published.append)
    assert caught.value is failure
    assert len(published)==1 and published[0].tests[0].status=='passed'
    assert pools and all(owner.closed and not owner.thread.is_alive() for owner in pools[0].owners)


def test_failed_trace_diagnostic_is_fatal_without_converting_to_optional_cleanup(tmp_path,monkeypatch):
    from api_agent.testing.setup_backend import SetupBackend,_SetupOwner
    from api_agent.testing.trace_cleanup import optional_trace_cleanup
    from api_agent.telemetry import operation
    with collector() as (endpoint,received):
        async def run():
            backend=SetupBackend({'TRACE_PROVIDER':PROVIDER,'TRACE_MODE':'OTEL','TRACE_EXPORT_ENDPOINT':endpoint,'JAEGER_VIEWER_PORT':16686},tmp_path)
            owner=_SetupOwner(backend)
            await owner.trace.astart()
            diagnostic=OSError('diagnostic writer unavailable')
            with monkeypatch.context() as patch:
                patch.setattr(operation.logging.getLogger(operation.__name__),'error',Mock(side_effect=diagnostic))
                with pytest.raises(OSError):owner.trace.runtime.losses.record()
            original=owner.trace.aclose
            monkeypatch.setattr(owner.trace,'aclose',AsyncMock(side_effect=diagnostic))
            with pytest.raises(OSError) as caught:await owner._close_tracing()
            assert caught.value is diagnostic and not optional_trace_cleanup(caught.value)
            assert not owner._trace_cleanup_reported
            monkeypatch.setattr(owner.trace,'aclose',original)
            await owner.aclose()
        asyncio.run(run())


@pytest.mark.parametrize('subclass',[False,True])
def test_setup_backend_rejects_unadmitted_credential_authority_before_allocation(tmp_path,monkeypatch,subclass):
    from api_agent.testing.setup_backend import SetupBackend
    from api_agent.providers.contracts import ContractError
    class UnadmittedBoundary(EmissionBoundary):pass
    authority=UnadmittedBoundary() if subclass else object()
    thread=Mock(side_effect=AssertionError('Invalid authority must remain inert'))
    monkeypatch.setattr('threading.Thread',thread)
    with pytest.raises(ContractError,match='Invalid integration credential authority'):
        SetupBackend({},tmp_path,authority=authority)
    thread.assert_not_called()


def test_publication_before_backend_allocation_has_no_cleanup_effect(tmp_path):
    from api_agent.testing.setup_backend import SetupBackend
    backend=SetupBackend({},tmp_path)
    asyncio.run(backend._prepare_publication())
    assert backend.loop is backend.thread is backend.server is backend.registry is None
    assert not backend.closed and not backend.pending
    backend.close()
    assert backend.closed


from test_setup_backend import fake_host


def test_first_genuine_server_failure_remains_primary_when_provider_cleanup_also_fails(fake_host,monkeypatch):
    from test_setup_backend import checkpoint
    from api_agent.testing.trace_cleanup import optional_trace_cleanup
    state=fake_host
    backend=state.backend
    backend.start();checkpoint(backend)
    original=backend.server.aclose
    first=OSError('server disposal pending')
    async def fail():raise first
    monkeypatch.setattr(backend.server,'aclose',fail)
    state.provider_failure=True
    with pytest.raises(CleanupPendingError) as caught:backend.close()
    assert not optional_trace_cleanup(caught.value)
    cause=caught.value
    while cause.__cause__ is not None:cause=cause.__cause__
    assert cause is first
    assert backend.pending and not backend.closed and backend.thread.is_alive()
    state.provider_failure=False
    monkeypatch.setattr(backend.server,'aclose',original)
    asyncio.run(caught.value.retry())
    assert backend.closed and not backend.thread.is_alive()
