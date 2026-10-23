from api_agent.telemetry.privacy import ContentPolicy
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from api_agent.workflows.evidence import attachment
from api_agent.workflows.journal import WorkflowJournal, validate_journal


def definition():
    node = SimpleNamespace(id='Make', kind='node', parent=None)
    layout = SimpleNamespace(steps=[node], tables={'Edges': []})
    resource = SimpleNamespace(kind='declaration', logical_path='flows/main.md.html', sha256='a' * 64,
                               content_bytes=b'# Workflow v2')
    return SimpleNamespace(graph_id='Example', definition_id='b' * 64, entry_root='base',
                           layout=layout, resources=[resource], replacements=[])


def test_journal_retains_implicit_structure_and_scrubs_before_artifact():
    journal = WorkflowJournal(definition(), secrets=('needle',))
    journal.observe({'event': 'scope_start', 'scope_id': 'scope', 'kind': 'graph',
                     'owner': 'GRAPH', 'started_ms': 1, 'input': 'needle'})
    journal.observe({'event': 'edge', 'source': 'START', 'target': 'Make',
                     'owner': 'GRAPH', 'implicit': True})
    journal.observe({'event': 'scope_start', 'scope_id': 'visit', 'parent_id': 'scope', 'kind': 'node',
                     'owner': 'Make', 'started_ms': 1})
    journal.observe({'event': 'node', 'scope_id': 'visit', 'node': 'Make'})
    journal.observe({'event': 'scope_end', 'scope_id': 'visit', 'parent_id': 'scope', 'kind': 'node',
                     'owner': 'Make', 'started_ms': 1, 'stopped_ms': 2,
                     'outcome': 'passed'})
    journal.observe({'event': 'edge', 'source': 'Make', 'target': 'END',
                     'owner': 'GRAPH', 'implicit': True, 'visit_id': 'visit'})
    journal.observe({'event': 'scope_end', 'scope_id': 'scope', 'kind': 'graph',
                     'owner': 'GRAPH', 'started_ms': 1, 'stopped_ms': 2,
                     'outcome': 'passed'})
    payload = journal.finalize()
    assert payload['completeness'] == 'complete'
    assert [row['sequence'] for row in payload['events']] == list(range(1, 8))
    assert payload['projection']['edges'][0]['target'] == 'Make'
    assert payload['content'] == 'withheld'
    assert 'needle' not in json.dumps(payload)
    title, data, mime = attachment(SimpleNamespace(text=json.dumps(payload)), None)
    assert json.loads(data) == payload
    assert mime == 'application/json'


def test_missing_terminal_event_remains_partial():
    journal = WorkflowJournal(definition())
    journal.observe({'event': 'scope_start', 'scope_id': 'scope', 'kind': 'graph',
                     'owner': 'GRAPH', 'started_ms': 1})
    payload = journal.finalize()
    assert payload['completeness'] == 'partial'
    assert validate_journal(payload) == payload


def test_changed_or_resequenced_artifact_rejected():
    journal = WorkflowJournal(definition())
    journal.observe({'event': 'edge', 'source': 'START', 'target': 'Make'})
    payload = journal.finalize()
    payload['events'][0]['sequence'] = 7
    with pytest.raises(ValueError, match='digest'):
        validate_journal(payload)


def test_exact_admitted_expression_is_recorded_and_tampering_rejected():
    selected = definition()
    expression = 'Make["route"] == "skip"'
    selected.layout.tables['Edges'] = [{'From': 'Make', 'To': 'END', 'When': expression}]
    journal = WorkflowJournal(selected)
    journal.observe({'event': 'scope_start', 'scope_id': 'scope', 'kind': 'graph',
                     'owner': 'GRAPH', 'started_ms': 1})
    journal.observe({'event': 'edge', 'source': 'START', 'target': 'Make',
                     'owner': 'GRAPH', 'condition': 'always', 'implicit': True})
    journal.observe({'event': 'scope_start', 'scope_id': 'visit', 'parent_id': 'scope',
                     'kind': 'node', 'owner': 'Make', 'started_ms': 1})
    journal.observe({'event': 'node', 'scope_id': 'visit', 'node': 'Make'})
    journal.observe({'event': 'scope_end', 'scope_id': 'visit', 'parent_id': 'scope',
                     'kind': 'node', 'owner': 'Make', 'started_ms': 1, 'stopped_ms': 2,
                     'outcome': 'passed'})
    journal.observe({'event': 'edge', 'source': 'Make', 'target': 'END', 'owner': 'GRAPH',
                     'condition': expression, 'implicit': False, 'visit_id': 'visit'})
    journal.observe({'event': 'scope_end', 'scope_id': 'scope', 'kind': 'graph',
                     'owner': 'GRAPH', 'started_ms': 1, 'stopped_ms': 2, 'outcome': 'passed'})

    packet = journal.finalize()
    assert packet['completeness'] == 'complete'
    assert validate_journal(packet, expected_definition=selected) == packet
    recorded = next(row for row in packet['events'] if row.get('condition') == expression)
    recorded['condition'] = 'Make["route"] != "skip"'
    packet['sha256'] = sha256(json.dumps({key: value for key, value in packet.items() if key != 'sha256'},
                                      sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with pytest.raises(ValueError, match='Undeclared workflow event edge'):
        validate_journal(packet)


@pytest.mark.parametrize('condition,diagnostic', [
    ('Make["route"] != "skip"', 'Undeclared workflow observation edge'),
    ('__import__("os").system("id")', 'Unsafe workflow observation condition'),
    (7, 'Unsafe workflow observation condition'),
])
def test_unadmitted_expression_cannot_be_observed(condition, diagnostic):
    selected = definition()
    selected.layout.tables['Edges'] = [{'From': 'Make', 'To': 'END', 'When': 'Make["route"] == "skip"'}]
    journal = WorkflowJournal(selected)

    with pytest.raises(ValueError, match=diagnostic):
        journal.observe({'event': 'edge', 'source': 'Make', 'target': 'END', 'owner': 'GRAPH',
                         'condition': condition, 'implicit': False})


def test_scrubbed_predicate_literal_cannot_misstate_executed_route():
    selected = definition()
    expression = 'Make["route"] == "skip"'
    selected.layout.tables['Edges'] = [{'From': 'Make', 'To': 'END', 'When': expression}]
    journal = WorkflowJournal(selected, secrets=('skip',))

    with pytest.raises(ValueError, match='condition label changed during export'):
        journal.observe({'event': 'edge', 'source': 'Make', 'target': 'END', 'owner': 'GRAPH',
                         'condition': expression, 'implicit': False})


def test_runtime_emits_implicit_entry_and_exit_edges():
    import asyncio
    from pathlib import Path
    from api_agent.providers.llm import LLMResult, Message, TextContent
    from api_agent.workflows.contrived import build

    async def complete(request):
        return LLMResult(Message('assistant', (TextContent(json.dumps({
            'original_input': 'hello', 'route': 'skip', 'ready': True,
            'final_output': 'hello',
        })),)))

    root = Path(__file__).resolve().parents[2]
    workflow = build(root, llm=complete, selection='example', trial='synthetic')
    journal = WorkflowJournal(workflow.definition)
    workflow.services['journal'] = journal
    asyncio.run(workflow.arun({'user_input': 'hello'}))
    payload = journal.finalize()
    assert payload['completeness'] == 'complete'
    assert any(row['event'] == 'edge' and row.get('source') == 'START'
               and row.get('owner') == 'GRAPH' for row in payload['events'])
    assert any(row['event'] == 'edge' and row.get('target') == 'END'
               and row.get('owner') == 'GRAPH' for row in payload['events'])


def test_missing_middle_route_stays_partial_even_with_terminal_events():
    first = SimpleNamespace(id='First', kind='node', parent=None)
    second = SimpleNamespace(id='Second', kind='node', parent=None)
    source = definition()
    source.layout.steps[:] = [first, second]
    journal = WorkflowJournal(source)
    journal.observe({'event': 'scope_start', 'scope_id': 'scope', 'kind': 'graph',
                     'owner': 'GRAPH', 'started_ms': 1})
    journal.observe({'event': 'edge', 'source': 'START', 'target': 'First', 'owner': 'GRAPH'})
    journal.observe({'event': 'edge', 'source': 'Second', 'target': 'END', 'owner': 'GRAPH'})
    journal.observe({'event': 'scope_end', 'scope_id': 'scope', 'kind': 'graph',
                     'owner': 'GRAPH', 'started_ms': 1, 'stopped_ms': 2})
    assert journal.finalize()['completeness'] == 'partial'


def test_leaf_call_requires_one_observed_node_visit():
    from uuid import uuid4
    journal = WorkflowJournal(definition())
    visit = str(uuid4())
    journal.observe({'event': 'scope_start', 'scope_id': visit, 'kind': 'node',
                     'owner': 'Make', 'started_ms': 1})
    call_id = journal.record_call(visit_id=visit,
        usage={'input_tokens': 3, 'output_tokens': 2, 'estimated_cost_usd': 0.01,
               'basis': 'public_rate_card'})
    assert call_id is not None
    payload = journal.finalize()
    assert validate_journal(payload) == payload
    assert [row['usage']['input_tokens'] for row in payload['events'] if row['event'] == 'call'] == [3]


def test_leaf_call_without_visit_rejected_at_artifact_boundary():
    journal = WorkflowJournal(definition())
    with pytest.raises(ValueError, match='open node visit'):
        journal.record_call(visit_id='missing', usage={'total_tokens': 2})


def test_node_context_call_is_noop_without_journal_and_uses_owner_when_present():
    from api_agent.workflows.execution import NodeContext
    from uuid import uuid4
    visit, call = str(uuid4()), str(uuid4())
    context = NodeContext('Make', (), (), {}, visit_id=visit)
    assert context.record_call(call_id=call) is None
    journal = WorkflowJournal(definition())
    journal.observe({'event': 'scope_start', 'scope_id': visit,
                     'kind': 'node', 'owner': 'Make', 'started_ms': 1})
    context.services['journal'] = journal
    assert context.record_call(call_id=call, usage={'input_tokens': 1}) == call
    retained = next(row for row in journal.finalize()['events'] if row['event'] == 'call')
    assert retained['visit_id'] == visit and retained['call_id'] == call


def test_explicit_content_capture_keeps_safe_schema_fields_and_scrubs_secret():
    visit = 'visit'
    schema = {'type': 'object', 'properties': {
        'question': {'type': 'string'}, 'api_token': {'type': 'string'},
        'metadata': {'type': 'object', 'properties': {'label': {'type': 'string'},
                                                    'continuation': {'type': 'string'}}}}}
    journal = WorkflowJournal(definition(), log_content=True, content_policy=ContentPolicy(True), secrets=('needle',))
    journal.capture(visit_id=visit, direction='input',
                    values={'question': 'hello needle', 'api_token': 'needle',
                            'metadata': {'label': 'safe', 'continuation': 'opaque'}},
                    schema=schema, plain_fields=('question',))
    captured = journal.finalize()['events'][0]
    assert captured['values']['question'] == 'hello [REDACTED]'
    assert 'metadata' not in captured['values']
    assert 'api_token' not in captured['values']
    assert 'needle' not in json.dumps(captured)


@pytest.mark.parametrize('prefix,secret', [
    ('', 'K' * 535),
    ('a' * 505, 'K' * 36),
    ('normal ', 'short-key'),
    ('é' * 505, 'K' * 36),
    ('%20' * 169, 'K' * 36),
])
def test_capture_scrubs_original_scalar_before_display_limit(prefix, secret):
    journal = WorkflowJournal(definition(), log_content=True, content_policy=ContentPolicy(True), secrets=(secret,))
    journal.capture(visit_id='visit', direction='output',
                    values={'answer': prefix + secret + ' end'},
                    schema={'type': 'object', 'properties': {'answer': {'type': 'string'}}},
                    plain_fields=('answer',))
    event = journal.finalize()['events'][0]
    assert event['values']['answer'] == (prefix + '[REDACTED]' + ' end')[:512]
    assert secret[:7] not in event['values']['answer']
    assert len(event['values']['answer']) <= 512


def test_capture_rejects_unbounded_classified_scalar():
    journal = WorkflowJournal(definition(), log_content=True, content_policy=ContentPolicy(True))
    with pytest.raises(ValueError, match='exceeds limit'):
        journal.capture(visit_id='visit', direction='input',
                        values={'question': 'x' * 65537},
                        schema={'type': 'object', 'properties': {'question': {'type': 'string'}}},
                        plain_fields=('question',))
    assert journal.finalize()['events'] == []


def test_capture_rejects_boundary_withheld_scalar(monkeypatch):
    journal = WorkflowJournal(definition(), log_content=True, content_policy=ContentPolicy(True))
    monkeypatch.setattr(journal._boundary, 'content_export', lambda _value: {})
    with pytest.raises(ValueError, match='value was withheld'):
        journal.capture(visit_id='visit', direction='input',
                        values={'question': 'ordinary'},
                        schema={'type': 'object', 'properties': {'question': {'type': 'string'}}},
                        plain_fields=('question',))
    assert journal.finalize()['events'] == []


def test_capture_scrubs_full_scalar_before_runner_attachment(monkeypatch):
    from types import SimpleNamespace
    from api_agent.workflows.evidence import attachment
    from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
    secret = 'long-credential-' + 'Z' * 520
    original = 'ordinary ' + secret
    journal = WorkflowJournal(definition(), log_content=True, content_policy=ContentPolicy(True), secrets=(secret,))
    seen = []
    scrub = journal._boundary.content_export

    def inspected(payload):
        if 'value' in payload:
            seen.append(payload['value'])
        return scrub(payload)

    monkeypatch.setattr(journal._boundary, 'content_export', inspected)
    journal.capture(visit_id='visit', direction='output', values={'answer': original},
                    schema={'type': 'object', 'properties': {'answer': {'type': 'string'}}},
                    plain_fields=('answer',))
    payload = journal.finalize()
    assert seen == [original]
    assert 'Z' * 7 not in json.dumps(payload)
    name, retained, media_type = attachment(SimpleNamespace(text=json.dumps(payload)),
        EmissionBoundary(ContentPolicy(True), secrets=(secret,)))
    assert name == 'Workflow execution evidence' and media_type == 'application/json'
    assert b'ZZZZZZZ' not in retained


def test_default_content_policy_records_withheld_reason():
    journal = WorkflowJournal(definition(), log_content=False)
    journal.capture(visit_id='visit', direction='output', values={'answer': 'sensitive'},
                    schema={'type': 'object', 'properties': {'answer': {'type': 'string'}}})
    captured = journal.finalize()['events'][0]
    assert captured['reason'] == 'withheld' and 'values' not in captured


@pytest.mark.parametrize('observe', [False, True])
def test_actual_inference_call_id_joins_node_visit(monkeypatch, tmp_path, observe):
    import asyncio
    from pathlib import Path
    from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
    from api_agent.testing.inference_usage import publish as publish_usage
    from api_agent.workflows.contrived import build
    from api_agent.workflows import provenance

    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_workflow.py::test_join (call)')
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    monkeypatch.setattr(provenance, 'committed_documents', lambda definition: None)
    monkeypatch.setattr(provenance, 'verified_documents', lambda definition, **kwargs: None)
    monkeypatch.setattr('api_agent.workflows.packaged_provenance.installed_proof',
                        lambda definition, **kwargs: None)
    # The runtime imports the same function directly for its pre-effect gate.
    calls = []

    async def complete(request):
        call_id = publish_usage(request)
        calls.append(call_id)
        result = LLMResult(Message('assistant', (TextContent(json.dumps({
            'original_input': 'hello', 'route': 'skip', 'ready': True,
            'final_output': 'hello'})),)), Usage(input_tokens=3, output_tokens=2))
        publish_usage(request, result, call_id=call_id)
        return result

    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '0')
    workflow = build(Path(__file__).resolve().parents[2], llm=complete,
                     selection='example', trial='synthetic')
    rows = []
    if observe:
        from api_agent.workflows.observations import WorkflowObservationFactory
        from api_agent.telemetry.privacy import EmissionBoundary
        workflow.services['observation_factory'] = WorkflowObservationFactory(
            {}, boundary=EmissionBoundary(), sink=rows.append)
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    asyncio.run(workflow.arun({'user_input': 'hello'}))
    packets = [json.loads(line) for line in path.read_text().splitlines()]
    call_rows = [packet['data'] for packet in packets if packet['kind'] == 'inference_usage'
                 and packet['data']['usage'] is not None]
    journal = next(packet['data'] for packet in packets if packet['kind'] == 'workflow_evidence')
    assert journal['completeness'] == 'complete'
    if observe:
        assert rows
        assert [row['event_id'] for row in rows] == [row['event_id'] for row in journal['events']]
    assert any(row['event'] == 'attempt' for row in journal['events'])
    assert any(row['event'] == 'guard' for row in journal['events'])
    from copy import deepcopy
    from hashlib import sha256
    for omitted in ('attempt', 'guard', 'node'):
        mutant = deepcopy(journal)
        mutant['events'] = [row for row in mutant['events'] if row['event'] != omitted]
        for sequence, row in enumerate(mutant['events'], 1):
            row['sequence'] = sequence
        mutant['sha256'] = sha256(json.dumps({key: value for key, value in mutant.items()
            if key != 'sha256'}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with pytest.raises(ValueError, match='completeness'):
            validate_journal(mutant)
    linked = [row for row in journal['events'] if row['event'] == 'call']
    assert len(calls) == len(linked) == 1
    assert linked[0]['call_id'] == calls[0] == call_rows[0]['call_id']
    assert call_rows[0]['turn_id'] == journal['turn_id']
    assert linked[0]['visit_id'] in {row['scope_id'] for row in journal['events']
                                     if row['event'] == 'scope_start' and row.get('kind') == 'node'}


@pytest.mark.parametrize('selection',['x'*201,'invalid\nmetadata',123])
def test_unavailable_optional_model_metadata_is_omitted_without_losing_call(selection):
    journal = WorkflowJournal(definition())
    journal.observe(dict(event='scope_start',scope_id='visit',kind='node',owner='Make'))
    journal.record_call(visit_id='visit',selection=selection)
    row = journal.finalize()['events'][-1]
    assert row['event']=='call' and row['selection'] is None
    assert row['reason']=='selection_unavailable'


def test_explicit_observation_invalid_selection_remains_contract_error():
    journal = WorkflowJournal(definition())
    with pytest.raises(ValueError,match='Unsafe workflow observation selection'):
        journal.observe(dict(event='node',selection='invalid\nmetadata'))


@pytest.mark.parametrize('authority',[None,ContentPolicy(False),ContentPolicy(True)])
@pytest.mark.parametrize('requested',[False,True])
def test_standalone_journal_requires_explicit_content_authority(authority,requested):
    journal = WorkflowJournal(definition(),log_content=requested,content_policy=authority)
    permitted = requested and authority is not None and authority.allow_content
    assert journal.finalize()['content']==('permitted_scrubbed' if permitted else 'withheld')


@pytest.mark.parametrize('authority',[True,{},'enabled'])
def test_standalone_journal_rejects_untyped_content_authority(authority):
    with pytest.raises(TypeError,match='explicit policy'):
        WorkflowJournal(definition(),log_content=True,content_policy=authority)


def test_untrusted_selection_is_withheld_before_direct_journal_subscriber_and_retention():
    journal = WorkflowJournal(definition())
    rows = []
    journal.subscribe(rows.append)

    journal.observe({'event': 'node', 'node': 'Make', 'selection': 'customer_private_question'})

    retained = journal.finalize()
    assert rows[0]['selection'] is None
    assert rows[0]['reason'] == 'selection_unavailable'
    assert 'customer_private_question' not in repr(rows) + repr(retained)


@pytest.mark.parametrize('result', ['truthy', 'failure'])
def test_selection_authority_must_return_exact_true_without_failure(result):
    def authority(value):
        if result == 'failure':
            raise RuntimeError('metadata unavailable')
        return 'yes'
    journal = WorkflowJournal(definition(), model_identity=authority)

    journal.observe({'event': 'node', 'node': 'Make', 'selection': 'customer_private_question'})

    assert 'customer_private_question' not in repr(journal.finalize())
