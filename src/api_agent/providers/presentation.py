"""Request-local delivery of provider-approved, displayable summaries."""
from contextlib import contextmanager
from contextvars import ContextVar

_summary_sink = ContextVar('llm_display_summary_sink', default=None)


@contextmanager
def summary_delivery(sink):
    token = _summary_sink.set(sink)
    try:
        yield
    finally:
        _summary_sink.reset(token)


async def present_summary(result):
    """Publish summaries only; never infer them from raw text or opaque state."""
    sink = _summary_sink.get()
    if sink is not None and result.reasoning_summary:
        await sink(result.reasoning_summary)
