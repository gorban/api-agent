"""Trusted selected definitions and launch metadata cannot expand a worker context."""
import json

import pytest

from api_agent.testing import provider_setup
from api_agent.providers.contracts import ContractError
from api_agent.testing.workflow_admission import capture_workflow_admissions
from api_agent.tooling.selection import compose_tools
from test_tool_selection import selection_fixture
from test_tool_test_runtime import packet


def test_workflow_admission_uses_selected_definitions_without_application_fallback(selection_fixture):
    context, schema = selection_fixture
    plan = compose_tools(context, schema, selection=('base::tests/test_flow.py::test_dual',), environ={})

    admissions = capture_workflow_admissions(context, tool_plan=plan)

    assert set(admissions) == {plan.definitions[0].definition_id}
    assert admissions[plan.definitions[0].definition_id]['graph_id'] == 'dual_graph'
    empty = compose_tools(context, schema, selection=('base::tests/test_flow.py::test_graphless',), environ={})
    assert capture_workflow_admissions(context, tool_plan=empty) == {}


@pytest.mark.parametrize('change', [
    lambda data: data.update(tool_cases={'test': False}),
    lambda data: data.update(base_root='relative'),
    lambda data: data.pop('tool_plan'),
])
def test_invalid_tool_snapshot_metadata_is_rejected(packet, change):
    data = json.loads(packet.path.read_text())
    change(data)
    packet.path.write_text(json.dumps(data))

    with pytest.raises(ContractError, match='Integration provider context is unavailable'):
        provider_setup._read_snapshot()
