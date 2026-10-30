"""Credential-authority provider payload capture and workflow attribution."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID, uuid4

from .attribution import current_workflow_visit

_COLLECTOR = ContextVar('provider_payload_collector', default=None)
_CALL_ID = ContextVar('provider_payload_call_id', default=None)
_CALL_REQUESTS = ContextVar('provider_payload_requests', default=None)
_DIRECT_ROWS = ContextVar('provider_payload_direct_rows', default=None)
_DISPATCH = ContextVar('provider_response_dispatch', default=None)
_WITHHELD = ContextVar('provider_inference_content_withheld', default=False)


@contextmanager
def withholding_inference_content(withhold=True):
    """Withhold evidence while a producer cannot join its credential authority."""
    token = _WITHHELD.set(_WITHHELD.get() or withhold)
    try:
        yield
    finally:
        _WITHHELD.reset(token)


@contextmanager
def bind_inference_call(call_id):
    """Join adapter-added request capture to the existing canonical call."""
    token = _CALL_ID.set(call_id)
    requests_token = _CALL_REQUESTS.set([])
    rows_token = _DIRECT_ROWS.set({'rows': [], 'overflow': False, 'remaining': 4 * 1024 * 1024, 'transport_diagnostics': {}})
    try:
        yield
    finally:
        rows = _DIRECT_ROWS.get()['rows']
        if rows:
            selected = select_capture_rows(rows)
            for row in selected:
                row.setdefault('capture_source', 'provider')
            capture_rows(selected, call_id)
        _DIRECT_ROWS.reset(rows_token)
        _CALL_REQUESTS.reset(requests_token)
        _CALL_ID.reset(token)


@contextmanager
def bind_response_dispatch(observer=None):
    """Retain content-free facts across inherited tasks, including cancellation."""
    inherited = _DISPATCH.get()
    state = inherited if inherited is not None and observer is None else {'response': None, 'usage': None, 'observer': observer}
    token = _DISPATCH.set(state)
    try:
        yield state
    finally:
        _DISPATCH.reset(token)


@contextmanager
def isolated_response_dispatch():
    """Keep each physical dispatch's evidence separate from prior failures."""
    with bind_response_dispatch(lambda evidence: None) as state:
        yield state


def promote_response_dispatch(state):
    """Admit the successful physical dispatch into the enclosing call."""
    if state['response'] is not None:
        record_response_dispatch(state['response'])


def record_response_dispatch(evidence):
    """Adapters call this after auth/preflight, immediately before dispatch."""
    from api_agent.providers.rich_inference import response_evidence_to_wire
    from api_agent.providers.contracts import ContractError
    response_evidence_to_wire(evidence)
    state = _DISPATCH.get()
    if state is None:
        return
    if state['response'] is not None:
        if state['response'] != evidence:
            raise ContractError('Conflicting dispatched response policy')
        return
    state['response'] = evidence
    if state['observer'] is not None:
        state['observer'](evidence)


def retain_response_failure(error, response, usage=None):
    """Merge admitted dispatch facts without hiding contradictory evidence."""
    if response is None:
        return
    from api_agent.providers.rich_inference import response_evidence_to_wire
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.inference import error_usage
    response_evidence_to_wire(response)
    terminal = getattr(error, 'response', None)
    if terminal is not None and terminal != response:
        conflict = ContractError('Conflicting dispatched response policy')
        conflict.response = response
        conflict.usage = error_usage(error) or usage
        raise conflict
    error.response = response
    if usage is not None and error_usage(error) is None:
        error.usage = usage
        error_usage(error)


def record_response_failure(error):
    """Keep explicitly reported partial usage before existing error wrappers."""
    from api_agent.providers.inference import error_usage
    state = _DISPATCH.get()
    if state is not None and state['response'] is not None:
        usage = error_usage(error)
        if usage is not None:
            state['usage'] = usage


@contextmanager
def collect_inference(boundary=None, call_id=None, *, retain_raw=False):
    """Collect only payloads scrubbed inside the provider's auth callback."""
    with bind_response_dispatch() as dispatch:
        collector = {'boundary': boundary, 'call_id': call_id or str(uuid4()), 'rows': [], 'dispatch': dispatch,
                     'retain_raw': retain_raw is True,
                     'remaining': 4 * 1024 * 1024, 'charged': {}, 'transport_diagnostics': {},
                     'trace_collector': None, 'trace_owner': None,
                     'content_withheld': boundary is not None and
                         getattr(getattr(boundary, 'policy', None), 'allow_content', None) is not True}
        token = _COLLECTOR.set(collector)
        try:
            yield collector
        finally:
            _COLLECTOR.reset(token)


def active_inference_boundary():
    """Return the evidence authority for this call, when one exists."""
    from api_agent.telemetry.privacy import EmissionBoundary
    collector = _COLLECTOR.get()
    if collector is not None:
        boundary = collector.get('boundary')
        return boundary if isinstance(boundary, EmissionBoundary) else None
    owner = current_workflow_visit()
    if owner is None:
        return None
    boundary = getattr(owner[0], 'boundary', None) or getattr(owner[0], '_boundary', None)
    return boundary if isinstance(boundary, EmissionBoundary) else None


def bind_boundary(boundary):
    collector = _COLLECTOR.get()
    if collector is None or collector.get('authority_conflict'):
        return
    existing = collector['boundary']
    if existing is None:
        collector['boundary'] = boundary
        collector['content_withheld'] = getattr(getattr(boundary, 'policy', None), 'allow_content', None) is not True
        return
    from api_agent.telemetry.privacy import EmissionBoundary
    shared = (isinstance(existing, EmissionBoundary) and isinstance(boundary, EmissionBoundary)
              and existing._authority is boundary._authority)
    if existing is boundary or shared:
        if getattr(getattr(boundary, 'policy', None), 'allow_content', None) is not True:
            if not collector['content_withheld']:
                collector['rows'] = [dict(phase=phase, content={}, reason='withheld')
                                     for phase in ('request', 'response')]
                collector['charged'].clear()
            collector['content_withheld'] = True
        collector['boundary'] = boundary
        return
    # Earlier rows may have been projected using an incomplete secret registry.
    # Once authorities disagree, no evidence from this collection is trustworthy.
    collector['authority_conflict'] = True
    collector['trace_collector'] = None
    collector['rows'] = [dict(phase=phase, content={}, reason='withheld')
                         for phase in ('request', 'response')]
    collector['charged'].clear()


def bind_inference_trace(owner):
    """The setup host alone binds the independent destination to this call."""
    from api_agent.telemetry.privacy import EmissionBoundary
    collector = _COLLECTOR.get()
    if (collector is None or collector.get('authority_conflict') or collector['trace_owner'] is not None
            or type(collector.get('boundary')) is not EmissionBoundary):
        return
    call = owner.begin(collector['call_id'])
    if call is None:
        return
    boundary = collector['boundary'].for_policy(call.boundary.policy)
    # A separate collector owns its own budget. Its rows are never copied into
    # the report lane, even when both destinations permit content.
    with collect_inference(boundary, collector['call_id'], retain_raw=collector['retain_raw']) as trace:
        collector['trace_collector'] = trace
    collector['trace_owner'] = call


def select_capture_rows(rows):
    """Select authoritative phases; conflicting physical payloads stay incomplete."""
    if type(rows) not in (list, tuple) or len(rows) > 128:
        return [dict(phase=phase, content={}, reason='truncated') for phase in ('request', 'response')]
    selected = {}
    for row in rows:
        from api_agent.telemetry.inference_diagnostics import valid_diagnostics
        if (type(row) is not dict or len(row) > 7 or set(row) - {'phase', 'content', 'reason', '_source', '_rejected', 'transport_attempt_id', 'diagnostics'}
                or row.get('phase') not in ('request', 'response')
                or type(row.get('content')) is not dict
                or row.get('reason') not in (None, 'captured', 'partial', 'truncated', 'unavailable', 'withheld')
                or row.get('_source') not in (None, 'fallback')
                or ('_rejected' in row and (row['_rejected'] is not True or row.get('phase') != 'response'))
                or ('diagnostics' in row and not valid_diagnostics(row['diagnostics']))
                or ('transport_attempt_id' in row and (row['transport_attempt_id'] is None
                    or not _valid_attempt_id(row['transport_attempt_id'])))):
            return [dict(phase=phase, content={}, reason='truncated') for phase in ('request', 'response')]
        phase = (row['phase'], row.get('transport_attempt_id'))
        rank = 0 if row.get('_source') == 'fallback' else 1
        clean = {key: value for key, value in row.items() if key != '_source'}
        if row.get('_source') == 'fallback' or row.get('transport_attempt_id') is not None:
            clean['capture_source'] = 'fallback' if row.get('_source') == 'fallback' else 'provider'
        current = selected.get(phase)
        if current is None or rank > current[0]:
            selected[phase] = (rank, clean)
        elif rank == current[0] and current[1] != clean:
            selected[phase] = (rank, dict(phase=phase[0], content={}, reason='truncated',
                                           **({'capture_source': clean['capture_source']} if 'capture_source' in clean else {}),
                                           **({'transport_attempt_id': phase[1]} if phase[1] is not None else {})))
    return [value for _, value in selected.values()]


def _valid_attempt_id(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def capture_transport_diagnostics(value):
    """Retain bounded worker metadata under this call/physical attempt only."""
    from api_agent.telemetry.inference_diagnostics import valid_diagnostics
    from api_agent.providers.generation_attempts import current_transport_attempt
    state = _COLLECTOR.get() or _DIRECT_ROWS.get()
    if state is not None and valid_diagnostics(value):
        state['transport_diagnostics'][current_transport_attempt()] = value
        if state.get('trace_collector') is not None:
            state['trace_collector']['transport_diagnostics'][current_transport_attempt()] = value


def _report_capture_loss(call_id=None, direction=None):
    """Report lost workflow evidence through the runner's acknowledged channel."""
    import os
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if not node:
        import logging
        logging.getLogger(__name__).warning('Workflow inference evidence could not be recorded')
        return
    from api_agent.testing.inference_content import notify_failure
    owner = current_workflow_visit()
    scope = None
    if owner is not None:
        from uuid import UUID
        journal, visit_id, _ = owner
        invocation_id = getattr(getattr(journal, 'journal', journal), 'invocation_id', None)
        try:
            if direction in ('input', 'output') and all(type(value) is str and str(UUID(value)) == value
                    for value in (invocation_id, visit_id, call_id)):
                scope = dict(invocation_id=invocation_id, visit_id=visit_id, call_id=call_id, direction=direction)
        except (ValueError, TypeError):
            pass
    notify_failure(node.rsplit(' ', 1)[0], scope)


def capture_rows(rows, call_id):
    """Rebind already scrubbed remote provider phases to the worker visit."""
    owner = current_workflow_visit()
    if owner is None:
        return
    journal, visit_id, _ = owner
    for row in rows:
        direction = 'input' if row['phase'] == 'request' else 'output'
        name = 'llm_request' if direction == 'input' else 'llm_response'
        omitted = row['content'] == {} and row.get('reason') in ('truncated', 'unavailable')
        try:
            journal.capture_json(visit_id=visit_id, direction=direction, cause_id=call_id,
                values={} if omitted else {name: row['content']}, schema={'type': 'object', 'properties': {name: {'type': 'object'}}},
                capture_reason=row.get('reason'),
                **({'transport_attempt_id': row['transport_attempt_id']} if row.get('transport_attempt_id') is not None else {}),
                **({'capture_source': row['capture_source']} if row.get('capture_source') is not None else {}))
        except (Exception, asyncio.CancelledError):
            _report_capture_loss(call_id, direction)
        else:
            if getattr(getattr(journal, 'journal', journal), '_gap', None) is not None:
                _report_capture_loss(call_id, direction)


async def prepare_capture_auth(auth):
    """Admit a credential before inference uses it or attempts capture."""
    if type(auth) is not str or not auth:
        return
    collector = _COLLECTOR.get()
    owner = current_workflow_visit()
    boundary = collector['boundary'] if collector is not None else None
    if boundary is None and owner is not None:
        boundary = getattr(owner[0], 'boundary', None) or getattr(owner[0], '_boundary', None)
    if boundary is None:
        return
    from api_agent.telemetry.privacy import register_secret_async
    await register_secret_async(boundary, auth)


def _snapshot_raw(value, maximum):
    """Copy bounded JSON structure without serializing large strings on the loop.

    String length is a lower bound for encoded size; exact UTF-8/escaping
    admission occurs in the owned projection worker before publication.
    """
    import math
    remaining = [maximum, 100000]
    active = set()
    def charge(amount):
        remaining[0] -= amount
        if remaining[0] < 0:
            raise ValueError('Capture exceeds limit')
    def copy(item, depth):
        remaining[1] -= 1
        if remaining[1] < 0 or depth > 20:
            raise ValueError('Capture exceeds structure limit')
        if type(item) is str:
            charge(len(item) + 2)
            return item
        if item is None:
            charge(4)
            return None
        if type(item) is bool:
            charge(4 if item else 5)
            return item
        if type(item) is int and abs(item) <= (1 << 53) - 1:
            charge(len(str(item)))
            return item
        if type(item) is float and math.isfinite(item):
            charge(len(repr(item)))
            return item
        if type(item) not in (dict, list) or id(item) in active:
            raise ValueError('Unsupported capture value')
        charge(2 + max(0, len(item) - 1))
        active.add(id(item))
        try:
            if type(item) is dict:
                result = {}
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError('Unsupported capture key')
                    charge(len(key) + 3)
                    result[key] = copy(child, depth + 1)
                return result
            return [copy(child, depth + 1) for child in item]
        finally:
            active.remove(id(item))
    snapshot = copy(value, 0)
    return snapshot, maximum - remaining[0]


def capture_inference(direction, wire, cause_id, *, auth=None, fallback=False, partial=False, decoded_visible_text_characters=None, unavailable=False, rejected=False):
    """Scrub before retention/IPC; optional evidence cannot affect inference."""
    from .capture import project_values, capture_reason
    collector = _COLLECTOR.get()
    if collector is not None and collector.get('authority_conflict'):
        return
    owner = current_workflow_visit()
    boundary = collector['boundary'] if collector is not None else None
    if boundary is None and owner is not None:
        boundary = getattr(owner[0], 'boundary', None) or getattr(owner[0], '_boundary', None)
    if boundary is None:
        return
    trace = None if collector is None else collector.get('trace_collector')
    if trace is not None:
        token = _COLLECTOR.set(trace)
        requests_token = _CALL_REQUESTS.set(trace.setdefault('requests', []))
        try:
            capture_inference(direction, wire, cause_id, auth=auth, fallback=fallback, partial=partial,
                              decoded_visible_text_characters=decoded_visible_text_characters, unavailable=unavailable,
                              rejected=rejected)
        finally:
            _CALL_REQUESTS.reset(requests_token)
            _COLLECTOR.reset(token)
    try:
        from api_agent.providers.generation_attempts import current_transport_attempt
        attempt_id = current_transport_attempt()
        if type(auth) is str and auth:
            boundary.register_secret(auth)
        if collector is not None and collector['retain_raw']:
            phase = 'request' if direction == 'input' else 'response'
            for prior in collector['rows']:
                if prior.get('phase') != phase or prior.get('transport_attempt_id') != attempt_id:
                    continue
                if fallback and prior.get('_source') != 'fallback':
                    return
                if (prior.get('_source') == ('fallback' if fallback else None)
                        and prior.get('reason') == ('partial' if partial else 'captured')
                        and prior.get('content') == wire):
                    return
        if collector is not None and not fallback:
            phase = 'request' if direction == 'input' else 'response'
            obsolete = [item for item in collector['rows'] if item.get('phase') == phase
                        and item.get('transport_attempt_id') == attempt_id and item.get('_source') == 'fallback']
            for item in obsolete:
                collector['remaining'] += collector['charged'].pop(id(item), 0)
            collector['rows'] = [item for item in collector['rows'] if item not in obsolete]
        report = set()
        allowed = (not unavailable and not _WITHHELD.get() and (collector is None or
                   (not collector['content_withheld'] and boundary.policy.allow_content is True)))
        name = 'llm_request' if direction == 'input' else 'llm_response'
        if collector is not None and collector['retain_raw'] and allowed:
            # This mode is only for a trusted process owner which will project
            # once with the complete per-call union before any publication.
            try:
                if type(wire) is not dict:
                    raise ValueError('Unsupported capture')
                safe, raw_size = _snapshot_raw(wire, collector['remaining'])
                reason = 'captured'
            except ValueError:
                safe, reason = {}, 'truncated'
                raw_size = 0
        else:
            projected = project_values({name: wire}, {'type': 'object'}, boundary, report=report,
                                       max_bytes=4 * 1024 * 1024, _provider_content=True) if allowed else {}
            safe = projected.get(name, {})
            reason = capture_reason(safe, report) if allowed else 'withheld'
        if unavailable and not _WITHHELD.get():
            reason = 'unavailable'
        row = {'phase': 'request' if direction == 'input' else 'response',
               'content': safe, 'reason': reason}
        if rejected is True and direction == 'output' and collector is not None and collector['retain_raw']:
            # Internal admission fact, never optional/scrubbable diagnostics.
            # The final projection consumes this before evidence IPC.
            row['_rejected'] = True
        if not _WITHHELD.get() and not fallback:
            from api_agent.telemetry.inference_diagnostics import sdk_observation, scrub_diagnostics
            diagnostics = sdk_observation(wire, row['phase'])
            state = collector if collector is not None else _DIRECT_ROWS.get()
            if direction == 'output' and state is not None:
                diagnostics.update(state.get('transport_diagnostics', {}).get(attempt_id, {}))
            if diagnostics:
                if type(decoded_visible_text_characters) is int and decoded_visible_text_characters >= 0:
                    diagnostics['decoded_visible_text_characters'] = decoded_visible_text_characters
                diagnostics = scrub_diagnostics(diagnostics, boundary)
                if diagnostics:
                    row['diagnostics'] = diagnostics
        if partial is True and row['reason'] == 'captured':
            row['reason'] = 'partial'
        if attempt_id is not None:
            row['transport_attempt_id'] = attempt_id
        if collector is not None:
            import json
            existing = collector['rows']
            if fallback and any(item.get('phase') == row['phase'] and item.get('transport_attempt_id') == attempt_id and item.get('_source') != 'fallback' for item in existing):
                return
            if not fallback:
                obsolete = [item for item in existing if item.get('phase') == row['phase'] and item.get('transport_attempt_id') == attempt_id and item.get('_source') == 'fallback']
                for item in obsolete:
                    collector['remaining'] += collector['charged'].pop(id(item), 0)
                collector['rows'] = [item for item in existing if item not in obsolete]
        requests = _CALL_REQUESTS.get()
        if direction == 'input' and requests is not None and not (collector is not None and collector['retain_raw']):
            from hashlib import sha256
            import json
            fingerprint = sha256(json.dumps([fallback, row], sort_keys=True, separators=(',', ':'),
                                             ensure_ascii=False, allow_nan=False).encode()).digest()
            if fingerprint in requests:
                return
            requests.append(fingerprint)
        identifier = collector['call_id'] if collector is not None else _CALL_ID.get() or cause_id
        if collector is not None:
            import json
            size = (raw_size if collector['retain_raw'] and allowed else
                    len(json.dumps(row['content'], sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()))
            charged = 0
            if size > collector['remaining'] or len(collector['rows']) >= 128:
                row = {'phase': row['phase'], 'content': {}, 'reason': 'truncated',
                       **({'transport_attempt_id': attempt_id} if attempt_id is not None else {})}
                if len(collector['rows']) >= 128:
                    collector['rows'] = [dict(phase=phase, content={}, reason='truncated')
                                         for phase in ('request', 'response')]
                    collector['remaining'] = 0
                    collector['charged'].clear()
                    return
            else:
                collector['remaining'] -= size
                charged = size
            if fallback:
                row['_source'] = 'fallback'
            collector['rows'].append(row)
            collector['charged'][id(row)] = charged
    except (Exception, asyncio.CancelledError):
        if collector is not None:
            collector['rows'].append({'phase': 'request' if direction == 'input' else 'response',
                                      'content': {}, 'reason': 'unavailable',
                                      **({'_source': 'fallback'} if fallback else {}),
                                      **({'transport_attempt_id': attempt_id} if 'attempt_id' in locals() and attempt_id is not None else {})})
        else:
            _report_capture_loss(_CALL_ID.get() or cause_id, direction)
    else:
        if collector is None:
            state = _DIRECT_ROWS.get()
            if state is None:
                row['capture_source'] = 'fallback' if fallback else 'provider'
                capture_rows((row,), identifier)
            else:
                if fallback:
                    row['_source'] = 'fallback'
                rows = state['rows']
                if not state['overflow']:
                    import json
                    size = len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode('utf-8'))
                    if len(rows) < 128 and size <= state['remaining']:
                        rows.append(row)
                        state['remaining'] -= size
                    else:
                        state['overflow'] = True
                        rows[:] = [dict(phase=phase, content={}, reason='truncated')
                                   for phase in ('request', 'response')]
