"""Journals of bounded loops stay valid across re-visits and remain strict otherwise."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from api_agent.workflows.execution import WorkflowError
from api_agent.workflows.identity import workflow_identity
from api_agent.workflows.journal import WorkflowJournal, validate_journal
from loop_support import build, edges, graph, limits, load
from test_workflow_loops import retry


def sign(payload):
    unsigned = {key: value for key, value in payload.items() if key != 'sha256'}
    payload['sha256'] = sha256(json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return payload


def recorded(entry, update, **kwargs):
    journal = WorkflowJournal(load(entry))
    workflow, _, definition = build(entry, journal=journal, **kwargs)
    asyncio.run(workflow.arun(update))
    return journal.finalize(), definition


def routes(payload):
    return [(event['source'], event['target']) for event in payload['events'] if event['event'] == 'edge']


@pytest.mark.parametrize('accept_at,final', [(1, 'accept'), (2, 'accept'), (9, 'failed')])
def test_a_retry_loop_journal_is_complete_and_validates(tmp_path, accept_at, final):
    payload, definition = recorded(retry(tmp_path), {'accept_at': accept_at})

    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition) == payload
    visited = [target for _, target in routes(payload)]
    assert visited.count('draft') == min(accept_at, 2) and visited[-2:] == [final, 'END']
    assert (('verify', 'draft') in routes(payload)) == (accept_at > 1)


def test_a_replan_loop_between_components_journals_each_visit(tmp_path):
    flow = [('component', 'planning'), ('component', 'analysis'), ('node', 'finish')]
    sections = limits('REPLAN_PASSES') + edges(
        ('analysis', 'planning', 'check.route == "replan" and planning.attempt < REPLAN_PASSES'),
        ('analysis', 'finish', 'otherwise'))
    entry = graph(tmp_path, flow, sections=sections, components={
        'planning': (['plan'], ''), 'analysis': (['analyze', 'check'], '')})

    payload, definition = recorded(entry, {'accept_at': 2})

    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition) == payload
    assert ('analysis', 'planning') in routes(payload)
    assert [event['owner'] for event in payload['events']
            if event['event'] == 'scope_start' and event['kind'] == 'component'] == [
        'planning', 'analysis', 'planning', 'analysis']


def test_a_loop_journal_that_skips_a_visit_is_rejected_as_complete(tmp_path):
    payload, definition = recorded(retry(tmp_path), {'accept_at': 2})
    forged = deepcopy(payload)
    start = next(index for index, event in enumerate(forged['events'])
                 if event['event'] == 'edge' and (event['source'], event['target']) == ('verify', 'draft'))
    del forged['events'][start]

    with pytest.raises(ValueError):
        validate_journal(sign(forged), expected_definition=definition)


def test_a_partial_loop_journal_is_marked_partial_not_complete(tmp_path):
    payload, definition = recorded(retry(tmp_path), {'accept_at': 2})
    cut = deepcopy(payload)
    last = max(index for index, event in enumerate(cut['events']) if event['event'] == 'edge')
    del cut['events'][last:]
    cut['completeness'] = 'partial'

    assert validate_journal(sign(cut), expected_definition=definition)['completeness'] == 'partial'
    cut['completeness'] = 'complete'
    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(sign(cut), expected_definition=definition)


def test_an_edge_event_outside_the_declared_edges_is_rejected(tmp_path):
    payload, definition = recorded(retry(tmp_path), {'accept_at': 2})
    forged = deepcopy(payload)
    for event in forged['events']:
        if event['event'] == 'edge' and (event['source'], event['target']) == ('verify', 'draft'):
            event['condition'] = 'not verify.accepted'

    with pytest.raises(ValueError):
        validate_journal(sign(forged), expected_definition=definition)


def test_a_journal_bound_to_other_limit_values_is_refused(tmp_path):
    entry = retry(tmp_path)
    definition = load(entry)

    def journal(value):
        identity = workflow_identity(definition, timeouts={}, component_passes={}, limits={'PLAN_PASSES': value})
        return WorkflowJournal(definition, semantic_identity=identity)

    mismatched, _, _ = build(entry, journal=journal(3))
    with pytest.raises(WorkflowError, match='semantic identity'):
        asyncio.run(mismatched.arun({'accept_at': 2}))

    matching, _, _ = build(entry, journal=journal(2))
    asyncio.run(matching.arun({'accept_at': 2}))
