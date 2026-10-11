"""Deterministic suite usage accounting; no provider or artifact I/O on import."""
from __future__ import annotations

import csv
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import math
from pathlib import Path
from threading import RLock
from typing import Callable

CSV_COLUMNS = ('llm_provider', 'llm_model', 'llm_calls', 'session_id',
               'input_tokens', 'output_tokens', 'total_tokens',
               'cache_read_tokens', 'cache_creation_tokens', 'reasoning_tokens')
_TOKEN_FIELDS = CSV_COLUMNS[4:]
_PROMPT_KINDS = frozenset({'initial', 'clarification', 'approval'})


def _count(value: int | None) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError('token counts must be nonnegative integers or None')
    return value


def _valid_pct(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= 100
    except OverflowError:
        return False


def _snapshot_json(snapshot: QuotaSnapshot | None) -> dict | None:
    if snapshot is None:
        return None
    row = {'pct_used': snapshot.pct_used if _valid_pct(snapshot.pct_used) else None,
            'window_id': snapshot.window_id,
            'reset_at': None if snapshot.reset_at is None else snapshot.reset_at.isoformat(),
            'captured_at': snapshot.captured_at.isoformat()}
    if snapshot.pct_step is not None:
        row['pct_step'] = snapshot.pct_step
    return row


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('timestamp must have timezone')
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class QuotaSnapshot:
    pct_used: float | None
    window_id: str | None
    reset_at: datetime | None
    captured_at: datetime
    pct_step: float | None = None

    def __post_init__(self):
        if self.pct_step is not None and (not _valid_pct(self.pct_step) or self.pct_step == 0):
            raise ValueError('percentage step must be positive and at most 100')
        object.__setattr__(self, 'captured_at', _utc(self.captured_at))
        if self.reset_at is not None:
            object.__setattr__(self, 'reset_at', _utc(self.reset_at))
        if self.window_id is not None and (type(self.window_id) is not str or not self.window_id):
            raise ValueError('window_id must be nonempty text or None')


@dataclass(frozen=True)
class QuotaReadError:
    """Content-free failure evidence; never accept arbitrary provider messages."""
    code: str
    status: int | None = None

    def __post_init__(self):
        if type(self.code) is not str or self.code not in {'identity', 'http', 'response', 'window', 'request'}:
            raise ValueError('Invalid quota error code')
        if ((self.code == 'http' and (type(self.status) is not int or not 100 <= self.status <= 599))
                or (self.code != 'http' and self.status is not None)):
            raise ValueError('Invalid quota error status')

    @property
    def message(self):
        if self.code == 'http':
            return f'Quota endpoint returned HTTP {self.status}.'
        return {'identity': 'The authenticated token has no usable quota account identity.',
                'response': 'Quota endpoint returned an invalid response.',
                'window': 'Quota response did not include the configured quota window.',
                'request': 'Quota request failed or timed out.'}[self.code]

    def to_json(self):
        return {'code': self.code, 'status': self.status}


class SuiteUsage:
    def __init__(self, run_id: str, *, cost_estimator=None):
        if type(run_id) is not str or not run_id:
            raise ValueError('run_id is required')
        self.run_id = run_id
        self.cost_estimator = cost_estimator
        self.before: QuotaSnapshot | None = None
        self.after: QuotaSnapshot | None = None
        self.quota_errors = {}
        self._rows: dict[tuple[str, str, str], dict[str, int | None]] = {}
        self._unknown: dict[tuple[str, str, str], set[str]] = {}
        self._known: dict[tuple[str, str, str], dict[str, int]] = {}
        self._prompts: dict[tuple[str, str], str] = {}
        self.inference_calls = {}
        self.contaminated = False
        self._lock = RLock()
        from .costs import CostLedger
        self._costs = CostLedger()

    def mark_quota_contaminated(self):
        """Retain known unrelated consumption; raw observations remain available."""
        with self._lock:
            self.contaminated = True

    def add_cost(self, call_id, session_id, quote=None):
        with self._lock:
            self._costs.record(call_id, session_id, quote)

    def record_inference(self, call_id, session_id, evidence):
        """Retain validated content-free call facts without double accounting."""
        with self._lock:
            prior = self.inference_calls.get(call_id)
            value = deepcopy(evidence)
            value['test_id'] = session_id
            if prior is not None:
                if (prior['test_id'] != session_id or any(prior.get(key) != value.get(key)
                        for key in ('call_id', 'selection', 'started_at', 'attempt', 'turn_id', 'turn_kind'))):
                    raise ValueError('Conflicting inference identity')
                if (prior.get('usage') is not None or prior.get('finished_at') is not None) and prior != value:
                    raise ValueError('Conflicting inference evidence')
            self.inference_calls[call_id] = value
            return prior != value

    def add_usage(self, *, provider: str, model: str, session_id: str,
                  llm_calls: int = 1, input_tokens: int | None = None,
                  output_tokens: int | None = None, total_tokens: int | None = None,
                  cache_read_tokens: int | None = None,
                  cache_creation_tokens: int | None = None,
                  reasoning_tokens: int | None = None) -> None:
        if any(type(x) is not str or not x for x in (provider, model, session_id)):
            raise ValueError('provider, model and session_id are required')
        if type(llm_calls) is not int or llm_calls < 1:
            raise ValueError('llm_calls must be positive')
        values = {k: _count(v) for k, v in locals().copy().items() if k in _TOKEN_FIELDS}
        if values['total_tokens'] is not None and all(values[k] is not None for k in ('input_tokens', 'output_tokens')):
            if values['total_tokens'] != values['input_tokens'] + values['output_tokens']:
                raise ValueError('total_tokens conflicts with input and output')
        if values['total_tokens'] is None and all(values[k] is not None for k in ('input_tokens', 'output_tokens')):
            values['total_tokens'] = values['input_tokens'] + values['output_tokens']
        key = (provider, model, session_id)
        with self._lock:
            row = self._rows.setdefault(key, {'llm_calls': 0, **{k: None for k in _TOKEN_FIELDS}})
            unknown = self._unknown.setdefault(key, set())
            known = self._known.setdefault(key, {})
            row['llm_calls'] += llm_calls
            for field, value in values.items():
                if value is None:
                    unknown.add(field)
                    row[field] = None
                else:
                    known[field] = known.get(field, 0) + value
                    if field not in unknown:
                        row[field] = (row[field] or 0) + value

    def record_prompt(self, *, turn_id: str, prompt_id: str, kind: str) -> bool:
        if kind not in _PROMPT_KINDS or any(type(x) is not str or not x for x in (turn_id, prompt_id)):
            raise ValueError('explicit turn_id, prompt_id and valid kind required')
        key = (turn_id, prompt_id)
        with self._lock:
            prior = self._prompts.get(key)
            if prior is not None and prior != kind:
                raise ValueError('prompt identity reused with another kind')
            self._prompts[key] = kind
            return prior is None

    def observe_snapshot(self, phase, snapshot):
        if phase not in ('before', 'after') or not isinstance(snapshot, (QuotaSnapshot, QuotaReadError)):
            raise ValueError('Invalid quota observation')
        with self._lock:
            if isinstance(snapshot, QuotaReadError):
                self.quota_errors[phase] = snapshot.to_json()
                return
            prior = getattr(self, phase)
            if prior is None or ((snapshot.captured_at < prior.captured_at) if phase == 'before'
                                 else (snapshot.captured_at > prior.captured_at)):
                setattr(self, phase, snapshot)

    def capture_before(self, fetcher: Callable[[], QuotaSnapshot]) -> QuotaSnapshot:
        snapshot = fetcher()
        if not isinstance(snapshot, QuotaSnapshot):
            raise TypeError('fetcher must return QuotaSnapshot')
        with self._lock:
            if self.before is not None:
                raise ValueError('before snapshot already captured')
            self.before = snapshot
        return snapshot

    def capture_after(self, fetcher: Callable[[], QuotaSnapshot]) -> QuotaSnapshot:
        snapshot = fetcher()
        if not isinstance(snapshot, QuotaSnapshot):
            raise TypeError('fetcher must return QuotaSnapshot')
        with self._lock:
            if self.after is not None:
                raise ValueError('after snapshot already captured')
            self.after = snapshot
        return snapshot

    def rows(self) -> list[dict[str, str | int | None]]:
        with self._lock:
            return [dict(zip(CSV_COLUMNS[:3], (p, m, v['llm_calls'])),
                         session_id=s, **{k: v[k] for k in _TOKEN_FIELDS})
                    for (p, m, s), v in sorted(self._rows.items())]

    def report(self, *, now: datetime | None = None) -> dict:
        with self._lock:
            before, after = self.before, self.after
            counts = {kind: sum(v == kind for v in self._prompts.values()) for kind in sorted(_PROMPT_KINDS)}
            prompts = len(self._prompts)
        reason = None
        if before is None or after is None or before.pct_used is None or after.pct_used is None:
            reason = 'missing_snapshot'
        elif not all(_valid_pct(x.pct_used) for x in (before, after)):
            reason = 'invalid_percentage'
        elif before.window_id is None or after.window_id is None or before.reset_at is None or after.reset_at is None:
            reason = 'missing_window_identifier'
        elif before.window_id != after.window_id or before.reset_at != after.reset_at:
            reason = 'changed_window'
        elif after.captured_at < before.captured_at:
            reason = 'reversed_capture_time'
        elif before.captured_at >= before.reset_at or after.captured_at >= after.reset_at:
            reason = 'reset_boundary_crossed'
        elif after.pct_used < before.pct_used:
            reason = 'negative_delta'
        delta = None if reason else after.pct_used - before.pct_used
        per_prompt = None if delta is None or prompts == 0 else delta / prompts
        reset_seconds = None
        if after is not None and after.reset_at is not None:
            reset_seconds = max(0, int((after.reset_at - _utc(now or datetime.now(timezone.utc))).total_seconds()))
        rows = self.rows()
        estimate = None
        if self.cost_estimator is not None:
            estimate = self.cost_estimator(usage_rows=rows, before=before, after=after)
            if type(estimate) not in (int, float) or not math.isfinite(estimate) or estimate < 0:
                raise ValueError('provider cost estimate must be finite and nonnegative')
        report = {'run_id': self.run_id, 'before': _snapshot_json(before), 'after': _snapshot_json(after),
                'pct_used_before': None if before is None or not _valid_pct(before.pct_used) else before.pct_used,
                'pct_used_after': None if after is None or not _valid_pct(after.pct_used) else after.pct_used,
                'pct_point_delta': delta, 'quota_unavailable_reason': reason,
                'quota_errors': deepcopy(self.quota_errors),
                'raw_quota_cost_usd': None,
                'estimated_cost_usd': estimate, 'cost_per_prompt_usd': None if estimate is None or prompts == 0 else estimate / prompts,
                'pct_used_per_prompt': None if per_prompt is None else f'{per_prompt:.3f}',
                'prompt_counts': counts, 'prompt_count': prompts,
                'concurrent_consumption_possible': True, 'contaminated': self.contaminated,
                'reset_countdown_seconds': reset_seconds,
                'reset_countdown_hms': None if reset_seconds is None else f'{reset_seconds // 3600:02d}:{reset_seconds // 60 % 60:02d}:{reset_seconds % 60:02d}',
                'usage_rows': rows}
        with self._lock:
            report['usage_observations'] = [dict(row, **self._known[(row['llm_provider'], row['llm_model'], row['session_id'])],
                unknown_fields=sorted(self._unknown[(row['llm_provider'], row['llm_model'], row['session_id'])]))
                for row in rows]
            report['unmeasured_calls'] = {}
            for call in self.inference_calls.values():
                if call.get('usage') is None:
                    session = call['test_id']
                    report['unmeasured_calls'][session] = report['unmeasured_calls'].get(session, 0) + 1
            if any(quote is not None for _, quote in self._costs.calls.values()):
                prompts_by_session = {session: sum(key[0] == session for key in self._prompts)
                                      for session, _ in self._costs.calls.values()}
                report.update(self._costs.report(report, prompts_by_session, now=now))
        return report

    def write_csv(self, run_csv: Path, external_csv: Path | None = None, *, evidence_boundary=None) -> None:
        """Write the run artifact and optionally append to the shared usage CSV."""
        run_csv = Path(run_csv)
        run_csv.parent.mkdir(parents=True, exist_ok=True)
        rows = self.rows()
        if evidence_boundary is not None:
            rows = [evidence_boundary.content_export(row) for row in rows]
        with open(run_csv, 'w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        if external_csv is None:
            return
        external_csv = Path(external_csv)
        external_csv.parent.mkdir(parents=True, exist_ok=True)
        lock_path = external_csv.with_name(external_csv.name + '.lock')
        with open(lock_path, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                header_needed = not external_csv.exists()
                if not header_needed:
                    with open(external_csv, newline='') as stream:
                        header_needed = next(csv.reader(stream), None) != list(CSV_COLUMNS)
                    if header_needed:
                        legacy = external_csv.with_name(external_csv.name + '.legacy')
                        suffix = 1
                        while legacy.exists():
                            legacy = external_csv.with_name(f'{external_csv.name}.legacy{suffix}')
                            suffix += 1
                        external_csv.rename(legacy)
                with open(external_csv, 'a', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
                    if header_needed:
                        writer.writeheader()
                    writer.writerows(rows)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
