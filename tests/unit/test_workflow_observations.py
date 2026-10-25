"""Destination policy and real wire proofs use synthetic data and loopback only."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.workflows.observations import WorkflowObservationFactory


def config(log=False, trace=False, mode='OTEL', **overrides):
    values = {'LOG_CONTENT': log, 'TRACELOOP_TRACE_CONTENT': trace,
              'TRACE_MODE': mode, **overrides}
    fields = {key: ResolvedValue(value, 'cli', True) for key, value in values.items()}
    return ConfigResult(values, fields, (), (), ())


def definition():
    node = SimpleNamespace(id='Make', kind='node', parent=None)
    layout = SimpleNamespace(steps=[node], tables={'Edges': [],
        'Functions': [{'ID': 'Make', 'Kind': 'deterministic'}]})
    resource = SimpleNamespace(kind='declaration', logical_path='flows/main.md.html',
        sha256='a' * 64, content_bytes=b'# Workflow v2')
    return SimpleNamespace(graph_id='Example', definition_id='b' * 64,
        entry_root='base', layout=layout, resources=[resource], replacements=[])


@contextmanager
def collector():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('log,trace', [(False,False),(False,True),(True,False),(True,True)])
@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
def test_independent_projections_and_real_serialized_ancestry(log, trace, mode):
    with collector() as (endpoint, received):
        resolved = config(log, trace, mode, TRACE_TARGET=endpoint,
            LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret')
        # Deliberately permissive injection cannot elevate resolved trace policy.
        credentials = EmissionBoundary(ContentPolicy(True))
        runtime = TraceRuntime(resolved, boundary=credentials, model_identities=('example-model', 'actual-model'))
        asyncio.run(runtime.astart())
        rows = []
        adapter = WorkflowObservationFactory(resolved, boundary=credentials,
            sink=rows.append, trace=runtime).create(definition())
        credentials.register_secret('distinct-credential')
        with runtime.span({'hidden_reasoning':'hidden-manual-sentinel',
                'continuation_state':'hidden-manual-sentinel',
                'nested':json.dumps({'hiddenReasoning':'hidden-manual-sentinel','text':'manual-visible'}),
                'prompt':'manual-visible'}):
            pass
        graph, visit = str(uuid4()), str(uuid4())
        adapter.observe(dict(event='scope_start',scope_id=graph,owner='GRAPH',kind='graph'))
        adapter.observe(dict(event='scope_start',scope_id=visit,parent_id=graph,owner='Make',kind='node'))
        text = 'user-visible-sentinel distinct-credential ' + 'x' * 600
        capture = adapter.capture(visit_id=visit,direction='input', values={'text':text},
            schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
        call = str(uuid4())
        for _ in range(2):
            adapter.record_call(visit_id=visit,call_id=call,usage={'input_tokens':7,'output_tokens':2},selection='example-model',elapsed=.01,
                actual_model='actual-model', reasoning_effort='low')
        adapter.observe(dict(event='scope_end',scope_id=visit,owner='Make',kind='node',outcome='passed'))
        adapter.observe(dict(event='scope_end',scope_id=graph,owner='GRAPH',kind='graph',outcome='passed'))
        adapter.close()
        journal = adapter.finalize()
        assert len([row for row in rows if row['event']=='call']) == 1
        live = next(row for row in rows if row['event']=='capture')
        retained = next(row for row in journal['events'] if row['event']=='capture')
        assert live['event_id'] == retained['event_id'] == capture['event_id']
        assert ('values' in live) is log
        if log:
            assert len(live['values']['text']) > 512
            assert len(retained['values']['text']) == 512
        with pytest.raises(TypeError):
            live['reason'] = 'changed'
        assert runtime.shutdown()
        assert received
        wire = ExportTraceServiceRequest.FromString(received[0][1])
        spans = [span for resource in wire.resource_spans for scope in resource.scope_spans for span in scope.spans]
        assert len(spans) == 4
        root = next(span for span in spans if span.name == 'graph GRAPH')
        node = next(span for span in spans if span.name == 'node Make')
        leaf = next(span for span in spans if span.name == 'chat example-model')
        assert node.parent_span_id == root.span_id
        assert leaf.parent_span_id == node.span_id
        assert leaf.kind == 3
        usage_attrs = {attribute.key:attribute.value.int_value for attribute in leaf.attributes}
        strings = {attribute.key:attribute.value.string_value for attribute in leaf.attributes}
        assert strings['gen_ai.request.model'] == 'example-model'
        assert strings['gen_ai.response.model'] == 'actual-model'
        assert strings['api_agent.request.reasoning_effort'] == 'low'
        call_row = next(row for row in journal['events'] if row['event'] == 'call')
        assert call_row['actual_model'] == 'actual-model'
        assert call_row['reasoning_effort'] == 'low'
        assert usage_attrs['gen_ai.usage.input_tokens']==7
        assert usage_attrs['gen_ai.usage.output_tokens']==2
        assert 9_000_000 <= leaf.end_time_unix_nano - leaf.start_time_unix_nano <= 11_000_000
        encoded = str(wire)
        assert ('user-visible-sentinel' in encoded) is trace
        assert 'distinct-credential' not in encoded
        assert 'hidden-manual-sentinel' not in encoded
        assert ('manual-visible' in encoded) is trace
        assert 'distinct-credential' not in repr(rows)
        assert 'distinct-credential' not in json.dumps(journal)
        assert received[0][0] == ('/api/public/otel/v1/traces' if mode=='LangFuse' else '/')


@pytest.mark.parametrize('source,explicit,value', [('default',False,True),('environment',True,False),('cli',True,'True'),('contributed',True,True)])
def test_trace_opt_in_requires_valid_explicit_external_boolean(source, explicit, value):
    resolved = config()
    resolved.fields['TRACELOOP_TRACE_CONTENT'] = ResolvedValue(value,source,explicit)
    resolved.values['TRACELOOP_TRACE_CONTENT'] = value
    assert not TraceRuntime(resolved).boundary.policy.allow_content


def test_disabled_trace_adapter_does_not_import_optional_sdk():
    runtime = TraceRuntime(config(mode=''))
    adapter = WorkflowObservationFactory(config(mode=''), boundary=EmissionBoundary(), trace=runtime).create(definition())
    adapter.observe(dict(event='scope_start',scope_id='scope',owner='GRAPH',kind='graph'))
    adapter.close()
    assert runtime.shutdown()


@pytest.mark.parametrize('outcome', ['failed','cancelled'])
def test_terminal_outcome_and_absent_usage_are_structural(outcome):
    from unittest.mock import Mock
    trace = Mock(boundary=EmissionBoundary())
    adapter = WorkflowObservationFactory(config(),boundary=EmissionBoundary(),trace=trace).create(definition())
    adapter.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
    adapter.record_call(visit_id='visit',call_id=str(uuid4()),outcome=outcome)
    adapter.observe(dict(event='scope_end',scope_id='visit',outcome=outcome))
    adapter.observe(dict(event='scope_end',scope_id='missing',outcome=outcome))
    assert adapter.invocation_id == adapter.journal.invocation_id
    assert trace.begin.call_count == 2
    assert 'gen_ai.usage.input_tokens' not in trace.begin.call_args.args[1]
    adapter.close()


def test_scope_retention_is_bounded_and_unmatched_capture_cannot_create_span():
    from unittest.mock import Mock
    trace = Mock(boundary=EmissionBoundary(ContentPolicy(True)))
    adapter = WorkflowObservationFactory(config(trace=True),boundary=EmissionBoundary(),trace=trace).create(definition())
    for index in range(257):
        adapter.observe(dict(event='scope_start',scope_id=f'scope-{index}',owner='GRAPH',kind='graph'))
    assert trace.begin.call_count == 256
    adapter.capture(visit_id='outside',direction='input',values={'text':'visible'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    trace.begin.return_value.event.assert_not_called()
    adapter.close()
    assert trace.begin.return_value.end.call_count == 256


def test_journal_gap_prevents_fanout_and_unclassified_values_stay_withheld(monkeypatch):
    from api_agent.workflows import journal
    rows = []
    adapter = WorkflowObservationFactory(config(log=True),boundary=EmissionBoundary(),sink=rows.append).create(definition())
    adapter.capture(visit_id='visit',direction='input',values={'text':'never captured'},schema={},plain_fields=None)
    assert rows[0]['reason'] == 'unclassified' and 'values' not in rows[0]
    monkeypatch.setattr(journal,'_MAX_EVENTS',1)
    assert adapter.capture(visit_id='visit',direction='input',values={},schema={}) is None
    assert len(rows) == 1


@pytest.mark.parametrize('secret,field,value,kind', [
    ('1234567','value',1234567,'integer'),
    ('syntheticfield','syntheticfield','visible','string'),
])
def test_live_complete_projection_scrubs_registered_typed_values_and_field_keys(secret,field,value,kind):
    rows = []
    boundary = EmissionBoundary(secrets=[secret])
    adapter = WorkflowObservationFactory(config(log=True),boundary=boundary,sink=rows.append).create(definition())
    adapter.capture(visit_id='visit',direction='input',values={field:value},
        schema={'type':'object','properties':{field:{'type':kind}}},plain_fields=[field])
    assert secret not in repr(rows)
    assert secret not in repr(adapter.finalize())


@pytest.mark.parametrize('failure', [RuntimeError('observer failure'),asyncio.CancelledError()])
def test_observational_failures_do_not_replace_execution_or_contract_validation(failure):
    from unittest.mock import Mock
    def broken(row):
        raise failure
    trace = Mock(boundary=EmissionBoundary(ContentPolicy(True)))
    trace.begin.side_effect = failure
    adapter = WorkflowObservationFactory(config(log=True),boundary=EmissionBoundary(),sink=broken,trace=trace).create(definition())
    adapter.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
    adapter.capture(visit_id='visit',direction='input',values={'text':'visible'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    with pytest.raises(ValueError,match='Invalid workflow observation'):
        adapter.observe('invalid explicit contract')
    adapter.close()
    assert len(adapter.finalize()['events']) == 2


@pytest.mark.parametrize('log,trace', [(False,False),(False,True),(True,False),(True,True)])
@pytest.mark.parametrize('kind,value,reason', [('string','x'*65537,'payload_limit'),('array',[], 'unsupported')])
def test_unavailable_capture_does_not_depend_on_destination_opt_in(log,trace,kind,value,reason):
    from unittest.mock import Mock
    rows = []
    runtime = Mock(boundary=EmissionBoundary(ContentPolicy(trace)))
    adapter = WorkflowObservationFactory(config(log,trace),boundary=EmissionBoundary(),sink=rows.append,trace=runtime).create(definition())
    adapter.capture(visit_id='visit',direction='input',values={'text':value},
        schema={'type':'object','properties':{'text':{'type':kind}}},plain_fields=['text'])
    assert rows[0]['reason'] == reason and 'values' not in rows[0]
    assert adapter.finalize()['events'][0]['reason'] == reason
    runtime.begin.assert_not_called()


def test_trace_event_and_close_failures_are_observational():
    from unittest.mock import Mock
    runtime = Mock(boundary=EmissionBoundary(ContentPolicy(True)))
    span = runtime.begin.return_value
    span.event.side_effect = asyncio.CancelledError()
    span.attributes.side_effect = RuntimeError('unavailable')
    adapter = WorkflowObservationFactory(config(trace=True),boundary=EmissionBoundary(),trace=runtime).create(definition())
    adapter.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
    adapter.capture(visit_id='visit',direction='input',values={'text':'visible'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    adapter.close()
    assert len(adapter.finalize()['events']) == 2


@pytest.mark.parametrize('outcome,status', [('success', 'ok'), ('partial', 'error'), ('failure', 'error'), ('cancelled', 'cancelled')])
def test_unpriced_tool_leaf_is_not_traced_as_an_unknown_chat(outcome, status):
    from unittest.mock import Mock
    runtime = Mock(boundary=EmissionBoundary(ContentPolicy(False)))
    adapter = WorkflowObservationFactory(config(), boundary=EmissionBoundary(),
        trace=runtime).create(definition())
    call_id = str(uuid4())
    adapter.observe(dict(event='scope_start', scope_id='tool-visit', kind='node', owner='Make'))

    adapter.record_call(visit_id='tool-visit', call_id=call_id,
        elapsed=.01, outcome=outcome)

    name, attributes = runtime.begin.call_args.args
    assert name == 'execute_tool'
    assert attributes['gen_ai.operation.name'] == 'execute_tool'
    assert attributes['api_agent.call_id'] == call_id
    assert 'gen_ai.request.model' not in attributes
    assert not any(key.startswith('gen_ai.usage.') for key in attributes)
    assert runtime.begin.return_value.attributes.call_args.args[0]['status'] == status
    retained = adapter.finalize()['events'][-1]
    assert retained['usage'] is None and retained['selection'] is None


@pytest.mark.parametrize('call_kind,expected_name', [
    ('llm', 'chat unknown'), ('deterministic', 'execute_tool')])
def test_withheld_failed_model_identity_does_not_change_callable_kind(call_kind, expected_name):
    from unittest.mock import Mock
    runtime = Mock(boundary=EmissionBoundary(ContentPolicy(False)))
    source = definition()
    source.layout.tables['Functions'][0]['Kind'] = call_kind
    adapter = WorkflowObservationFactory(config(), boundary=EmissionBoundary(),
        trace=runtime).create(source)
    adapter.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))

    adapter.record_call(visit_id='visit', call_id=str(uuid4()),
        selection='unadmitted-model' if call_kind == 'llm' else None,
        usage=None, outcome='failed')

    name, attributes = runtime.begin.call_args.args
    assert name == expected_name
    assert attributes['gen_ai.operation.name'] == ('chat' if call_kind == 'llm' else 'execute_tool')
    assert ('gen_ai.request.model' in attributes) == (call_kind == 'llm')
    if call_kind == 'llm':
        assert attributes['gen_ai.request.model'] == 'unknown'
    assert adapter.finalize()['events'][-1]['selection'] is None


def test_real_serialized_tool_span_has_error_status_without_model_accounting():
    with collector() as (endpoint, received):
        resolved = config(TRACE_TARGET=endpoint)
        runtime = TraceRuntime(resolved, model_identities=('example-model',))
        asyncio.run(runtime.astart())
        adapter = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(),
            trace=runtime).create(definition())
        adapter.observe(dict(event='scope_start', scope_id='tool-visit', kind='node', owner='Make'))
        adapter.record_call(visit_id='tool-visit', call_id=str(uuid4()), elapsed=.01,
            outcome='partial')
        adapter.record_call(visit_id='tool-visit', call_id=str(uuid4()), elapsed=.01,
            selection='example-model', usage={'input_tokens': 7, 'output_tokens': 2})
        adapter.close()

        assert runtime.shutdown()
        wire = ExportTraceServiceRequest.FromString(received[0][1])
        spans = [span for resource in wire.resource_spans for scope in resource.scope_spans for span in scope.spans]
        tool = next(span for span in spans if span.name == 'execute_tool')
        chat = next(span for span in spans if span.name == 'chat example-model')
        attributes = {item.key: item.value for item in tool.attributes}
        assert attributes['gen_ai.operation.name'].string_value == 'execute_tool'
        assert 'gen_ai.request.model' not in attributes
        assert not any(key.startswith('gen_ai.usage.') for key in attributes)
        assert tool.kind == 3 and tool.status.code == 2
        assert tool.parent_span_id == chat.parent_span_id
        assert next(item.value.int_value for item in chat.attributes if item.key == 'gen_ai.usage.input_tokens') == 7


def test_empty_classification_without_properties_remains_unclassified():
    adapter = WorkflowObservationFactory(config(),boundary=EmissionBoundary()).create(definition())
    adapter.capture(visit_id='visit',direction='input',values={},schema={},plain_fields=[])
    assert adapter.finalize()['events'][0]['reason'] == 'withheld'


def test_terminal_receipts_are_context_owned_deduplicated_and_bounded():
    from api_agent.workflows.attribution import (
        admit_terminal_call_receipt,bind_terminal_call_receipts,bind_workflow_visit)
    owner, other = object(), object()
    row = {'call_id':'call-0','visit_id':'visit','attempt_id':None,'selection':'model','usage':None,'outcome':'completed'}
    with bind_workflow_visit(owner,'visit'):
        admit_terminal_call_receipt(row,owner)
        with bind_terminal_call_receipts() as receipts:
            admit_terminal_call_receipt(row,other)
            admit_terminal_call_receipt({**row,'visit_id':'other'},owner)
            admit_terminal_call_receipt({**row,'attempt_id':'other'},owner)
            assert receipts == []
            admit_terminal_call_receipt(row,owner)
            admit_terminal_call_receipt(row,owner)
            assert len(receipts) == 1
            for index in range(1,130):
                admit_terminal_call_receipt({**row,'call_id':f'call-{index}'},owner)
            assert len(receipts) == 128


@pytest.mark.parametrize('visit,direction,fields', [('visit','invalid',['text']),('', 'input',['text']),('visit','input',['text','text'])])
def test_unavailable_payload_cannot_hide_explicit_capture_contract_errors(visit,direction,fields):
    adapter = WorkflowObservationFactory(config(),boundary=EmissionBoundary()).create(definition())
    with pytest.raises(ValueError):
        adapter.capture(visit_id=visit,direction=direction,values={'text':'x'*65537},
            schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=fields)


@pytest.fixture(params=[
    pytest.param(('interactive',True,(),(),True,False),id='interactive'),
    pytest.param(('default',False,(),(),True,False),id='defaulted'),
    pytest.param(('environment',True,(),(),False,False),id='omitted'),
    pytest.param(('environment',True,('invalid configuration',),(),True,False),id='invalid'),
    pytest.param(('environment',True,(),('UNRESOLVED',),True,False),id='unresolved'),
    pytest.param(('cli',True,(),(),True,True),id='explicit-cli'),
    pytest.param(('environment',True,(),(),True,True),id='explicit-environment'),
])
def recording_policy(request):
    source,explicit,errors,missing,present,allowed = request.param
    fields = {'LOG_CONTENT':ResolvedValue(True,source,explicit)} if present else {}
    return ConfigResult({'LOG_CONTENT':True},fields,(),missing,errors),allowed


def test_injected_recorded_journal_permission_matches_resolved_provenance(recording_policy):
    from api_agent.workflows.journal import WorkflowJournal
    resolved,allowed = recording_policy
    journal = WorkflowJournal(definition(),log_content=True)
    identity = journal.invocation_id,journal.turn_id
    rows = []
    adapter = WorkflowObservationFactory(resolved,boundary=EmissionBoundary(),sink=rows.append).create(definition(),journal=journal)
    adapter.capture(visit_id='visit',direction='input',values={'text':'synthetic-private-prompt'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    payload = adapter.finalize()
    assert (adapter.invocation_id,adapter.turn_id)==identity
    assert ('values' in rows[0]) is allowed
    assert ('values' in payload['events'][0]) is allowed
    assert payload['content']==('permitted_scrubbed' if allowed else 'withheld')
    assert rows[0]['event_id']==payload['events'][0]['event_id']


@pytest.mark.parametrize('journal_content,boundary_content',[(False,True),(False,False)])
def test_resolved_opt_in_cannot_elevate_stricter_injected_journal(journal_content,boundary_content):
    from api_agent.workflows.journal import WorkflowJournal
    rows = []
    journal = WorkflowJournal(definition(),log_content=journal_content,boundary=EmissionBoundary(ContentPolicy(boundary_content)))
    adapter = WorkflowObservationFactory(config(log=True),boundary=EmissionBoundary(),sink=rows.append).create(definition(),journal=journal)
    adapter.capture(visit_id='visit',direction='output',values={'text':'withheld-sentinel'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    payload = adapter.finalize()
    assert payload['content']=='withheld' and 'withheld-sentinel' not in repr(payload)
    assert 'withheld-sentinel' not in repr(rows)


def test_denied_log_source_never_delivers_raw_values_to_injected_journal():
    from api_agent.workflows.journal import WorkflowJournal
    from unittest.mock import Mock
    journal = WorkflowJournal(definition(),log_content=True)
    original = journal.capture
    journal.capture = Mock(wraps=original)
    adapter = WorkflowObservationFactory(config(log=False),boundary=EmissionBoundary()).create(definition(),journal=journal)
    adapter.capture(visit_id='visit',direction='input',values={'text':'raw-source-sentinel'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    assert journal.capture.call_args.kwargs['values']=={}
    assert journal.capture.call_args.kwargs['plain_fields'] is None


@pytest.mark.parametrize('injected',[False,True])
def test_dynamic_credentials_from_both_journal_authorities_scrub_log_and_trace(injected):
    from api_agent.workflows.journal import WorkflowJournal
    from unittest.mock import Mock
    root_boundary = EmissionBoundary(ContentPolicy(True))
    original_boundary = EmissionBoundary(ContentPolicy(True))
    journal = WorkflowJournal(definition(),log_content=True,boundary=original_boundary) if injected else None
    trace = Mock(boundary=EmissionBoundary(ContentPolicy(True)))
    rows = []
    adapter = WorkflowObservationFactory(config(log=True,trace=True),boundary=root_boundary,sink=rows.append,trace=trace).create(definition(),journal=journal)
    root_boundary.register_secret('root-credential')
    original_boundary.register_secret('journal-credential')
    adapter.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
    text = 'visible root-credential'+(' journal-credential' if injected else '')
    adapter.capture(visit_id='visit',direction='input',values={'text':text},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    assert 'root-credential' not in repr(rows)+repr(trace.mock_calls)+repr(adapter.finalize())
    if injected:
        assert 'journal-credential' not in repr(rows)+repr(trace.mock_calls)
    assert 'visible' in repr(trace.mock_calls)
    adapter.close()


def test_trace_only_capture_stays_independent_of_injected_recorded_log_gate():
    from api_agent.workflows.journal import WorkflowJournal
    from unittest.mock import Mock
    journal = WorkflowJournal(definition(),log_content=True)
    trace = Mock(boundary=EmissionBoundary(ContentPolicy(True)))
    adapter = WorkflowObservationFactory(config(trace=True),boundary=EmissionBoundary(),trace=trace).create(definition(),journal=journal)
    adapter.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
    adapter.capture(visit_id='visit',direction='input',values={'text':'trace-only-sentinel'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    assert 'trace-only-sentinel' in repr(trace.begin.return_value.event.call_args)
    assert 'trace-only-sentinel' not in repr(adapter.finalize())
    adapter.close()


@pytest.mark.parametrize('state',['nonempty','finalized','admitted','gap'])
def test_policy_admission_refuses_late_history_reinterpretation(state,monkeypatch):
    from api_agent.workflows import journal as journal_module
    journal = journal_module.WorkflowJournal(definition(),log_content=True)
    factory = WorkflowObservationFactory(config(),boundary=EmissionBoundary())
    if state=='nonempty':
        journal.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
    elif state=='finalized':
        journal.finalize()
    elif state=='admitted':
        factory.create(definition(),journal=journal)
    else:
        monkeypatch.setattr(journal_module,'_MAX_EVENTS',0)
        journal.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
    before = repr(journal._events),journal.invocation_id,journal.turn_id,journal._log_content
    with pytest.raises(ValueError,match='before retention'):
        factory.create(definition(),journal=journal)
    assert (repr(journal._events),journal.invocation_id,journal.turn_id,journal._log_content)==before


@pytest.mark.parametrize('policy,boundary',[(None,EmissionBoundary()),(ContentPolicy(),None)])
def test_journal_policy_admission_requires_declared_types(policy,boundary):
    from api_agent.workflows.journal import WorkflowJournal
    with pytest.raises(TypeError,match='explicit policy and boundary'):
        WorkflowJournal(definition()).admit_policy(policy,boundary)


def test_credential_boundary_policy_does_not_veto_resolved_log_opt_in():
    from api_agent.workflows.journal import WorkflowJournal
    credentials = EmissionBoundary(ContentPolicy(False),secrets=['registered-credential'])
    journal = WorkflowJournal(definition(),log_content=True,boundary=credentials)
    rows = []
    adapter = WorkflowObservationFactory(config(log=True),boundary=EmissionBoundary(),sink=rows.append).create(definition(),journal=journal)
    adapter.capture(visit_id='visit',direction='input',values={'text':'visible registered-credential'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    payload = adapter.finalize()
    assert payload['content']=='permitted_scrubbed'
    assert payload['events'][0]['values']=={'text':'visible [REDACTED]'}
    assert dict(rows[0]['values'])=={'text':'visible [REDACTED]'}


@pytest.mark.parametrize('producer',['capture','observe'])
def test_policy_admission_rejects_inflight_producer_before_credential_export_finishes(producer):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from api_agent.workflows.journal import WorkflowJournal
    entered,release = Event(),Event()
    class PausedBoundary(EmissionBoundary):
        def content_export(self,payload):
            if not entered.is_set():
                entered.set()
                assert release.wait(2),'Synthetic boundary release deadline exceeded'
            return super().content_export(payload)
    boundary = PausedBoundary(ContentPolicy(True))
    journal = WorkflowJournal(definition(),log_content=True,boundary=boundary,content_policy=ContentPolicy(True),
        model_identity=lambda value: value == 'race-private-sentinel')
    original_identity = journal.invocation_id,journal.turn_id
    existing_rows,new_rows = [],[]
    journal.subscribe(existing_rows.append)
    factory = WorkflowObservationFactory(config(log=False),boundary=EmissionBoundary(),sink=new_rows.append)
    def produce():
        if producer=='capture':
            return journal.capture(visit_id='visit',direction='input',values={'text':'race-private-sentinel'},
                schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
        return journal.observe(dict(event='node',node='Make',selection='race-private-sentinel'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(produce)
        try:
            assert entered.wait(2),'Synthetic producer did not enter credential export'
            with pytest.raises(ValueError,match='before retention'):
                factory.create(definition(),journal=journal)
            assert journal._log_content is True and journal._boundary is boundary
        finally:
            release.set()
        admitted = future.result(timeout=2)
    payload = journal.finalize()
    assert payload['content']=='permitted_scrubbed'
    assert 'race-private-sentinel' in repr(payload)
    assert (journal.invocation_id,journal.turn_id)==original_identity
    assert existing_rows[0]['event_id']==admitted['event_id']==payload['events'][0]['event_id']
    assert new_rows==[]


def test_policy_admission_rejects_inflight_record_call_preparation(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from api_agent.workflows import journal as journal_module
    entered,release = Event(),Event()
    journal = journal_module.WorkflowJournal(definition(),log_content=True)
    factory = WorkflowObservationFactory(config(),boundary=EmissionBoundary())
    def stopped_time():
        entered.set()
        assert release.wait(2),'Synthetic accounting release deadline exceeded'
        return 1.0
    monkeypatch.setattr(journal_module,'time',SimpleNamespace(time=stopped_time))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(journal.record_call,visit_id='unopened-visit',call_id=str(uuid4()))
        try:
            assert entered.wait(2),'Synthetic accounting did not enter preparation'
            with pytest.raises(ValueError,match='before retention'):
                factory.create(definition(),journal=journal)
        finally:
            release.set()
        with pytest.raises(ValueError,match='no open node visit'):
            future.result(timeout=2)
    assert journal.finalize()['events']==[]


@pytest.mark.parametrize('producer',['capture','observe','record_call'])
def test_even_rejected_producer_contract_seals_policy_admission(producer):
    from api_agent.workflows.journal import WorkflowJournal
    journal = WorkflowJournal(definition(),log_content=True)
    with pytest.raises(ValueError):
        if producer=='capture':
            journal.capture(visit_id='',direction='input',values={},schema={})
        elif producer=='observe':
            journal.observe('invalid observation')
        else:
            journal.record_call(visit_id='')
    with pytest.raises(ValueError,match='before retention'):
        WorkflowObservationFactory(config(),boundary=EmissionBoundary()).create(definition(),journal=journal)


def test_unavailable_plain_capture_keeps_fixed_withholding_reason(monkeypatch):
    from api_agent.workflows import observations
    original = observations._plain_values
    def unavailable(*args, **kwargs):
        if 'limit' in kwargs:
            raise observations.CaptureUnavailable('unavailable')
        return original(*args, **kwargs)
    monkeypatch.setattr(observations, '_plain_values', unavailable)
    rows = []
    adapter = WorkflowObservationFactory(config(log=True), boundary=EmissionBoundary(),
        sink=rows.append).create(definition())

    adapter.capture(visit_id='visit', direction='input', values={'text': 'withheld'},
        schema={'type': 'object', 'properties': {'text': {'type': 'string'}}}, plain_fields=['text'])

    assert rows[0]['reason'] == 'privacy_withheld'
    assert 'withheld' not in repr(rows[0].get('values', {}))


def test_explicit_constructor_denial_cannot_be_elevated_by_factory_policy():
    from api_agent.workflows.journal import WorkflowJournal
    journal = WorkflowJournal(definition(), log_content=True, content_policy=ContentPolicy(False))
    adapter = WorkflowObservationFactory(config(log=True), boundary=EmissionBoundary()).create(
        definition(), journal=journal)

    adapter.capture(visit_id='visit', direction='input', values={'text': 'private-sentinel'},
        schema={'type': 'object', 'properties': {'text': {'type': 'string'}}}, plain_fields=['text'])

    assert not adapter.log_policy.allow_content
    assert 'private-sentinel' not in repr(adapter.finalize())


@pytest.mark.parametrize('authority',[None,False,True])
@pytest.mark.parametrize('factory',[False,True])
def test_explicit_constructor_denial_survives_factory_admission(authority,factory):
    from api_agent.workflows.journal import WorkflowJournal
    policy = None if authority is None else ContentPolicy(authority)
    journal = WorkflowJournal(definition(),log_content=True,content_policy=policy)
    target = (WorkflowObservationFactory(config(log=True),boundary=EmissionBoundary()).create(
        definition(),journal=journal) if factory else journal)
    target.capture(visit_id='visit',direction='output',values={'text':'authority-sentinel'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    if factory:
        assert target.projection is journal.projection
    payload = target.finalize()
    allowed = authority is True or (authority is None and factory)
    assert ('authority-sentinel' in repr(payload)) is allowed
    assert payload['content']==('permitted_scrubbed' if allowed else 'withheld')


@pytest.mark.parametrize('overlap',['observe','capture','crossed','unavailable'])
@pytest.mark.parametrize('log,trace',[(False,False),(True,False),(False,True),(True,True)])
def test_concurrent_capture_fanout_preserves_each_retained_identity(overlap,log,trace,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from collections import Counter
    entered,release = Event(),Event()
    second_entered,second_release = Event(),Event()
    rows,trace_events = [],[]
    runtime = SimpleNamespace(boundary=EmissionBoundary(ContentPolicy(True)),
        is_model_identity=lambda value:False) if trace else None
    adapter = WorkflowObservationFactory(config(log=log),boundary=EmissionBoundary(),
        sink=rows.append,trace=runtime).create(definition())
    adapter._spans['visit'] = SimpleNamespace(event=trace_events.append)
    original = adapter.journal.capture
    def paused_capture(**options):
        if options['direction']=='input':
            entered.set()
            assert release.wait(5)
        elif overlap=='crossed':
            second_entered.set()
            assert second_release.wait(5)
        return original(**options)
    monkeypatch.setattr(adapter.journal,'capture',paused_capture)
    schema = {'type':'object','properties':{'text':{'type':'string'}}}
    def capture(direction):
        return adapter.capture(visit_id='visit',direction=direction,
            values={'text':direction+'-private-sentinel'},schema=schema,plain_fields=['text'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(capture,'input')
        try:
            assert entered.wait(5)
            if overlap=='unavailable':
                second = pool.submit(adapter.capture,visit_id='visit',direction='output',
                    values={'text':'x'*65537},schema=schema,plain_fields=['text'])
            elif overlap in ('capture','crossed'):
                second = pool.submit(capture,'output')
            else:
                second = pool.submit(adapter.observe,{'event':'node','node':'Make'})
            if overlap=='crossed':
                assert second_entered.wait(5)
                release.set()
                first.result(timeout=5)
                second_release.set()
            second_row = second.result(timeout=5)
        finally:
            release.set()
            second_release.set()
        first_row = first.result(timeout=5)
    payload = adapter.finalize()
    retained = payload['events']
    assert len(retained)==len(rows)==2
    assert Counter(row['event_id'] for row in rows)==Counter(row['event_id'] for row in retained)
    assert {first_row['event_id'],second_row['event_id']}=={row['event_id'] for row in retained}
    assert [row['sequence'] for row in retained]==[1,2]
    captures = [row for row in rows if row['event']=='capture']
    assert all(('values' in row) is (log and row.get('reason')!='payload_limit') for row in captures)
    if not log:
        assert 'private-sentinel' not in repr(rows+retained)
    if trace:
        assert Counter(row['event_id'] for row in trace_events)==Counter(
            row['event_id'] for row in retained if row['event']=='capture' and row.get('reason')!='payload_limit')
        assert all('private-sentinel' in row['values']['text'] for row in trace_events)


def test_capture_context_does_not_suppress_reentrant_node_and_resets_on_error(monkeypatch):
    rows = []
    adapter = WorkflowObservationFactory(config(log=True),boundary=EmissionBoundary(),sink=rows.append).create(definition())
    def failing_capture(**options):
        adapter.observe({'event':'node','node':'Make'})
        adapter.observe({'event':'capture','visit_id':'other','direction':'output','reason':'withheld'})
        raise ValueError('declared capture failure')
    monkeypatch.setattr(adapter.journal,'capture',failing_capture)
    with pytest.raises(ValueError,match='declared capture failure'):
        adapter.capture(visit_id='visit',direction='input',values={},schema={})
    adapter.observe({'event':'capture','visit_id':'visit','direction':'input','reason':'withheld'})
    retained = adapter.finalize()['events']
    assert [row['event'] for row in rows]==['node','capture','capture']
    assert [row['event_id'] for row in rows]==[row['event_id'] for row in retained]


@pytest.mark.parametrize('log',[False,True])
def test_reentrant_capture_shaped_observation_keeps_its_distinct_identity(log,monkeypatch):
    rows = []
    adapter = WorkflowObservationFactory(config(log=log),boundary=EmissionBoundary(),sink=rows.append).create(definition())
    original = adapter.journal.capture
    def reentrant_capture(**options):
        adapter.observe({'event':'capture','visit_id':'visit','direction':'input','reason':'withheld'})
        return original(**options)
    monkeypatch.setattr(adapter.journal,'capture',reentrant_capture)
    own = adapter.capture(visit_id='visit',direction='input',values={'text':'plain-visible'},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    retained = adapter.finalize()['events']
    assert len(rows)==len(retained)==2
    assert [row['event_id'] for row in rows]==[row['event_id'] for row in retained]
    assert rows[1]['event_id']==own['event_id']
    assert ('values' in rows[1]) is log
    assert 'values' not in rows[0]


@pytest.mark.parametrize('log',[False,True])
@pytest.mark.parametrize('timing',['before-close','after-close'])
def test_copied_capture_context_emits_before_and_after_invocation_close(log,timing):
    from contextvars import copy_context
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from collections import Counter
    rows,contexts = [],[]
    entered,release = Event(),Event()
    adapter = WorkflowObservationFactory(config(log=log),boundary=EmissionBoundary(),sink=rows.append).create(definition())
    def subscriber(row):
        if row['event']=='capture':
            contexts.append(copy_context())
            entered.set()
            if timing=='before-close':
                assert release.wait(5)
    adapter.journal.subscribe(subscriber)
    with ThreadPoolExecutor(max_workers=2) as pool:
        capture = pool.submit(adapter.capture,visit_id='visit',direction='input',
            values={'text':'private-sentinel'},
            schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
        try:
            assert entered.wait(5)
            if timing=='after-close':
                capture.result(timeout=5)
            observed = pool.submit(contexts[0].run,adapter.observe,{'event':'node','node':'Make'})
            late = observed.result(timeout=5)
            assert late['event_id'] in {row['event_id'] for row in rows}
        finally:
            release.set()
        own = capture.result(timeout=5)
    retained = adapter.finalize()['events']
    assert Counter(row['event_id'] for row in rows)==Counter(row['event_id'] for row in retained)
    assert {own['event_id'],late['event_id']}=={row['event_id'] for row in rows}
    if not log:
        assert 'private-sentinel' not in repr(rows+retained)


@pytest.mark.parametrize('log',[False,True])
def test_async_task_inherits_closed_capture_context_without_losing_row(log):
    from collections import Counter
    rows,tasks = [],[]
    adapter = WorkflowObservationFactory(config(log=log),boundary=EmissionBoundary(),sink=rows.append).create(definition())
    async def later():
        await asyncio.sleep(0)
        return adapter.observe({'event':'node','node':'Make'})
    def subscriber(row):
        if row['event']=='capture':
            tasks.append(asyncio.create_task(later()))
    adapter.journal.subscribe(subscriber)
    async def run():
        adapter.capture(visit_id='visit',direction='input',values={'text':'private-sentinel'},
            schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
        return await tasks[0]
    late = asyncio.run(run())
    retained = adapter.finalize()['events']
    assert late['event_id'] in {row['event_id'] for row in rows}
    assert Counter(row['event_id'] for row in rows)==Counter(row['event_id'] for row in retained)
    if not log:
        assert 'private-sentinel' not in repr(rows+retained)


@pytest.mark.parametrize('log',[False,True])
def test_capture_substitution_preserves_other_subscribers_and_synchronous_reentry(log):
    rows,previews = [],[]
    adapter = WorkflowObservationFactory(config(log=log),boundary=EmissionBoundary(),sink=rows.append).create(definition())
    def other(row):
        assert row['event_id'] in {live['event_id'] for live in rows}
        if row['event']=='capture':
            previews.append(row)
            nested = adapter.observe({'event':'node','node':'Make'})
            assert nested['event_id'] in {live['event_id'] for live in rows}
    adapter.journal.subscribe(other)
    text = 'visible' * 100
    own = adapter.capture(visit_id='visit',direction='input',values={'text':text},
        schema={'type':'object','properties':{'text':{'type':'string'}}},plain_fields=['text'])
    retained = adapter.finalize()['events']
    assert [row['event_id'] for row in rows]==[row['event_id'] for row in retained]
    assert len(previews)==1 and previews[0]['event_id']==own['event_id']
    if log:
        assert len(previews[0]['values']['text'])==512
        assert rows[0]['values']['text']==text
    else:
        assert 'values' not in previews[0] and 'values' not in rows[0]


@pytest.mark.parametrize('log,trace', [(False,False),(False,True),(True,False),(True,True)])
def test_unadmitted_actual_model_cannot_become_structural_evidence(log, trace):
    resolved = config(log, trace, mode='')
    credentials = EmissionBoundary(ContentPolicy(True))
    runtime = TraceRuntime(resolved, boundary=credentials, model_identities=('requested-model',))
    adapter = WorkflowObservationFactory(resolved, boundary=credentials, trace=runtime).create(definition())
    visit = str(uuid4())
    adapter.observe(dict(event='scope_start', scope_id=visit, owner='Make', kind='node'))
    adapter.record_call(visit_id=visit, call_id=str(uuid4()), selection='requested-model',
        actual_model='unadmitted-sensitive-model', reasoning_effort='low')
    call = next(row for row in adapter.finalize()['events'] if row['event'] == 'call')
    assert call['selection'] == 'requested-model'
    assert call['actual_model'] is None
    assert call['reasoning_effort'] == 'low'
    assert 'unadmitted-sensitive-model' not in json.dumps(call)
    adapter.close()
    assert runtime.shutdown()


@pytest.mark.parametrize('trace_content', [False, True])
def test_reserved_response_model_requires_catalog_admission_at_every_trace_entry(trace_content):
    from unittest.mock import Mock
    runtime = TraceRuntime(config(trace=trace_content), model_identities=('catalog-model',))
    tracer = Mock()
    runtime._tracer = tracer
    unknown = {'gen_ai.response.model': 'unadmitted-sensitive-model'}
    known = {'gen_ai.response.model': 'catalog-model'}
    span = runtime.begin('chat catalog-model', {}, payload=unknown)
    assert 'gen_ai.response.model' not in tracer.start_span.call_args.kwargs['attributes']
    span.attributes(unknown)
    assert 'gen_ai.response.model' not in tracer.start_span.return_value.set_attributes.call_args.args[0]
    span.event(unknown)
    assert 'gen_ai.response.model' not in tracer.start_span.return_value.add_event.call_args.kwargs['attributes']
    span.attributes(known)
    assert tracer.start_span.return_value.set_attributes.call_args.args[0]['gen_ai.response.model'] == 'catalog-model'
    span.event(known)
    assert tracer.start_span.return_value.add_event.call_args.kwargs['attributes']['gen_ai.response.model'] == 'catalog-model'
    span.end()
    with runtime.span(unknown):
        assert 'gen_ai.response.model' not in tracer.start_span.call_args.kwargs['attributes']
    assert 'unadmitted-sensitive-model' not in repr(tracer.mock_calls)
    runtime._tracer = None
    assert runtime.shutdown()


def test_journal_rejects_unrecognized_reasoning_effort_without_retaining_content():
    from api_agent.workflows.journal import WorkflowJournal

    journal = WorkflowJournal(definition())
    with pytest.raises(ValueError, match='Invalid workflow reasoning effort'):
        journal.observe(dict(event='node', node='Make', reasoning_effort='sensitive-unknown-effort'))

    payload = journal.finalize()
    assert payload['events'] == []
    assert 'sensitive-unknown-effort' not in json.dumps(payload)


@pytest.mark.parametrize(('field', 'value', 'message'), [
    ('reasoning_effort', 'sensitive-unknown-effort', 'Invalid workflow reasoning effort'),
    ('actual_model', 'sensitive-model\nprivate', 'Invalid workflow actual model'),
])
def test_journal_admission_rejects_invalid_inference_metadata_with_valid_digest(field, value, message):
    from hashlib import sha256
    from api_agent.workflows.journal import WorkflowJournal, validate_journal

    journal = WorkflowJournal(definition())
    journal.observe(dict(event='scope_start', scope_id=str(uuid4()), owner='GRAPH', kind='graph'))
    payload = journal.finalize()
    assert validate_journal(payload) == payload
    payload['events'][0][field] = value
    unsigned = {key: item for key, item in payload.items() if key != 'sha256'}
    payload['sha256'] = sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    with pytest.raises(ValueError, match=message):
        validate_journal(payload)
