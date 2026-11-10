"""Journal validation bounds every re-visit of a loop by the limit recorded in its projection."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from api_agent.workflows.execution import WorkflowError
from api_agent.workflows.identity import workflow_identity
from api_agent.workflows.journal import WorkflowJournal, projection, validate_journal
from loop_support import build, edges, graph, limits, load
from test_workflow_loops import retry
from test_workflow_loops_journal import routes, sign

INCLUSIVE = (('verify', 'accept', 'verify.accepted'),
             ('verify', 'draft', 'not verify.accepted and draft.attempt <= PLAN_PASSES'),
             ('verify', 'failed', 'otherwise'))


def recorded(entry, update, limit):
    """Run with PLAN_PASSES=``limit`` under a journal that carries those effective limits."""
    definition = load(entry)
    values = {'PLAN_PASSES': limit}
    identity = workflow_identity(definition, timeouts={}, component_passes={}, limits=values)
    journal = WorkflowJournal(definition, semantic_identity=identity, limits=values)
    workflow, native, _ = build(entry, config=values, journal=journal)
    asyncio.run(workflow.arun(update))
    return journal.finalize(), definition, native['CALLS'].count('draft')


def forge_limit(payload, value):
    forged = deepcopy(payload)
    forged['projection']['limits'][0]['value'] = value
    return sign(forged)


def test_the_projection_records_effective_limits_and_each_backward_edge_bound(tmp_path):
    definition = load(retry(tmp_path))

    default = projection(definition)
    effective = projection(definition, limits={'PLAN_PASSES': 3})

    assert default['limits'] == [{'key': 'PLAN_PASSES', 'value': 2}]
    assert effective['limits'] == [{'key': 'PLAN_PASSES', 'value': 3}]
    bounded = [edge for edge in effective['edges'] if 'bound' in edge]
    assert [(edge['source'], edge['target'], edge['bound']) for edge in bounded] == [
        ('verify', 'draft', {'key': 'PLAN_PASSES', 'inclusive': False})]


def test_the_projection_marks_an_inclusive_bound(tmp_path):
    definition = load(retry(tmp_path, *INCLUSIVE))

    bounded = [edge for edge in projection(definition)['edges'] if 'bound' in edge]

    assert [edge['bound'] for edge in bounded] == [{'key': 'PLAN_PASSES', 'inclusive': True}]


def test_a_graph_without_limits_keeps_its_projection_shape(tmp_path):
    definition = load(graph(tmp_path, [('node', 'draft'), ('node', 'accept')],
                            nodes=['draft', 'accept'], sections=edges(('accept', 'END', 'always'))))

    shape = projection(definition)

    assert 'limits' not in shape and not any('bound' in edge for edge in shape['edges'])


@pytest.mark.parametrize('given', [{}, {'OTHER': 1}, {'PLAN_PASSES': -1}, {'PLAN_PASSES': 100},
                                   {'PLAN_PASSES': True}, {'PLAN_PASSES': 1.0}])
def test_the_projection_refuses_limits_that_do_not_match_the_declaration(tmp_path, given):
    with pytest.raises(ValueError, match='limit'):
        projection(load(retry(tmp_path)), limits=given)


def test_a_loop_journal_over_its_recorded_limit_validates_only_with_those_limits(tmp_path):
    payload, definition, drafts = recorded(retry(tmp_path), {'accept_at': 3}, 3)

    assert drafts == 3 and payload['completeness'] == 'complete'
    assert validate_journal(payload) == payload
    assert validate_journal(payload, expected_definition=definition,
                            expected_limits={'PLAN_PASSES': 3}) == payload
    # The declared default is two: three drafts do not belong to a run admitted at that limit.
    with pytest.raises(ValueError, match='projection differs'):
        validate_journal(payload, expected_definition=definition)


def test_a_forged_lower_limit_rejects_a_journal_that_claims_to_be_complete(tmp_path):
    payload, definition, _ = recorded(retry(tmp_path), {'accept_at': 3}, 3)

    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(forge_limit(payload, 2))

    # Against the limits the run was admitted under, the edited projection no longer matches.
    with pytest.raises(ValueError, match='projection differs'):
        validate_journal(forge_limit(payload, 2), expected_definition=definition,
                         expected_limits={'PLAN_PASSES': 3})


def test_a_journal_that_exceeds_its_limit_can_only_be_partial(tmp_path):
    payload, _, _ = recorded(retry(tmp_path), {'accept_at': 3}, 3)
    forged = forge_limit(payload, 2)
    forged['completeness'] = 'partial'

    assert validate_journal(sign(forged))['completeness'] == 'partial'


@pytest.mark.parametrize('limit,drafts', [(0, 1), (1, 2), (2, 3)])
def test_an_inclusive_bound_admits_one_more_start_than_the_limit(tmp_path, limit, drafts):
    payload, definition, ran = recorded(retry(tmp_path, *INCLUSIVE), {'accept_at': 99}, limit)

    assert ran == drafts and payload['completeness'] == 'complete'
    assert validate_journal(payload, expected_definition=definition,
                            expected_limits={'PLAN_PASSES': limit}) == payload
    if limit:
        with pytest.raises(ValueError, match='completeness mismatch'):
            validate_journal(forge_limit(payload, limit - 1))


def test_an_exclusive_bound_stops_at_the_limit(tmp_path):
    payload, _, ran = recorded(retry(tmp_path), {'accept_at': 99}, 3)

    assert ran == 3 and payload['completeness'] == 'complete'
    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(forge_limit(payload, 2))
    assert validate_journal(forge_limit(payload, 3))['completeness'] == 'complete'


def test_a_bound_on_a_component_loop_counts_component_starts(tmp_path):
    from loop_support import graph as build_graph
    sections = limits('REPLAN_PASSES') + edges(
        ('analysis', 'planning', 'check.route == "replan" and planning.attempt < REPLAN_PASSES'),
        ('analysis', 'finish', 'otherwise'))
    entry = build_graph(tmp_path, [('component', 'planning'), ('component', 'analysis'), ('node', 'finish')],
                        sections=sections, components={
                            'planning': (['plan'], ''), 'analysis': (['analyze', 'check'], '')})
    definition = load(entry)
    values = {'REPLAN_PASSES': 3}
    journal = WorkflowJournal(definition, limits=values, semantic_identity=workflow_identity(
        definition, timeouts={}, component_passes={'planning': 1, 'analysis': 1}, limits=values))
    workflow, _, _ = build(entry, config=values, journal=journal)
    asyncio.run(workflow.arun({'accept_at': 3}))
    payload = journal.finalize()

    assert routes(payload).count(('analysis', 'planning')) == 2
    assert validate_journal(payload)['completeness'] == 'complete'
    forged = deepcopy(payload)
    forged['projection']['limits'][0]['value'] = 1
    with pytest.raises(ValueError, match='completeness mismatch'):
        validate_journal(sign(forged))


@pytest.mark.parametrize('edit,message', [
    (lambda shape: shape.update(limits={}), 'Invalid workflow projection'),
    (lambda shape: shape['limits'].append(dict(shape['limits'][0])), 'Invalid workflow projection'),
    (lambda shape: shape['limits'][0].update(value=-1), 'Invalid workflow projection'),
    (lambda shape: shape['limits'][0].update(value=True), 'Invalid workflow projection'),
    (lambda shape: shape['limits'][0].update(key='bad key'), 'Invalid workflow projection'),
    (lambda shape: shape['limits'][0].update(extra=1), 'Invalid workflow projection'),
    (lambda shape: next(edge for edge in shape['edges'] if 'bound' in edge).update(bound='PLAN_PASSES'),
     'Invalid workflow projection edge'),
    (lambda shape: next(edge for edge in shape['edges'] if 'bound' in edge)['bound'].update(key='OTHER'),
     'Invalid workflow projection edge'),
    (lambda shape: next(edge for edge in shape['edges'] if 'bound' in edge)['bound'].update(inclusive=1),
     'Invalid workflow projection edge'),
    (lambda shape: next(edge for edge in shape['edges'] if 'bound' in edge)['bound'].update(extra=1),
     'Invalid workflow projection edge'),
])
def test_a_malformed_limit_or_bound_in_the_projection_is_rejected(tmp_path, edit, message):
    payload, _, _ = recorded(retry(tmp_path), {'accept_at': 2}, 2)
    forged = deepcopy(payload)
    edit(forged['projection'])

    with pytest.raises(ValueError, match=message):
        validate_journal(sign(forged))


def test_a_workflow_refuses_a_journal_whose_projection_records_other_limits(tmp_path):
    entry = retry(tmp_path)
    definition = load(entry)
    identity = workflow_identity(definition, timeouts={}, component_passes={}, limits={'PLAN_PASSES': 3})
    journal = WorkflowJournal(definition, semantic_identity=identity)
    workflow, _, _ = build(entry, config={'PLAN_PASSES': 3}, journal=journal)

    with pytest.raises(WorkflowError, match='different effective limits'):
        asyncio.run(workflow.arun({'accept_at': 2}))


def test_the_observation_factory_carries_effective_limits_into_the_journal(tmp_path):
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.workflows.observations import WorkflowObservationFactory
    definition = load(retry(tmp_path))

    adapter = WorkflowObservationFactory({}, boundary=EmissionBoundary()).create(
        definition, limits={'PLAN_PASSES': 3})

    assert adapter.projection['limits'] == [{'key': 'PLAN_PASSES', 'value': 3}]


def test_profile_admission_carries_the_limits_each_trusted_profile_selects(tmp_path):
    from api_agent.testing.workflow_admission import capture_workflow_admissions
    definition = load(retry(tmp_path))
    plan = SimpleNamespace(definitions=(definition,), case_definitions={'case': (definition.definition_id,)})

    admissions = capture_workflow_admissions(None, tool_plan=plan, profiles={'case': {'config': {'PLAN_PASSES': 3}}})

    assert admissions['case', definition.definition_id]['limits'] == [{'key': 'PLAN_PASSES', 'value': 3}]
