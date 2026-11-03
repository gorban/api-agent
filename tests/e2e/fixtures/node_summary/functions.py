"""An optional summary is projected without changing the node's answer schema."""
from dataclasses import replace
from uuid import uuid4

from api_agent.providers.llm import LLMRequest, Message, TextContent
from api_agent.providers.rich_inference import response_json


async def explain(inputs, context):
    messages = (Message('user', (TextContent(inputs['prompt']),)),)
    request = LLMRequest(context.services['selection'],
        context.response_request(messages, generation=context.services['generation']), str(uuid4()))
    result = await context.services['llm'](request)
    answer = response_json(result)['answer']
    context.services['result_store']['result'] = replace(result,
        payload=Message('assistant', (TextContent(answer),)), response=None)
    return {'answer': answer}


async def finish(inputs, context):
    # The browser test owns this rendezvous; production graphs need no delay.
    await context.services['before_final']()
    return inputs
