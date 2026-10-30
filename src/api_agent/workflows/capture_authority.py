"""Invocation-local capture composition and credential-only admission.

Destination policies belong to the supplied observation factory. Borrowed
credentials authorize scrubbing only; they never enable a destination.
"""
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from types import MappingProxyType

from api_agent.providers.progress import component_timeout
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

_BOUND = ContextVar('workflow_capture_services', default=None)
_CAPTURE_KEYS = frozenset({'emissions', 'content_policy', 'observation_factory', 'capture_authority'})


@contextmanager
def bind_capture_services(services):
    """Bind an explicitly owned capture bundle for workflows invoked here."""
    if not isinstance(services, Mapping) or set(services) - _CAPTURE_KEYS:
        raise ValueError('Invalid workflow capture services')
    token = _BOUND.set(MappingProxyType(dict(services)))
    try:
        yield
    finally:
        _BOUND.reset(token)


def resolve_capture_services(explicit, *, inherited=None):
    """Select the highest explicit capture bundle without mixing authority owners."""
    if not isinstance(explicit, Mapping) or (inherited is not None and not isinstance(inherited, Mapping)):
        raise TypeError('Workflow services must be a mapping')
    inherited = inherited or {}
    selected = (explicit if _CAPTURE_KEYS.intersection(explicit) else
                inherited if _CAPTURE_KEYS.intersection(inherited) else (_BOUND.get() or {}))
    ordinary = {key: value for key, value in {**inherited, **explicit}.items()
                if key not in _CAPTURE_KEYS}
    return MappingProxyType({**ordinary,
        **{key: selected[key] for key in _CAPTURE_KEYS if key in selected}})


def requested_log_content(config, services):
    if 'LOG_CONTENT' in config:
        return config.get('LOG_CONTENT') is True
    policy = services.get('content_policy')
    return isinstance(policy, ContentPolicy) and policy.allow_content is True


async def capture_node(journal, services, *, visit_id, direction, values, schema,
                       plain_fields=..., tool_invocation_id=None, attempt_id=None):
    """Retain scrubbed values under a short lease, then dispatch callbacks."""
    if journal is None:
        return None
    tool_fields = ({'tool_invocation_id': tool_invocation_id, 'attempt_id': attempt_id}
                   if tool_invocation_id is not None else {})
    capability = services.get('capture_authority')
    emissions = services.get('emissions')
    if capability is None and type(emissions) is EmissionBoundary:
        @asynccontextmanager
        async def local_authority():
            lease = emissions.begin_evidence_lease()
            try:
                # Borrow registrations only; the journal/trace still owns its
                # independent destination permission. Retain before release.
                yield emissions.for_policy(ContentPolicy(True))
            finally:
                lease.release()
        capability = local_authority
    if capability is None:
        if plain_fields is not ...:
            return journal.capture(visit_id=visit_id, direction=direction, values=values,
                schema=schema, plain_fields=plain_fields)
        return journal.capture_json(visit_id=visit_id, direction=direction,
            values=values, schema=schema, **tool_fields)
    from .journal import defer_capture_callbacks
    from .capture import capture_reason, project_values
    callbacks = []
    result = None
    # Source leases end before any subscriber/sink/trace can reenter a provider.
    with defer_capture_callbacks(callbacks):
        async with AsyncExitStack() as owned:
            try:
                async with component_timeout('Workflow capture authority', 30):
                    boundary = await owned.enter_async_context(capability())
                if type(boundary) is not EmissionBoundary:
                    raise TypeError('Workflow capture credential authority unavailable')
            except Exception:
                result = journal.capture_json(visit_id=visit_id, direction=direction,
                    values={}, schema=schema, capture_reason='unavailable', **tool_fields)
            else:
                report = set()
                safe = project_values(values, schema, boundary, report=report)
                reason = capture_reason(safe, report)
                if plain_fields is not ... and reason == 'captured':
                    result = journal.capture(visit_id=visit_id, direction=direction,
                        values=safe, schema=schema, plain_fields=plain_fields)
                else:
                    result = journal.capture_json(visit_id=visit_id, direction=direction,
                        values=safe, schema=schema, capture_reason=reason, **tool_fields)
    for callback, row in callbacks:
        callback(row)
    return result
