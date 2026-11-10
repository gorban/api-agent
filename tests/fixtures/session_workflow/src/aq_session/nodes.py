"""Deterministic nodes for the session-state workflow fixture."""
import asyncio

from api_agent.workflows.execution import WorkflowError


def reply(inputs, context):
    message = inputs['message']
    text = f'{message} (turns seen: {len(inputs["history"]) // 2})'
    seen = {'history': len(inputs['history']), 'lines': inputs['lines'], 'last': inputs['last']}
    return {'reply': text, 'topic': message, 'seen': seen,
            'messages': [{'role': 'user', 'text': message}, {'role': 'assistant', 'text': text}]}


async def draft(inputs, context):
    if inputs['message'] == 'boom':
        raise WorkflowError('draft failed')
    if inputs['message'] == 'hang':
        await asyncio.Event().wait()
    line = f'{inputs["message"]}#{inputs["attempt"]}'
    return {'text': line, 'lines': [line], 'saw_topic': inputs['known_topic']}


def check(inputs, context):
    accepted = inputs['attempt'] >= 2
    return {'accepted': accepted, 'feedback': {'reason': 'accepted' if accepted else 'retry once'}}
