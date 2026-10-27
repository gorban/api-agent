"""Permanent contrived node catalog: never replaced by the useful demo."""
import json
from pathlib import Path
from uuid import UUID, uuid4
from dataclasses import asdict
import time
import math
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, StructuredOutput, Usage
from api_agent.providers.progress import component_timeout
from .execution import NodeFunction, Workflow, WorkflowError
from .definition import load_definition, load_native_catalog


def trial_decisions(trial, component):
    """Independent UUID nibbles choose the route and each initial readiness."""
    try:
        seed = UUID(trial)
    except (ValueError, TypeError, AttributeError):
        raise WorkflowError('Contrived trial requires a canonical version-4 UUID') from None
    if type(trial) is not str or str(seed) != trial.lower() or seed.version != 4:
        raise WorkflowError('Contrived trial requires a canonical version-4 UUID')
    if component not in ('first', 'second'):
        raise WorkflowError('Contrived producer requires a first or second component identity')
    route_digit = int(seed.hex[-1], 16)
    route = 'skip' if route_digit < 5 else 'next_a' if route_digit < 10 else 'next_b'
    ready_digit = int(seed.hex[-2 if component == 'first' else -3], 16)
    return {'component': component, 'route': route, 'ready': ready_digit < 8}


def requested_decisions(inputs, context):
    """Bind declared decisions to the actual component and correction state."""
    component = {'first_draft': 'first', 'second_draft': 'second'}.get(context.node_id)
    decisions = trial_decisions(context.services['trial'], component)
    upstream = inputs['upstream']
    if (component == 'first' and upstream is not None
            or component == 'second' and type(upstream) is not dict):
        raise WorkflowError('Contrived component identity disagrees with its upstream input')
    previous = inputs['previous']
    if (context.attempt == 1) != (previous is None):
        raise WorkflowError('Contrived correction requires its previous draft after the initial attempt')
    if previous is not None:
        if previous.get('route') != decisions['route']:
            raise WorkflowError('Contrived correction state changed its original routing choice')
        decisions = dict(decisions, route=previous['route'], ready=True)
    return decisions


async def produce(inputs, context):
    decisions = requested_decisions(inputs, context)
    previous = inputs['previous']
    tail = dict(decisions=decisions, upstream=inputs['upstream'], previous=previous, feedback=inputs['feedback'],
                attempt=context.attempt, max_passes=context.max_passes, trial=context.services['trial'])
    messages = (
        Message('system', (TextContent('\n\n'.join(context.prompts)),)),
        Message('user', (TextContent(inputs['user_input']),)),
        Message('user', (TextContent(json.dumps(tail, ensure_ascii=False)),)))
    contract = getattr(context, 'response_contract', None)
    if contract is not None and 'Output mode' in contract:
        payload = context.response_request(messages)
    else:
        # Four-column declarations retain their existing explicit native request.
        payload = CompletionRequest(messages, structured=StructuredOutput(
            'contrived_draft', json.dumps(context.services['output_schema'])))
    request = LLMRequest(context.services['selection'], payload, str(uuid4()))
    started = time.monotonic()
    async with component_timeout('Workflow LLM call', context.services.get('llm_timeout', 120)) as budget:
        result = await context.services['llm'](request)
        if budget.remaining == 0:
            raise TimeoutError('Workflow LLM call deadline expired')
    observer = context.services.get('observe')
    if observer is not None:
        observer(dict(event='llm', node=context.node_id, attempt=context.attempt,
                      elapsed=time.monotonic() - started, usage=asdict(getattr(result, 'usage', Usage()))))
    if result.outcome != 'completed' or len(result.payload.content) != 1:
        raise WorkflowError('Contrived producer did not return one completed structured result')
    try:
        decoded = strict_json(result.payload.content[0].text)
    except ValueError:
        raise WorkflowError('Contrived producer returned invalid JSON') from None
    if type(decoded) is not dict:
        raise WorkflowError('Contrived producer returned a non-object')
    if previous is not None and decoded.get('route') != previous.get('route'):
        raise WorkflowError('A correction changed its original routing choice')
    if decoded.get('route') != decisions['route']:
        raise WorkflowError('Contrived producer did not honor the requested route')
    if decoded.get('ready') is not decisions['ready']:
        raise WorkflowError('Contrived producer did not honor the requested readiness')
    return decoded


def strict_json(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate JSON key')
            value[key] = item
        return value
    def constant(value):
        raise ValueError('Nonfinite JSON number')
    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('Nonfinite JSON number')
        return parsed
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def validate_format(inputs, context):
    draft = inputs['draft']
    return {'valid': (draft['original_input'] == inputs['user_input']
                      and bool(draft['final_output'])
                      and inputs['user_input'] in draft['final_output'])}


def guard(inputs, context):
    accepted = inputs['draft']['ready'] and inputs['format']['valid']
    return {'accepted': accepted,
            'feedback': {'reason': 'Accepted' if accepted else 'Repair this draft: preserve original_input and route; make final_output include the original input and set ready=true.'}}


def build(base_root, *, llm, selection, trial, observe=None, config=None):
    root = Path(base_root).resolve()
    path = root / 'demo/contrived/flow.md.html'
    if not path.is_file():
        from importlib.resources import files
        path = Path(str(files('api_agent').joinpath('demo/contrived/flow.md.html')))
    definition = load_definition(path, base_root=root if path.is_relative_to(root) else path.parents[2],
                                 pack_root=path.parent.parent)
    draft = next(row for row in definition.layout.tables['Functions'] if row['ID'] == 'first_draft')
    schema = json.loads(definition.contents[draft['Output']])
    source = next(step.source for step in definition.layout.steps if step.id == 'first_draft')
    catalog = load_native_catalog(definition, source)
    return Workflow(definition, {'produce': NodeFunction(catalog['produce'], 'llm'),
        'validate_format': NodeFunction(catalog['validate_format']), 'guard': NodeFunction(catalog['guard'])},
        config=config,
        services=dict(llm=llm, selection=selection, trial=trial, output_schema=schema, observe=observe,
                      llm_timeout=(config or {}).get('WORKFLOW_LLM_TIMEOUT_SECONDS', 120)))
