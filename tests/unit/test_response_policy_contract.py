"""Response policy admission is explicit, content-free and wire-versioned."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.inference import request_to_wire, request_from_wire, result_to_wire, result_from_wire, async_request_to_wire
from api_agent.providers.llm import (CompletionRequest, LLMRequest, LLMResult, Message,
    TextContent, StructuredOutput, ResponsePolicy, ResponseEvidence, Tool, ToolCall, Usage)
from api_agent.providers import rich_inference as rich
from api_agent.providers.setup_transport import _error, _raise_error

SCHEMA = '{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}'


def request(mode='structured', fallback='allow', strict=True):
    definition = None if mode == 'text' else StructuredOutput('answer', SCHEMA, strict)
    return LLMRequest('synthetic-model', CompletionRequest(
        (Message('user', (TextContent('authored prompt'),)),), response=ResponsePolicy(mode, fallback, definition)), 'call-1')


def evidence(req, actual=None, reason=None):
    policy = req.payload.response
    definition = policy.schema
    return ResponseEvidence(policy.mode, policy.fallback, actual or policy.mode,
        None if definition is None else sha256(definition.schema.encode()).hexdigest(),
        None if definition is None else definition.strict, reason)


def result(req, text='{"answer":"yes"}', **kwargs):
    return LLMResult(Message('assistant', (TextContent(text),)), Usage(5, 3), response=evidence(req), **kwargs)


@pytest.mark.parametrize('mode', ['structured', 'json', 'prompted_json', 'text'])
@pytest.mark.parametrize('strict', [False, True])
def test_policy_wire_roundtrip_preserves_explicit_original_schema_and_intent(mode, strict):
    req = request(mode, strict=strict)
    row = request_to_wire(req)
    assert row['version'] == 3 and row['structured'] is None
    assert set(row['response']) == {'mode', 'fallback', 'schema'}
    assert row['response']['schema'] == (None if mode == 'text' else dict(name='answer', schema=SCHEMA, strict=strict))
    assert request_from_wire(row) == req
    output = result(req)
    assert result_from_wire(result_to_wire(output)) == output
    assert rich.validate_result(req, output) == output
    assert asyncio.run(rich.async_validate_result(req, output)) == output


def test_original_open_schema_reaches_adapter_without_native_subset_rewrite():
    req = request()
    definition = StructuredOutput('original', '{"type":"object","properties":{"arguments":{"type":"object"}},"required":["arguments"]}', True)
    req = replace(req, payload=replace(req.payload, response=replace(req.payload.response, schema=definition)))
    assert request_from_wire(request_to_wire(req)).payload.response.schema == definition
    output = replace(result(req, '{"arguments":{"additional":"value"}}'), response=evidence(req, 'prompted_json', 'unsupported_schema'))
    assert rich.validate_result(req, output) == output


@pytest.mark.parametrize('change', [
    lambda r:r.update(version=2), lambda r:r.update(version=4),
    lambda r:r.pop('response'), lambda r:r.update(structured=dict(name='answer',schema=SCHEMA)),
    lambda r:r['response'].update(extra='private'), lambda r:r['response'].pop('schema'),
    lambda r:r['response'].update(mode='other'), lambda r:r['response'].update(fallback='retry'),
    lambda r:r['response'].update(schema=None), lambda r:r['response']['schema'].pop('strict'),
    lambda r:r['response']['schema'].update(strict=1), lambda r:r['response']['schema'].update(schema='{"type":"invalid"}'),
])
def test_request_rejects_policy_downgrade_unknown_keys_and_unbound_schema(change):
    row = request_to_wire(request())
    change(row)
    with pytest.raises(ContractError):
        request_from_wire(row)


@pytest.mark.parametrize('mode,actual,reason', [('structured','json','unsupported_mode'), ('structured','prompted_json','unsupported_schema'), ('json','prompted_json','unknown_capability')])
def test_only_authorized_downward_modes_admit_exact_original_evidence(mode, actual, reason):
    req = request(mode)
    output = replace(result(req), response=evidence(req, actual, reason))
    assert rich.validate_result(req, output) == output
    assert rich.response_evidence_from_wire(rich.response_evidence_to_wire(output.response)) == output.response


@pytest.mark.parametrize('changes', [dict(actual_mode='text',fallback_reason='unsupported_mode'), dict(actual_mode='json'), dict(fallback='reject',actual_mode='json',fallback_reason='unsupported_mode'), dict(fallback_reason='arbitrary upstream text'), dict(strict=None), dict(schema_sha256='bad'), dict(requested_mode='other'), dict(actual_mode='other'), dict(fallback='other')])
def test_evidence_constructor_rejects_uncontrolled_or_inconsistent_values(changes):
    fields = rich.response_evidence_to_wire(evidence(request())) | changes
    with pytest.raises(ContractError):
        rich.response_evidence_from_wire(fields)


@pytest.mark.parametrize('change', [lambda e:replace(e, schema_sha256='0'*64), lambda e:replace(e,strict=False), lambda e:replace(e,fallback='reject'), lambda e:None])
def test_result_evidence_must_match_originating_request(change):
    req = request()
    output = replace(result(req), response=change(evidence(req)))
    with pytest.raises(ProviderAccessError, match='evidence'):
        rich.validate_result(req, output)


def test_legacy_request_rejects_unsolicited_evidence_and_preserves_wire_shapes():
    req = request()
    legacy = replace(req, payload=replace(req.payload,response=None))
    assert 'version' not in request_to_wire(legacy)
    rich_legacy = replace(legacy,payload=replace(legacy.payload,structured=StructuredOutput('answer',SCHEMA)))
    assert request_to_wire(rich_legacy)['version'] == 2
    assert 'response' not in request_to_wire(rich_legacy)
    assert 'version' not in result_to_wire(replace(result(req),response=None))
    with pytest.raises(ProviderAccessError):
        rich.validate_result(legacy,result(req))
    with pytest.raises(ValueError):
        replace(req.payload,structured=StructuredOutput('answer',SCHEMA))
    with pytest.raises(ValueError):
        ResponsePolicy('text','reject',StructuredOutput('answer',SCHEMA))
    with pytest.raises(ValueError):
        replace(req.payload,response=object())
    with pytest.raises(ValueError):
        replace(result(req),response=object())


@pytest.mark.parametrize('text', ['{"answer":4}', '{}', 'not JSON', '{"answer":"x","extra":1}'])
def test_every_completed_json_mode_validates_original_schema(text):
    req = request('json')
    output = replace(result(req,text), response=evidence(req,'prompted_json','unsupported_mode'))
    with pytest.raises(ProviderAccessError, match='original schema') as caught:
        rich.validate_result(req,output)
    assert caught.value.response == output.response and caught.value.usage == output.usage
    with pytest.raises(ProviderAccessError, match='original schema'):
        asyncio.run(rich.async_validate_result(req,output))


@pytest.mark.parametrize('outcome', ['length','refused','tool_calls'])
def test_intermediate_and_noncompletion_outcomes_keep_usage_and_evidence(outcome):
    req = request()
    if outcome == 'tool_calls':
        tool=Tool('lookup','lookup',SCHEMA)
        req=replace(req,payload=replace(req.payload,tools=(tool,)))
        payload=Message('assistant',calls=(ToolCall('tool-1','lookup','{"answer":"x"}'),))
    else:
        payload=Message('assistant',(TextContent('not generated JSON'),))
    output=LLMResult(payload,Usage(5,3),outcome,response=evidence(req))
    assert result_from_wire(result_to_wire(output)) == output
    assert rich.validate_result(req,output) == output
    assert asyncio.run(rich.async_validate_result(req,output)) == output


def test_text_mode_does_not_parse_json_and_completed_cannot_hide_tool_calls():
    req=request('text')
    assert rich.validate_result(req,result(req,'ordinary prose')).payload.content[0].text=='ordinary prose'
    output=replace(result(req),payload=Message('assistant',calls=(ToolCall('tool-1','lookup','{}'),)))
    with pytest.raises(ProviderAccessError,match='outcome'):
        rich.validate_result(req,output)


@pytest.mark.parametrize('change', [lambda r:r.update(version=2),lambda r:r.pop('response'),lambda r:r['response'].update(extra=1),lambda r:r.update(outcome='other'),lambda r:r['message'].update(role='user'),lambda r:r.update(version=True)])
def test_result_v3_rejects_missing_unknown_or_conflicting_fields(change):
    row=result_to_wire(result(request()))
    change(row)
    with pytest.raises(ContractError):result_from_wire(row)


def test_postdispatch_error_evidence_is_nested_versioned_and_legacy_shape_unchanged():
    error=ProviderAccessError('unknown','Safe failure',usage=Usage(5,3),response=evidence(request()))
    frame=_error(error)
    assert frame['version']==1 and frame['details']['response']['version']==3
    with pytest.raises(ProviderAccessError) as caught:_raise_error(frame)
    assert caught.value.response==error.response and caught.value.usage==error.usage
    legacy=_error(ProviderAccessError('unknown','Safe failure'))
    assert set(legacy['details'])=={'kind','status','code','type','request_id'}
    with pytest.raises(ContractError):ProviderAccessError('unknown','Safe failure',response={})


@pytest.mark.parametrize('nested',[{}, {'version':2,'response':{}},{'version':True,'response':{}},{'version':3,'response':{},'extra':1},{'version':3,'response':{}}])
def test_error_nested_evidence_rejects_old_or_unbounded_shapes(nested):
    frame=_error(ProviderAccessError('unknown','Safe failure'))
    frame['details']['response']=nested
    with pytest.raises(ContractError):_raise_error(frame)


@pytest.mark.parametrize('failure', [False, True])
def test_v3_real_setup_ipc_preserves_request_and_terminal_evidence(failure):
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    req=request('json')
    seen=[]
    async def scenario():
        async def complete(value):
            seen.append(value)
            from api_agent.workflows.inference_capture import record_response_dispatch
            record_response_dispatch(evidence(value))
            if failure:raise ProviderAccessError('unknown','Safe dispatch failure',usage=Usage(5,3),response=evidence(value))
            return result(value)
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        registry=SetupRegistry('operation','synthetic-selection',lambda:owner)
        server=SetupServer(registry,'synthetic-response-policy-capability-000000')
        endpoint=await server.start()
        client=SetupClient(endpoint,worker_id='worker',timeout=5)
        try:
            if failure:
                with pytest.raises(ProviderAccessError) as caught:await asyncio.to_thread(client.complete,req)
                assert caught.value.response==evidence(req) and caught.value.usage==Usage(5,3)
            else:
                actual=await asyncio.to_thread(client.complete,req)
                assert actual==result(req)
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
        assert seen==[req]
    asyncio.run(scenario())


def test_journal_and_terminal_accounting_keep_one_content_free_evidence_receipt(tmp_path,monkeypatch):
    from api_agent.workflows.journal import WorkflowJournal,validate_journal
    from api_agent.workflows.attribution import bind_workflow_visit,bind_terminal_call_receipts
    from api_agent.testing.inference_usage import publish,validate_event
    from test_workflow_journal import definition
    req=request('json');output=result(req)
    journal=WorkflowJournal(definition(),model_identity=lambda value:value=='synthetic-model')
    journal.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make',started_ms=1))
    events=tmp_path/'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test_policy.py::test_request (call)')
    with bind_workflow_visit(journal,'visit'),bind_terminal_call_receipts() as receipts:
        identifier=publish(req,output)
    assert len(receipts)==1 and receipts[0]['call_id']==identifier
    wire=rich.response_evidence_to_wire(output.response)
    assert receipts[0]['response']==wire
    raw=json.loads(events.read_text())['data']
    _,checked=validate_event(SimpleNamespace(text=json.dumps(raw)))
    assert checked['response']==wire
    journal.observe(dict(event='scope_end',scope_id='visit',kind='node',owner='Make',started_ms=1,stopped_ms=2,outcome='passed'))
    artifact=journal.finalize()
    assert validate_journal(artifact)==artifact
    calls=[r for r in artifact['events'] if r['event']=='call']
    assert len(calls)==1 and calls[0]['response']==wire and calls[0]['call_id']==identifier
    assert 'authored prompt' not in json.dumps(artifact) and SCHEMA not in json.dumps(artifact)
    bad=deepcopy(checked);bad['response']['prompt']='sensitive'
    from api_agent.telemetry.accounting import validate_evidence
    with pytest.raises(ContractError):validate_evidence(bad)


def test_call_capture_context_keeps_effective_input_output_correlation_and_resets():
    from api_agent.workflows.inference_capture import bind_inference_call,capture_inference,collect_inference
    from api_agent.workflows.attribution import bind_workflow_visit
    from api_agent.telemetry.privacy import EmissionBoundary,ContentPolicy
    captured=[]
    journal=SimpleNamespace(boundary=EmissionBoundary(ContentPolicy(True)),capture_json=lambda **kw:captured.append(kw))
    with bind_workflow_visit(journal,'visit'):
        with bind_inference_call('canonical'):
            capture_inference('input',{'effective':'payload'},'adapter-id')
            capture_inference('output',{'result':'value'},'other-id')
        capture_inference('input',{'effective':'next'},'after-id')
        with collect_inference(journal.boundary,call_id='outer-authoritative') as collector:
            with bind_inference_call('inner'):
                capture_inference('input',{'effective':'collected'},'adapter-id')
    assert [r['cause_id'] for r in captured]==['canonical','canonical','after-id']
    assert collector['call_id']=='outer-authoritative' and len(collector['rows'])==1


def test_setup_client_rejects_wrong_call_evidence_on_success_and_error(monkeypatch):
    from api_agent.providers.setup_transport import SetupClient
    client=SetupClient(dict(version=1,host='127.0.0.1',port=12345,capability='synthetic-response-policy-capability-000000'),worker_id='worker',timeout=5)
    req=request('json')
    wrong=replace(evidence(req),schema_sha256='0'*64)
    def complete(*args):
        client.response_evidence=wrong
        return replace(result(req),response=wrong)
    monkeypatch.setattr(client,'_run_request',complete)
    with pytest.raises(ProviderAccessError,match='evidence'):client.complete(req)
    def fail(*args):
        client.response_evidence=wrong
        raise ProviderAccessError('unknown','Safe failure',response=wrong)
    monkeypatch.setattr(client,'_run_request',fail)
    with pytest.raises(ProviderAccessError,match='evidence'):client.complete(req)


@pytest.mark.parametrize('definition,text', [('{"type":"array","items":{"type":"integer"}}','[1,2]'),('{"type":"string"}','"answer"'),('{"type":"number"}','1'),('{"type":"null"}','null')])
def test_policy_original_json_schema_is_not_rewritten_to_object_only(definition,text):
    req=request('prompted_json')
    req=replace(req,payload=replace(req.payload,response=replace(req.payload.response,schema=StructuredOutput('original',definition))))
    assert request_from_wire(request_to_wire(req))==req
    output=result(req,text)
    assert rich.validate_result(req,output)==output
    assert asyncio.run(rich.async_validate_result(req,output))==output


@pytest.mark.parametrize('text',['[NaN]','[1e999]','{"answer":"x","answer":"y"}','{'])
def test_policy_original_json_rejects_nonfinite_duplicates_and_malformed_content(text):
    req=request('prompted_json')
    with pytest.raises(ProviderAccessError):rich.validate_result(req,result(req,text))


@pytest.mark.parametrize('definition,text', [('{"type":"array","items":{"type":"integer"}}','["wrong"]'),('{"type":"string","minLength":4}','"x"'),('{"type":"number","minimum":4}','1')])
def test_original_nonobject_constraints_are_independently_enforced(definition,text):
    req=request('structured')
    req=replace(req,payload=replace(req.payload,response=replace(req.payload.response,schema=StructuredOutput('original',definition))))
    output=replace(result(req,text),response=evidence(req,'prompted_json','unsupported_schema'))
    with pytest.raises(ProviderAccessError,match='original schema'):rich.validate_result(req,output)
    with pytest.raises(ProviderAccessError,match='original schema'):asyncio.run(rich.async_validate_result(req,output))


def test_setup_client_independently_admits_original_schema_not_only_matching_evidence(monkeypatch):
    from api_agent.providers.setup_transport import SetupClient
    client=SetupClient(dict(version=1,host='127.0.0.1',port=12345,capability='synthetic-response-policy-capability-000000'),worker_id='worker',timeout=5)
    req=request('json')
    output=result(req,'{"answer":4}')
    def complete(*args):
        client.response_evidence=output.response
        return output
    monkeypatch.setattr(client,'_run_request',complete)
    with pytest.raises(ProviderAccessError,match='original schema') as caught:client.complete(req)
    assert caught.value.response==output.response and caught.value.usage==output.usage


def test_response_evidence_serializer_rejects_untyped_values():
    with pytest.raises(ContractError,match='response evidence'):
        rich.response_evidence_to_wire({})


def test_absent_error_response_preserves_legacy_instance_shape_and_safe_access():
    expected={'kind','execution','retry_after','status','code','type','request_id','message','usage'}
    for error in (ProviderAccessError('unknown','Safe failure'),ProviderAccessError('unknown','Safe failure',response=None)):
        assert error.response is None
        assert set(vars(error))==expected
        assert set(_error(error)['details'])=={'kind','status','code','type','request_id'}


def test_nonempty_error_response_is_typed_instance_data_with_bounded_content_free_wire():
    req=request('json');observed=evidence(req,'prompted_json','unsupported_mode')
    error=ProviderAccessError('unknown','Safe failure',response=observed)
    assert vars(error)['response'] is observed
    nested=_error(error)['details']['response']
    assert nested==dict(version=3,response=rich.response_evidence_to_wire(observed))
    assert set(nested['response'])=={'requested_mode','fallback','actual_mode','schema_sha256','strict','fallback_reason'}
    assert len(json.dumps(nested).encode())<512
    assert SCHEMA not in repr(vars(error)) and 'authored prompt' not in repr(vars(error))
    with pytest.raises(ContractError):ProviderAccessError('unknown','Safe failure',response={'prompt':'unadmitted'})


OTHER_DIALECT='http://json-schema.org/draft-07/schema#'
SUPPORTED_DIALECT='https://json-schema.org/draft/2020-12/schema'


def dialect_request(definition):
    req=request('json')
    return replace(req,payload=replace(req.payload,response=replace(req.payload.response,schema=StructuredOutput('original',json.dumps(definition),False))))


def test_direct_policy_cannot_accept_output_from_an_unsupported_authored_dialect():
    from jsonschema import Draft7Validator
    definition={'$schema':OTHER_DIALECT,'type':'object','properties':{'a':{'type':'string'},'b':{'type':'string'}},'dependencies':{'a':['b']}}
    assert not Draft7Validator(definition).is_valid({'a':'x'})
    req=dialect_request(definition)
    with pytest.raises(ProviderAccessError,match='original schema'):rich.validate_result(req,result(req,'{"a":"x"}'))
    with pytest.raises(ProviderAccessError,match='original schema'):asyncio.run(rich.async_validate_result(req,result(req,'{"a":"x"}')))


@pytest.mark.parametrize('definition',[
    {'$schema':OTHER_DIALECT,'type':'object'},
    {'type':'object','properties':{'item':{'$schema':OTHER_DIALECT,'type':'string'}}},
    {'type':'array','prefixItems':[{'$schema':OTHER_DIALECT,'type':'string'}]},
    {'$defs':{'item':{'$schema':OTHER_DIALECT,'type':'string'}},'$ref':'#/$defs/item'},
])
def test_new_policy_producer_and_consumer_admission_reject_unsupported_schema_dialects(definition):
    req=dialect_request(definition)
    with pytest.raises(ContractError):request_to_wire(req)
    with pytest.raises(ContractError):asyncio.run(async_request_to_wire(req))
    wire=request_to_wire(request('json'))
    wire['response']['schema']=dict(name='original',schema=json.dumps(definition),strict=False)
    with pytest.raises(ContractError):request_from_wire(wire)


def test_unsupported_direct_policy_dialect_rejects_before_authentication_or_inference():
    from unittest.mock import Mock
    from api_agent.providers.shared_setup import SharedSetupOwner
    req=dialect_request({'$schema':OTHER_DIALECT,'type':'object'})
    async def authenticated(callback):return await callback(auth='synthetic-auth')
    factory=Mock(return_value=SimpleNamespace(call=authenticated,aclose=AsyncMock()))
    complete=AsyncMock(return_value=result(req,'{}'))
    async def scenario():
        owner=SharedSetupOwner('operation','identity',factory,complete=complete)
        try:
            with pytest.raises(ContractError):await owner.complete(req)
            factory.assert_not_called();complete.assert_not_called()
        finally:await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('definition,text',[
    ({'$schema':SUPPORTED_DIALECT,'type':'array','items':{'type':'integer'}},'[1,2]'),
    ({'$schema':SUPPORTED_DIALECT,'type':'string'},'"answer"'),
    ({'$schema':SUPPORTED_DIALECT,'type':'object','dependentRequired':{'a':['b']}},'{"a":"x","b":"y"}'),
    ({'$schema':SUPPORTED_DIALECT,'type':'object','const':{'$schema':'instance data, not a dialect'}},'{"$schema":"instance data, not a dialect"}'),
])
def test_explicit_supported_dialect_preserves_root_types_and_instance_valued_keywords(definition,text):
    req=dialect_request(definition)
    assert request_from_wire(request_to_wire(req))==req
    assert rich.validate_result(req,result(req,text)).response==evidence(req)


def test_legacy_structured_output_dialect_admission_is_unchanged():
    definition=json.dumps({'$schema':OTHER_DIALECT,'type':'object','dependencies':{'a':['b']}})
    req=request();req=replace(req,payload=replace(req.payload,response=None,structured=StructuredOutput('legacy',definition,False)))
    wire=request_to_wire(req)
    assert wire['version']==2 and request_from_wire(wire)==req


@pytest.mark.parametrize('keyword', ['enum','const','examples','default','custom_metadata','dependencies'])
def test_dialect_walk_preserves_instance_data_and_ignored_annotations(keyword):
    data={'$schema':OTHER_DIALECT}
    annotation=[data] if keyword in ('enum','examples') else data
    if keyword=='dependencies':annotation={'unused':data}
    definition={'$schema':SUPPORTED_DIALECT,'type':'object',keyword:annotation}
    req=dialect_request(definition)
    assert request_from_wire(request_to_wire(req))==req
    text=json.dumps(data) if keyword in ('enum','const') else '{}'
    assert rich.validate_result(req,result(req,text)).response==evidence(req)


@pytest.mark.parametrize('dialect', ['http://json-schema.org/draft/2020-12/schema','https://json-schema.org/draft/2020-12/schema#'])
def test_policy_dialect_spelling_is_exact(dialect):
    with pytest.raises(ContractError):request_to_wire(dialect_request({'$schema':dialect,'type':'object'}))


@pytest.mark.parametrize('location', ['custom_metadata','const','enum','examples','default','dependencies'])
def test_local_reference_activates_dialect_guard_in_otherwise_ignored_data(location):
    target={'$schema':OTHER_DIALECT,'type':'object'}
    container=[target] if location in ('enum','examples') else {'target':target}
    pointer='#/'+location+('/0' if isinstance(container,list) else '/target')
    definition={'$schema':SUPPORTED_DIALECT,location:container,'$ref':pointer}
    req=dialect_request(definition)
    with pytest.raises(ContractError):request_to_wire(req)
    wire=request_to_wire(request('json'))
    wire['response']['schema']=dict(name='original',schema=json.dumps(definition),strict=False)
    with pytest.raises(ContractError):request_from_wire(wire)
