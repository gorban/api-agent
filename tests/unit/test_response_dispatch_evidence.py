"""Dispatch metadata survives real local/IPC failure and cancellation boundaries."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.control import OperationalError
from api_agent.providers.inference import request_to_wire, result_to_wire, error_usage
from api_agent.providers.llm import Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupClient, SetupServer, _request_digest, _error, _raise_error
from api_agent.workflows.inference_capture import bind_response_dispatch, record_response_dispatch
from test_response_policy_contract import request, result, evidence


@pytest.mark.parametrize('failure_type', [TimeoutError, OperationalError])
def test_shared_owner_keeps_dispatched_mode_and_explicit_usage_without_changing_failure(failure_type):
    from api_agent.providers.shared_setup import SharedSetupOwner
    req=request('json');error=failure_type('deadline');error.usage=Usage(3,2)
    calls=[]
    async def complete(value, *, auth):
        calls.append(value)
        record_response_dispatch(evidence(value,'prompted_json','unsupported_mode'))
        raise error
    async def authenticated(callback):return await callback(auth='synthetic-auth')
    child=SimpleNamespace(call=authenticated,aclose=AsyncMock())
    async def scenario():
        owner=SharedSetupOwner('operation','identity',lambda _:child,complete=complete)
        try:
            with bind_response_dispatch() as state:
                with pytest.raises(failure_type) as caught:await owner.complete(req)
            assert caught.value is error
            assert error.response==state['response']==evidence(req,'prompted_json','unsupported_mode')
            assert error_usage(error)==Usage(3,2) and calls==[req]
        finally:await owner.aclose()
    asyncio.run(scenario())


def test_shared_owner_cancellation_retains_dispatch_fact_until_child_drains():
    from api_agent.providers.shared_setup import SharedSetupOwner
    req=request('json')
    async def scenario():
        entered=asyncio.Event();release=asyncio.Event();closed=[]
        async def complete(value, *, auth):
            record_response_dispatch(evidence(value));entered.set()
            try:await release.wait();return result(value)
            finally:closed.append('once')
        async def authenticated(callback):return await callback(auth='synthetic-auth')
        owner=SharedSetupOwner('operation','identity',lambda _:SimpleNamespace(call=authenticated,aclose=AsyncMock()),complete=complete)
        try:
            with bind_response_dispatch() as state:
                task=asyncio.create_task(owner.complete(req));await entered.wait()
                task.cancel();release.set()
                with pytest.raises(asyncio.CancelledError) as caught:await task
                assert caught.value.response==state['response']==evidence(req)
            assert task.cancelled() and closed==['once']
        finally:await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure_type',[TimeoutError,OperationalError,ProviderAccessError])
def test_real_ipc_postdispatch_failure_keeps_existing_error_category_evidence_and_usage(failure_type):
    req=request('json')
    calls=[]
    async def scenario():
        async def complete(value):
            calls.append(value);record_response_dispatch(evidence(value))
            error=ProviderAccessError('deadline','Safe failure',usage=Usage(3,2)) if failure_type is ProviderAccessError else failure_type('deadline')
            error.usage=Usage(3,2)
            raise error
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        registry=SetupRegistry('operation','identity',lambda:owner)
        server=SetupServer(registry,'synthetic-response-dispatch-capability-000000')
        client=SetupClient(await server.start(),worker_id='worker',timeout=5)
        try:
            expected=failure_type if failure_type in (ProviderAccessError,OperationalError) else ContractError
            with pytest.raises(expected) as caught:await asyncio.to_thread(client.complete,req)
            assert caught.value.response==client.response_evidence==evidence(req)
            assert error_usage(caught.value)==Usage(3,2) and calls==[req]
        finally:
            await asyncio.to_thread(client.close);await server.aclose()
    asyncio.run(scenario())


def test_real_ipc_cancellation_preserves_cancelled_exception_after_dispatch_and_drain():
    req=request('json')
    async def scenario():
        cleaned=[]
        async def complete(value):
            record_response_dispatch(evidence(value))
            try:await asyncio.Future()
            finally:cleaned.append('once')
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        server=SetupServer(SetupRegistry('operation','identity',lambda:owner),'synthetic-response-dispatch-capability-000000',cleanup_timeout=.1)
        client=SetupClient(await server.start(),worker_id='worker',timeout=5,cleanup_timeout=.1)
        pending=asyncio.create_task(asyncio.to_thread(client.complete,req))
        try:
            async with asyncio.timeout(3):
                while client.response_evidence is None:await asyncio.sleep(.001)
            client._loop.call_soon_threadsafe(client._request_task.cancel)
            with pytest.raises(asyncio.CancelledError) as caught:await pending
            assert caught.value.response==evidence(req) and error_usage(caught.value) is None
        finally:
            await asyncio.to_thread(client.close);await server.aclose()
        assert cleaned==['once']
    asyncio.run(scenario())


def test_real_ipc_preflight_rejection_then_identical_valid_calls_keep_peer_sequence():
    req=replace(request('json'),request_id='');calls=[]
    async def scenario():
        async def complete(value):
            calls.append(value)
            if len(calls)==1:raise ProviderAccessError('unsupported','Unsupported preflight')
            record_response_dispatch(evidence(value));return result(value)
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        server=SetupServer(SetupRegistry('operation','identity',lambda:owner),'synthetic-response-dispatch-capability-000000')
        client=SetupClient(await server.start(),worker_id='worker',timeout=5)
        try:
            with pytest.raises(ProviderAccessError):await asyncio.to_thread(client.complete,req)
            assert client.response_evidence is None
            assert await asyncio.to_thread(client.complete,req)==result(req)
            assert await asyncio.to_thread(client.complete,req)==result(req)
            assert client._complete_count==3 and len(calls)==3
        finally:
            await asyncio.to_thread(client.close);await server.aclose()
    asyncio.run(scenario())


def dispatch_frame(req,sequence=1):
    from api_agent.providers.rich_inference import response_evidence_to_wire
    return dict(version=1,status='response_policy',call_sequence=sequence,request_sha256=_request_digest(request_to_wire(req)),binding_sha256=_request_digest({}),response=dict(version=3,response=response_evidence_to_wire(evidence(req))))


@pytest.mark.parametrize('change',[
    lambda f:f.update(call_sequence=True),lambda f:f.update(call_sequence=2),lambda f:f.update(request_sha256='0'*64),
    lambda f:f.pop('request_sha256'),lambda f:f.update(extra=1),lambda f:f['response'].update(version=2),
    lambda f:f['response'].pop('version'),lambda f:f['response'].update(extra=1),
    lambda f:f['response']['response'].update(schema_sha256='0'*64),lambda f:f['response']['response'].update(fallback='reject'),
])
def test_dispatch_status_rejects_unbound_replayed_or_non_v3_values(monkeypatch,change):
    import api_agent.providers.setup_transport as transport
    req=request('json');frame=dispatch_frame(req);change(frame)
    client=SetupClient(dict(version=1,host='127.0.0.1',port=12345,capability='synthetic-response-dispatch-capability-000000'),worker_id='worker')
    client._writer=SimpleNamespace(write=lambda _:None,drain=AsyncMock());client._reader=object();client._binding={}
    monkeypatch.setattr(transport,'_read',AsyncMock(return_value=frame))
    with pytest.raises((ContractError,ProviderAccessError)):asyncio.run(client._exchange('complete',request_to_wire(req)))


def test_dispatch_status_rejects_duplicate_and_later_replay_even_for_empty_request_id(monkeypatch):
    import api_agent.providers.setup_transport as transport
    req=replace(request('json'),request_id='');frame=dispatch_frame(req)
    client=SetupClient(dict(version=1,host='127.0.0.1',port=12345,capability='synthetic-response-dispatch-capability-000000'),worker_id='worker')
    client._writer=SimpleNamespace(write=lambda _:None,drain=AsyncMock());client._reader=object();client._binding={}
    monkeypatch.setattr(transport,'_read',AsyncMock(side_effect=[frame,dict(version=1,status='completed',result=result_to_wire(result(req))),frame]))
    assert asyncio.run(client._exchange('complete',request_to_wire(req)))==result(req)
    client.response_evidence=None
    with pytest.raises(ContractError):asyncio.run(client._exchange('complete',request_to_wire(req)))
    client._complete_count=0;client.response_evidence=None
    monkeypatch.setattr(transport,'_read',AsyncMock(side_effect=[frame,frame]))
    with pytest.raises(ContractError):asyncio.run(client._exchange('complete',request_to_wire(req)))


def test_dispatch_fact_is_typed_idempotent_and_never_overwrites_chosen_mode():
    req=request('json');observed=[]
    with bind_response_dispatch(observed.append) as state:
        record_response_dispatch(evidence(req));record_response_dispatch(evidence(req))
        assert state['response']==evidence(req) and observed==[evidence(req)]
        with pytest.raises(ContractError):record_response_dispatch(evidence(req,'prompted_json','unsupported_mode'))
    assert error_usage(SimpleNamespace(usage={'input_tokens':3})) is None


def test_generic_legacy_failure_wire_is_unchanged_and_nested_policy_failure_is_exact_v3():
    assert 'details' not in _error(TimeoutError())
    error=TimeoutError();error.response=evidence(request());error.usage=Usage(3,2)
    frame=_error(error)
    assert frame['details']['version']==3
    with pytest.raises(ContractError) as caught:_raise_error(frame)
    assert caught.value.response==error.response and error_usage(caught.value)==error.usage
    bad=deepcopy(frame);bad['details']['version']=2
    with pytest.raises(ContractError,match='evidence'):_raise_error(bad)


def bound_client():
    client=SetupClient(dict(version=1,host='127.0.0.1',port=12345,capability='synthetic-response-dispatch-capability-000000'),worker_id='worker')
    client._binding=dict(version=1,operation_id='operation',selected_identity='identity',worker_id='worker',attempt=1)
    client._writer=SimpleNamespace(write=lambda _:None,drain=AsyncMock());client._reader=object()
    return client


def test_terminal_error_cannot_contradict_admitted_dispatch_mode(monkeypatch):
    req=request('json');client=bound_client()
    error=ProviderAccessError('unknown','Safe failure',usage=Usage(3,2),response=evidence(req))
    def fail(*args):
        client.response_evidence=evidence(req,'prompted_json','unsupported_mode')
        raise error
    monkeypatch.setattr(client,'_run_request',fail)
    with pytest.raises(ContractError,match='Conflicting') as caught:client.complete(req)
    assert error_usage(caught.value)==Usage(3,2)
    assert caught.value.response==evidence(req,'prompted_json','unsupported_mode')
    assert error.response==evidence(req) and error.usage==Usage(3,2)


def test_shared_owner_rejects_terminal_error_conflict_without_overwriting_evidence():
    from api_agent.providers.shared_setup import SharedSetupOwner
    req=request('json');error=ProviderAccessError('unknown','Safe failure',usage=Usage(3,2),response=evidence(req))
    async def complete(value, *, auth):
        record_response_dispatch(evidence(value,'prompted_json','unsupported_mode'));raise error
    async def authenticated(callback):return await callback(auth='synthetic-auth')
    async def scenario():
        owner=SharedSetupOwner('operation','identity',lambda _:SimpleNamespace(call=authenticated,aclose=AsyncMock()),complete=complete)
        try:
            with pytest.raises(ContractError,match='Conflicting'):await owner.complete(req)
            assert error.response==evidence(req) and error.usage==Usage(3,2)
        finally:await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('remote',[False,True])
def test_publisher_wrappers_reject_conflicting_terminal_mode_before_overwrite(monkeypatch,remote):
    from api_agent.testing import provider_setup,inference_usage
    from api_agent.telemetry.privacy import EmissionBoundary,ContentPolicy
    monkeypatch.setattr(inference_usage,'publish',lambda *a,**k:None)
    req=request('json');error=ProviderAccessError('unknown','Safe failure',usage=Usage(3,2),response=evidence(req))
    chosen=evidence(req,'prompted_json','unsupported_mode')
    if remote:
        owner=provider_setup.RemoteIntegrationLLM.__new__(provider_setup.RemoteIntegrationLLM)
        owner.config={};owner.client=SimpleNamespace(response_evidence=chosen,complete=lambda _:(_ for _ in ()).throw(error))
        owner._request=lambda call:call()
    else:
        owner=provider_setup.IntegrationLLM.__new__(provider_setup.IntegrationLLM)
        owner.config={};owner._provider_config={};owner.closed=False;owner.skip_unavailable=False;owner.emissions=EmissionBoundary(ContentPolicy(False))
        async def complete(value):record_response_dispatch(chosen);raise error
        owner._complete=complete;owner._run_private=asyncio.run
    with pytest.raises(ContractError,match='Conflicting'):owner.complete(req)
    assert error.response==evidence(req) and error.usage==Usage(3,2)


def test_remote_publisher_preserves_legacy_client_without_optional_metadata(monkeypatch):
    from api_agent.testing import provider_setup,inference_usage
    monkeypatch.setattr(inference_usage,'publish',lambda *a,**k:None)
    req=request('json');error=TimeoutError('controlled')
    owner=provider_setup.RemoteIntegrationLLM.__new__(provider_setup.RemoteIntegrationLLM)
    owner.config={};owner.client=SimpleNamespace(complete=lambda _:(_ for _ in ()).throw(error));owner._request=lambda call:call()
    with pytest.raises(TimeoutError) as caught:owner.complete(req)
    assert caught.value is error


@pytest.mark.parametrize('field,replacement',[('worker_id','foreign-worker'),('operation_id','foreign-operation'),('selected_identity','foreign-identity'),('attempt',2)])
def test_dispatch_status_binds_exact_admitted_peer_grant(monkeypatch,field,replacement):
    import api_agent.providers.setup_transport as transport
    req=replace(request('json'),request_id='');client=bound_client()
    frame=dispatch_frame(req);frame['binding_sha256']=_request_digest(client._binding)
    monkeypatch.setattr(transport,'_read',AsyncMock(side_effect=[frame,dict(version=1,status='completed',result=result_to_wire(result(req)))]))
    assert asyncio.run(client._exchange('complete',request_to_wire(req)))==result(req)
    client._complete_count=0;client.response_evidence=None
    foreign={**client._binding,field:replacement};frame['binding_sha256']=_request_digest(foreign)
    monkeypatch.setattr(transport,'_read',AsyncMock(return_value=frame))
    with pytest.raises(ContractError):asyncio.run(client._exchange('complete',request_to_wire(req)))


@pytest.mark.parametrize('remote',[False,True])
@pytest.mark.parametrize('dispatched',[False,True])
@pytest.mark.parametrize('partial_usage',[False,True])
def test_publisher_failure_wrappers_preserve_only_admitted_mode_and_explicit_usage(monkeypatch,remote,dispatched,partial_usage):
    from api_agent.testing import provider_setup,inference_usage
    from api_agent.telemetry.privacy import EmissionBoundary,ContentPolicy
    from api_agent.workflows.inference_capture import record_response_failure
    publications=[]
    monkeypatch.setattr(inference_usage,'publish',lambda *a,**k:publications.append(k))
    req=request('json');chosen=evidence(req);error=TimeoutError('controlled')
    if remote:
        if partial_usage:error.usage=Usage(3,2)
        owner=provider_setup.RemoteIntegrationLLM.__new__(provider_setup.RemoteIntegrationLLM)
        owner.config={};owner.client=SimpleNamespace(response_evidence=chosen if dispatched else None,complete=lambda _:(_ for _ in ()).throw(error));owner._request=lambda call:call()
    else:
        owner=provider_setup.IntegrationLLM.__new__(provider_setup.IntegrationLLM)
        owner.config={};owner._provider_config={};owner.closed=False;owner.skip_unavailable=False;owner.emissions=EmissionBoundary(ContentPolicy(False))
        async def complete(value):
            if dispatched:record_response_dispatch(chosen)
            if partial_usage:
                reported=TimeoutError();reported.usage=Usage(3,2);record_response_failure(reported)
                if not dispatched:error.usage=reported.usage
            raise error
        owner._complete=complete;owner._run_private=asyncio.run
    with pytest.raises(TimeoutError) as caught:owner.complete(req)
    assert caught.value is error
    assert getattr(error,'response',None)==(chosen if dispatched else None)
    assert error_usage(error)==(Usage(3,2) if partial_usage else None)
    assert publications[-1]['error'] is error and publications[-1]['failed'] is True


def test_server_rejects_conflicting_error_evidence_instead_of_rewriting_it():
    req=request('json');error=ProviderAccessError('unknown','Safe failure',usage=Usage(3,2),response=evidence(req))
    async def scenario():
        async def complete(value):record_response_dispatch(evidence(value,'prompted_json','unsupported_mode'));raise error
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        server=SetupServer(SetupRegistry('operation','identity',lambda:owner),'synthetic-response-dispatch-capability-000000')
        client=SetupClient(await server.start(),worker_id='worker',timeout=5)
        try:
            with pytest.raises(ContractError):await asyncio.to_thread(client.complete,req)
            assert error.response==evidence(req) and error.usage==Usage(3,2)
        finally:
            await asyncio.to_thread(client.close);await server.aclose()
    asyncio.run(scenario())


def test_dispatch_is_rejected_for_noncompletion_operations():
    from unittest.mock import Mock
    req=request('json')
    server=SetupServer.__new__(SetupServer)
    async def invalid(*args):record_response_dispatch(evidence(req))
    server._dispatch_captured=invalid
    peer=SimpleNamespace(complete_count=0,binding={},writer=SimpleNamespace(write=Mock()))
    with pytest.raises(ContractError,match='completion'):asyncio.run(server._dispatch(peer,{},'describe'))
    peer.writer.write.assert_not_called()


def test_expired_before_wire_admission_does_not_advance_peer_sequence(monkeypatch):
    import api_agent.providers.setup_transport as transport
    from unittest.mock import Mock
    req=request('json');client=bound_client();client._writer.write=Mock()
    token=transport._scope.set(SimpleNamespace(id='00000000-0000-0000-0000-000000000001',remaining=0))
    try:
        with pytest.raises(ProviderAccessError):asyncio.run(client._exchange('complete',request_to_wire(req)))
    finally:transport._scope.reset(token)
    assert client._complete_count==0
    client._writer.write.assert_not_called()
    frame=dispatch_frame(req);frame['binding_sha256']=_request_digest(client._binding)
    monkeypatch.setattr(transport,'_read',AsyncMock(side_effect=[frame,dict(version=1,status='completed',result=result_to_wire(result(req)))]))
    assert asyncio.run(client._exchange('complete',request_to_wire(req)))==result(req)
    assert client._complete_count==1


def test_tracker_is_inert_before_dispatch_and_retains_authoritative_terminal_usage():
    from api_agent.workflows import inference_capture as capture
    req=request('json');error=TimeoutError();error.usage=Usage(3,2)
    token=capture._DISPATCH.set(None)
    try:record_response_dispatch(evidence(req));capture.record_response_failure(error)
    finally:capture._DISPATCH.reset(token)
    with bind_response_dispatch() as state:
        capture.record_response_failure(error)
        assert state['response'] is None and state['usage'] is None
        record_response_dispatch(evidence(req))
        capture.record_response_failure(SimpleNamespace(usage={}))
        assert state['usage'] is None
    capture.retain_response_failure(error,evidence(req),Usage(1,1))
    assert error.usage==Usage(3,2)


@pytest.mark.parametrize('remote',[False,True])
@pytest.mark.parametrize('partial_usage',[False,True])
def test_conflicting_publisher_failure_still_has_one_terminal_canonical_receipt(tmp_path,monkeypatch,remote,partial_usage):
    import json
    from api_agent.testing import provider_setup
    from api_agent.testing.inference_usage import capture
    from api_agent.telemetry.usage import SuiteUsage
    from api_agent.telemetry.privacy import EmissionBoundary,ContentPolicy
    req=request('json');chosen=evidence(req,'prompted_json','unsupported_mode')
    error=ProviderAccessError('unknown','Safe failure',usage=Usage(9,2) if partial_usage else None,response=evidence(req))
    path=tmp_path/'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test_policy.py::test_conflict (call)')
    if remote:
        owner=provider_setup.RemoteIntegrationLLM.__new__(provider_setup.RemoteIntegrationLLM)
        owner.config={};owner.client=SimpleNamespace(response_evidence=chosen,complete=lambda _:(_ for _ in ()).throw(error));owner._request=lambda call:call()
    else:
        owner=provider_setup.IntegrationLLM.__new__(provider_setup.IntegrationLLM)
        owner.config={};owner._provider_config={};owner.closed=False;owner.skip_unavailable=False;owner.emissions=EmissionBoundary(ContentPolicy(False))
        async def complete(value):record_response_dispatch(chosen);raise error
        owner._complete=complete;owner._run_private=asyncio.run
    with pytest.raises(ContractError,match='Conflicting') as caught:owner.complete(req)
    rows=[json.loads(line)['data'] for line in path.read_text().splitlines()]
    assert len(rows)==2 and [r['outcome'] for r in rows]==['unknown','failed']
    assert rows[0]['call_id']==rows[1]['call_id']
    assert rows[1]['response']['actual_mode']=='prompted_json'
    assert rows[1]['usage']==(dict(input_tokens=9,output_tokens=2,cached_input_tokens=None,reasoning_tokens=None) if partial_usage else None)
    assert error_usage(caught.value)==(Usage(9,2) if partial_usage else None)
    assert error.response==evidence(req)
    tracker=SuiteUsage('run')
    for row in rows:capture(SimpleNamespace(text=json.dumps(row),identity=SimpleNamespace(id='test-id')),tracker)
    assert len(tracker.inference_calls)==1
    retained=next(iter(tracker.inference_calls.values()))
    assert retained['outcome']=='failed' and retained['response']==rows[1]['response']
    assert 'authored prompt' not in path.read_text() and 'Safe failure' not in path.read_text()


def test_local_failure_before_capture_starts_has_one_unlabeled_terminal_receipt(tmp_path,monkeypatch):
    import json
    from api_agent.testing import provider_setup
    from api_agent.testing.inference_usage import capture
    from api_agent.telemetry.privacy import EmissionBoundary,ContentPolicy
    from api_agent.telemetry.usage import SuiteUsage
    req=request('json');error=TimeoutError('controlled before dispatch')
    path=tmp_path/'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test_policy.py::test_before_capture (call)')
    owner=provider_setup.IntegrationLLM.__new__(provider_setup.IntegrationLLM)
    owner.config={};owner._provider_config={};owner.closed=False;owner.skip_unavailable=False
    owner.emissions=EmissionBoundary(ContentPolicy(False))
    entered=[];closed=[]
    async def complete(value):
        entered.append(value)
        raise AssertionError('Provider must not execute before capture startup')
    def fail_before_start(coroutine):
        coroutine.close();closed.append(coroutine.cr_frame is None)
        raise error
    owner._complete=complete;owner._run_private=fail_before_start
    with pytest.raises(TimeoutError) as caught:owner.complete(req)
    assert caught.value is error and entered==[] and closed==[True]
    rows=[json.loads(line)['data'] for line in path.read_text().splitlines()]
    assert len(rows)==2 and [row['outcome'] for row in rows]==['unknown','failed']
    assert rows[0]['call_id']==rows[1]['call_id']
    assert all(row['usage'] is None and 'response' not in row for row in rows)
    assert 'authored prompt' not in path.read_text() and 'controlled before dispatch' not in path.read_text()
    tracker=SuiteUsage('run')
    for row in rows:capture(SimpleNamespace(text=json.dumps(row),identity=SimpleNamespace(id='test-id')),tracker)
    assert len(tracker.inference_calls)==1
    retained=next(iter(tracker.inference_calls.values()))
    assert retained['outcome']=='failed' and retained['usage'] is None and 'response' not in retained


def test_real_ipc_conflicting_terminal_result_keeps_canonical_failed_receipt_and_usage(tmp_path,monkeypatch):
    import json
    from api_agent.testing import provider_setup
    req=request('json');chosen=evidence(req,'prompted_json','unsupported_mode')
    terminal=replace(result(req),usage=Usage(9,2))
    path=tmp_path/'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test_policy.py::test_result_conflict (call)')
    calls=[]
    async def scenario():
        async def complete(value):
            calls.append(value);record_response_dispatch(chosen);return terminal
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        server=SetupServer(SetupRegistry('operation','identity',lambda:owner),'synthetic-response-dispatch-capability-000000')
        client=SetupClient(await server.start(),worker_id='worker',timeout=5)
        publisher=provider_setup.RemoteIntegrationLLM.__new__(provider_setup.RemoteIntegrationLLM)
        publisher.config={};publisher.client=client;publisher._request=lambda call:call()
        try:
            with pytest.raises(ContractError,match='Conflicting') as caught:await asyncio.to_thread(publisher.complete,req)
            assert caught.value.response==client.response_evidence==chosen
            assert error_usage(caught.value)==Usage(9,2)
            assert terminal.response==evidence(req) and calls==[req]
        finally:
            await asyncio.to_thread(client.close);await server.aclose()
    asyncio.run(scenario())
    rows=[json.loads(line)['data'] for line in path.read_text().splitlines()]
    assert len(rows)==2 and [row['outcome'] for row in rows]==['unknown','failed']
    assert rows[0]['call_id']==rows[1]['call_id']
    assert rows[1]['response']['actual_mode']=='prompted_json'
    assert rows[1]['usage']['input_tokens']==9 and rows[1]['usage']['output_tokens']==2
    assert 'authored prompt' not in path.read_text() and '"answer":"yes"' not in path.read_text()


@pytest.mark.parametrize('terminal_kind,usage', [
    ('success', Usage()), ('success', Usage(7, 2)),
    ('provider_error', None), ('provider_error', Usage(7, 2)),
    ('runtime_error', None), ('runtime_error', Usage(7, 2)),
    ('operational_error', None), ('operational_error', Usage(7, 2)),
])
def test_real_ipc_rejects_unobserved_terminal_mode_and_keeps_usage(tmp_path, monkeypatch, terminal_kind, usage):
    import json
    from api_agent.testing import provider_setup
    req = request('json')
    reported = replace(result(req), usage=usage or Usage())
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_policy.py::test_unobserved (call)')

    async def scenario():
        calls = []

        async def complete(value):
            calls.append(value)
            if len(calls) == 1:
                record_response_dispatch(evidence(value))
                return result(value)
            if terminal_kind == 'success':
                return reported
            error = (ProviderAccessError('unknown', 'Safe terminal failure', usage=usage, response=evidence(value))
                     if terminal_kind == 'provider_error' else OperationalError('deadline') if terminal_kind == 'operational_error' else RuntimeError('Safe terminal failure'))
            error.response, error.usage = evidence(value), usage
            raise error

        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        server = SetupServer(SetupRegistry('operation', 'identity', lambda: owner),
                             'synthetic-response-dispatch-capability-000000')
        client = SetupClient(await server.start(), worker_id='worker', timeout=5)
        publisher = provider_setup.RemoteIntegrationLLM.__new__(provider_setup.RemoteIntegrationLLM)
        publisher.config = {}; publisher.client = client; publisher._request = lambda call: call()
        try:
            assert await asyncio.to_thread(client.complete, req) == result(req)
            with pytest.raises(ContractError, match='Unobserved') as caught:
                await asyncio.to_thread(publisher.complete, req)
            assert getattr(caught.value, 'response', None) is None
            assert error_usage(caught.value) == usage
            assert client.response_evidence is None
            assert calls == [req, req]
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()

    asyncio.run(scenario())
    rows = [json.loads(line)['data'] for line in path.read_text().splitlines()]
    assert len(rows) == 2 and [row['outcome'] for row in rows] == ['unknown', 'failed']
    assert rows[0]['call_id'] == rows[1]['call_id']
    assert rows[1].get('response') is None
    if usage is None:
        assert rows[1]['usage'] is None
    else:
        assert rows[1]['usage']['input_tokens'] == usage.input_tokens
        assert rows[1]['usage']['output_tokens'] == usage.output_tokens


def test_real_ipc_preflight_error_keeps_legacy_error_shape_without_mode():
    req = request('json')

    async def scenario():
        async def complete(value):
            raise ProviderAccessError('unsupported', 'Safe preflight failure')

        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        server = SetupServer(SetupRegistry('operation', 'identity', lambda: owner),
                             'synthetic-response-dispatch-capability-000000')
        client = SetupClient(await server.start(), worker_id='worker', timeout=5)
        try:
            with pytest.raises(ProviderAccessError, match='Safe preflight failure') as caught:
                await asyncio.to_thread(client.complete, req)
            assert caught.value.response is None and error_usage(caught.value) is None
            assert 'response' not in vars(caught.value)
            assert client.response_evidence is None
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()

    asyncio.run(scenario())


def test_real_operational_preflight_remote_publisher_keeps_type_and_peer_reusable(tmp_path, monkeypatch):
    import json
    from api_agent.testing import provider_setup
    req=request('json');calls=[];path=tmp_path/'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test_policy.py::test_operational_rejection (call)')
    async def scenario():
        async def complete(value):
            calls.append(value)
            if len(calls)==1:raise OperationalError('unsupported',execution='not_started',retry_after=2)
            record_response_dispatch(evidence(value));return result(value)
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        server=SetupServer(SetupRegistry('operation','identity',lambda:owner),'synthetic-response-dispatch-capability-000000')
        client=SetupClient(await server.start(),worker_id='worker',timeout=5)
        publisher=provider_setup.RemoteIntegrationLLM.__new__(provider_setup.RemoteIntegrationLLM)
        publisher.config={};publisher.client=client;publisher._request=lambda call:call()
        try:
            with pytest.raises(OperationalError) as caught:await asyncio.to_thread(publisher.complete,req)
            assert (caught.value.kind,caught.value.execution,caught.value.retry_after)==('unsupported','not_started',2)
            assert error_usage(caught.value) is None and getattr(caught.value,'response',None) is None
            assert client.response_evidence is None and client.evidence==[]
            assert await asyncio.to_thread(client.complete,req)==result(req)
            assert calls==[req,req] and client._complete_count==2
        finally:
            await asyncio.to_thread(client.close);await server.aclose()
    asyncio.run(scenario())
    rows=[json.loads(line)['data'] for line in path.read_text().splitlines()]
    assert len(rows)==2 and [r['outcome'] for r in rows]==['unknown','failed']
    assert rows[0]['call_id']==rows[1]['call_id'] and rows[1]['usage'] is None
    assert rows[1].get('response') is None


@pytest.mark.parametrize('mutation',[
    lambda f:f.update(extra=True), lambda f:f.update(version=True),lambda f:f.update(status='ready'),
    lambda f:f.update(index_source='override'),lambda f:f.update(message='private detail'),
    lambda f:f.update(details=[]),lambda f:f['details'].pop('usage'),lambda f:f['details'].update(extra=True),
    lambda f:f['details'].update(version=True),lambda f:f['details'].update(kind='other'),
    lambda f:f['details'].update(execution='other'),lambda f:f['details'].update(retry_after=True),
    lambda f:f['details'].update(retry_after=-1),lambda f:f['details'].update(kind='unknown',execution='not_started'),
    lambda f:f['details'].update(response={'extra':True}),lambda f:f['details'].update(usage={'input_tokens':-1}),
])
def test_operational_wire_rejects_unbounded_unknown_or_malformed_fields(mutation):
    frame=_error(OperationalError('unsupported'));mutation(frame)
    with pytest.raises(ContractError):_raise_error(frame)


@pytest.mark.parametrize('usage',[None,Usage(9,2)])
def test_operational_wire_preserves_only_explicit_usage_and_safe_dispatch_evidence(usage):
    req=request('json');error=OperationalError('deadline',execution='possibly_executed',retry_after=3)
    error.response=evidence(req);error.usage=usage
    frame=_error(error)
    assert frame['message']=='Shared operation failed' and frame['details']['version']==3
    with pytest.raises(OperationalError) as caught:_raise_error(frame)
    assert (caught.value.kind,caught.value.execution,caught.value.retry_after)==('deadline','possibly_executed',3)
    assert caught.value.response==error.response and error_usage(caught.value)==usage
    assert 'authored prompt' not in str(frame)


@pytest.mark.parametrize('retry',[10**1000,float('nan'),float('inf')])
def test_operational_retry_delay_rejects_nonfinite_and_overflowing_values(retry):
    frame=_error(OperationalError('unsupported'));frame['details']['retry_after']=retry
    with pytest.raises(ContractError):_raise_error(frame)


@pytest.mark.parametrize('field,value', [
    ('kind', 'synthetic-sensitive-marker'),
    ('kind', ['synthetic-sensitive-marker']),
    ('kind', None),
    ('execution', 'synthetic-sensitive-marker'),
    ('execution', {'private': 'synthetic-sensitive-marker'}),
    ('execution', True),
    ('retry_after', 'synthetic-sensitive-marker'),
    ('retry_after', ['synthetic-sensitive-marker']),
    ('retry_after', True),
    ('retry_after', -1),
    ('retry_after', float('nan')),
    ('retry_after', float('inf')),
    ('retry_after', 10**1000),
])
def test_operational_producer_normalizes_mutated_fields_before_encoding(field, value):
    import json
    from api_agent.providers.setup_transport import _encode
    error = OperationalError('authorization', execution='not_started', retry_after=2)
    setattr(error, field, value)

    encoded = _encode(_error(error))

    assert b'synthetic-sensitive-marker' not in encoded
    frame = json.loads(encoded)
    assert frame['message'] == 'Shared operation failed'
    assert frame['details'] == dict(version=3, kind='unknown', execution='possibly_executed',
                                    retry_after=None, response=None, usage=None)
    with pytest.raises(OperationalError) as caught:
        _raise_error(frame)
    assert caught.value.kind == 'unknown'
    assert caught.value.execution == 'possibly_executed'
    assert caught.value.retry_after is None


@pytest.mark.parametrize('field,value', [
    ('kind', 'synthetic-sensitive-marker'), ('kind', ['synthetic-sensitive-marker']),
    ('execution', 'synthetic-sensitive-marker'), ('execution', True),
    ('retry_after', 'synthetic-sensitive-marker'), ('retry_after', float('inf')),
    ('message', ['synthetic-sensitive-marker']), ('message', None),
    ('message', 'synthetic-sensitive-marker' * 30),
    ('message', 'synthetic-sensitive-marker\n'),
    ('status', 'synthetic-sensitive-marker'), ('status', True),
    ('code', ['synthetic-sensitive-marker']), ('code', 'synthetic-sensitive-marker' * 10),
    ('type', 'synthetic-sensitive-marker\x00'),
    ('request_id', {'private': 'synthetic-sensitive-marker'}),
])
def test_provider_producer_validates_mutated_fields_before_encoding(field, value):
    import json
    from api_agent.providers.setup_transport import _encode
    req = request('json')
    error = ProviderAccessError('authorization', 'Safe rejection', status=403,
                                code='safe-code', usage=Usage(9, 2), response=evidence(req))
    setattr(error, field, value)

    encoded = _encode(_error(error))

    assert b'synthetic-sensitive-marker' not in encoded
    frame = json.loads(encoded)
    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(frame)
    assert caught.value.usage == Usage(9, 2)
    assert caught.value.response == evidence(req)
    if field in {'kind', 'execution', 'retry_after'}:
        assert caught.value.kind == 'unknown'
        assert caught.value.execution == 'possibly_executed'
    else:
        assert caught.value.kind == 'authorization'
    assert caught.value.status == (None if field == 'status' else 403)
    assert caught.value.code == (None if field == 'code' else 'safe-code')
    assert caught.value.message == ('Shared provider operation failed' if field == 'message' else 'Safe rejection')


@pytest.mark.parametrize('field', ['kind', 'execution', 'retry_after', 'message', 'status', 'code', 'type', 'request_id', 'usage'])
def test_provider_producer_handles_missing_mutable_attributes(field):
    import json
    from api_agent.providers.setup_transport import _encode
    error = ProviderAccessError('authorization', 'Safe rejection', status=403, code='safe-code')
    delattr(error, field)

    frame = json.loads(_encode(_error(error)))

    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(frame)
    assert caught.value.kind == ('unknown' if field in {'kind', 'execution', 'retry_after'} else 'authorization')
    assert caught.value.message == ('Shared provider operation failed' if field == 'message' else 'Safe rejection')
    assert caught.value.status == (None if field == 'status' else 403)
    assert caught.value.code == (None if field == 'code' else 'safe-code')


def test_provider_producer_keeps_bounded_details_and_redacts_capability_before_encoding():
    import json
    from api_agent.providers.setup_transport import _encode
    capability = 'synthetic-capability'
    error = ProviderAccessError('authorization', 'Safe rejection ' + capability, status=403,
                                code=capability, type='safe-type', request_id='safe-request')
    error.execution = 'completed'

    encoded = _encode(_error(error, capability))

    assert capability.encode() not in encoded
    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(json.loads(encoded))
    assert caught.value.message == 'Safe rejection [redacted]'
    assert caught.value.code == '[redacted]'
    assert caught.value.type == 'safe-type' and caught.value.request_id == 'safe-request'
    assert caught.value.status == 403 and caught.value.execution == 'completed'


@pytest.mark.parametrize('execution', ['not_started', 'possibly_executed', 'completed'])
def test_provider_unknown_wire_preserves_valid_explicit_execution_and_evidence(execution):
    import json
    from api_agent.providers.setup_transport import _encode
    req = request('json')
    stage_message = 'Acquisition failed. Stage: credential_resolution; category: unknown.'
    error = ProviderAccessError('unknown', stage_message, status=403,
                                code='unknown', type='acquisition', request_id='safe-request',
                                usage=Usage(9, 2), response=evidence(req))
    error.execution = execution

    frame = json.loads(_encode(_error(error)))

    assert frame['message'] == stage_message
    assert frame['details']['kind'] == 'unknown'
    assert frame['details'].get('execution', 'possibly_executed') == execution
    with pytest.raises(ProviderAccessError) as caught:
        _raise_error(frame)
    assert caught.value.kind == 'unknown'
    assert caught.value.execution == execution
    assert caught.value.message == stage_message
    assert (caught.value.status, caught.value.code, caught.value.type, caught.value.request_id) == (
        403, 'unknown', 'acquisition', 'safe-request')
    assert caught.value.usage == Usage(9, 2)
    assert caught.value.response == evidence(req)
    assert error.execution == execution and error.usage == Usage(9, 2)


def test_base_unknown_wire_retains_conservative_execution_normalization():
    import json
    from api_agent.providers.setup_transport import _encode
    error = OperationalError('unknown')
    error.execution = 'not_started'

    frame = json.loads(_encode(_error(error)))

    assert frame['details']['execution'] == 'possibly_executed'
    assert error.execution == 'not_started'


def test_provider_classification_is_read_once_before_validation_and_encoding():
    import json
    from api_agent.providers.setup_transport import _encode
    class ChangingProviderError(ProviderAccessError):
        def __getattribute__(self, name):
            if name in {'kind', 'execution', 'retry_after'}:
                reads = object.__getattribute__(self, '__dict__').get('reads')
                if reads is not None:
                    reads[name] += 1
                    if reads[name] > 1:
                        return 'synthetic-sensitive-marker'
            return super().__getattribute__(name)
    error = ChangingProviderError('unknown', 'Safe pre-inference rejection')
    error.execution = 'not_started'
    error.reads = dict.fromkeys(('kind', 'execution', 'retry_after'), 0)

    encoded = _encode(_error(error))

    assert b'synthetic-sensitive-marker' not in encoded
    frame = json.loads(encoded)
    assert frame['details']['kind'] == 'unknown'
    assert frame['details']['execution'] == 'not_started'
    assert error.reads == {'kind': 1, 'execution': 1, 'retry_after': 1}
