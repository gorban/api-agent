"""Bounded, content-free evidence for physical inference dispatches."""
from contextlib import contextmanager
from dataclasses import replace
from contextvars import ContextVar
from copy import deepcopy
import math
import time
from uuid import uuid4

from .contracts import ContractError
from .inference import error_usage, usage_to_wire, result_from_wire
from .costs import validate_billing, validate_quote

_CURRENT = ContextVar('generation_attempt_ledger', default=None)


def _limit_negotiation(row):
    return (row['status'] in (400, 422) and row['error_kind'] == 'invalid_request'
            and row['execution'] == 'not_started' and row['usage'] is None
            and row['retry_delay_seconds'] == 0)


def validate_transport_attempts(value):
    if (type(value) is not dict or set(value) != {'version', 'complete', 'attempts'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['complete']) is not bool or type(value['attempts']) is not list
            or len(value['attempts']) > 21):
        raise ContractError('Invalid transport attempt ledger')
    ids = set()
    for index, row in enumerate(value['attempts'], 1):
        if (type(row) is not dict or set(row) != {'id', 'index', 'execution', 'usage', 'billing', 'quote', 'retry_delay_seconds', 'started_ms', 'stopped_ms', 'outcome', 'status', 'error_kind', 'response'}
                or type(row['id']) is not str or len(row['id']) != 36 or row['id'] in ids
                or type(row['index']) is not int or row['index'] != index
                or row['execution'] not in ('not_started', 'possibly_executed', 'completed')
                or type(row['started_ms']) is not int or row['started_ms'] < 0
                or (row['stopped_ms'] is not None and (type(row['stopped_ms']) is not int or row['stopped_ms'] < row['started_ms']))
                or row['outcome'] not in ('started', 'observed', 'completed', 'failed')
                or (row['outcome'] in ('started', 'observed')) != (row['stopped_ms'] is None)
                or (row['outcome'] in ('started', 'observed') and (index != len(value['attempts']) or value['complete']))
                or (row['status'] is not None and (type(row['status']) is not int or not 100 <= row['status'] <= 599))
                or row['error_kind'] not in (None, 'unknown', 'deadline', 'unavailable', 'network', 'rate_limit', 'authentication', 'authorization', 'invalid_request', 'server', 'cancelled')):
            raise ContractError('Invalid transport attempt ledger')
        if index > 1:
            previous = value['attempts'][index - 2]
            if (previous['outcome'] != 'failed' or previous['retry_delay_seconds'] is None
                    or row['started_ms'] < previous['stopped_ms']):
                raise ContractError('Invalid transport attempt sequence')
        if row['outcome'] == 'started' and (row['execution'] != 'possibly_executed' or any(row[key] is not None for key in ('usage', 'billing', 'quote', 'response', 'status', 'error_kind', 'retry_delay_seconds'))):
            raise ContractError('Invalid transport attempt start')
        if row['outcome'] == 'observed' and (row['execution'] != 'completed' or any(row[key] is not None for key in ('status', 'error_kind', 'retry_delay_seconds'))):
            raise ContractError('Invalid transport attempt observation')
        if row['outcome'] == 'completed' and (row['execution'] != 'completed' or any(row[key] is not None for key in ('status', 'error_kind', 'retry_delay_seconds'))):
            raise ContractError('Invalid transport attempt result')
        if row['outcome'] == 'failed' and row['error_kind'] is None:
            raise ContractError('Invalid transport attempt failure')
        try:
            if str(__import__('uuid').UUID(row['id'])) != row['id']:
                raise ValueError()
            if row['usage'] is not None:
                result_from_wire(dict(text='', outcome='completed', usage=row['usage']))
            if row['response'] is not None:
                from .rich_inference import response_evidence_from_wire
                response_evidence_from_wire(row['response'])
            if row['billing'] is not None:
                validate_billing(row['billing'])
            if row['quote'] is not None:
                validate_quote(row['quote'])
            delay = row['retry_delay_seconds']
            if delay is not None and (type(delay) not in (int, float) or not math.isfinite(delay)
                    or not 0 <= delay <= 86400 or row['outcome'] != 'failed'
                    or not (row['status'] == 429 or type(row['status']) is int and 500 <= row['status'] <= 599
                            or row['status'] in (None, 200) and row['error_kind'] == 'network'
                            or _limit_negotiation(row))):
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            raise ContractError('Invalid transport attempt ledger') from None
        ids.add(row['id'])
    negotiations = sum(_limit_negotiation(row) for row in value['attempts'])
    if negotiations > 1 or len(value['attempts']) > 20 + negotiations:
        raise ContractError('Invalid output limit retry sequence')
    return deepcopy(value)


class TransportAttemptLedger:
    def __init__(self, max_attempts, observer=None, quote=None):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise ContractError('Invalid transport attempt limit')
        self.max_attempts = max_attempts
        self.observer = observer
        self.quote = quote
        self._rows = []
        self._complete = True
        self._active = None

    def _notify(self, phase, row):
        if self.observer is not None:
            self.observer(phase, deepcopy(row))

    def begin(self, index):
        if (self._active is not None or type(index) is not int or index != len(self._rows) + 1 or index > self.max_attempts + sum(_limit_negotiation(row) for row in self._rows)
                or (self._rows and (self._rows[-1]['outcome'] != 'failed' or self._rows[-1]['retry_delay_seconds'] is None))):
            raise ContractError('Invalid transport attempt sequence')
        row = dict(id=str(uuid4()), index=index, execution='possibly_executed', usage=None,
                   billing=None, quote=None, retry_delay_seconds=None, started_ms=int(time.time() * 1000),
                   stopped_ms=None, outcome='started', status=None, error_kind=None, response=None)
        if self._rows:
            row['started_ms'] = max(row['started_ms'], self._rows[-1]['stopped_ms'])
        self._rows.append(row)
        self._active = row['id']
        self._notify('start', row)
        return row['id']

    def observe_result(self, result):
        if self._active is None:
            return
        row = self._rows[-1].copy()
        usage = getattr(result, 'usage', None)
        try:
            if usage is not None:
                candidate = usage_to_wire(usage)
                result_from_wire(dict(text='', outcome='completed', usage=candidate))
                if row['usage'] is None:
                    row['usage'] = candidate
        except (AttributeError, TypeError, ValueError, ContractError):
            usage = None
        try:
            billing = getattr(result, 'billing', None)
            if billing is not None:
                if row['billing'] is None:
                    row['billing'] = deepcopy(validate_billing(billing))
        except (TypeError, ValueError, ContractError):
            pass
        if self.quote is not None and usage is not None and row['quote'] is None:
            try:
                row['quote'] = deepcopy(validate_quote(self.quote(result)))
            except Exception:
                pass
        evidence = getattr(result, 'response', None)
        if evidence is not None and row['response'] is None:
            from .rich_inference import response_evidence_to_wire
            try:
                row['response'] = response_evidence_to_wire(evidence)
            except (TypeError, ValueError, ContractError):
                pass
        row['execution'], row['outcome'] = 'completed', 'observed'
        validate_transport_attempts(dict(version=1, complete=False, attempts=[*self._rows[:-1], row]))
        self._rows[-1] = row
        self._notify('evidence', row)

    def finish(self, token, *, result=None, error=None, retry_delay_seconds=None, response=None):
        if token != self._active or (result is not None and error is not None):
            raise ContractError('Invalid transport attempt completion')
        row = self._rows[-1].copy()
        usage = getattr(result, 'usage', None) if result is not None else error_usage(error)
        if usage is not None and row['usage'] is None:
            row['usage'] = usage_to_wire(usage)
        quotable = result if result is not None else error
        if quotable is not None and self.quote is not None and row['quote'] is None and getattr(quotable, 'usage', None) is not None:
            try:
                row['quote'] = deepcopy(validate_quote(self.quote(quotable)))
            except Exception:
                pass
        billing = getattr(result if result is not None else error, 'billing', None)
        if billing is not None and row['billing'] is None:
            row['billing'] = deepcopy(billing)
        row['execution'] = 'completed' if result is not None or row['outcome'] == 'observed' else getattr(error, 'execution', 'possibly_executed')
        if response is not None:
            from .rich_inference import response_evidence_to_wire
            row['response'] = response_evidence_to_wire(response)
        row['retry_delay_seconds'] = retry_delay_seconds
        row['stopped_ms'] = max(row['started_ms'], int(time.time() * 1000))
        row['outcome'] = 'completed' if result is not None else 'failed'
        status = getattr(error, 'http_status', None)
        row['status'] = status if type(status) is int and 100 <= status <= 599 else None
        kind = getattr(error, 'kind', None)
        row['error_kind'] = kind if kind in ('unknown', 'deadline', 'unavailable', 'network', 'rate_limit', 'authentication', 'authorization', 'invalid_request', 'server', 'cancelled') else ('unknown' if error is not None else None)
        validate_transport_attempts(dict(version=1, complete=False, attempts=[*self._rows[:-1], row]))
        self._rows[-1] = row
        self._active = None
        self._notify('finish', row)
        return deepcopy(row)

    def mark_incomplete(self):
        self._complete = False

    def snapshot(self):
        return validate_transport_attempts(dict(version=1, complete=self._complete and self._active is None, attempts=self._rows))


@contextmanager
def bind_transport_attempt_ledger(ledger):
    token = _CURRENT.set(ledger)
    try:
        yield ledger
    finally:
        _CURRENT.reset(token)


def current_transport_attempt():
    """Return the active physical dispatch identity, if a ledger is bound."""
    ledger = _CURRENT.get()
    return None if ledger is None else ledger._active


def begin_transport_attempt(index):
    ledger = _CURRENT.get()
    return None if ledger is None else ledger.begin(index)


def finish_transport_attempt(token, *, result=None, error=None, retry_delay_seconds=None, response=None):
    ledger = _CURRENT.get()
    if ledger is not None:
        return ledger.finish(token, result=result, error=error, retry_delay_seconds=retry_delay_seconds, response=response)
    return None


def snapshot_transport_result(result):
    ledger = _CURRENT.get()
    if ledger is not None:
        ledger.observe_result(result)
    return result


def attach_transport_attempts(value, snapshot):
    if value is not None and snapshot is not None:
        from .llm import LLMResult
        if isinstance(value, LLMResult):
            value = replace(value)
        object.__setattr__(value, 'transport_attempts', validate_transport_attempts(snapshot))
    return value


def rollup_transport_attempts(snapshot):
    """Return known physical totals and explicit unknown evidence counts."""
    ledger = validate_transport_attempts(snapshot)
    names = ('input_tokens', 'output_tokens', 'cached_input_tokens', 'reasoning_tokens', 'cache_write_tokens')
    totals = {name: 0 for name in names}
    unknown = {name: 0 for name in names}
    known_total = None
    known_cost = 0.0
    unknown_cost = 0
    known_charge = 0.0
    unknown_charge = 0
    for row in ledger['attempts']:
        for name in names:
            value = None if row['usage'] is None else row['usage'].get(name)
            if value is None:
                unknown[name] += 1
            else:
                totals[name] += value
        from .llm import known_usage_lower_bound
        usage = row['usage'] or {}
        bound = known_usage_lower_bound(*(usage.get(name) for name in
            ('input_tokens', 'output_tokens', 'cached_input_tokens', 'cache_write_tokens', 'reasoning_tokens')))
        if bound is not None:
            known_total = (known_total or 0) + bound
        from .costs import qualified_charge
        charge = None if row['billing'] is None or row['usage'] is None else qualified_charge(row['billing'], row['usage'])
        if charge is None:
            unknown_charge += 1
        else:
            known_charge += charge
        amount = None if row['quote'] is None else row['quote']['list_cost_usd']
        if amount is None:
            unknown_cost += 1
        else:
            known_cost += amount
    return dict(usage=totals, unknown_usage_attempts=unknown, known_total_tokens=known_total, list_cost_usd=known_cost,
                unknown_cost_attempts=unknown_cost, reported_charge_usd=known_charge,
                unknown_charge_attempts=unknown_charge, unknown_remainder=not ledger['complete'])
