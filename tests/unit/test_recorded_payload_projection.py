"""Native evidence links retain occurrence ownership and original journal data."""
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.telemetry.privacy import ContentPolicy
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.payloads import PayloadResolver, hydrate_journal
from api_agent.testing.recorded import run_recorded_integration
from api_agent.testing.workflow_steps import attach_payloads, scope_steps
from api_agent.workflows.journal import WorkflowJournal


def packet(call, turn, test_id):
    node = SimpleNamespace(id='Make', kind='node', parent=None)
    definition = SimpleNamespace(graph_id='Example', definition_id='b' * 64, entry_root='base',
        layout=SimpleNamespace(steps=[node], tables={'Edges': []}), resources=[], replacements=[])
    journal = WorkflowJournal(definition, log_content=True, content_policy=ContentPolicy(True), turn_id=turn, test_id=test_id)
    graph_id, node_id = str(uuid4()), str(uuid4())
    graph = dict(scope_id=graph_id, kind='graph', owner='GRAPH', parent_id=None, started_ms=1)
    node = dict(scope_id=node_id, kind='node', owner='Make', parent_id=graph_id, started_ms=2)
    journal.observe(dict(event='scope_start', **graph))
    journal.observe(dict(event='edge',source='START',target='Make',owner='GRAPH',implicit=True))
    journal.observe(dict(event='scope_start', **node))
    wire = {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}],
            'metadata': {'nested': [1, True, None]}}
    journal.capture_json(visit_id=node_id, direction='input',
        values={'domain': {'nested': [None, [1, True]], 'long': 'x' * 800}}, schema={'type': 'object'})
    journal.capture_json(visit_id=node_id, direction='input', values={'llm_request': wire},
                         schema={'type': 'object'}, cause_id=call)
    journal.record_call(visit_id=node_id, call_id=call, outcome='failed')
    response = {'output': {'text': 'partial', 'items': [None, False]}}
    journal.capture_json(visit_id=node_id, direction='output', values={'llm_response': response},
        schema={'type': 'object'}, cause_id=call, capture_reason='partial')
    journal.capture_json(visit_id=node_id, direction='output', values={'result': [None, {'ok': False}]},
        schema={'type': 'object'})
    journal.observe(dict(event='scope_end', stopped_ms=3, outcome='passed', **node))
    journal.observe(dict(event='edge',source='Make',target='END',owner='GRAPH',implicit=True))
    journal.observe(dict(event='scope_end', stopped_ms=4, outcome='passed', **graph))
    return journal.finalize(), wire, response


@pytest.mark.parametrize('wire_first', [True, False])
@pytest.mark.parametrize('early_result', [True, False])
def test_recording_uses_one_wire_file_for_native_node_and_llm_attachments(tmp_path, wire_first, early_result):
    test = TestIdentity('base::test.py::test_graph', 'base', 'test.py', 'test.py::test_graph')
    outcome = TestResult(test, 'passed', .01)
    calls = [str(uuid4()), str(uuid4())]
    packets = [packet(call, str(uuid4()), test.node_id) for call in calls]
    orphan = str(uuid4())
    snapshots = []

    def runner(context, tests, **hooks):
        hooks['before_run']()
        reported = False
        for call, (journal, wire, response) in zip(calls, packets):
            events = [('workflow_evidence', journal), ('inference_content',
                dict(call_id=call, phase='request', content=wire, reason='captured')),
                ('inference_content', dict(call_id=call, phase='response', content=response, reason='partial'))]
            if wire_first:
                events.reverse()
            for kind, data in events:
                hooks['on_event'](RunnerEvent(kind, 'base', test, text=json.dumps(data)))
                if kind == 'workflow_evidence':
                    hooks['on_event'](RunnerEvent(kind, 'base', test, text=json.dumps(data)))
                if early_result and not reported:
                    hooks['on_result'](outcome)
                    reported = True
        hooks['on_event'](RunnerEvent('inference_content', 'base', test, text=json.dumps(
            dict(call_id=orphan, phase='request', content=packets[0][1], reason='captured'))))
        hooks['on_event'](RunnerEvent('browser_attachment', 'base', test,
            attachment=('Browser evidence', b'{"visible":true}', 'application/json')))
        if not reported:
            hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    context = SimpleNamespace(base_root=tmp_path/'base', owner_root=tmp_path/'base')
    result = run_recorded_integration(context, [test], dict(TUI_MODE='integration',
        ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path/'results', LOG_CONTENT=True),
        accepted=True, runner=runner, storage=SimpleNamespace(persist=lambda *args: None),
        on_snapshot=snapshots.append)
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    assert row['status'] == 'passed'
    browser = next(item for item in row['attachments'] if item['name'] == 'Browser evidence')
    assert json.loads((result.directory/browser['source']).read_bytes()) == {'visible': True}
    assert len(row['steps']) == 2
    sources = []
    for graph in row['steps']:
        node = graph['steps'][0]
        assert node['name'] == 'node Make' and node['steps'] == []
        assert [item['name'] for item in node['attachments']][0] == 'Input'
        assert graph['status'] == 'failed' and node['status'] == 'failed'
        assert len(node['attachments']) == 4
        assert node['attachments'][1]['name'].startswith('LLM request')
        assert node['attachments'][2]['name'].startswith('LLM response')
        domain = json.loads((result.directory/node['attachments'][0]['source']).read_bytes())
        assert domain == {'domain': {'nested': [None, [1, True]], 'long': 'x' * 800}}
        output = json.loads((result.directory/node['attachments'][3]['source']).read_bytes())
        assert output == {'result': [None, {'ok': False}]}
        sources.append(node['attachments'][1]['source'])
    assert sources[0] != sources[1]  # Equal text is a distinct call occurrence.
    orphan_attachment = next(item for item in row['attachments'] if orphan in item['name'])
    assert orphan_attachment['source'] not in sources
    index = next(item for item in row['attachments'] if item['name'] == 'Payload index')
    assert len(json.loads((result.directory/index['source']).read_bytes())['entries']) == 9
    resolver = PayloadResolver.local(result.directory, result.manifest)
    resolver.index(index['source'])
    durable = [json.loads((result.directory/item['source']).read_bytes()) for item in row['attachments']
               if item['name'] == 'Workflow execution evidence']
    assert [hydrate_journal(item, resolver) for item in durable] == [item[0] for item in packets]
    assert all(source in snapshots[-1].files for source in sources)
    assert not any(source in snapshots[0].files for source in sources)
    early = json.loads(next(body for name, body in snapshots[0].files.items() if name.endswith('-result.json')))
    assert {name: early[name] for name in ('uuid', 'status', 'start', 'stop')} == {
        name: row[name] for name in ('uuid', 'status', 'start', 'stop')}
    assert len(list(result.directory.glob('payload-index-*.json'))) == 1
    joined = json.loads((result.directory/'workflow-accounting-join.json').read_bytes())
    assert len(joined['tests'][0]['routes']) == 2
    assert joined['tests'][0]['duplicate_journal_call_ids'] == []
    assert all(json.loads((result.directory/source).read_bytes()) == packets[0][1] for source in sources)


def test_attachment_projection_rejects_ambiguous_call_ownership():
    identifier = str(uuid4())
    step = dict(name='node Make', parameters=[dict(name='call '+identifier+' outcome', value='completed')], steps=[])
    with pytest.raises(ValueError, match='Ambiguous payload call owner'):
        attach_payloads([step, deepcopy(step)], [], {identifier: {}})


def test_timed_call_attachments_preserve_execution_tree_and_json_as_data():
    call = str(uuid4())
    leaf = dict(name='call '+call, status='failed', start=2, stop=3, parameters=[], steps=[])
    node = dict(name='node Make', status='failed', start=1, stop=4,
        parameters=[dict(name='scope_id', value=str(uuid4()))], steps=[leaf])
    before = deepcopy(node)
    reference = dict(name='LLM response', source='payload.json', type='application/json')
    assert attach_payloads([node], [], {call: {'response': reference}}) == []
    assert leaf['attachments'] == [reference]
    leaf.pop('attachments')
    assert node == before


def test_capture_projection_rejects_missing_and_ambiguous_scope():
    visit = str(uuid4())
    event = dict(visit_id=visit)
    reference = dict(name='Input', source='payload.json', type='application/json')
    with pytest.raises(ValueError, match='no observed scope'):
        attach_payloads([], [(event, reference)], {})
    step = dict(name='node Make', parameters=[dict(name='scope_id', value=visit)], steps=[])
    with pytest.raises(ValueError, match='Ambiguous payload scope'):
        attach_payloads([step, deepcopy(step)], [(event, reference)], {})


@pytest.mark.parametrize('legacy', [False, True])
def test_parent_rejects_content_bearing_workflow_under_disabled_policy(tmp_path, legacy):
    test = TestIdentity('base::test.py::test_graph', 'base', 'test.py', 'test.py::test_graph')
    journal, _, _ = packet(str(uuid4()), str(uuid4()), test.node_id)
    evidence = {'events': [{'event': 'capture', 'values': {'text': 'retain-no-content'}}]} if legacy else journal
    context = SimpleNamespace(base_root=tmp_path/'base', owner_root=tmp_path/'base')

    def runner(context, tests, **hooks):
        hooks['on_event'](RunnerEvent('workflow_evidence', 'base', test, text=json.dumps(evidence)))

    with pytest.raises(ValueError, match='Workflow content violates recording policy'):
        run_recorded_integration(context, [test], dict(TUI_MODE='integration',
            ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path/'results', LOG_CONTENT=False),
            accepted=True, runner=runner, storage=SimpleNamespace(persist=lambda *args: None))
    assert list((tmp_path/'results').rglob('payload-*.json')) == []


def test_conflicting_repeat_invocation_rejects_before_payload_materialization(tmp_path):
    import hashlib
    from api_agent.workflows.journal import _encoded
    test = TestIdentity('base::test.py::test_graph', 'base', 'test.py', 'test.py::test_graph')
    journal, _, _ = packet(str(uuid4()), str(uuid4()), test.node_id)
    changed = deepcopy(journal)
    capture = next(event for event in changed['events'] if 'values' in event)
    capture['values']['domain']['long'] = 'different'
    changed['sha256'] = hashlib.sha256(_encoded({key: value for key, value in changed.items() if key != 'sha256'})).hexdigest()
    context = SimpleNamespace(base_root=tmp_path/'base', owner_root=tmp_path/'base')

    def runner(context, tests, **hooks):
        for evidence in (journal, changed):
            hooks['on_event'](RunnerEvent('workflow_evidence', 'base', test, text=json.dumps(evidence)))

    with pytest.raises(ValueError, match='Conflicting workflow payload invocation'):
        run_recorded_integration(context, [test], dict(TUI_MODE='integration',
            ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path/'results', LOG_CONTENT=True),
            accepted=True, runner=runner, storage=SimpleNamespace(persist=lambda *args: None))
    assert list((tmp_path/'results').rglob('payload-*.json')) == []


@pytest.mark.parametrize('reason', ['withheld', 'unavailable', 'truncated'])
def test_omitted_wire_phase_retains_reason_without_a_payload_file(tmp_path, reason):
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    outcome = TestResult(test, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_result'](outcome)
        hooks['on_event'](RunnerEvent('inference_content', identity=test, text=json.dumps(
            dict(call_id=str(uuid4()), phase='response', content={}, reason=reason))))
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [test], config, accepted=True, runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    index = next(item for item in row['attachments'] if item['name'] == 'Payload index')
    entries = json.loads((result.directory/index['source']).read_bytes())['entries']
    assert len(entries) == 1 and entries[0]['reason'] == reason and entries[0]['kind'] == 'omitted'
    assert not any(item['name'].startswith('Unattached LLM') for item in row['attachments'])
    assert not any(path.name.startswith('payload-') and not path.name.startswith('payload-index-')
                   for path in result.directory.iterdir())


@pytest.mark.parametrize('capture_first', [False, True])
def test_cross_invocation_capture_cannot_reassign_a_call_before_materialization(tmp_path, capture_first):
    import hashlib
    from api_agent.workflows.journal import _encoded
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    call = str(uuid4())
    owned, wire, response = packet(call, str(uuid4()), test.node_id)
    conflicting, _, _ = packet(call, str(uuid4()), test.node_id)
    conflicting['events'] = [event for event in conflicting['events'] if event['event'] != 'call']
    for sequence, event in enumerate(conflicting['events'], 1):
        event['sequence'] = sequence
    conflicting['sha256'] = hashlib.sha256(_encoded({key: value for key, value in conflicting.items() if key != 'sha256'})).hexdigest()
    outcome = TestResult(test, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for phase, content in [('request', wire), ('response', response)]:
            hooks['on_event'](RunnerEvent('inference_content', identity=test, text=json.dumps(
                dict(call_id=call, phase=phase, content=content))))
        journals = (conflicting, owned) if capture_first else (owned, conflicting)
        for evidence in journals:
            hooks['on_event'](RunnerEvent('workflow_evidence', identity=test, text=json.dumps(evidence)))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    with pytest.raises(ValueError, match='correlation.*owner'):
        run_recorded_integration(context, [test], config, accepted=True, runner=runner, storage=Storage())
    assert list(config['ALLURE_RESULTS_DIR'].rglob('workflow-*.json')) == []
    assert list(config['ALLURE_RESULTS_DIR'].rglob('payload-index-*.json')) == []


def test_native_projection_rejects_duplicate_timed_call_owners():
    call = str(uuid4())
    step = dict(name='call '+call, parameters=[], steps=[])
    with pytest.raises(ValueError, match='Ambiguous payload call owner'):
        attach_payloads([step, deepcopy(step)], [], {})


def test_native_projection_rejects_mixed_timed_and_untimed_call_owners():
    call = str(uuid4())
    timed = dict(name='call '+call, parameters=[], steps=[])
    untimed = dict(name='node Make', parameters=[dict(name='call '+call+' outcome', value='completed')], steps=[])
    reference = dict(name='Request', source='payload.json', type='application/json')
    with pytest.raises(ValueError, match='Ambiguous payload call owner'):
        attach_payloads([timed, untimed], [], {call: {'request': reference}})


@pytest.mark.parametrize('conflicting', [False, True])
def test_repeated_wire_occurrence_is_idempotent_or_rejects_changed_content(tmp_path, conflicting):
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    call = str(uuid4())
    outcome = TestResult(test, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for text in ('first', 'changed' if conflicting else 'first'):
            hooks['on_event'](RunnerEvent('inference_content', identity=test, text=json.dumps(
                dict(call_id=call, phase='request', content={'text': text}))))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    if conflicting:
        with pytest.raises(ValueError, match='Conflicting payload occurrence'):
            run_recorded_integration(context, [test], config, accepted=True, runner=runner, storage=Storage())
    else:
        result = run_recorded_integration(context, [test], config, accepted=True, runner=runner, storage=Storage())
        row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
        payloads = [item for item in row['attachments'] if call in item['name']]
        assert len(payloads) == 1
        assert json.loads((result.directory/payloads[0]['source']).read_bytes()) == {'text': 'first'}
    payload_files = [path for path in config['ALLURE_RESULTS_DIR'].rglob('payload-*.json')
                     if not path.name.startswith('payload-index-')]
    assert len(payload_files) == 1
    assert json.loads(payload_files[0].read_bytes()) == {'text': 'first'}


@pytest.mark.parametrize('phase', ['request', 'response'])
@pytest.mark.parametrize('reason', ['truncated', 'unavailable'])
@pytest.mark.parametrize('legacy_wrapper', [False, True])
def test_omitted_journal_wire_phase_preserves_completed_accounting_and_exact_history(
        tmp_path, phase, reason, legacy_wrapper):
    _record_omitted_journal(tmp_path, phase, reason, legacy_wrapper=legacy_wrapper, enabled=True)


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('phase', ['request', 'response'])
def test_withheld_journal_wire_phase_preserves_completed_accounting_for_both_flags(tmp_path, enabled, phase):
    _record_omitted_journal(tmp_path, phase, 'withheld', legacy_wrapper=False, enabled=enabled)


def _record_omitted_journal(tmp_path, phase, reason, *, legacy_wrapper, enabled):
    import hashlib
    from api_agent.workflows.journal import _encoded, validate_journal
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = enabled
    call, turn = str(uuid4()), str(uuid4())
    journal, _, _ = packet(call, turn, test.node_id)
    direction = 'input' if phase == 'request' else 'output'
    journal['events'] = [event for event in journal['events'] if event['event'] != 'capture'
                         or event.get('cause_id') == call and event['direction'] == direction]
    for sequence, event in enumerate(journal['events'], 1):
        event['sequence'] = sequence
        if event['event'] == 'call':
            event['outcome'] = 'completed'
        if event['event'] == 'capture':
            event['reason'] = reason
            if reason == 'withheld':
                event.pop('values')
            else:
                event['values'] = {('llm_request' if phase == 'request' else 'llm_response'): {}} if legacy_wrapper else {}
    journal['content'] = 'permitted_scrubbed' if enabled else 'withheld'
    journal['sha256'] = hashlib.sha256(_encoded({key: value for key, value in journal.items() if key != 'sha256'})).hexdigest()
    validate_journal(journal)
    outcome = TestResult(test, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_result'](outcome)
        hooks['on_event'](RunnerEvent('workflow_evidence', identity=test, text=json.dumps(journal)))
        hooks['on_event'](RunnerEvent('inference_content', identity=test, text=json.dumps(
            dict(call_id=call, phase=phase, content={}, reason=reason))))
        hooks['on_event'](RunnerEvent('inference_usage', identity=test, text=json.dumps(dict(
            call_id=call, selection='synthetic', turn_id=turn, outcome='completed',
            usage=dict(input_tokens=3, output_tokens=2, cached_input_tokens=0, reasoning_tokens=0)))))
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [test], config, accepted=True, runner=runner, storage=Storage())
    assert result.persisted and result.exit_code == 0 and result.manifest['status'] == 'completed'
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    assert row['status'] == 'passed' and row['steps'][0]['status'] == 'passed'
    index = next(item for item in row['attachments'] if item['name'] == 'Payload index')
    entries = json.loads((result.directory/index['source']).read_bytes())['entries']
    assert all(item['kind'] == 'omitted' and item['reason'] == reason for item in entries)
    assert not any(path.name.startswith('payload-') and not path.name.startswith('payload-index-')
                   for path in result.directory.iterdir())
    resolver = PayloadResolver.local(result.directory, result.manifest)
    resolver.index(index['source'])
    durable = next(item for item in row['attachments'] if item['name'] == 'Workflow execution evidence')
    assert hydrate_journal(json.loads((result.directory/durable['source']).read_bytes()), resolver) == journal
    accounting = json.loads((result.directory/'accounting.json').read_bytes())
    recorded_call = accounting['tests'][0]['calls'][0]
    assert recorded_call['call_id'] == call and recorded_call['outcome'] == 'completed'
    assert recorded_call['usage']['input_tokens'] == 3 and recorded_call['usage']['output_tokens'] == 2


def test_untimed_wire_placement_replaces_only_same_owner_source_and_mime():
    call = str(uuid4())
    shared = dict(name='Input', source='shared.json', type='application/json')
    distinct = dict(name='Input', source='other-occurrence.json', type='application/json')
    different_mime = dict(name='Input', source='shared.json', type='text/plain')
    node = dict(name='node Make', steps=[], parameters=[dict(name='call '+call+' outcome', value='completed')],
        attachments=[shared, distinct, dict(shared, name='Another label'), different_mime])
    specific = dict(shared, name='LLM request ('+call+')')
    assert attach_payloads([node], [], {call: {'request': specific}}) == []
    assert node['attachments'] == [specific, distinct, different_mime]


def test_shared_wire_remains_visible_on_parent_capture_and_separate_timed_call():
    call, visit = str(uuid4()), str(uuid4())
    leaf = dict(name='call '+call, steps=[], parameters=[])
    node = dict(name='node Make', steps=[leaf], parameters=[dict(name='scope_id', value=visit)])
    captured = dict(name='Input', source='shared.json', type='application/json')
    specific = dict(captured, name='LLM request ('+call+')')
    assert attach_payloads([node], [(dict(visit_id=visit), captured)], {call: {'request': specific}}) == []
    assert node['attachments'] == [captured] and leaf['attachments'] == [specific]
