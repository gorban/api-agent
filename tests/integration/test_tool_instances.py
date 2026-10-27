"""Actual selected-provider inference through two Runtime-owned GraphQL tools."""
from pathlib import Path
from runpy import run_path
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest


from api_agent.providers.llm import GenerationOptions
from api_agent.runtime.tasks import offload
from api_agent.workflows.definition import load_native_catalog
from api_agent.workflows.evidence import publish
from api_agent.workflows.execution import NodeFunction, Workflow
from api_agent.workflows.tool_evidence import tool_evidence


# This is a sibling test resource, not an admitted tool/native factory.
measure_targets = run_path(str(Path(__file__).parent / "fixtures/tool_instances/measurement.py"))["measure_targets"]

pytestmark = [pytest.mark.feature('Named tool workflows'), pytest.mark.requires_llm]
INSTANCES = {'primary_read': 'fixture/primary', 'secondary_read': 'fixture/shared-secondary'}


def test_shared_primary(workflow_tools, llm_provider, llm_selection, integration_config):
    definition = workflow_tools.definition_for_case('shared_primary_tools')
    node = next(step for step in definition.layout.steps if step.id == 'conversation')
    native = load_native_catalog(definition, node.source)
    journal, events, ownership, sharing = {}, [], {}, None

    async def execute():
        nonlocal sharing
        operation = None
        target_count = 0
        try:
            async with workflow_tools.open(definition) as operation:
                assert operation.plan.shared_primary
                assert len(operation.plan.tools) == 2
                endpoint = operation.endpoint_for('fixture/primary')
                secondary = operation.endpoint_for('fixture/shared-secondary')
                target_count = len({endpoint, secondary})
                assert secondary == endpoint
                selected = {tool.id: tool for tool in operation.plan.tools}
                derived = selected['fixture/shared-secondary'].derived_from
                assert derived == {'ENDPOINT': 'fixture/primary', 'HEADERS': 'fixture/primary'}
                sharing = {'primary': 'fixture/primary', 'secondary': ['fixture/shared-secondary'],
                           'derived_slots': sorted(derived)}
                parsed = urlsplit(endpoint)
                control = urlunsplit((parsed.scheme, parsed.netloc, '/control/value', '', ''))
                async with measure_targets((endpoint, secondary)):
                    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                        before = (await client.get(control)).raise_for_status().json()
                        assert before['seed_runs'] == 1
                        expected = before['hello']

                        async def complete(request):
                            return await offload(llm_provider.complete, request)

                        graph = Workflow(definition, {'converse': NodeFunction(native['converse'], 'llm')},
                            config=integration_config.values, services={
                                'tools': operation.tools, 'complete': complete, 'selection': llm_selection,
                                'generation': GenerationOptions(reasoning_effort=integration_config.values.get('LLM_REASONING_EFFORT') or None),
                                'expected_value': expected, 'tool_journal': journal, 'observe': events.append})
                        result = await graph.arun({})
                        after = (await client.get(control)).raise_for_status().json()
                    assert result['conversation'] == {'verified': True}
                    assert after['backend_id'] == before['backend_id']
                    assert after['reads'] - before['reads'] == 2
                    assert len(journal['calls']) == 2
                    assert sum(row['kind'] == 'relevance' for row in journal['inference']) == 2
                    assert all(row['verified_by_inference'] and row['result_matches'] for row in journal['calls'])
                    assert journal['final_matches'] is True
        finally:
            if operation is not None:
                receipt = operation.receipt
                ownership.update(targets=target_count, owners_started=receipt['tool_count'],
                                 owners_remaining=receipt['remaining_owners'],
                                 service_leases=1 if receipt['service_count'] else 0)
            publish(tool_evidence(journal, graph_id=definition.graph_id,
                                  instances=INSTANCES, ownership=ownership, sharing=sharing))
            publish({'graph_id': definition.graph_id, 'events': events})
        assert operation.receipt['tool_count'] == 2
        assert operation.receipt['service_count'] in (0, 2)
        assert operation.receipt['closed_tools'] == 2
        assert operation.receipt['remaining_owners'] == 0
        assert operation.receipt['released'] is True

    workflow_tools.run(execute())
