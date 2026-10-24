"""Single selected-model chat call for the bundled application flow."""
from uuid import uuid4

from api_agent.providers.inference import request_from_wire, result_to_wire
from api_agent.providers.llm import GenerationOptions
from api_agent.workflows.execution import WorkflowError
from api_agent.workflows.tool_gate import ToolGateError
from api_agent.providers.tools import MAX_ARGUMENT_BYTES, canonical
import json
import time


async def llm_call(inputs, context):
    generation = context.services.get('generation') or GenerationOptions()
    selection = context.services['selection']
    messages = inputs['messages']
    request_id = context.services.get('request_id')
    if request_id is None:
        request_id = str(uuid4())
    wire = {
        'selected_llm': selection,
        'request_id': request_id,
        'messages': [{'role': item['role'], 'text': item['content']} for item in messages],
        'max_output_tokens': generation.max_output_tokens,
        'cache_policy': generation.cache_policy,
    }
    if generation.reasoning_effort is not None:
        wire['reasoning_effort'] = generation.reasoning_effort
    request = request_from_wire(wire)
    result = await context.services['llm'](request)
    if result.outcome != 'completed':
        raise WorkflowError('Chat inference did not complete')
    response = result_to_wire(result)
    if not response['text']:
        raise WorkflowError('Chat inference returned no text')
    response['response_text'] = response.pop('text')
    return {'selected_model': selection, **response}


async def _demo_llm(context, purpose, payload):
    """Every model call uses the source-selected model and current workflow visit."""
    wire = {'selected_llm': context.services['selection'],
            'request_id': str(uuid4()),
            'messages': [{'role': 'system', 'text': purpose},
                         {'role': 'user', 'text': json.dumps(payload, sort_keys=True)}],
            'max_output_tokens': context.services['generation'].max_output_tokens,
            'cache_policy': context.services['generation'].cache_policy}
    if context.services['generation'].reasoning_effort is not None:
        wire['reasoning_effort'] = context.services['generation'].reasoning_effort
    result = await context.services['llm'](request_from_wire(wire))
    if result.outcome != 'completed':
        raise WorkflowError('Demo inference did not complete')
    return result, result_to_wire(result)['text']


async def plan(inputs, context):
    spec = await context.services['tool_gate'].describe()
    result, text = await _demo_llm(context,
        'Plan a read-only response. Return JSON with use_tool:boolean, arguments:object, answer:string. '
        'Do not execute the tool. Use empty arguments when no tool is needed.',
        {'messages': inputs['messages'], 'feedback': inputs.get('feedback') or {},
         'tool': {'name': spec.name, 'description': spec.description,
                  'input_schema': spec.input_schema}})
    try:
        candidate = json.loads(text)
    except ValueError as error:
        raise WorkflowError('Planner returned invalid JSON') from error
    if (type(candidate) is not dict or type(candidate.get('use_tool')) is not bool
            or type(candidate.get('arguments')) is not dict or type(candidate.get('answer')) is not str
            or len(candidate['answer']) > 8192
            or (candidate['use_tool'] and not candidate['arguments'])
            or (not candidate['use_tool'] and candidate['arguments'])):
        raise WorkflowError('Planner returned invalid candidate')
    try:
        canonical(candidate['arguments'], MAX_ARGUMENT_BYTES)
    except ValueError as error:
        raise WorkflowError('Planner returned invalid candidate') from error
    return {key: candidate[key] for key in ('use_tool', 'arguments', 'answer')}


async def review(inputs, context):
    candidate = inputs['candidate']
    gate = context.services['tool_gate']
    verdict = {}

    async def relevance(prepared):
        nonlocal verdict
        _, text = await _demo_llm(context,
            'Assess correctness, grounding, relevance and presentation preference. '
            'Return JSON with correct, grounded, relevant, preference booleans. '
            'A query is only a candidate and has not run.',
            {'messages': context.services['messages'], 'candidate': candidate})
        try:
            verdict = json.loads(text)
        except ValueError as error:
            raise WorkflowError('Reviewer returned invalid JSON') from error
        if type(verdict) is not dict or any(type(verdict.get(key)) is not bool for key in
                                              ('correct', 'grounded', 'relevant', 'preference')):
            raise WorkflowError('Reviewer returned invalid verdict')
        return verdict

    grant = None
    if candidate['use_tool']:
        try:
            prepared = await gate.prepare(candidate['arguments'])
            grant = await gate.verify(prepared, relevance=relevance)
        except (ToolGateError, ValueError):
            return {'accepted': False, 'feedback': {'reason': 'Invalid or ungrounded tool candidate'}}
    else:
        verdict = await relevance(None)
    accepted = (verdict['correct'] and verdict['grounded'] and verdict['relevant']
                and (verdict['preference'] or context.attempt == context.max_passes))
    if accepted:
        context.services['verified_call'] = grant
    return {'accepted': accepted, 'feedback': {'reason': 'Revise candidate against the request'}}


async def execute_tool(inputs, context):
    grant = context.services.get('verified_call')
    if grant is None:
        raise WorkflowError('Tool execution lacked accepted verification')
    started = time.monotonic()
    call_id = str(uuid4())
    try:
        result = await context.services['tool_gate'].execute(grant)
    except Exception:
        context.record_call(call_id=call_id, elapsed=time.monotonic() - started, outcome='failed')
        raise
    context.record_call(call_id=call_id, elapsed=time.monotonic() - started, outcome=result.status)
    context.services['tool_result'] = result
    return {'status': result.status, 'data': result.data,
            'errors': list(result.errors), 'diagnostic': result.diagnostic}


async def render(inputs, context):
    candidate = inputs['candidate']
    result = context.services.get('tool_result')
    tool = None if result is None else {'status': result.status, 'data': result.data,
                                       'errors': list(result.errors), 'diagnostic': result.diagnostic}
    response, text = await _demo_llm(context,
        'Answer the user using the checked plan and tool evidence. Preserve partial data and errors, '
        'and describe failures accurately. Do not claim an unexecuted query ran.',
        {'messages': context.services['messages'], 'candidate': candidate, 'tool': tool})
    if not text:
        raise WorkflowError('Renderer returned no text')
    context.services['rendered_result'] = response
    return {'response_text': text, 'selected_model': context.services['selection']}
