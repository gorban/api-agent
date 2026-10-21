"""Single selected-model chat call for the bundled application flow."""
from uuid import uuid4

from api_agent.providers.inference import request_from_wire, result_to_wire
from api_agent.providers.llm import GenerationOptions
from api_agent.workflows.execution import WorkflowError


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
