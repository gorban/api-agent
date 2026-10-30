"""Actual generic gate dispatch owns canonical tool evidence, never LLM usage."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.providers.tools import ToolResult, ToolSpec
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.attribution import bind_workflow_visit
from api_agent.workflows.journal import WorkflowJournal
from api_agent.workflows.tool_gate import ToolGate, ToolGateError
from api_agent.workflows.tool_observations import validate_tool_events


GRAPH = str(uuid4())
VISIT = str(uuid4())

SCHEMA = {'type': 'object', 'properties': {'query': {'type': 'string'}},
          'required': ['query'], 'additionalProperties': False}


class Owner:
    def __init__(self, *, error=None, status='success'):
        self.spec = ToolSpec('lookup', 'Read a synthetic item', SCHEMA,
            {'type': ['object', 'null']}, sha256(b'synthetic-contract').hexdigest())
        self.received = []
        self.error, self.status = error, status

    async def describe(self):
        return self.spec

    async def validate(self, prepared):
        return None

    async def invoke(self, prepared):
        assert prepared.spec is self.spec
        self.received.append(deepcopy(prepared.arguments))
        if self.error is not None:
            raise self.error
        return ToolResult(self.status, {'answer': 'visible synthetic-secret'})

    async def aclose(self):
        return None


def workflow_definition():
    node = SimpleNamespace(id='Make', kind='node', parent=None)
    return SimpleNamespace(graph_id='Example', definition_id='a' * 64,
        entry_root='base', layout=SimpleNamespace(steps=[node], tables={'Edges': []}),
        resources=[], replacements=[])


def start_node(journal):
    journal.observe(dict(event='scope_start', scope_id=GRAPH, parent_id=None,
        kind='graph', owner='GRAPH', started_ms=0))
    journal.observe(dict(event='scope_start', scope_id=VISIT, parent_id=GRAPH,
        kind='node', owner='Make', started_ms=0))


def observed_journal(log=True):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=['synthetic-secret'])
    journal = WorkflowJournal(workflow_definition(), log_content=log, content_policy=ContentPolicy(log), boundary=boundary)
    start_node(journal)
    return journal, boundary


class TraceRecorder:
    def __init__(self, enabled):
        self.boundary = EmissionBoundary(ContentPolicy(enabled))
        self.events = []

    def is_model_identity(self, value):
        return False

    def begin(self, *args, **kwargs):
        return SimpleNamespace(event=self.events.append, attributes=lambda _: None,
                               end=lambda **kwargs: None)


def observed_destinations(log, trace):
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    from api_agent.workflows.observations import WorkflowObservationFactory
    values = {'LOG_CONTENT': log, 'TRACELOOP_TRACE_CONTENT': trace}
    sources = {name: ResolvedValue(value, 'cli', True) for name, value in values.items()}
    config = ConfigResult(values, sources, (), (), ())
    boundary = EmissionBoundary(ContentPolicy(True), secrets=['synthetic-secret'])
    tracing = TraceRecorder(trace)
    sink = []
    journal = WorkflowObservationFactory(config, boundary=boundary, sink=sink.append,
        trace=tracing).create(workflow_definition())
    start_node(journal)
    return journal, boundary, tracing, sink


async def verified_gate(owner, invocation='external-call'):
    gate = ToolGate(owner, invocation_id=invocation, tool_alias='named_lookup',
        tool_instance='group.lookup')
    prepared = await gate.prepare({'query': 'visible synthetic-secret'})
    grant = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
    return gate, prepared, grant


@pytest.mark.asyncio
@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
async def test_actual_gate_has_distinct_scrubbed_tool_phases_with_independent_flags(log, trace):
    journal, boundary, tracing, sink = observed_destinations(log, trace)
    owner = Owner()
    gate, _, grant = await verified_gate(owner)
    services = {'emissions': boundary, 'content_policy': ContentPolicy(log),
                'TRACELOOP_TRACE_CONTENT': trace}

    with bind_workflow_visit(journal, VISIT, capture_services=services):
        answer = await gate.execute(grant)

    events = journal.finalize()['events']
    tool = [row for row in events if row.get('tool_invocation_id')]
    assert [row['event'] for row in tool] == ['tool_start', 'capture', 'capture', 'tool_end']
    assert validate_tool_events(events)
    assert answer.data['answer'] == 'visible synthetic-secret'
    assert owner.received == [{'query': 'visible synthetic-secret'}]
    assert not any(key in row for row in tool for key in ('call_id', 'cause_id', 'usage', 'transport_attempts'))
    captures = [row for row in tool if row['event'] == 'capture']
    if log:
        assert captures[0]['values']['arguments'] == {'query': 'visible [REDACTED]'}
        assert captures[1]['values']['data'] == {'answer': 'visible [REDACTED]'}
        assert tool[0]['tool_name'] == 'named_lookup'
    else:
        assert all('values' not in row and row['reason'] == 'withheld' for row in captures)
    traced = [row for row in tracing.events if row.get('tool_invocation_id')]
    assert len(traced) == (2 if trace else 0)
    if trace:
        assert traced[0]['values']['arguments'] == {'query': 'visible [REDACTED]'}
        assert traced[1]['values']['data'] == {'answer': 'visible [REDACTED]'}
    logged = [row for row in sink if row.get('tool_invocation_id') and row['event'] == 'capture']
    assert len(logged) == 2
    assert all(('values' in row) is log for row in logged)


@pytest.mark.asyncio
async def test_staged_admission_mutation_rejects_before_actual_tool_leaf():
    journal, boundary = observed_journal()
    owner = Owner()
    gate, prepared, grant = await verified_gate(owner)

    @asynccontextmanager
    async def authority():
        prepared.arguments['query'] = 'changed during admission'
        yield boundary

    with bind_workflow_visit(journal, VISIT, capture_services={'capture_authority': authority}):
        with pytest.raises(ToolGateError, match='arguments changed'):
            await gate.execute(grant)

    assert owner.received == []
    assert not any(row.get('tool_invocation_id') for row in journal.finalize()['events'])


@pytest.mark.asyncio
async def test_reentrant_observer_cannot_change_private_dispatch_arguments():
    journal, boundary = observed_journal()
    owner = Owner()
    gate, prepared, grant = await verified_gate(owner)

    def subscriber(row):
        if row['event'] == 'tool_start':
            prepared.arguments['query'] = 'changed by subscriber'
    journal.subscribe(subscriber)

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        await gate.execute(grant)

    assert owner.received == [{'query': 'visible synthetic-secret'}]
    request = next(row for row in journal.finalize()['events']
        if row.get('tool_invocation_id') and row.get('direction') == 'input')
    assert request['values']['arguments'] == {'query': 'visible [REDACTED]'}


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('synthetic-owner-failure'), asyncio.CancelledError()])
async def test_owner_error_identity_survives_missing_response_evidence(error):
    journal, boundary = observed_journal()
    owner = Owner(error=error)
    gate, _, grant = await verified_gate(owner)

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        with pytest.raises(type(error)) as caught:
            await gate.execute(grant)

    assert caught.value is error
    events = journal.finalize()['events']
    output = next(row for row in events if row.get('tool_invocation_id') and row.get('direction') == 'output')
    assert output['reason'] == 'unavailable'
    assert events[-1]['outcome'] == ('cancelled' if isinstance(error, asyncio.CancelledError) else 'failure')
    assert not validate_tool_events(events)


@pytest.mark.asyncio
async def test_missing_authority_withholds_content_and_secret_tool_labels():
    journal, _ = observed_journal()
    owner = Owner()
    gate, _, grant = await verified_gate(owner)
    gate.tool_alias = 'synthetic-secret'

    with bind_workflow_visit(journal, VISIT):
        await gate.execute(grant)

    events = journal.finalize()['events']
    assert 'synthetic-secret' not in str(events)
    assert all('values' not in row for row in events)
    assert len(owner.received) == 1


@pytest.mark.asyncio
async def test_replay_does_not_create_another_tool_invocation():
    journal, boundary = observed_journal()
    gate, _, grant = await verified_gate(Owner())

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        await gate.execute(grant)
        with pytest.raises(ToolGateError, match='already used'):
            await gate.execute(grant)

    assert sum(row['event'] == 'tool_start' for row in journal.finalize()['events']) == 1


@pytest.mark.asyncio
async def test_concurrent_gates_share_external_id_without_sharing_tool_identity():
    journal, boundary = observed_journal()
    first, _, first_grant = await verified_gate(Owner(), invocation='same-external-call')
    second, _, second_grant = await verified_gate(Owner(), invocation='same-external-call')

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        await asyncio.gather(first.execute(first_grant), second.execute(second_grant))

    events = journal.finalize()['events']
    identifiers = [row['tool_invocation_id'] for row in events if row['event'] == 'tool_start']
    assert len(identifiers) == len(set(identifiers)) == 2
    assert validate_tool_events(events)


@pytest.mark.asyncio
async def test_failure_result_data_uses_bounded_json_without_success_schema():
    journal, boundary = observed_journal()
    owner = Owner(status='failure')
    owner.spec = ToolSpec('lookup', 'Read a synthetic item', SCHEMA,
        {'type': 'object', 'required': ['required_success_field']}, sha256(b'failure-contract').hexdigest())
    gate, _, grant = await verified_gate(owner)

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        answer = await gate.execute(grant)

    output = next(row for row in journal.finalize()['events']
        if row.get('tool_invocation_id') and row.get('direction') == 'output')
    assert answer.status == 'failure'
    assert output['values'] == {'status': 'failure', 'data': {'answer': 'visible [REDACTED]'},
        'errors': [], 'diagnostic': None}


@pytest.mark.asyncio
async def test_canonical_tool_roundtrip_and_native_phase_attachments(tmp_path):
    from datetime import datetime, timezone
    import json
    import time
    from api_agent.testing.payloads import PayloadStore, PayloadResolver, hydrate_journal, encoded
    from api_agent.testing.recording import RunRecorder, RunIdentity
    from api_agent.testing.workflow_steps import scope_steps, attach_payloads

    journal, boundary = observed_journal()
    gate, _, grant = await verified_gate(Owner())
    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        await gate.execute(grant)
    stopped = time.time() * 1000 + 1
    journal.observe(dict(event='scope_end', scope_id=VISIT, parent_id=GRAPH,
        kind='node', owner='Make', started_ms=0, stopped_ms=stopped, outcome='passed'))
    journal.observe(dict(event='scope_end', scope_id=GRAPH, parent_id=None,
        kind='graph', owner='GRAPH', started_ms=0, stopped_ms=stopped, outcome='passed'))
    packet = journal.finalize()
    recorder = RunRecorder(tmp_path, RunIdentity(str(uuid4()), 'synthetic', 'integration', datetime.now(timezone.utc)))
    store = PayloadStore(recorder, 'recorded-test')

    durable = store.materialize_journal(packet)
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.collection import TestIdentity
    recorder.record_artifact('index.json', encoded(store.index()))
    recorder.record_test(TestResult(TestIdentity('recorded-test', 'synthetic', 'test.py', 'test.py::test'), 'passed'))
    manifest = recorder.finalize()
    resolver = PayloadResolver.local(tmp_path, manifest)
    resolver.index('index.json')
    reopened = hydrate_journal(durable, resolver)
    steps = scope_steps(packet['events'])
    captures = [(row, {'name': 'Tool request' if row['direction'] == 'input' else 'Tool response',
        'source': row['payload']['source'], 'type': 'application/json'})
        for row in durable['events'] if row.get('tool_invocation_id') and row['event'] == 'capture']
    assert attach_payloads(steps, captures, {}) == []

    assert reopened == packet
    entries = store.index()['entries']
    assert len(entries) == 2
    assert all(row['occurrence']['kind'] == 'tool' for row in entries)
    assert len({row['source'] for row in entries}) == 2
    leaf = steps[0]['steps'][0]['steps'][0]
    assert leaf['name'] == 'Tool named_lookup'
    assert [item['name'] for item in leaf['attachments']] == ['Tool request', 'Tool response']
    assert 'attachments' not in steps[0]['steps'][0]
    assert 'synthetic-secret' not in json.dumps(durable)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['cross_visit', 'cross_attempt', 'duplicate_input', 'duplicate_start',
    'transport', 'llm_call', 'backward_time', 'changed_name'])
async def test_tool_lifecycle_rejects_corrupt_ownership_and_protocol_fields(mutation):
    journal, boundary = observed_journal()
    gate, _, grant = await verified_gate(Owner())
    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        await gate.execute(grant)
    events = deepcopy(journal.finalize()['events'])
    start = next(row for row in events if row['event'] == 'tool_start')
    request = next(row for row in events if row.get('tool_invocation_id') and row.get('direction') == 'input')
    end = events[-1]
    if mutation == 'cross_visit':
        request['visit_id'] = 'other-visit'
    elif mutation == 'cross_attempt':
        request['attempt_id'] = 'other-attempt'
    elif mutation == 'duplicate_input':
        events.insert(events.index(request), deepcopy(request))
    elif mutation == 'duplicate_start':
        events.insert(events.index(start), deepcopy(start))
    elif mutation == 'transport':
        request['transport_attempt_id'] = str(uuid4())
    elif mutation == 'llm_call':
        end['call_id'] = str(uuid4())
    elif mutation == 'backward_time':
        end['stopped_ms'] = start['started_ms'] - 1
    else:
        end['tool_name'] = 'different-name'

    with pytest.raises(ValueError):
        validate_tool_events(events)


_TOOL_NODE = '''async def inspect(inputs, context):
    gate = context.services['gate']
    prepared = await gate.prepare({'query': 'visible synthetic-secret'})
    verified = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
    await gate.execute(verified)
    return {'seen': True}
'''


def classic_tool_workflow(tmp_path, services):
    from test_workflow_definition import make_graph
    from api_agent.workflows.definition import load_definition, load_native_catalog
    from api_agent.workflows.execution import Workflow, NodeFunction
    flow = make_graph(tmp_path)
    (tmp_path / 'native.py').write_text(_TOOL_NODE)
    definition = load_definition(flow, base_root=tmp_path, pack_root=tmp_path)
    journal = WorkflowJournal(definition, log_content=True, content_policy=ContentPolicy(True), boundary=EmissionBoundary(ContentPolicy(True)))
    catalog = load_native_catalog(definition, tmp_path / 'native.py')
    workflow = Workflow(definition, {'inspect': NodeFunction(catalog['inspect'])},
        services={**services, 'journal': journal})
    async def run():
        return (await workflow.arun({}))['inspect']
    return journal, run


def stateful_tool_workflow(tmp_path, services):
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_definition, load_stateful_catalog
    (tmp_path / 'native.py').write_text('from typing import TypedDict\nclass State(TypedDict):\n    seen: bool\n\n' + _TOOL_NODE)
    (tmp_path / 'inspect.md.html').write_text('''# Node v3: inspect

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| inspect | deterministic | [Python](native.py) |
''')
    flow = tmp_path / 'flow.md.html'
    flow.write_text('''# Workflow v3

## Graph ID

| ID |
| --- |
| actual_tool |

## State

| Registry | Source |
| --- | --- |
| State | [Python](native.py) |

## Flow

1.  [node inspect](inspect.md.html)

## Edges

| From | To | Router | Source |
| --- | --- | --- | --- |
| inspect | END | - | - |
''')
    definition = load_stateful_definition(flow, base_root=tmp_path, pack_root=tmp_path)
    journal = WorkflowJournal(definition, log_content=True, content_policy=ContentPolicy(True), boundary=EmissionBoundary(ContentPolicy(True)))
    workflow = StatefulWorkflow(definition, catalog=load_stateful_catalog(definition),
        reducers={}, journal=journal, services=services)
    return journal, lambda: workflow.arun({'seen': False}, thread_id='synthetic-tool-thread')


@pytest.mark.asyncio
@pytest.mark.parametrize('build_workflow', [classic_tool_workflow, stateful_tool_workflow])
async def test_both_parsed_executors_observe_generic_gate_dispatch(tmp_path, build_workflow, monkeypatch):
    monkeypatch.delenv('API_AGENT_ALLURE_REPORTING', raising=False)
    boundary = EmissionBoundary(ContentPolicy(True), secrets=['synthetic-secret'])
    gate = ToolGate(Owner(), invocation_id='external-call', tool_alias='named_lookup')
    journal, run = build_workflow(tmp_path, {'gate': gate, 'emissions': boundary,
        'content_policy': ContentPolicy(True)})

    result = await run()

    assert result['seen'] is True
    packet = journal.finalize()
    start = next(row for row in packet['events'] if row['event'] == 'tool_start')
    node = next(row for row in packet['events'] if row.get('scope_id') == start['visit_id']
        and row['event'] == 'scope_start')
    assert node['owner'] == 'inspect'
    assert start['invocation_id'] == packet['invocation_id']
    captures = [row for row in packet['events'] if row.get('tool_invocation_id') and row['event'] == 'capture']
    assert [row['direction'] for row in captures] == ['input', 'output']
    assert all('synthetic-secret' not in str(row) for row in captures)
    assert captures[0]['values']['arguments'] == {'query': 'visible [REDACTED]'}
    assert captures[1]['values']['data'] == {'answer': 'visible [REDACTED]'}


@pytest.mark.asyncio
async def test_remote_owner_lease_covers_retention_and_callbacks_run_after_release():
    from api_agent.providers.authority_channel import AuthorityChannel, leased_snapshot
    journal, local = observed_journal()
    remote = EmissionBoundary(ContentPolicy(True), secrets=['remote-secret'])
    key = b'k' * 32
    binding = {'worker_id': 'synthetic-tool-worker'}
    channel = AuthorityChannel(key, lambda *_: None, snapshot_source=lambda _: remote)
    path = await channel.start()
    retained_before_callback = []
    owner = Owner()
    gate = ToolGate(owner, invocation_id='external-call')
    prepared = await gate.prepare({'query': 'visible remote-secret'})
    grant = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})

    @asynccontextmanager
    async def authority():
        async with leased_snapshot(path, key, binding) as snapshot:
            yield snapshot

    def subscriber(row):
        if row['event'] == 'tool_start':
            retained_before_callback.append(any(event.get('tool_invocation_id') == row['tool_invocation_id']
                and event.get('direction') == 'input' for event in journal._events))
            remote.register_secret('callback-credential')
    journal.subscribe(subscriber)
    try:
        with bind_workflow_visit(journal, VISIT, capture_services={'capture_authority': authority}):
            await gate.execute(grant)
    finally:
        await channel.close()

    assert retained_before_callback == [True]
    assert 'callback-credential' in remote._secrets
    request = next(row for row in journal.finalize()['events']
        if row.get('tool_invocation_id') and row.get('direction') == 'input')
    assert request['values']['arguments'] == {'query': 'visible [REDACTED]'}
    assert owner.received == [{'query': 'visible remote-secret'}]


@pytest.mark.asyncio
async def test_cancellation_after_successful_invoke_during_output_admission_propagates():
    journal, boundary = observed_journal()
    owner = Owner()
    gate, _, grant = await verified_gate(owner)
    output_entered = asyncio.Event()
    admissions = []

    @asynccontextmanager
    async def authority():
        admissions.append(True)
        if len(admissions) == 2:
            output_entered.set()
            await asyncio.Event().wait()
        yield boundary

    async def execute():
        with bind_workflow_visit(journal, VISIT, capture_services={'capture_authority': authority}):
            return await gate.execute(grant)
    task = asyncio.create_task(execute())
    await output_entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    packet = journal.finalize()
    assert len(owner.received) == 1
    assert packet['events'][-1]['event'] == 'tool_end'
    assert packet['events'][-1]['outcome'] == 'cancelled'
    assert packet['gap'] == 'tool_evidence_unavailable'


@pytest.mark.asyncio
async def test_legal_local_schema_definitions_survive_both_tool_capture_envelopes():
    journal, boundary = observed_journal()
    owner = Owner()
    input_schema = {'type': 'object', 'properties': {'query': {'$ref': '#/$defs/Text'}},
        'required': ['query'], '$defs': {'Text': {'type': 'string'}}}
    output_schema = {'type': 'object', 'properties': {'answer': {'$ref': '#/$defs/Text'}},
        'required': ['answer'], '$defs': {'Text': {'type': 'string'}}}
    owner.spec = ToolSpec('lookup', 'Read a synthetic item', input_schema, output_schema,
        sha256(b'local-reference-contract').hexdigest())
    gate, _, grant = await verified_gate(owner)

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        await gate.execute(grant)

    captures = [row for row in journal.finalize()['events']
        if row.get('tool_invocation_id') and row['event'] == 'capture']
    assert [row['reason'] for row in captures] == ['captured', 'captured']
    assert captures[0]['values']['arguments'] == {'query': 'visible [REDACTED]'}
    assert captures[1]['values']['data'] == {'answer': 'visible [REDACTED]'}
    assert input_schema['properties']['query']['$ref'] == '#/$defs/Text'
    assert output_schema['properties']['answer']['$ref'] == '#/$defs/Text'


def test_schema_envelope_rebases_schema_resources_without_touching_literal_reference_data():
    from api_agent.workflows.tool_observations import tool_payload_schema
    literal = {'$ref': 'literal://instance-data'}
    schema = {'type': 'object', 'properties': {'value': {'$ref': '#/$defs/Value'}},
        '$defs': {'Value': {'const': literal, 'enum': [literal], 'default': literal, 'examples': [literal]}}}

    projected = tool_payload_schema(schema, 'arguments')

    assert projected['properties']['value']['$ref'] == '#/properties/arguments/$defs/Value'
    assert projected['$defs']['Value'] == schema['$defs']['Value']
    assert schema['properties']['value']['$ref'] == '#/$defs/Value'


def protocol_events():
    identifier = str(uuid4())
    node = {'event': 'scope_start', 'scope_id': VISIT, 'kind': 'node', 'started_ms': 1}
    start = {'event': 'tool_start', 'tool_invocation_id': identifier, 'visit_id': VISIT,
             'attempt_id': None, 'started_ms': 2, 'tool_name': 'lookup'}
    request = {'event': 'capture', 'tool_invocation_id': identifier, 'visit_id': VISIT,
               'attempt_id': None, 'direction': 'input', 'reason': 'captured'}
    response = {**request, 'direction': 'output'}
    end = {**start, 'event': 'tool_end', 'stopped_ms': 3, 'outcome': 'success'}
    end.pop('started_ms')
    return [node, start, request, response, end]


@pytest.mark.parametrize('index,changes', [
    (1, {'tool_invocation_id': None}),
    (1, {'tool_invocation_id': 123}),
    (1, {'tool_invocation_id': 'not-a-uuid'}),
    (2, {'tool_invocation_id': str(uuid4())}),
    (2, {'capture_source': 'provider'}),
    (2, {'response': {}}),
    (1, {'tool_name': 123}),
    (1, {'tool_name': 'unsafe label'}),
    (1, {'started_ms': float('inf')}),
    (1, {'started_ms': 0}),
    (4, {'stopped_ms': float('nan')}),
    (3, {'direction': 'input'}),
])
def test_invalid_tool_protocol_fields_reject_before_native_projection(index, changes):
    events = protocol_events()
    events[index].update(changes)

    with pytest.raises(ValueError):
        validate_tool_events(events)


def test_tool_interval_cannot_extend_beyond_its_own_node():
    events = protocol_events()
    events.append({'event': 'scope_end', 'scope_id': VISIT, 'kind': 'node', 'stopped_ms': 2})

    with pytest.raises(ValueError, match='interval'):
        validate_tool_events(events)


def test_open_tool_lifecycle_is_partial_without_inventing_completion():
    events = protocol_events()[:-1]

    assert validate_tool_events(events) is False


def test_tool_output_cannot_precede_its_request():
    events = protocol_events()
    events[2], events[3] = events[3], events[2]

    with pytest.raises(ValueError, match='phase'):
        validate_tool_events(events)


@pytest.mark.parametrize('reference', ['https://example.invalid/schema', 123])
def test_tool_envelope_does_not_widen_reference_policy(reference):
    from api_agent.workflows.tool_observations import tool_payload_schema

    with pytest.raises(ValueError, match='local references'):
        tool_payload_schema({'$ref': reference}, 'arguments')


@pytest.mark.asyncio
async def test_evidence_admission_failure_does_not_change_successful_tool_result(monkeypatch):
    from api_agent.workflows import capture_authority
    journal, boundary = observed_journal()
    owner = Owner()
    gate, _, grant = await verified_gate(owner)
    async def failed_capture(*args, **kwargs):
        raise RuntimeError('synthetic evidence failure')
    monkeypatch.setattr(capture_authority, 'capture_node', failed_capture)

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        answer = await gate.execute(grant)

    assert answer.status == 'success'
    assert len(owner.received) == 1
    assert journal.finalize()['gap'] == 'tool_evidence_unavailable'


@pytest.mark.asyncio
async def test_reentrant_subscriber_failure_marks_loss_without_changing_tool_outcome():
    journal, boundary = observed_journal()
    owner = Owner()
    gate, _, grant = await verified_gate(owner)
    def failing_subscriber(row):
        if row['event'] in ('tool_start', 'tool_end'):
            raise RuntimeError('synthetic subscriber failure')
    journal.subscribe(failing_subscriber)

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        answer = await gate.execute(grant)

    assert answer.status == 'success'
    assert len(owner.received) == 1
    assert journal.finalize()['gap'] == 'tool_evidence_unavailable'


@pytest.mark.asyncio
async def test_revoked_grant_during_admission_cannot_publish_actual_tool_start():
    journal, boundary = observed_journal()
    owner = Owner()
    gate, _, grant = await verified_gate(owner)
    @asynccontextmanager
    async def authority():
        gate._grants.clear()
        yield boundary

    with bind_workflow_visit(journal, VISIT, capture_services={'capture_authority': authority}):
        with pytest.raises(ToolGateError, match='stale'):
            await gate.execute(grant)

    assert owner.received == []
    assert not any(row.get('tool_invocation_id') for row in journal.finalize()['events'])


@pytest.mark.asyncio
async def test_owner_error_identity_survives_output_observer_cancellation(monkeypatch):
    from api_agent.workflows.tool_observations import ToolObservation
    journal, boundary = observed_journal()
    error = RuntimeError('synthetic owner failure')
    gate, _, grant = await verified_gate(Owner(error=error))
    original = ToolObservation.capture
    async def cancel_output(observation, direction, *args, **kwargs):
        if direction == 'output':
            raise asyncio.CancelledError()
        return await original(observation, direction, *args, **kwargs)
    monkeypatch.setattr(ToolObservation, 'capture', cancel_output)

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': boundary}):
        with pytest.raises(RuntimeError) as caught:
            await gate.execute(grant)

    assert caught.value is error
    packet = journal.finalize()
    assert packet['events'][-1]['outcome'] == 'failure'
    assert packet['gap'] == 'tool_evidence_unavailable'


@pytest.mark.asyncio
async def test_tool_outside_graph_keeps_success_when_optional_output_observer_fails(monkeypatch):
    from api_agent.workflows.tool_observations import ToolObservation
    owner = Owner()
    gate, _, grant = await verified_gate(owner)
    original = ToolObservation.capture
    async def fail_output(observation, direction, *args, **kwargs):
        if direction == 'output':
            raise RuntimeError('synthetic optional observer failure')
        return await original(observation, direction, *args, **kwargs)
    monkeypatch.setattr(ToolObservation, 'capture', fail_output)

    answer = await gate.execute(grant)

    assert answer.status == 'success'
    assert owner.received == [{'query': 'visible synthetic-secret'}]


@pytest.mark.parametrize('identity_field,leaf_prefix', [('cause_id', 'call '), ('tool_invocation_id', 'Tool ')])
def test_native_payload_cannot_move_to_a_different_node(identity_field, leaf_prefix):
    from api_agent.testing.workflow_steps import attach_payloads
    identifier = str(uuid4())
    parameters = ([{'name': 'tool_invocation_id', 'value': identifier}]
        if identity_field == 'tool_invocation_id' else [])
    leaf = {'name': leaf_prefix + identifier, 'parameters': parameters, 'steps': []}
    first = {'name': 'node First', 'parameters': [{'name': 'scope_id', 'value': 'first'}], 'steps': [leaf]}
    second = {'name': 'node Second', 'parameters': [{'name': 'scope_id', 'value': 'second'}], 'steps': []}
    capture = {'visit_id': 'second', identity_field: identifier}
    attachment = {'name': 'Request', 'source': 'canonical.json', 'type': 'application/json'}

    with pytest.raises(ValueError, match='exact node owner'):
        attach_payloads([first, second], [(capture, attachment)], {})


def test_native_tool_projection_is_idempotent_without_repeating_canonical_attachment():
    from api_agent.testing.workflow_steps import attach_payloads
    identifier = str(uuid4())
    leaf = {'name': 'Tool lookup', 'parameters': [{'name': 'tool_invocation_id', 'value': identifier}], 'steps': []}
    node = {'name': 'node Make', 'parameters': [{'name': 'scope_id', 'value': 'visit'}], 'steps': [leaf]}
    capture = {'visit_id': 'visit', 'tool_invocation_id': identifier}
    attachment = {'name': 'Tool request', 'source': 'canonical.json', 'type': 'application/json'}

    attach_payloads([node], [(capture, attachment)], {})
    attach_payloads([node], [(capture, attachment)], {})

    assert leaf['attachments'] == [attachment]
    duplicate_node = deepcopy(node)
    duplicate_node['parameters'] = [{'name': 'scope_id', 'value': 'other'}]
    with pytest.raises(ValueError, match='Ambiguous tool'):
        attach_payloads([node, duplicate_node], [], {})


@pytest.mark.parametrize('change', [
    {'extra': 'forbidden'}, {'phase': 'other'}, {'visit_id': ''},
    {'visit_id': 1}, {'attempt_id': ''}, {'attempt_id': 1},
    {'attempt_id': 'a' * 65}, {'invocation_id': 1},
    {'event_id': 'invalid'}, {'tool_invocation_id': 'INVALID'},
])
def test_canonical_tool_index_rejects_malformed_identity(change):
    from api_agent.testing.payloads import validate_payload_index
    occurrence = dict(kind='tool', invocation_id=str(uuid4()), event_id=str(uuid4()),
        visit_id='visit', attempt_id=None, tool_invocation_id=str(uuid4()), phase='request')
    occurrence.update(change)
    index = dict(version=1, test_id='test', entries=[dict(kind='omitted',
        test_id='test', occurrence=occurrence, reason='withheld')])
    with pytest.raises(ValueError, match='tool payload'):
        validate_payload_index(index, {})


@pytest.mark.parametrize('conflict', ['visit', 'attempt', 'invocation', 'phase'])
def test_canonical_tool_index_rejects_replayed_phase_or_changed_owner(conflict):
    from api_agent.testing.payloads import validate_payload_index
    occurrence = dict(kind='tool', invocation_id=str(uuid4()), event_id=str(uuid4()),
        visit_id='visit', attempt_id='attempt', tool_invocation_id=str(uuid4()), phase='request')
    second = dict(occurrence, event_id=str(uuid4()), phase='response')
    if conflict == 'visit':
        second['visit_id'] = 'other'
    elif conflict == 'attempt':
        second['attempt_id'] = 'other'
    elif conflict == 'invocation':
        second['invocation_id'] = str(uuid4())
    else:
        second['phase'] = 'request'
    index = dict(version=1, test_id='test', entries=[dict(kind='omitted',
        test_id='test', occurrence=row, reason='withheld') for row in (occurrence, second)])
    with pytest.raises(ValueError, match='Conflicting tool payload'):
        validate_payload_index(index, {})


def test_canonical_tool_index_requires_exact_native_node_owner():
    from api_agent.testing.payloads import validate_payload_index
    occurrence = dict(kind='tool', invocation_id=str(uuid4()), event_id=str(uuid4()),
        visit_id='visit', attempt_id=None, tool_invocation_id=str(uuid4()), phase='request')
    index = dict(version=1, test_id='test', entries=[dict(kind='payload',
        test_id='test', occurrence=occurrence, owner='other')])
    with pytest.raises(ValueError, match='lacks its node owner'):
        validate_payload_index(index, {})


@pytest.mark.asyncio
@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('status', ['partial', 'failure'])
async def test_selected_local_authority_scrubs_distinct_destination_boundaries_and_result_explanations(log, trace, status):
    journal, destination, tracing, sink = observed_destinations(log, trace)
    selected = EmissionBoundary(ContentPolicy(False), secrets=['selected-secret'])
    result = ToolResult(status, {'answer': 'visible selected-secret synthetic-secret'},
        errors=({'message': 'explanation selected-secret synthetic-secret', 'path': ['item', 2]},),
        diagnostic='diagnostic selected-secret synthetic-secret')
    class DetailedOwner(Owner):
        async def invoke(self, prepared):
            await super().invoke(prepared)
            return result
    owner = DetailedOwner()
    gate = ToolGate(owner, invocation_id='synthetic-external-call', tool_alias='selected-secret')
    prepared = await gate.prepare({'query': 'visible selected-secret synthetic-secret'})
    grant = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
    callbacks = []
    def subscriber(row):
        if row['event'] == 'tool_start':
            callbacks.append((selected._authority.evidence_leases, any(
                event.get('tool_invocation_id') == row['tool_invocation_id']
                and event.get('direction') == 'input' for event in journal.journal._events)))
            selected.register_secret('callback-secret')
    journal.journal.subscribe(subscriber)

    with bind_workflow_visit(journal, VISIT, capture_services={'emissions': selected}):
        actual = await gate.execute(grant)

    assert actual is result
    assert owner.received == [{'query': 'visible selected-secret synthetic-secret'}]
    assert callbacks == [(0, True)]
    packet = journal.finalize()
    captures = [row for row in packet['events'] if row['event'] == 'capture']
    assert len(captures) == 2
    assert 'tool_name' not in next(row for row in packet['events'] if row['event'] == 'tool_start')
    assert ('values' in captures[0]) is log
    assert ('values' in captures[1]) is log
    expected_output = {'status': status, 'data': {'answer': 'visible [REDACTED] [REDACTED]'},
        'errors': [{'message': 'explanation [REDACTED] [REDACTED]', 'path': ['item', 2]}],
        'diagnostic': 'diagnostic [REDACTED] [REDACTED]'}
    if log:
        assert captures[0]['values']['arguments'] == {'query': 'visible [REDACTED] [REDACTED]'}
        assert captures[1]['values'] == expected_output
    traced = [row for row in tracing.events if row.get('tool_invocation_id')]
    assert len(traced) == (2 if trace else 0)
    if trace:
        assert traced[0]['values']['arguments'] == {'query': 'visible [REDACTED] [REDACTED]'}
        assert traced[1]['values'] == expected_output
    assert all('selected-secret' not in str(rows) and 'synthetic-secret' not in str(rows)
        for rows in (packet, tracing.events, sink))
    assert selected._authority.evidence_leases == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('subscriber_error', [RuntimeError, asyncio.CancelledError])
async def test_withheld_tool_start_subscriber_failure_marks_loss_and_preserves_dispatch(subscriber_error):
    journal, _ = observed_journal()
    owner = Owner()
    gate, _, grant = await verified_gate(owner)
    observed = []
    def failing_start(row):
        observed.append(row['event'])
        if row['event'] == 'tool_start':
            raise subscriber_error('synthetic optional observer failure')
    journal.subscribe(failing_start)

    with bind_workflow_visit(journal, VISIT):
        result = await gate.execute(grant)

    assert result.status == 'success'
    assert result.data == {'answer': 'visible synthetic-secret'}
    assert owner.received == [{'query': 'visible synthetic-secret'}]
    assert observed == ['tool_start', 'capture', 'capture', 'tool_end']
    packet = journal.finalize()
    tools = [row for row in packet['events'] if row.get('tool_invocation_id')]
    assert [row['event'] for row in tools] == ['tool_start', 'capture', 'capture', 'tool_end']
    assert len({row['tool_invocation_id'] for row in tools}) == 1
    assert tools[-1]['outcome'] == 'success'
    captures = [row for row in tools if row['event'] == 'capture']
    assert all(row['reason'] == 'withheld' and 'values' not in row for row in captures)
    assert packet['gap'] == 'tool_evidence_unavailable'
    assert packet['completeness'] == 'partial'
    assert 'synthetic-secret' not in str(packet)
