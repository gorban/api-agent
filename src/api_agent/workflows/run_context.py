"""Trusted, task-local grouping labels for independently identified executions."""
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

_EXECUTION = ContextVar('workflow_execution_context', default=None)
_TURN = ContextVar('workflow_execution_turn', default=None)


def canonical_uuid(value):
    if type(value) is not str or len(value) != 36:
        raise ValueError('Invalid execution identity')
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ValueError('Invalid execution identity') from None
    return value


def validate_execution_context(value):
    if type(value) is not dict:
        raise ValueError('Invalid execution context')
    if set(value) == {'kind', 'conversation_id'} and value['kind'] == 'conversation':
        canonical_uuid(value['conversation_id'])
    elif (set(value) == {'kind', 'trial_index'} and value['kind'] == 'trial'
          and type(value['trial_index']) is int and 1 <= value['trial_index'] <= 60):
        pass
    else:
        raise ValueError('Invalid execution context')
    return dict(value)


def current_execution_context():
    value = _EXECUTION.get()
    return dict(value) if value is not None else None


def current_turn_id():
    return _TURN.get()


@contextmanager
def _bind(variable, value):
    token = variable.set(value)
    try:
        yield
    finally:
        variable.reset(token)


def conversation(conversation_id):
    return _bind(_EXECUTION, validate_execution_context(
        {'kind': 'conversation', 'conversation_id': conversation_id}))


def trial(trial_index):
    return _bind(_EXECUTION, validate_execution_context(
        {'kind': 'trial', 'trial_index': trial_index}))


def turn(turn_id):
    return _bind(_TURN, canonical_uuid(turn_id))


def execution_context(value):
    """Bind an admitted envelope for the actual execution, including legacy absence."""
    return _bind(_EXECUTION, validate_execution_context(value) if value is not None else None)
