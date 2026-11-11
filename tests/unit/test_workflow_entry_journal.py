"""Journals record a START choice as the entry edge and admit it only as declared."""
import asyncio
from copy import deepcopy

import pytest

from api_agent.workflows.journal import WorkflowJournal, validate_journal

import scripted_support as scripted
from loop_support import edges
from test_workflow_entry import ENTRY, NODES, STR, graph
from test_workflow_loops_journal import sign


def recorded(entry, script=None, update=None):
    definition = scripted.load(entry)
    journal = WorkflowJournal(definition)
    workflow, calls, _ = scripted.start(entry, script, journal=journal)
    asyncio.run(workflow.arun({} if update is None else update))
    return journal.finalize(), definition, calls


def entry_edges(payload):
    return [event for event in payload['events'] if event['event'] == 'edge' and event['source'] == 'START']


def test_the_chosen_entry_row_is_recorded_and_validates(tmp_path):
    payload, definition, calls = recorded(graph(tmp_path), {'pre': [{'mode': 'c'}]})

    assert calls == ['pre', 'c', 'post']
    assert [(edge['owner'], edge['target'], edge.get('condition', 'always'), edge['implicit'])
            for edge in entry_edges(payload)] == [
        ('GRAPH', 'pre', 'always', True), ('group', 'c', 'pre.mode == "c"', False)]
    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition) == payload


def test_the_projection_lists_the_declared_entry_rows_instead_of_the_implicit_one(tmp_path):
    payload, _, _ = recorded(graph(tmp_path), {'pre': [{'mode': 'b'}]})

    group = [(edge['target'], edge['condition'], edge['implicit']) for edge in payload['projection']['edges']
             if edge['owner'] == 'group' and edge['source'] == 'START']

    assert group == [('b', 'pre.mode == "b"', False), ('c', 'pre.mode == "c"', False),
                     ('a', 'otherwise', False)]


def test_a_graph_entry_row_is_recorded_on_the_graph_scope(tmp_path):
    nodes = {**NODES, 'a': ({'mode': ('START', 'mode', '"none"', STR)}, {'tag': STR})}
    entry = graph(tmp_path, '', extra='', nodes=nodes, flow=['a', 'b', 'c'],
                  sections=edges(('START', 'c', 'START.mode == "c"'), ('START', 'a', 'otherwise')))

    payload, definition, calls = recorded(entry, update={'mode': 'c'})

    assert calls == ['c']
    assert [(edge['owner'], edge['target'], edge.get('condition', 'always'), edge['implicit'])
            for edge in entry_edges(payload)] == [('GRAPH', 'c', 'START.mode == "c"', False)]
    assert validate_journal(payload, expected_definition=definition) == payload


def forged(tmp_path, change, mode='c'):
    payload, definition, _ = recorded(graph(tmp_path), {'pre': [{'mode': mode}]})
    forged = deepcopy(payload)
    change(next(edge for edge in entry_edges(forged) if edge['owner'] == 'group'))
    return sign(forged), definition


@pytest.mark.parametrize('change', [
    lambda edge: edge.update(condition='otherwise'),
    lambda edge: edge.update(condition='pre.mode == "b"'),
    lambda edge: edge.update(implicit=True, condition='always'),
    lambda edge: edge.update(target='a', condition='otherwise'),
    lambda edge: edge.update(target='b', condition='pre.mode == "b"'),
    lambda edge: edge.update(target='post'),
    lambda edge: edge.update(visit_id='forged'),
])
def test_a_forged_entry_choice_is_not_complete(tmp_path, change):
    payload, definition = forged(tmp_path, change)

    with pytest.raises(ValueError, match='Undeclared workflow event edge|completeness mismatch'):
        validate_journal(payload, expected_definition=definition)


def test_an_entry_edge_removed_from_the_journal_is_not_complete(tmp_path):
    payload, definition, _ = recorded(graph(tmp_path), {'pre': [{'mode': 'c'}]})
    forged = deepcopy(payload)
    forged['events'].remove(next(edge for edge in entry_edges(forged) if edge['owner'] == 'group'))
    for index, event in enumerate(forged['events'], 1):
        event['sequence'] = index

    with pytest.raises(ValueError, match='Undeclared workflow event edge|completeness mismatch'):
        validate_journal(sign(forged), expected_definition=definition)


def test_a_journal_for_a_graph_without_entry_rows_cannot_claim_one(tmp_path):
    for name in ('declared', 'plain'):
        (tmp_path / name).mkdir()
    payload, definition, _ = recorded(graph(tmp_path / 'declared', ENTRY), {'pre': [{'mode': 'c'}]})
    other = scripted.load(graph(tmp_path / 'plain', ''))

    with pytest.raises(ValueError, match='Workflow projection differs'):
        validate_journal(payload, expected_definition=other)
