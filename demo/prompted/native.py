"""Typed demo whose declared prompt slots are filled from the selected resource pack."""
import tomllib
from typing import TypedDict
from uuid import uuid4

from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent


class PromptedState(TypedDict, total=False):
    question: str
    answer: str
    turns: int


def _profile(context):
    return tomllib.loads(context.resources['profile'].content_bytes.decode('utf-8'))


def assistant_descriptor(state, context):
    return _profile(context)['assistant_descriptor']


def domain_rules(state, context):
    rules = _profile(context).get('domain_rules', [])
    return '\n'.join(f'- {rule}' for rule in rules) if rules else '- Follow the user request.'


async def answer(state, context):
    request = LLMRequest(context.services['selection'], CompletionRequest((
        Message('system', (TextContent('\n\n'.join(context.prompts)),)),
        Message('user', (TextContent(state['question']),)))), str(uuid4()))
    result = await context.services['llm'](request)
    return {'answer': result.payload.content[0].text, 'turns': state.get('turns', 0) + 1}
