"""Finalized native conversation trees reuse canonical files and frozen call costs."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.payloads import PayloadResolver, hydrate_journal
from api_agent.testing.recorded import run_recorded_integration
from api_agent.workflows.journal import _encoded
from api_agent.workflows.run_context import conversation, trial
from test_recorded_integration import fixture, Storage
from test_recorded_payload_projection import packet


def parameters(step):
    return {item['name']: item['value'] for item in step['parameters']}


def resign(journal):
    journal['sha256'] = sha256(_encoded({key: value for key, value in journal.items()
                                        if key != 'sha256'})).hexdigest()
    return journal


def test_finalized_conversation_costs_turns_and_canonical_payloads(tmp_path):
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = True
    first_chat, second_chat = str(uuid4()), str(uuid4())
    first_turn, followup, new_turn = str(uuid4()), str(uuid4()), str(uuid4())
    calls = [str(uuid4()) for _ in range(6)]
    ownership = [(first_chat, first_turn), (first_chat, first_turn),
                 (first_chat, followup), (second_chat, new_turn)]
    packets = []
    for index, (call, (chat, turn)) in enumerate(zip(calls, ownership)):
        with conversation(chat):
            journal, request, response = packet(call, turn, selected.node_id)
        for event in journal['events']:
            for key in ('started_ms', 'stopped_ms'):
                if event.get(key) is not None:
                    event[key] += index * 10
            if event['event'] == 'call':
                event['outcome'] = 'completed'
        packets.append((resign(journal), request, response))
    ownership += [(first_chat, followup), (None, str(uuid4()))]
    outcome = TestResult(selected, 'passed', .01)

    def runner(context, tests, **hooks):
        hooks['before_run']()
        # Terminal receipts can arrive before journals and out of turn order.
        for index in reversed(range(len(calls))):
            chat, turn = ownership[index]
            base = dict(call_id=calls[index], selection='synthetic-model', turn_id=turn,
                        turn_kind='initial', attempt=1,
                        started_at=f'2026-01-01T00:00:{index * 2:02d}+00:00',
                        finished_at=f'2026-01-01T00:00:{index * 2 + 1:02d}+00:00')
            if chat is not None:
                base['conversation_id'] = chat
            for usage in (None, dict(input_tokens=index + 1, output_tokens=2,
                                    cached_input_tokens=0, cache_write_tokens=0, reasoning_tokens=0)):
                data = dict(base, usage=usage, finished_at=base['finished_at'] if usage is not None else None)
                if usage is not None:
                    data.update(outcome='completed', quote={'list_cost_usd': (index + 1) / 100, 'payment': None})
                hooks['on_event'](RunnerEvent('inference_usage', identity=selected, text=json.dumps(data)))
        for journal, request, response in reversed(packets):
            call = next(event['call_id'] for event in journal['events'] if event['event'] == 'call')
            for phase, value in [('request', request), ('response', response)]:
                hooks['on_event'](RunnerEvent('inference_content', identity=selected, text=json.dumps(
                    dict(call_id=call, phase=phase, content=value, reason='captured'))))
            hooks['on_event'](RunnerEvent('workflow_evidence', identity=selected, text=json.dumps(journal)))
            hooks['on_event'](RunnerEvent('workflow_evidence', identity=selected, text=json.dumps(journal)))
        for call in calls[4:]:
            for phase, value in [('request', {'messages': [{'role': 'user', 'text': 'outside graph'}]}),
                                 ('response', {'output': {'text': 'outside response'}})]:
                hooks['on_event'](RunnerEvent('inference_content', identity=selected, text=json.dumps(
                    dict(call_id=call, phase=phase, content=value, reason='captured'))))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    projection = packets[0][0]['projection']
    result = run_recorded_integration(context, [selected], config, accepted=True,
        runner=runner, storage=Storage(), workflow_admissions={projection['definition_id']: projection})
    assert result.exit_code == 0 and result.persisted
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    assert row['status'] == 'passed'
    conversations = {parameters(step)['conversation_id']: step for step in row['steps']
                     if step['name'].startswith('Conversation ')}
    first = conversations[first_chat]
    assert [parameters(step)['turn_id'] for step in first['steps']] == [first_turn, followup]
    assert [step['name'] for step in first['steps']] == ['User turn 1', 'User turn 2']
    assert len([step for step in first['steps'][0]['steps'] if step['name'] == 'graph GRAPH']) == 2
    assert float(parameters(first)['inclusive_list_cost_usd']) == pytest.approx(.11)
    assert float(parameters(first['steps'][0])['inclusive_list_cost_usd']) == pytest.approx(.03)
    assert float(parameters(first['steps'][1])['inclusive_list_cost_usd']) == pytest.approx(.08)
    assert float(parameters(conversations[second_chat])['inclusive_list_cost_usd']) == pytest.approx(.04)
    assert result.usage['test_costs'][selected.id]['list_cost_usd'] == pytest.approx(.21)
    assert result.usage['prompt_count'] == 4  # graph invocations do not multiply user turns
    orphan = next(step for step in first['steps'][1]['steps'] if step['name'] == 'call ' + calls[4])
    assert len(orphan['attachments']) == 2
    payload_keys = [set(json.loads((result.directory / item['source']).read_bytes()))
                    for item in orphan['attachments']]
    assert {'messages'} in payload_keys and {'output'} in payload_keys
    assert not any(calls[4] in item['name'] for item in row['attachments'])
    index_attachment = next(item for item in row['attachments'] if item['name'] == 'Payload index')
    with PayloadResolver.local(result.directory, result.manifest) as resolver:
        index = resolver.index(index_attachment['source'])
        assert len(index['entries']) == 20
        assert not any(item['name'] == 'Workflow execution evidence' for item in row['attachments'])
        graphs = [graph for chat in conversations.values() for turn in chat['steps']
                  for graph in turn['steps'] if graph['name'] == 'graph GRAPH']
        durable = [json.loads((result.directory / item['source']).read_bytes()) for graph in graphs
                   for item in graph['attachments'] if item['name'] == 'Execution evidence']
        assert all(parameters(graph)['invocation_id'] == journal['invocation_id']
                   for graph, journal in zip(graphs, durable))
        hydrated = {item['invocation_id']: hydrate_journal(item, resolver) for item in durable}
    assert hydrated == {item[0]['invocation_id']: item[0] for item in packets}
    joined = json.loads((result.directory / 'workflow-accounting-join.json').read_bytes())
    summaries = {item['conversation_id']: item for item in joined['tests'][0]['conversations']}
    assert summaries[first_chat]['list_cost_usd'] == pytest.approx(.11)
    assert summaries[second_chat]['list_cost_usd'] == pytest.approx(.04)


@pytest.mark.parametrize('trial_index', [1, 60])
def test_trial_context_survives_payload_materialization_without_conversation(tmp_path, trial_index):
    from api_agent.testing.payloads import PayloadStore
    from test_payloads import recorder
    with trial(trial_index):
        journal, _, _ = packet(str(uuid4()), str(uuid4()), 'test')
    store = PayloadStore(recorder(tmp_path), 'test')
    # The recorded wire projections require their authoritative provider phases.
    for event in journal['events']:
        if event['event'] == 'capture' and event.get('cause_id'):
            phase = 'request' if event['direction'] == 'input' else 'response'
            store.put_wire(event['cause_id'], phase, next(iter(event['values'].values())))
    durable = store.materialize_journal(journal)
    hydrated = hydrate_journal(durable, lambda ref: store._values[ref['source']])
    assert hydrated == journal
    assert hydrated['execution_context'] == {'kind': 'trial', 'trial_index': trial_index}


@pytest.mark.parametrize("log_content, expected_phases", [(True, {"request", "response"}), (False, set())])
def test_finalized_graph_free_conversation_retains_canonical_call_attachments(tmp_path, log_content, expected_phases):
    context, selected, config = fixture(tmp_path)
    config['LOG_CONTENT'] = log_content
    chat, turn_id, call_id = str(uuid4()), str(uuid4()), str(uuid4())
    outcome = TestResult(selected, 'passed', .01)
    phases = {'request': {'messages': [{'role': 'user', 'text': 'standalone question'}]},
              'response': {'output': {'text': 'standalone response'}}}

    def runner(context, tests, **hooks):
        hooks['before_run']()
        base = dict(call_id=call_id, conversation_id=chat, turn_id=turn_id,
                    selection='synthetic-model', started_at='2026-01-01T00:00:00+00:00')
        for data in [dict(base, usage=None), dict(base, usage=dict(input_tokens=3, output_tokens=2,
                cached_input_tokens=0, cache_write_tokens=0, reasoning_tokens=0),
                finished_at='2026-01-01T00:00:01+00:00', outcome='completed',
                quote={'list_cost_usd': .05, 'payment': None})]:
            hooks['on_event'](RunnerEvent('inference_usage', identity=selected, text=json.dumps(data)))
        for phase, content in phases.items():
            hooks['on_event'](RunnerEvent('inference_content', identity=selected, text=json.dumps(
                dict(call_id=call_id, phase=phase, content=content, reason='captured'))))
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [selected], config, accepted=True,
                                      runner=runner, storage=Storage())
    row = json.loads(next(result.directory.glob('*-result.json')).read_bytes())
    conversation_step = row['steps'][0]
    assert conversation_step['name'] == 'Conversation ' + chat
    turn = conversation_step['steps'][0]
    assert turn['name'] == 'User turn 1' and parameters(turn)['turn_id'] == turn_id
    leaf = turn['steps'][0]
    assert leaf['name'] == 'call ' + call_id
    assert len(leaf.get('attachments', [])) == len(expected_phases)
    assert {json.dumps(json.loads((result.directory / item['source']).read_bytes()), sort_keys=True)
            for item in leaf.get('attachments', [])} == {json.dumps(phases[phase], sort_keys=True) for phase in expected_phases}
    payload_files = [path for path in result.directory.glob('payload-*.json')
                     if not path.name.startswith('payload-index-')]
    assert len(payload_files) == len(expected_phases)
    assert float(parameters(conversation_step)['inclusive_list_cost_usd']) == .05
    assert not any(item['name'].startswith('Unattached ') for item in row['attachments'])
    joined = json.loads((result.directory / 'workflow-accounting-join.json').read_bytes())
    assert joined['tests'][0]['conversations'][0]['call_ids'] == [call_id]
