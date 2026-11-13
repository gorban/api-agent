"""Request-local delivery of provider-approved, displayable summaries."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
import asyncio
import weakref
from uuid import uuid4

from .contracts import ContractError

_summary_sink = ContextVar('llm_display_summary_sink', default=None)
_progress_sink = ContextVar('llm_reasoning_progress_sink', default=None)
_call_sink = ContextVar('llm_call_progress_sink', default=None)
_tool_sink = ContextVar('tool_progress_sink', default=None)
_delivered = ContextVar('llm_delivered_reasoning', default=None)
_display_section = ContextVar('llm_display_reasoning_section', default=None)


@dataclass(frozen=True)
class ReasoningProgress:
    """Display text with the supplied call ID, or a generated ID when omitted."""
    request_id: str
    text: str = field(repr=False)
    complete: bool = False


@contextmanager
def reasoning_progress(sink):
    """Let an application translate deltas or completed calls independently of UI."""
    token = _progress_sink.set(sink)
    delivered = _delivered.set(_delivered.get() if _delivered.get() is not None else {'completed': {}, 'objects': {}})
    try:
        yield
    finally:
        _progress_sink.reset(token)
        _delivered.reset(delivered)


@dataclass(frozen=True)
class CallProgress:
    """One completed model call made by a workflow node; content stays in memory."""
    node: str
    attempt: str | None
    request_id: str
    response_text: str = field(repr=False)
    reasoning_summary: str = field(repr=False)
    tool_calls: tuple = field(default=(), repr=False)


@contextmanager
def call_progress(sink):
    """Report each completed node model call to an async sink(CallProgress)."""
    token = _call_sink.set(sink)
    try:
        yield
    finally:
        _call_sink.reset(token)


@contextmanager
def tool_progress(sink):
    """Report tool dispatch to an async sink(kind, tool), kind being 'start' or 'end'."""
    token = _tool_sink.set(sink)
    try:
        yield
    finally:
        _tool_sink.reset(token)


def call_progress_bound():
    return _call_sink.get() is not None


async def publish_call_progress(node, attempt, request_id, result):
    sink = _call_sink.get()
    if sink is not None:
        await sink(CallProgress(node, attempt, request_id,
            ''.join(part.text for part in result.payload.content), result.reasoning_summary or '',
            tuple((call.name, call.arguments) for call in result.payload.calls)))


async def publish_tool_progress(kind, tool):
    sink = _tool_sink.get()
    if sink is not None:
        await sink(kind, tool)


@contextmanager
def summary_delivery(sink):
    token = _summary_sink.set(sink)
    delivered = _delivered.set({'completed': {}, 'objects': {}})
    section = _display_section.set({'current': None})
    try:
        yield
    finally:
        _summary_sink.reset(token)
        _delivered.reset(delivered)
        _display_section.reset(section)


async def _display_fragment(text, section):
    sink = _summary_sink.get()
    if sink is not None:
        state = _display_section.get()
        if text and state['current'] is not section:
            if state['current'] is not None:
                await sink('\n\n')
            state['current'] = section
        await sink(text)


async def present_summary(result, *, request_id=''):
    """Publish provider-admitted display text, without interpreting answer text."""
    if result.reasoning_summary:
        await _completed_progress(result, request_id, display=True)


async def _completed_progress(result, request_id, *, display, display_id=None):
    state = _delivered.get()
    if state is None:
        return
    identity = request_id
    if not identity:
        row = state['objects'].get(id(result))
        if row is None or row[0]() is not result:
            row = (weakref.ref(result), display_id or str(uuid4()))
            state['objects'][id(result)] = row
        identity = row[1]
    marker = (identity, sha256(result.reasoning_summary.encode('utf-8')).digest())
    if marker in state['completed']:
        await asyncio.shield(state['completed'][marker])
        return
    receipt = asyncio.get_running_loop().create_future()
    # Reserve before calling an async sink. Concurrent consumers share the
    # same completion, including its error, without delivering it twice.
    state['completed'][marker] = receipt
    try:
        if display:
            await _display_fragment(result.reasoning_summary, object())
        sink = _progress_sink.get()
        if sink is not None:
            await sink(ReasoningProgress(identity, result.reasoning_summary, complete=True))
    except BaseException as error:
        receipt.set_exception(error)
        receipt.exception()
        raise
    else:
        receipt.set_result(None)


async def complete_with_progress(provider, request, *, auth=None):
    """Consume ordinary text streams without exposing draft answers to a graph.

    The request-local sink may display or translate admitted reasoning. Rich
    requests retain their existing nonstreaming format/fallback negotiation.
    """
    delivery = _summary_sink.get()
    payload = request.payload
    if ((delivery is None and _progress_sink.get() is None) or payload.tools or payload.structured is not None
            or payload.response is not None
            or 'stream' not in getattr(getattr(provider, 'capabilities', None), 'operations', ())):
        return await provider.acomplete(request, auth=auth)
    result = None
    summary = ''
    text = ''
    section = object()
    display_id = request.request_id or str(uuid4())
    events = provider.astream(request, auth=auth)
    try:
        async for event in events:
            if result is not None:
                raise ContractError('Provider emitted content after stream completion')
            if event.kind == 'reasoning_summary':
                summary += event.payload.text
                if len(summary.encode('utf-8')) > 65536:
                    raise ContractError('Provider display reasoning exceeds byte limit')
                await _display_fragment(event.payload.text, section)
                sink = _progress_sink.get()
                if sink is not None:
                    await sink(ReasoningProgress(display_id, event.payload.text))
            elif event.kind == 'text':
                text += event.payload.text
                if len(text.encode('utf-8')) > 524288:
                    raise ContractError('Provider answer exceeds byte limit')
            elif event.kind == 'completed':
                result = event.payload
            else:
                raise ContractError('Unexpected tool call in text progress stream')
    finally:
        close = getattr(events, 'aclose', None)
        if callable(close):
            await close()
    if (result is None or text != ''.join(part.text for part in result.payload.content)
            or summary != (result.reasoning_summary or '')):
        raise ContractError('Provider progress differs from completed result')
    if summary:
        await _completed_progress(result, request.request_id, display=False, display_id=display_id)
    return result
