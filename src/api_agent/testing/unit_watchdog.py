"""Fixed unit execution deadlines; progress and output never renew them."""
from dataclasses import dataclass
import math
import time

UNIT_LIMIT = 60.0


@dataclass(frozen=True)
class UnitDeadline:
    identity: str | None
    elapsed: float
    limit: float
    phase: str

    def diagnostic(self, ordinal=None):
        owner = f'Selected unit #{ordinal}' if self.identity is not None else 'Unit pytest'
        return f'{owner} exceeded its {self.phase} deadline: {self.elapsed:.2f}s elapsed; limit {self.limit:.2f}s.'


class UnitWatchdog:
    """Each active unit owns one setup-through-teardown clock, beside its peers."""
    def __init__(self, *, deadline=None, clock=None):
        self.clock = clock or time.monotonic
        self.deadline = deadline
        self.active = {}
        self.completed = set()
        self.last_completed = None
        self.violations = {}
        self.idle_started = self.clock()

    def _time(self, value):
        now = self.clock()
        if type(value) not in (int, float):
            return now
        try:
            return min(now, value) if math.isfinite(value) else now
        except OverflowError:
            return now

    def _expiry(self, identity, started, observed, phase):
        end = started + UNIT_LIMIT
        if self.deadline is not None:
            end = min(end, self.deadline)
        if observed >= end:
            return UnitDeadline(identity, max(0, observed - started), max(0, end - started), phase)
        return None

    def _remember(self, identity, started, observed, phase):
        expired = self._expiry(identity, started, observed, phase)
        if expired is not None:
            self.violations.setdefault(identity, expired)

    def observe(self, identity, kind, *, phase=None, at=None):
        if identity in self.completed or kind not in {'test_started', 'report'}:
            return
        observed = self._time(at)
        # A transition cannot replace a clock that had already expired when the
        # worker produced this event. Other active siblings keep their own clocks.
        if identity in self.active:
            self._remember(identity, self.active[identity], observed, 'setup-through-teardown')
        elif not self.active:
            self._remember(identity, self.idle_started, observed, 'startup/finalization gap')
        if kind == 'report' and phase == 'teardown':
            self.active.pop(identity, None)
            self.completed.add(identity)
            self.last_completed = observed if self.last_completed is None else max(self.last_completed, observed)
            if not self.active:
                self.idle_started = self.last_completed
        elif identity not in self.active:
            self.active[identity] = observed
            # A newly allocated sibling must also respect an exhausted caller.
            self._remember(identity, observed, observed, 'setup-through-teardown')

    def expired(self, *, at=None):
        observed = self._time(at)
        starts = self.active.items() if self.active else ((None, self.idle_started),)
        expired = dict(self.violations)
        for identity, started in starts:
            deadline = self._expiry(identity, started, observed,
                'setup-through-teardown' if identity is not None else 'startup/finalization gap')
            if deadline is not None:
                expired.setdefault(identity, deadline)
        return tuple(expired.values())
