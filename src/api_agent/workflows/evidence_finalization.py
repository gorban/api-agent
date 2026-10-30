"""Ownership of asynchronous evidence publication within a workflow visit."""
import asyncio
from contextvars import ContextVar

_PENDING = ContextVar('workflow_evidence_pending', default=None)


def begin():
    return _PENDING.set([])


def end(token):
    _PENDING.reset(token)


def enroll(future):
    pending = _PENDING.get()
    if pending is not None:
        pending.append(future)


async def finalize():
    pending = _PENDING.get()
    if not pending:
        return
    # Cancellation does not abandon an owned publisher while it can still write
    # to this visit. The transport's own deadline bounds the work.
    from api_agent.runtime.tasks import settle
    work = asyncio.ensure_future(asyncio.gather(
        *(asyncio.wrap_future(future) for future in pending), return_exceptions=True))
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        await settle(work)
        raise
