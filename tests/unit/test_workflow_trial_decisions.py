"""Native independent UUID decisions remain assertions on real model replies."""
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.llm import LLMResult, Message, TextContent
from api_agent.workflows import contrived
from api_agent.workflows.execution import WorkflowError

ROOT = Path(__file__).resolve().parents[2]


def trial_uuid(route='skip', *, first_ready=True, second_ready=True):
    """Controlled unit seed; production graph trials still use fresh uuid4()."""
    last = {'skip': '0', 'next_a': '5', 'next_b': 'a'}[route]
    return '00000000-0000-4000-8000-000000000' + ('0' if second_ready else '8') + ('0' if first_ready else '8') + last


def context(*, seed=None, component='first', attempt=1, llm=None):
    return SimpleNamespace(node_id=component + '_draft', attempt=attempt, max_passes=4,
        prompts=('Return the requested decisions.',),
        services={'trial': seed or trial_uuid(), 'selection': 'synthetic', 'llm': llm,
                  'output_schema': {'type': 'object'}})


def inputs(*, component='first', previous=None):
    return {'user_input': '  Original λ\n', 'upstream': None if component == 'first' else {
                'original_input': '  Original λ\n', 'route': 'skip', 'ready': True, 'final_output': '  Original λ\n'},
            'previous': previous, 'feedback': None if previous is None else {'reason': 'Repair this draft'}}


def reply(request, **changes):
    metadata = json.loads(request.payload.messages[-1].content[0].text)
    source = request.payload.messages[1].content[0].text
    value = {'original_input': source, 'final_output': source,
             'route': metadata['decisions']['route'], 'ready': metadata['decisions']['ready'], **changes}
    return LLMResult(Message('assistant', (TextContent(json.dumps(value)),)))


@pytest.mark.parametrize('digit,route', [(digit, route) for digit, route in zip(
    '0123456789abcdef', ['skip'] * 5 + ['next_a'] * 5 + ['next_b'] * 6)])
def test_all_route_bins_are_native_and_metadata_readiness_is_independent(digit, route):
    seed = trial_uuid()[:-1] + digit
    assert contrived.trial_decisions(seed, 'first') == {'component': 'first', 'route': route, 'ready': True}
    assert contrived.trial_decisions(seed, 'second') == {'component': 'second', 'route': route, 'ready': True}


def test_readiness_uses_separate_uniform_characters_for_each_component():
    first, second = Counter(), Counter()
    for a in '0123456789abcdef':
        for b in '0123456789abcdef':
            seed = trial_uuid()[:-3] + a + b + '5'
            lower = contrived.trial_decisions(seed, 'first')
            upper = contrived.trial_decisions(seed, 'second')
            assert lower['route'] == upper['route'] == 'next_a'
            assert lower['ready'] is (int(b, 16) < 8)
            assert upper['ready'] is (int(a, 16) < 8)
            first[lower['ready']] += 1
            second[upper['ready']] += 1
    assert first == second == {True: 128, False: 128}


@pytest.mark.parametrize('seed', [None, '', 'not-a-uuid', '0' * 32, '00000000-0000-1000-8000-000000000000',
                                 '{00000000-0000-4000-8000-000000000000}', True, 4])
def test_invalid_uuid_rejects_before_inference(seed):
    llm = AsyncMock()
    selected = context(llm=llm)
    selected.services['trial'] = seed
    with pytest.raises(WorkflowError, match='canonical version-4 UUID'):
        asyncio.run(contrived.produce(inputs(), selected))
    llm.assert_not_called()


@pytest.mark.parametrize('component', [None, 'other'])
def test_unknown_component_identity_rejects_before_inference(component):
    llm = AsyncMock()
    selected = context(llm=llm)
    selected.node_id = component
    with pytest.raises(WorkflowError, match='component identity'):
        asyncio.run(contrived.produce(inputs(), selected))
    llm.assert_not_called()


@pytest.mark.parametrize('component,upstream', [('first', {}), ('second', None), ('second', [])])
def test_component_and_upstream_identity_cannot_disagree(component, upstream):
    llm = AsyncMock()
    supplied = inputs(component=component)
    supplied['upstream'] = upstream
    with pytest.raises(WorkflowError, match='identity disagrees'):
        asyncio.run(contrived.produce(supplied, context(component=component, llm=llm)))
    llm.assert_not_called()


@pytest.mark.parametrize('attempt,previous', [(1, {'route': 'skip'}), (2, None)])
def test_correction_state_requires_actual_previous_draft(attempt, previous):
    llm = AsyncMock()
    with pytest.raises(WorkflowError, match='previous draft'):
        asyncio.run(contrived.produce(inputs(previous=previous), context(attempt=attempt, llm=llm)))
    llm.assert_not_called()


@pytest.mark.parametrize('changes,error', [({'route': 'next_a'}, 'requested route'),
                                         ({'ready': False}, 'requested readiness'),
                                         ({'ready': 1}, 'requested readiness')])
def test_model_decision_mismatch_is_terminal_and_preserves_response(changes, error):
    requests, replies = [], []
    async def llm(request):
        requests.append(request)
        result = reply(request, **changes)
        replies.append(result)
        return result
    with pytest.raises(WorkflowError, match=error):
        asyncio.run(contrived.produce(inputs(), context(llm=llm)))
    assert len(requests) == 1
    actual = json.loads(replies[0].payload.content[0].text)
    assert all(actual[key] == value for key, value in changes.items())


def test_initial_false_readiness_must_be_returned_not_bypassed():
    calls = []
    async def llm(request):
        calls.append(request)
        return reply(request, ready=True)
    with pytest.raises(WorkflowError, match='requested readiness'):
        asyncio.run(contrived.produce(inputs(), context(seed=trial_uuid(first_ready=False), llm=llm)))
    assert len(calls) == 1


def test_correction_preserves_original_route_and_explicitly_requires_ready_true():
    requests = []
    seed = trial_uuid('next_b', first_ready=False)
    previous = {'original_input': '  Original λ\n', 'route': 'next_b', 'ready': False, 'final_output': '  Original λ\n'}
    original = deepcopy(previous)
    async def llm(request):
        requests.append(request)
        return reply(request)
    result = asyncio.run(contrived.produce(inputs(previous=previous), context(seed=seed, attempt=2, llm=llm)))
    metadata = json.loads(requests[0].payload.messages[-1].content[0].text)
    assert metadata['decisions'] == {'component': 'first', 'route': 'next_b', 'ready': True}
    assert metadata['trial'] == seed and metadata['previous'] == previous == original
    assert result['route'] == 'next_b' and result['ready'] is True


def test_correction_cannot_reject_the_requested_readiness_and_trigger_another_retry():
    previous = {'route': 'skip', 'ready': False}
    calls = []
    async def llm(request):
        calls.append(request)
        return reply(request, ready=False)
    with pytest.raises(WorkflowError, match='requested readiness'):
        asyncio.run(contrived.produce(inputs(previous=previous), context(
            seed=trial_uuid(first_ready=False), attempt=2, llm=llm)))
    assert len(calls) == 1


def test_poisoned_previous_route_rejects_before_new_inference():
    llm = AsyncMock()
    with pytest.raises(WorkflowError, match='correction state changed'):
        asyncio.run(contrived.produce(inputs(previous={'route': 'next_a'}), context(attempt=2, llm=llm)))
    llm.assert_not_called()


def test_real_graph_observes_native_guard_rejection_and_actual_correction():
    requests, responses, events = [], [], []
    async def llm(request):
        requests.append(request)
        result = reply(request)
        responses.append(json.loads(result.payload.content[0].text))
        return result
    seed = trial_uuid('next_a', first_ready=False, second_ready=True)
    workflow = contrived.build(ROOT, llm=llm, selection='synthetic', trial=seed, observe=events.append)
    result = asyncio.run(workflow.arun({'user_input': '  Original λ\n'}))
    metadata = [json.loads(request.payload.messages[-1].content[0].text) for request in requests]
    assert [row['decisions'] for row in metadata] == [
        {'component': 'first', 'route': 'next_a', 'ready': False},
        {'component': 'first', 'route': 'next_a', 'ready': True},
        {'component': 'second', 'route': 'next_a', 'ready': True}]
    assert metadata[1]['previous'] == responses[0]
    assert metadata[2]['upstream'] == responses[1]
    assert [event['accepted'] for event in events if event['event'] == 'guard'] == [False, True, True]
    assert [event['attempt'] for event in events if event['event'] == 'attempt'] == [1, 2, 1]
    assert result['first_draft'] == responses[1] and result['second_draft'] == responses[2]
    assert result['second_draft']['original_input'] == result['second_draft']['final_output'] == '  Original λ\n'


def test_runtime_model_mismatch_has_no_guard_or_correction_inference():
    calls, events = [], []
    async def llm(request):
        calls.append(request)
        return reply(request, route='next_b')
    workflow = contrived.build(ROOT, llm=llm, selection='synthetic', trial=trial_uuid(), observe=events.append)
    with pytest.raises(WorkflowError, match='requested route'):
        asyncio.run(workflow.arun({'user_input': '  Original λ\n'}))
    assert len(calls) == 1
    assert not [event for event in events if event['event'] in {'guard', 'attempt'} and event.get('attempt', 1) > 1]
    assert not [event for event in events if event['event'] == 'guard']
