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


def packet(call, turn, test_id, *, wire=None, response=None):
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
    wire = wire if wire is not None else {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}],
                                           'metadata': {'nested': [1, True, None]}}
    journal.capture_json(visit_id=node_id, direction='input',
        values={'domain': {'nested': [None, [1, True]], 'long': 'x' * 800}}, schema={'type': 'object'})
    journal.capture_json(visit_id=node_id, direction='input', values={'llm_request': wire},
                         schema={'type': 'object'}, cause_id=call)
    journal.record_call(visit_id=node_id, call_id=call, outcome='failed')
    response = response if response is not None else {'output': {'text': 'partial', 'items': [None, False]}}
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
    browser_body = {'status': 'unavailable', 'phase': 'success', 'context': 1, 'page': 1}

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
            attachment=('UI success context 1 page 1', json.dumps(browser_body).encode(), 'application/json')))
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
    browser = next(item for item in row['attachments'] if item['name'] == 'UI success context 1 page 1')
    assert json.loads((result.directory/browser['source']).read_bytes()) == browser_body
    assert len(row['steps']) == 2
    sources = []
    for graph in row['steps']:
        node = graph['steps'][0]
        assert node['name'] == 'node Make' and len(node['steps']) == 1
        call = node['steps'][0]
        assert call['name'].startswith('call ') and 'start' not in call and 'stop' not in call
        assert [item['name'] for item in node['attachments']] == ['Input', 'Output']
        assert graph['status'] == node['status'] == call['status'] == 'failed'
        assert len(call['attachments']) == 2
        assert call['attachments'][0]['name'].startswith('LLM request')
        assert call['attachments'][1]['name'].startswith('LLM response')
        assert not any(item['name'].startswith(('frozen_call_', 'call ')) for item in node['parameters'])
        domain = json.loads((result.directory/node['attachments'][0]['source']).read_bytes())
        assert domain == {'domain': {'nested': [None, [1, True]], 'long': 'x' * 800}}
        output = json.loads((result.directory/node['attachments'][1]['source']).read_bytes())
        assert output == {'result': [None, {'ok': False}]}
        sources.append(call['attachments'][0]['source'])
    assert sources[0] != sources[1]  # Equal text is a distinct call occurrence.
    orphan_attachment = next(item for item in row['attachments'] if orphan in item['name'])
    assert orphan_attachment['source'] not in sources
    index = next(item for item in row['attachments'] if item['name'] == 'Payload index')
    assert len(json.loads((result.directory/index['source']).read_bytes())['entries']) == 9
    resolver = PayloadResolver.local(result.directory, result.manifest)
    resolver.index(index['source'])
    assert not any(item['name'] == 'Workflow execution evidence' for item in row['attachments'])
    durable = [json.loads((result.directory/item['source']).read_bytes()) for graph in row['steps']
               for item in graph['attachments'] if item['name'] == 'Execution evidence']
    assert all(next(item['value'] for item in graph['parameters'] if item['name'] == 'invocation_id')
               == durable[index]['invocation_id'] for index, graph in enumerate(row['steps']))
    assert {item['invocation_id']: hydrate_journal(item, resolver) for item in durable} == {
        item[0]['invocation_id']: item[0] for item in packets}
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


def test_chunked_native_parts_follow_their_call_and_keep_order(tmp_path):
    from api_agent.testing.payloads import encoded
    from test_recorded_integration import fixture, Storage

    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    calls = [str(uuid4()), str(uuid4())]
    values = {call: {'request': {'text': 'é' * 120000}, 'response': {'text': call * 7000}}
              for call in calls}
    journals = [packet(call, str(uuid4()), test.node_id, wire=values[call]['request'],
                       response=values[call]['response'])[0] for call in calls]
    outcome = TestResult(test, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for call, journal in zip(calls, journals):
            hooks['on_event'](RunnerEvent('workflow_evidence', identity=test, text=json.dumps(journal)))
            for phase, value in values[call].items():
                hooks['on_event'](RunnerEvent('inference_content', identity=test, text=json.dumps(
                    dict(call_id=call, phase=phase, content=value))))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [test], config, accepted=True, runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    resolver = PayloadResolver.local(result.directory, result.manifest)
    index = next(item for item in row['attachments'] if item['name'] == 'Payload index')
    resolver.index(index['source'])
    for call, journal in zip(calls, journals):
        graph = next(graph for graph in row['steps'] if any(
            item['name'] == 'invocation_id' and item['value'] == journal['invocation_id']
            for item in graph['parameters']))
        evidence = next(item for item in graph['attachments'] if item['name'] == 'Execution evidence')
        durable = json.loads((result.directory / evidence['source']).read_bytes())
        assert hydrate_journal(durable, resolver) == journal
        assert sum(event.get('cause_id') == call for event in durable['events']) == 2
        node = graph['steps'][0]
        leaf = next(step for step in node['steps'] if step['name'] == 'call ' + call)
        assert 'start' not in leaf and 'stop' not in leaf
        attachments = leaf['attachments']
        assert not any(item['name'].startswith('LLM ') for item in node.get('attachments', []))
        for phase, value in values[call].items():
            parts = [item for item in attachments if item['name'].startswith(f'LLM {phase} ({call})')]
            assert len(parts) == 2
            assert [item['name'].rsplit(' — ', 1)[1] for item in parts] == ['text part 1 of 2', 'text part 2 of 2']
            assert all(item['type'] == 'text/plain' for item in parts)
            bodies = [(result.directory / item['source']).read_bytes() for item in parts]
            assert all(len(body) <= 200 * 1024 and body.decode('utf-8') for body in bodies)
            assert b''.join(bodies) == encoded(value)
        assert not any(other in item['name'] for other in calls if other != call for item in attachments)


@pytest.mark.parametrize('lengths', [(2 * 1024 * 1024, 2 * 1024 * 1024),
                                    (1, 4 * 1024 * 1024 - 20)])
def test_wire_budget_exhaustion_omits_only_affected_occurrence(tmp_path, lengths):
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    calls = [str(uuid4()), str(uuid4())]
    outcome = TestResult(test, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for call, length in zip(calls, lengths):
            hooks['on_event'](RunnerEvent('inference_content', identity=test, text=json.dumps(
                dict(call_id=call, phase='request', content={'text': 'x' * length}))))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [test], config, accepted=True, runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    index = next(item for item in row['attachments'] if item['name'] == 'Payload index')
    entries = json.loads((result.directory / index['source']).read_bytes())['entries']
    assert entries[0]['kind'] in {'payload', 'chunked'}
    assert entries[1] == dict(kind='omitted', test_id=test.id,
                              occurrence=dict(kind='llm', call_id=calls[1], phase='request'),
                              reason='truncated')
    assert any(calls[0] in item['name'] for item in row['attachments'])
    assert not any(calls[1] in item['name'] for item in row['attachments'])
    notice = next(item for item in row['attachments'] if item['name'] == 'Incomplete LLM evidence')
    assert 'truncated' in (result.directory / notice['source']).read_text()
    assert row['status'] == 'passed'


@pytest.mark.parametrize('reason', ['truncated', 'unavailable'])
def test_transport_incomplete_evidence_is_visible_without_changing_test_outcome(tmp_path, reason):
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    outcome = TestResult(test, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for _ in range(2):
            hooks['on_event'](RunnerEvent('inference_evidence_incomplete', identity=test, text=reason))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [test], config, accepted=True, runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    notices = [item for item in row['attachments'] if item['name'] == 'Incomplete LLM evidence']
    assert len(notices) == 1 and notices[0]['type'] == 'text/plain'
    assert reason in (result.directory / notices[0]['source']).read_text()
    assert row['status'] == 'passed' and result.exit_code == 0


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


@pytest.mark.parametrize(('reason', 'diagnostic_count'), [
    ('withheld', 0), ('unavailable', 1), ('truncated', 0),
])
def test_omitted_wire_phase_retains_reason_without_a_payload_file(tmp_path, reason, diagnostic_count):
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
    diagnostics = [item for item in row['attachments'] if 'LLM response diagnostics' in item['name']]
    assert len(diagnostics) == diagnostic_count
    assert all(json.loads((result.directory/item['source']).read_bytes())['capture_reason'] == reason
               for item in diagnostics)
    assert not any(item['name'].startswith('Unattached LLM') and item not in diagnostics
                   for item in row['attachments'])
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
    assert not any(item['name'] == 'Workflow execution evidence' for item in row['attachments'])
    durable = next(item for item in row['steps'][0]['attachments'] if item['name'] == 'Execution evidence')
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


def test_omitted_wire_creates_verified_incomplete_journal_and_detects_corruption():
    from api_agent.testing.payloads import PayloadStore, _digest

    call, test_id = str(uuid4()), 'test.py::test_graph'
    original, request, response = packet(call, str(uuid4()), test_id)
    files = {}
    class Recorder:
        def record_artifact(self, name, body):
            files[name] = body
            return name
    store = PayloadStore(Recorder(), test_id)
    store.omit_wire(call, 'request')
    store.put_wire(call, 'response', response, reason='partial')

    durable = store.materialize_journal(original)
    assert durable['version'] == 3
    assert durable['evidence_status'] == 'incomplete'
    assert durable['unverified_original_sha256'] == original['sha256']
    assert durable['reduced_sha256'] != original['sha256']
    restored = hydrate_journal(durable, lambda ref: json.loads(files[ref['source']]))
    capture = next(event for event in restored['events']
                   if event.get('cause_id') == call and event.get('direction') == 'input')
    assert capture['reason'] == 'truncated'
    assert capture['values'] == {'llm_request': {}}

    tampered = deepcopy(durable)
    tampered['reduced_sha256'] = '0' * 64
    tampered['sha256'] = _digest({key: value for key, value in tampered.items() if key != 'sha256'})
    with pytest.raises(ValueError, match='journal digest mismatch'):
        hydrate_journal(tampered, lambda ref: json.loads(files[ref['source']]))


def test_recording_publishes_incomplete_journal_when_wire_exceeds_budget(tmp_path, monkeypatch):
    import api_agent.testing.payloads as payloads

    monkeypatch.setattr(payloads, 'MAX_TOTAL', 1600)
    test = TestIdentity('base::test.py::test_graph', 'base', 'test.py', 'test.py::test_graph')
    call = str(uuid4())
    journal, request, response = packet(call, str(uuid4()), test.node_id,
        wire={'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'a' * 2000}]}]})
    outcome = TestResult(test, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for kind, data in [('inference_content', dict(call_id=call, phase='request', content=request)),
                           ('inference_content', dict(call_id=call, phase='response', content=response, reason='partial')),
                           ('workflow_evidence', journal)]:
            hooks['on_event'](RunnerEvent(kind, 'base', test, text=json.dumps(data)))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    context = SimpleNamespace(base_root=tmp_path/'base', owner_root=tmp_path/'base')
    result = run_recorded_integration(context, [test], dict(TUI_MODE='integration',
        ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path/'results', LOG_CONTENT=True),
        accepted=True, runner=runner, storage=SimpleNamespace(persist=lambda *args: None))
    durable = json.loads(next(result.directory.glob('workflow-0-*.json')).read_bytes())
    assert durable['version'] == 3
    assert durable['evidence_status'] == 'incomplete'
    resolver = PayloadResolver.local(result.directory, result.manifest)
    index = next(result.directory.glob('payload-index-*.json'))
    resolver.index(index.name)
    hydrated = hydrate_journal(durable, resolver)
    request_capture = next(event for event in hydrated['events']
                           if event.get('cause_id') == call and event.get('direction') == 'input')
    assert request_capture['reason'] == 'truncated'
    assert request_capture['values'] == {'llm_request': {}}


def test_recording_preserves_graph_when_node_payload_exceeds_remaining_wire_budget(tmp_path, monkeypatch):
    import api_agent.testing.payloads as payloads

    monkeypatch.setattr(payloads, 'MAX_TOTAL', 1600)
    test = TestIdentity('base::test.py::test_graph', 'base', 'test.py', 'test.py::test_graph')
    call = str(uuid4())
    journal, request, response = packet(call, str(uuid4()), test.node_id,
        wire={'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'a' * 1100}]}]})
    outcome = TestResult(test, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for kind, data in [('inference_content', dict(call_id=call, phase='request', content=request)),
                           ('inference_content', dict(call_id=call, phase='response', content=response, reason='partial')),
                           ('workflow_evidence', journal)]:
            hooks['on_event'](RunnerEvent(kind, 'base', test, text=json.dumps(data)))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    context = SimpleNamespace(base_root=tmp_path/'base', owner_root=tmp_path/'base')
    result = run_recorded_integration(context, [test], dict(TUI_MODE='integration',
        ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path/'results', LOG_CONTENT=True),
        accepted=True, runner=runner, storage=SimpleNamespace(persist=lambda *args: None))
    durable = json.loads(next(result.directory.glob('workflow-0-*.json')).read_bytes())
    assert durable['version'] == 3 and durable['evidence_status'] == 'incomplete'
    index = json.loads(next(result.directory.glob('payload-index-*.json')).read_bytes())
    omitted = [entry for entry in index['entries'] if entry['kind'] == 'omitted']
    assert len(omitted) == 1 and omitted[0]['occurrence']['kind'] == 'node'
    resolver = PayloadResolver.local(result.directory, result.manifest)
    with pytest.raises(ValueError, match='Node omission lacks its admitted occurrence'):
        hydrate_journal(durable, resolver)
    resolver.index(next(result.directory.glob('payload-index-*.json')).name)
    hydrated = hydrate_journal(durable, resolver)
    node_input = next(event for event in hydrated['events']
                      if event.get('event') == 'capture' and event.get('direction') == 'input'
                      and event.get('cause_id') is None)
    assert node_input['reason'] == 'truncated' and 'values' not in node_input
    wire_input = next(event for event in hydrated['events']
                      if event.get('cause_id') == call and event.get('direction') == 'input')
    assert wire_input['values'] == {'llm_request': request}


def test_plain_node_overflow_hydrates_without_claiming_captured_values(monkeypatch):
    from api_agent.testing.payloads import PayloadStore, _digest

    call, test_id = str(uuid4()), 'test.py::test_graph'
    journal, request, response = packet(call, str(uuid4()), test_id)
    node = next(event for event in journal['events'] if event.get('event') == 'capture'
                and event.get('cause_id') is None and event.get('direction') == 'input')
    node.update(values={'text': 'x' * 200}, capture_class='plain_user_io', plain_fields=['text'])
    journal['sha256'] = _digest({key: value for key, value in journal.items() if key != 'sha256'})
    monkeypatch.setattr('api_agent.testing.payloads.MAX_TOTAL', 260)
    files = {}
    class Recorder:
        def record_artifact(self, name, body):
            files[name] = body
            return name
    store = PayloadStore(Recorder(), test_id)
    store.put_wire(call, 'request', request)
    store.put_wire(call, 'response', response, reason='partial')
    durable = store.materialize_journal(journal)
    restored = hydrate_journal(durable, lambda ref: json.loads(files[ref['source']]))
    node = next(event for event in restored['events'] if event.get('event') == 'capture'
                and event.get('cause_id') is None and event.get('direction') == 'input')
    assert node['reason'] == 'truncated'
    assert node['capture_class'] == 'plain_user_io'
    assert 'values' not in node


def test_scoped_inference_loss_attaches_only_to_matching_graph_visit(tmp_path):
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    calls = [str(uuid4()), str(uuid4())]
    packets = [packet(call, str(uuid4()), test.node_id) for call in calls]
    journals = [entry[0] for entry in packets]
    target = journals[1]
    visit = next(event['scope_id'] for event in target['events']
                 if event.get('event') == 'scope_start' and event.get('kind') == 'node')
    outcome = TestResult(test, 'passed', .01)
    def runner(context, tests, **hooks):
        hooks['before_run']()
        for call, (journal, request, response) in zip(calls, packets):
            hooks['on_event'](RunnerEvent('workflow_evidence', identity=test, text=json.dumps(journal)))
            for phase, content in (('request', request), ('response', response)):
                hooks['on_event'](RunnerEvent('inference_content', identity=test,
                    text=json.dumps(dict(call_id=call, phase=phase, content=content))))
        hooks['on_event'](RunnerEvent('workflow_inference_evidence_incomplete', identity=test,
            text=json.dumps(dict(invocation_id=target['invocation_id'], visit_id=visit,
                                 call_id=calls[1], direction='input'))))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result
    result = run_recorded_integration(context, [test], config, accepted=True, runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    for graph in row['steps']:
        invocation = next(p['value'] for p in graph['parameters'] if p['name'] == 'invocation_id')
        attachments = graph['steps'][0].get('attachments', [])
        markers = [a for a in attachments if a['name'] == 'Incomplete workflow LLM evidence']
        assert len(markers) == (invocation == target['invocation_id'])
        if markers:
            assert calls[1] in (result.directory / markers[0]['source']).read_text()


def test_scoped_loss_finds_nearest_nested_graph():
    from api_agent.testing.evidence_placement import graph_for_visit, place_evidence
    invocation, root, inner, visit = (str(uuid4()) for _ in range(4))
    journal = {'invocation_id': invocation, 'events': [
        dict(event='scope_start', kind='graph', scope_id=root, parent_id=None),
        dict(event='scope_start', kind='graph', scope_id=inner, parent_id=root),
        dict(event='scope_start', kind='node', scope_id=visit, parent_id=inner)]}
    leaf = dict(name='node Make', parameters=[dict(name='scope_id', value=visit)], steps=[])
    nested = dict(name='graph Inner', parameters=[dict(name='scope_id', value=inner)], steps=[leaf])
    outer = dict(name='graph Outer', parameters=[dict(name='scope_id', value=root),
        dict(name='invocation_id', value=invocation)], steps=[nested])
    marker = dict(name='Incomplete workflow LLM evidence', source='loss.txt', type='text/plain')
    assert place_evidence([outer], [(graph_for_visit([journal], invocation, visit), marker)]) == []
    assert nested['attachments'] == [marker]
    assert 'attachments' not in outer


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_recorded_tool_phases_use_one_canonical_copy_under_actual_tool_leaf(tmp_path, log, trace):
    test = TestIdentity('base::test.py::test_tool', 'base', 'test.py', 'test.py::test_tool')
    outcome = TestResult(test, 'passed', .01)
    node = SimpleNamespace(id='Make', kind='node', parent=None)
    definition = SimpleNamespace(graph_id='Example', definition_id='b' * 64, entry_root='base',
        layout=SimpleNamespace(steps=[node], tables={'Edges': []}), resources=[], replacements=[])
    journal = WorkflowJournal(definition, log_content=log, content_policy=ContentPolicy(log), test_id=test.node_id)
    graph_id, visit, tool = (str(uuid4()) for _ in range(3))
    graph = dict(scope_id=graph_id, kind='graph', owner='GRAPH', parent_id=None, started_ms=1)
    node = dict(scope_id=visit, kind='node', owner='Make', parent_id=graph_id, started_ms=2)
    journal.observe(dict(event='scope_start', **graph))
    journal.observe(dict(event='edge', source='START', target='Make', owner='GRAPH', implicit=True))
    journal.observe(dict(event='scope_start', **node))
    owner = dict(tool_invocation_id=tool, visit_id=visit, attempt_id=None)
    journal.observe(dict(owner, event='tool_start', tool_name='lookup', started_ms=3))
    for direction, values in [('input', {'arguments': {'query': 'synthetic'}}),
                               ('output', {'status': 'success', 'data': {'answer': 7}})]:
        journal.capture_json(**owner, direction=direction, values=values, schema={'type': 'object'})
    journal.observe(dict(owner, event='tool_end', tool_name='lookup', outcome='success', stopped_ms=4))
    journal.observe(dict(event='scope_end', stopped_ms=5, outcome='passed', **node))
    journal.observe(dict(event='edge', source='Make', target='END', owner='GRAPH', implicit=True))
    journal.observe(dict(event='scope_end', stopped_ms=6, outcome='passed', **graph))
    packet = journal.finalize()

    def runner(context, tests, **hooks):
        hooks['before_run']()
        event = RunnerEvent('workflow_evidence', 'base', test, text=json.dumps(packet))
        hooks['on_event'](event)
        hooks['on_event'](event)  # Re-delivery cannot duplicate native phases.
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    context = SimpleNamespace(base_root=tmp_path/'base', owner_root=tmp_path/'base')
    result = run_recorded_integration(context, [test], dict(TUI_MODE='integration',
        ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path/'results', LOG_CONTENT=log, TRACELOOP_TRACE_CONTENT=trace),
        accepted=True, runner=runner, storage=SimpleNamespace(persist=lambda *args: None))
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    graph = row['steps'][0]
    node = graph['steps'][0]
    assert len(node['steps']) == 1
    leaf = node['steps'][0]
    assert leaf['name'] == 'Tool lookup'
    assert {'name': 'tool_invocation_id', 'value': tool} in leaf['parameters']
    assert not node.get('attachments')
    attachments = leaf.get('attachments', [])
    assert [item['name'] for item in attachments] == (['Tool request', 'Tool response'] if log else [])
    index_ref = next(item for item in row['attachments'] if item['name'] == 'Payload index')
    resolver = PayloadResolver.local(result.directory, result.manifest)
    index = resolver.index(index_ref['source'])
    assert len(index['entries']) == (2 if log else 0)
    assert all(item['occurrence']['kind'] == 'tool' for item in index['entries'])
    assert {item.get('source') for item in index['entries']} == {item['source'] for item in attachments}
    if not log:
        captures = [item for item in packet['events'] if item['event'] == 'capture']
        assert len(captures) == 2
        assert all(item['reason'] == 'withheld' and 'values' not in item for item in captures)
    evidence = next(item for item in graph['attachments'] if item['name'] == 'Execution evidence')
    durable = json.loads((result.directory/evidence['source']).read_bytes())
    assert hydrate_journal(durable, resolver) == packet
    assert 'synthetic' not in json.dumps(durable)
