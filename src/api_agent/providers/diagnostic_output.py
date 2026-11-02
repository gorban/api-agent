"""Operation-local observation of owned stderr without global stream redirection."""
from contextlib import contextmanager
from contextvars import ContextVar

_SINK = ContextVar('api_agent_diagnostic_stderr', default=None)


def bounded_stderr(text):
    # Withhold an oversized raw capture rather than split a credential before
    # the receiving operation has applied its complete credential authority.
    return text if len(text) <= 65536 else 'stderr omitted: capture exceeded the 65536-character limit.\n'


def bounded_stdout(text):
    return text if len(text) <= 65536 else 'stdout omitted: capture exceeded the 65536-character limit.\n'


@contextmanager
def diagnostic_output(sink):
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def observe_stderr(text):
    sink = _SINK.get()
    if sink is not None:
        sink(text)
