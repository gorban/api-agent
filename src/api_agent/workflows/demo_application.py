"""Selected useful demo, separate from the admitted one-call chat application."""
from uuid import uuid4

from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import async_request_to_wire
from api_agent.providers.inference import result_to_wire
from api_agent.workflows.definition import load_native_catalog
from api_agent.workflows.execution import NodeFunction, Workflow, WorkflowError
from api_agent.workflows.tool_gate import ToolGate


class UsefulDemoApplication:
    """Admit one declared planning/review/tool/render graph before acquiring services."""
    requires_api = True
    NODE_KINDS = {'plan': 'llm', 'review': 'llm', 'execute_tool': 'deterministic', 'render': 'llm'}

    def __init__(self, definition, *, config=None, observations=None, tool_owner=None):
        self.definition, self.config = definition, dict(config or {})
        self.observations, self.tool_owner = observations, tool_owner
        self.tools = None
        layout = definition.layout
        nodes = [step for step in layout.steps if step.kind == 'node']
        rows = {row['ID']: row for row in layout.tables['Functions']}
        if ({step.id for step in nodes} != set(self.NODE_KINDS)
                or set(rows) != set(self.NODE_KINDS)
                or any(rows[name]['Kind'] != kind for name, kind in self.NODE_KINDS.items())
                or [step.id for step in nodes] != list(self.NODE_KINDS)
                or [(step.id, step.parent) for step in layout.steps] != [
                    ('draft', None), ('plan', 'draft'), ('review', 'draft'),
                    ('execute_tool', None), ('render', None)]
                or [(row['From'], row['To'], row['When']) for row in layout.tables['Edges']] != [
                    ('draft', 'execute_tool', 'plan.use_tool == True'),
                    ('draft', 'render', 'otherwise')]
                or len(layout.tables['Components']) != 1
                or layout.tables['Components'][0]['Guard'] != 'review'):
            raise ContractError('Selected workflow does not implement the useful demo contract')
        self.registry = {}
        for step in nodes:
            row = rows[step.id]
            catalog = load_native_catalog(definition, step.source)
            self.registry[row['Registry']] = NodeFunction(catalog[row['Registry']], row['Kind'])
        Workflow(definition, self.registry, config=self.config)

    async def complete(self, request, *, llm, context, tool_owner=None):
        await async_request_to_wire(request)
        messages = []
        for message in request.payload.messages:
            if (message.role not in ('system', 'user', 'assistant') or message.calls or message.results
                    or message.continuation is not None or message.cache_boundary is not None):
                raise ContractError('Useful demo accepts plain text messages')
            messages.append({'role': message.role,
                             'content': ''.join(part.text for part in message.content)})
        if (request.payload.tools or request.payload.structured is not None
                or request.payload.generation.temperature is not None):
            raise ContractError('Useful demo does not accept caller tools or structured output')
        selected_owner = tool_owner or self.tool_owner
        if selected_owner is None and self.tools is None:
            raise ContractError('Useful demo requires a selected tool owner')
        invocation_id = request.request_id or str(uuid4())
        factory = getattr(selected_owner, 'for_invocation', None)
        view = factory() if callable(factory) else selected_owner
        close_view = view is not selected_owner
        if self.tools is not None:
            view = self.tools.for_port(scope_kind='node', scope='execute_tool', port='primary')
            close_view = True
        observations = None
        try:
            gate = ToolGate(view, invocation_id=invocation_id)
            observations = self.observations.create(self.definition) if self.observations is not None else None
            services = {'selection': request.selected_llm, 'generation': request.payload.generation,
                        'messages': messages, 'tool_gate': gate, 'journal': observations,
                        'observation_factory': self.observations, 'tools': self.tools}

            async def selected(value):
                if value.selected_llm != request.selected_llm:
                    raise WorkflowError('Useful demo changed selected model')
                return await llm.complete(value, context=context)

            services['llm'] = selected
            workflow = Workflow(self.definition, self.registry, config=self.config, services=services)
            values = await workflow.arun({'messages': messages})
            rendered = services.get('rendered_result')
            if (rendered is None or values.get('render', {}).get('selected_model') != request.selected_llm
                    or values['render'].get('response_text') != result_to_wire(rendered)['text']):
                raise WorkflowError('Useful demo did not render through its selected model')
            return rendered
        finally:
            try:
                if observations is not None:
                    observations.close()
            finally:
                if close_view:
                    await view.aclose()
