"""Deterministic natives for the artifact acceptance qualification graph."""
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage
from langgraph.graph.message import add_messages


class State(TypedDict):
    messages: Annotated[list, add_messages]
    mode: str
    attempts: int
    feedback: str
    trail: list
    outcome: str


def intake(state, context):
    return {'trail': [*state['trail'], 'intake'], 'attempts': 0, 'feedback': '', 'outcome': ''}


async def work(state, context):
    trail = [*state['trail'], 'work:' + str(state['attempts'])]
    if state['mode'] == 'fail':
        raise RuntimeError('controlled qualification failure')
    if state['mode'] == 'block':
        context.services['started'].set()
        await context.services['gate'].wait()
    if state['attempts'] == 0:
        return {'trail': trail, 'attempts': 1, 'feedback': 'missing field: answer'}
    reply = 'accepted after feedback: ' + state['feedback']
    return {'trail': trail, 'attempts': state['attempts'] + 1, 'feedback': '',
            'messages': [AIMessage(id='answer', content=reply)]}


def route_work(state, context):
    return 'work' if state['feedback'] else 'finish'


def finish(state, context):
    return {'trail': [*state['trail'], 'finish'], 'outcome': 'done'}
