"""Execute the selected one-pass chat declaration through its native binding."""
import json
from dataclasses import replace

from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import result_to_wire
from .definition import load_native_catalog
from .execution import NodeFunction, Workflow, WorkflowError
from .resources import bundled_root


class LocalChatApplication:
    """Freeze the admitted definition; unsupported application shapes fail early."""
    def __init__(self, definition, *, config=None, observations=None):
        self.definition, self.config = definition, dict(config or {})
        self.observations = observations
        layout = definition.layout
        nodes = [step for step in layout.steps if step.kind == 'node']
        functions = layout.tables['Functions']
        if (len(nodes) != 1 or nodes[0].id != 'llm_call' or len(functions) != 1
                or functions[0]['Kind'] != 'llm' or layout.tables['Edges'] or layout.tables['Prompts']
                or any(row['Guard'] != '-' or int(row['Max passes']) != 1 for row in layout.tables['Components'])):
            raise ContractError('Selected workflow does not implement the local chat application contract.')
        contract_root = bundled_root() / 'workflows/contracts'
        for column, filename in (('Input', 'llm_call_input.json'), ('Output', 'llm_call_output.json')):
            if json.loads(definition.contents[functions[0][column]]) != json.loads((contract_root / filename).read_bytes()):
                raise ContractError('Selected workflow has an unsupported local chat schema.')
        catalog = load_native_catalog(definition, nodes[0].source)
        self.registry = {functions[0]['Registry']: NodeFunction(catalog[functions[0]['Registry']], 'llm')}
        # Compile now, before local services or provider resources are acquired.
        Workflow(definition, self.registry, config=self.config)

    async def complete(self, request, *, llm, context):
        from api_agent.providers.inference import async_request_to_wire
        await async_request_to_wire(request)
        messages = []
        for message in request.payload.messages:
            if (message.role not in ('system', 'user', 'assistant') or message.calls or message.results
                    or message.continuation is not None or message.cache_boundary is not None):
                raise ContractError('Local chat accepts plain text messages only.')
            messages.append({'role': message.role, 'content': ''.join(part.text for part in message.content)})
        if request.payload.tools or request.payload.structured is not None or request.payload.response is not None or request.payload.generation.temperature is not None:
            raise ContractError('Local chat does not support this request shape.')
        results = []
        called = False
        async def selected(value):
            nonlocal called
            if called or value.selected_llm != request.selected_llm or value.payload != request.payload or value.request_id != request.request_id:
                raise WorkflowError('Local chat changed the selected provider call.')
            called = True
            result = await llm.complete(value, context=context)
            results.append(result)
            return result
        observations = self.observations.create(self.definition) if self.observations is not None else None
        graph = Workflow(self.definition, self.registry, config=self.config,
            services={'llm': selected, 'selection': request.selected_llm,
                      'generation': request.payload.generation, 'request_id': request.request_id,
                      'journal': observations, 'observation_factory': self.observations})
        try:
            values = await graph.arun({'messages': messages})
        finally:
            if observations is not None:
                observations.close()
        if len(results) != 1:
            raise WorkflowError('Local chat did not execute its declared LLM call.')
        expected = result_to_wire(replace(results[0], response=None))
        expected['response_text'] = expected.pop('text')
        expected['selected_model'] = request.selected_llm
        if values.get('llm_call') != expected:
            raise WorkflowError('Local chat output does not match its declared LLM result.')
        return results[0]
