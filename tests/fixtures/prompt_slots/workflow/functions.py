"""Answer a question with the node's declared prompts as the system message."""
from uuid import uuid4

from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent


async def answer(inputs, context):
    request = LLMRequest(context.services['selection'], CompletionRequest((
        Message('system', (TextContent('\n\n'.join(context.prompts)),)),
        Message('user', (TextContent(inputs['question']),)))), str(uuid4()))
    result = await context.services['llm'](request)
    return {'answer': result.payload.content[0].text}
