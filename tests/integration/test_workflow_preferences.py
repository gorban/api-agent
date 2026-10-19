"""Controlled preference fallback uses its checked-in graph declaration."""
import asyncio
from pathlib import Path

from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import Workflow, NodeFunction, WorkflowError
from api_agent.workflows.evidence import publish
import pytest

pytestmark = pytest.mark.feature('Workflow preferences')
ROOT = Path(__file__).parent / 'fixtures/workflow_preferences'


def controlled_workflow(events):
    definition = load_definition(ROOT / 'flow.md.html', base_root=ROOT, pack_root=ROOT)
    source = next(step.source for step in definition.layout.steps if step.id == 'draft')
    catalog = load_native_catalog(definition, source)
    return Workflow(definition, {
        'produce': NodeFunction(catalog['produce']),
        'guard': NodeFunction(catalog['guard']),
    }, services={'observe': events.append})


def test_final_pass_relaxes_only_preference():
    events = []
    workflow = controlled_workflow(events)
    values = asyncio.run(workflow.arun({'truth': True}))
    publish({'events': events})
    assert values['draft'] == {'truth': True, 'preference': False}
    assert [row['accepted'] for row in events if row['event'] == 'guard'] == [False, True]


def test_truth_failure_stays_blocking_on_final_pass():
    events = []
    workflow = controlled_workflow(events)
    with pytest.raises(WorkflowError, match='exhausted'):
        asyncio.run(workflow.arun({'truth': False}))
    publish({'events': events})
    assert [row['accepted'] for row in events if row['event'] == 'guard'] == [False, False]
