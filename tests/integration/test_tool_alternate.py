"""Selected-provider proof for independent named targets and a REST alternate."""
from urllib.parse import urljoin

import httpx
import pytest

from api_agent.providers.llm import GenerationOptions
from api_agent.runtime.tasks import offload
from api_agent.workflows.definition import load_native_catalog
from api_agent.workflows.evidence import publish
from api_agent.workflows.execution import NodeFunction, Workflow
from api_agent.workflows.tool_evidence import tool_evidence


pytestmark = [pytest.mark.feature('Named tool workflows'), pytest.mark.requires_llm]
INSTANCES = {'primary_read': 'fixture/primary', 'secondary_read': 'fixture/secondary'}


def run_independent(workflow_tools, llm_provider, llm_selection, integration_config, *, graph_id):
    definition = workflow_tools.definition_for_case(graph_id)
    node = next(step for step in definition.layout.steps if step.id == 'conversation')
    native = load_native_catalog(definition, node.source)
    journal, events, ownership = {}, [], {}

    async def execute():
        operation = None
        try:
            async with workflow_tools.open(definition) as operation:
                assert not operation.plan.shared_primary
                selected = {tool.id: tool for tool in operation.plan.tools}
                assert set(selected) == set(INSTANCES.values())
                primary_gate = operation.tools.gate(scope_kind='node', scope='conversation',
                    port='primary', invocation_id='ownership-check')
                secondary_gate = operation.tools.gate(scope_kind='node', scope='conversation',
                    port='secondary', invocation_id='ownership-check')
                assert primary_gate.owner.owner is not secondary_gate.owner.owner
                endpoints = {alias: operation.endpoint_for(identity) for alias, identity in INSTANCES.items()}
                assert endpoints['primary_read'] != endpoints['secondary_read']
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    before = {alias: (await client.get(urljoin(endpoint, '/control/value'))).raise_for_status().json()
                              for alias, endpoint in endpoints.items()}
                    expected = {alias: value['hello'] for alias, value in before.items()}
                    assert len(set(expected.values())) == 2

                    async def complete(request):
                        return await offload(llm_provider.complete, request)

                    graph = Workflow(definition, {'converse': NodeFunction(native['converse'], 'llm')},
                        config=integration_config.values, services={
                            'tools': operation.tools, 'complete': complete, 'selection': llm_selection,
                            'generation': GenerationOptions(reasoning_effort='low'),
                            'expected_values': expected, 'tool_journal': journal, 'observe': events.append})
                    result = await graph.arun({})
                    after = {alias: (await client.get(urljoin(endpoint, '/control/value'))).raise_for_status().json()
                             for alias, endpoint in endpoints.items()}
                assert result['conversation'] == {'verified': True}
                for alias in INSTANCES:
                    assert after[alias]['backend_id'] == before[alias]['backend_id']
                    assert after[alias]['reads'] - before[alias]['reads'] == 1
                    headers = selected[INSTANCES[alias]].local_values.get('HEADERS', {})
                    expected_header = next((value for key, value in headers.items()
                                            if key.lower() == 'x-instance'), None)
                    assert after[alias]['headers'][-1:] == [expected_header]
                assert journal['completed'] and journal['final_matches']
                assert len(journal['calls']) == 2
                assert all(row['verified_by_inference'] and row['result_matches'] for row in journal['calls'])
        finally:
            if operation is not None:
                receipt = operation.receipt
                ownership.update(targets=2, owners_started=receipt['tool_count'],
                                 owners_remaining=receipt['remaining_owners'],
                                 service_leases=1 if receipt['service_count'] else 0)
            publish(tool_evidence(journal, graph_id=graph_id, instances=INSTANCES, ownership=ownership))
            publish({'graph_id': graph_id, 'events': events})
        assert operation.receipt['closed_tools'] == 2
        assert operation.receipt['remaining_owners'] == 0
        assert operation.receipt['released'] is True

    workflow_tools.run(execute())


def test_independent_tools(workflow_tools, llm_provider, llm_selection, integration_config):
    run_independent(workflow_tools, llm_provider, llm_selection, integration_config,
                    graph_id='independent_tools')
