"""Journals record a routed expiry on the scope that ended and admit it only as declared."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from api_agent.workflows.journal import WorkflowJournal, validate_journal
from expiry_support import ENDS, REPLAY, ROUTE, expiring
from loop_support import build, load
from test_workflow_expiry import GROUP_ROUTE, group
from test_workflow_loops_journal import routes, sign

SAME_TARGET = (('work', 'finish', 'work.expired'), ('work', 'finish', 'otherwise'))


def recorded(entry, script=None, limit=None):
    """Run one turn under a journal carrying the effective limit, as ``(payload, definition)``."""
    definition = load(entry)
    values = {} if limit is None else {'NODE_TIMEOUT_PASSES': limit}
    journal = WorkflowJournal(definition, limits=values or None)
    workflow, native, _ = build(entry, config=values, journal=journal)
    native['SCRIPT'].update({name: list(steps) for name, steps in (script or {}).items()})
    asyncio.run(workflow.arun({'accept_at': 1}))
    return journal.finalize(), definition


def ended(payload, owner):
    return [event for event in payload['events'] if event['event'] == 'scope_end' and event['owner'] == owner]


def renumbered(payload):
    for index, event in enumerate(payload['events'], 1):
        event['sequence'] = index
    return sign(payload)


def test_a_routed_expiry_is_recorded_on_the_scope_that_expired(tmp_path):
    payload, definition = recorded(expiring(tmp_path, *ROUTE), {'work': ['hang']})

    (work,) = ended(payload, 'work')
    assert (work['outcome'], work['reason']) == ('failed', 'declared_timeout_expired')
    assert work['declared_timeout'] == work['effective_timeout'] == 0.1
    # The turn's outcome follows the final route: the graph scope passes.
    assert ended(payload, 'GRAPH')[0]['outcome'] == 'passed'
    assert routes(payload) == [('START', 'work'), ('work', 'timed_out'), ('timed_out', 'END')]
    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition) == payload


def test_a_run_that_finishes_in_time_records_no_expiry(tmp_path):
    payload, _ = recorded(expiring(tmp_path, *ROUTE))

    assert ended(payload, 'work')[0]['outcome'] == 'passed' and 'reason' not in ended(payload, 'work')[0]
    assert routes(payload)[1:] == [('work', 'finish'), ('finish', 'END')]


@pytest.mark.parametrize('script,visited', [
    (['hang', 'ok'], ['work', 'work', 'finish', 'END']),
    (['hang', 'hang'], ['work', 'work', 'timed_out', 'END'])])
def test_replayed_expiries_validate(tmp_path, script, visited):
    payload, definition = recorded(expiring(tmp_path, *REPLAY), {'work': script})

    assert [target for _, target in routes(payload)] == visited
    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition) == payload


def test_a_replay_journal_over_a_forged_lower_limit_is_not_complete(tmp_path):
    payload, definition = recorded(expiring(tmp_path, *REPLAY), {'work': ['hang'] * 3}, limit=3)

    assert [target for _, target in routes(payload)].count('work') == 3
    assert validate_journal(payload, expected_definition=definition, expected_limits={'NODE_TIMEOUT_PASSES': 3}) == payload
    forged = deepcopy(payload)
    forged['projection']['limits'][0]['value'] = 2
    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(sign(forged))


def test_an_expired_component_journal_is_complete_and_names_the_component(tmp_path):
    payload, definition = recorded(group(tmp_path, *GROUP_ROUTE), {'work': ['hang']})

    (component,) = ended(payload, 'group')
    assert (component['outcome'], component['reason']) == ('failed', 'declared_timeout_expired')
    assert ('group', 'timed_out') in routes(payload)
    assert payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition) == payload


@pytest.mark.parametrize('change', [
    lambda end: end.pop('reason'),
    lambda end: end.update(declared_timeout=None),
    lambda end: end.update(effective_timeout=0.05),
    lambda end: end.update(outcome='cancelled'),
])
def test_forged_expiry_ends_are_rejected_as_complete(tmp_path, change):
    payload, definition = recorded(expiring(tmp_path, *ROUTE), {'work': ['hang']})
    forged = deepcopy(payload)
    change(ended(forged, 'work')[0])

    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(sign(forged), expected_definition=definition)


def test_an_expired_step_may_only_continue_along_an_edge_that_reads_its_expiry(tmp_path):
    payload, definition = recorded(expiring(tmp_path, *SAME_TARGET), {'work': ['hang']})
    assert validate_journal(payload, expected_definition=definition) == payload
    forged = deepcopy(payload)
    edge = next(event for event in forged['events'] if event['event'] == 'edge' and event['source'] == 'work')
    edge['condition'] = 'otherwise'

    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(sign(forged), expected_definition=definition)


def test_a_step_that_finished_cannot_claim_the_edge_that_requires_its_expiry(tmp_path):
    payload, definition = recorded(expiring(tmp_path, *SAME_TARGET))
    assert validate_journal(payload, expected_definition=definition) == payload
    forged = deepcopy(payload)
    edge = next(event for event in forged['events'] if event['event'] == 'edge' and event['source'] == 'work')
    edge['condition'] = 'work.expired'

    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(sign(forged), expected_definition=definition)


def test_an_expiry_forged_onto_a_step_that_declared_no_expiry_edge_is_rejected(tmp_path):
    payload, definition = recorded(expiring(tmp_path, ('work', 'finish', 'always')))
    forged = deepcopy(payload)
    work = ended(forged, 'work')[0]
    work.update(outcome='failed', reason='declared_timeout_expired', declared_timeout=0.1, effective_timeout=0.1)
    marker = deepcopy(work)
    marker.update(event='node', event_id=str(uuid4()), outcome='failed', node='work')
    forged['events'].insert(forged['events'].index(work), marker)

    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(renumbered(forged), expected_definition=definition)
