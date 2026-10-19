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
        self.idle_started = self.clock()

    def _time(self, value):
        now = self.clock()
        if type(value) not in (int, float):
            return now
        try:
            return min(now, value) if math.isfinite(value) else now
        except OverflowError:
            return now

    def observe(self, identity, kind, *, phase=None, at=None):
        if identity in self.completed or kind not in {'test_started', 'report'}:
            return
        if kind == 'report' and phase == 'teardown':
            self.active.pop(identity, None)
            self.completed.add(identity)
            completed_at = self._time(at)
            self.last_completed = completed_at if self.last_completed is None else max(self.last_completed, completed_at)
            if not self.active:
                self.idle_started = self.last_completed
        elif identity not in self.active:
            self.active[identity] = self._time(at)

    def expired(self):
        now = self.clock()
        starts = self.active.items() if self.active else ((None, self.idle_started),)
        expired = []
        for identity, started in starts:
            end = started + UNIT_LIMIT
            if self.deadline is not None:
                end = min(end, self.deadline)
            if now >= end:
                expired.append(UnitDeadline(identity, max(0, now - started), max(0, end - started),
                                            'setup-through-teardown' if identity is not None else 'startup/finalization gap'))
        return tuple(expired)
