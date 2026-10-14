"""Permanent contrived node catalog: never replaced by the useful demo."""
import json
from pathlib import Path
from uuid import uuid4
import runpy
from dataclasses import asdict
import time
import math
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, StructuredOutput, Usage
from api_agent.providers.progress import component_timeout
from .execution import NodeFunction, Workflow, WorkflowError
from .layout import load_layout


async def produce(inputs, context):
    previous = inputs['previous']
    tail = dict(upstream=inputs['upstream'], previous=previous, feedback=inputs['feedback'],
                attempt=context.attempt, max_passes=context.max_passes, trial=context.services['trial'])
    schema = context.services['output_schema']
    request = LLMRequest(context.services['selection'], CompletionRequest((
        Message('system', (TextContent('\n\n'.join(context.prompts)),)),
        Message('user', (TextContent(inputs['user_input']),)),
        Message('user', (TextContent(json.dumps(tail, ensure_ascii=False)),))),
        structured=StructuredOutput('contrived_draft', json.dumps(schema))), str(uuid4()))
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
    if inputs['draft']['original_input'] != inputs['user_input']:
        raise WorkflowError('The original user input was not preserved verbatim')
    if inputs['user_input'] not in inputs['draft']['final_output'] or not inputs['draft']['final_output']:
        raise WorkflowError('Final output must repeat the original user input')
    return {'valid': True}


def guard(inputs, context):
    return {'accepted': inputs['draft']['ready'],
            'feedback': {'reason': 'Accepted' if inputs['draft']['ready'] else 'Repair this draft: set ready=true while preserving original_input and route.'}}


def build(base_root, *, llm, selection, trial, observe=None, config=None):
    root = Path(base_root).resolve()
    path = root / 'demo/contrived/layout.md'
    if not path.is_file():
        from importlib.resources import files
        path = Path(str(files('api_agent').joinpath('demo/contrived/layout.md')))
    layout = load_layout(path, base_root=root, pack_root=path.parent.parent)
    schema = json.loads((path.parent / 'schemas/draft.json').read_text())
    catalog = runpy.run_path(str(path.parent / 'functions.py'))
    return Workflow(layout, {'produce': NodeFunction(catalog['produce'], 'llm'),
        'validate_format': NodeFunction(catalog['validate_format']), 'guard': NodeFunction(catalog['guard'])},
        {'skip_second': lambda values: values['first_draft']['route'] == 'skip'}, config=config,
        services=dict(llm=llm, selection=selection, trial=trial, output_schema=schema, observe=observe,
                      llm_timeout=(config or {}).get('WORKFLOW_LLM_TIMEOUT_SECONDS', 120)))
