"""The declared optional-summary graph publishes before its final node ends."""
import asyncio
import json
from pathlib import Path
import pytest

from api_agent.providers.llm import GenerationOptions, LLMResult, Message, TextContent
from api_agent.providers.presentation import summary_delivery
from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import NodeFunction, Workflow

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / 'tests/e2e/fixtures/node_summary/flow.md.html'


def test_declared_summary_is_delivered_before_final_node_without_changing_answer_schema():
    async def scenario():
        definition = load_definition(ENTRY, base_root=ROOT, pack_root=ROOT)
        registry = {}
        for step in definition.layout.steps:
            row = next(row for row in definition.layout.tables['Functions'] if row['ID'] == step.id)
            registry[row['Registry']] = NodeFunction(load_native_catalog(definition, step.source)[row['Registry']], row['Kind'])
        requests, progress, results = [], [], {}
        async def provider(request):
            requests.append(request)
            schema = json.loads(request.payload.response.schema.schema)
            assert schema['properties']['result']['required'] == ['answer']
            return LLMResult(Message('assistant', (TextContent(json.dumps({
                'progress_summary': 'The check completed.', 'result': {'answer': 'Ok'}})),)))
        async def display(text):
            progress.append(text)
        async def before_final():
            assert progress == ['Explanation: The check completed.']
            assert results['result'].payload.content[0].text == 'Ok'
        workflow = Workflow(definition, registry, services={'selection': 'selected-model',
            'generation': GenerationOptions(), 'llm': provider, 'before_final': before_final,
            'result_store': results})
        with summary_delivery(display):
            output = await workflow.arun({'prompt': 'Check and answer Ok.'})
        assert output['finish'] == {'answer': 'Ok'}
        assert len(requests) == 1
    asyncio.run(scenario())


@pytest.mark.requires_llm
@pytest.mark.credential_authorities('workflow')
def test_optional_summary_with_selected_provider(llm_provider, llm_selection, integration_config):
    """Keep provider failures directly diagnosable alongside the browser proof."""
    from api_agent.runtime.tasks import offload
    async def scenario():
        definition = load_definition(ENTRY, base_root=ROOT, pack_root=ROOT)
        registry = {}
        for step in definition.layout.steps:
            row = next(row for row in definition.layout.tables['Functions'] if row['ID'] == step.id)
            registry[row['Registry']] = NodeFunction(load_native_catalog(definition, step.source)[row['Registry']], row['Kind'])
        progress, results = [], {}
        async def provider(request):
            return await offload(llm_provider.complete, request)
        async def display(text):
            progress.append(text)
        async def before_final():
            assert ''.join(progress).strip(), 'Selected route returned no display summary'
        workflow = Workflow(definition, registry, config=integration_config.values,
            services={'selection':llm_selection,'generation':GenerationOptions(),
                'llm':provider,'before_final':before_final,'result_store':results})
        with summary_delivery(display):
            output = await workflow.arun({'prompt':'Check that 17 times 19 equals 323. Return the answer field exactly Ok.'})
        assert set(output['finish']) == {'answer'}
        assert output['finish']['answer'].strip().lower().rstrip('.') == 'ok'
    asyncio.run(scenario())
