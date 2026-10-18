"""Lazy process-owned reporting consumers for one interactive workbench."""
import json
from threading import RLock

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.reporting_registry import SharedReportingManager, _assets, _configuration
from api_agent.runtime.tasks import offload


class ReportingSessions:
    """Keep distinct session leases alongside active-operation leases.

    The original ownership journal supplies process identity and cross-process
    locking. This lock only protects this workbench's worker/shutdown handoff.
    Construction and publication-only plans perform no service I/O.
    """
    def __init__(self, factory=SharedReportingManager):
        self.factory = factory
        self.managers = {}
        self.lock = RLock()
        self.closing = False
        self.pending_cleanup = None

    def acquire(self, plan, *, engine_environment=None, deadline_seconds=120):
        if plan.profiles != ('localstack',):
            return
        key = json.dumps([_configuration(plan), _assets(plan), engine_environment], sort_keys=True)
        with self.lock:
            if self.closing:
                raise RuntimeError('Reporting session is closing.')
            if key not in self.managers:
                self.managers[key] = self.factory(plan, engine_environment=engine_environment,
                                                 deadline_seconds=deadline_seconds)
            # Register the owner before fallible acquisition so partial startup
            # cannot lose a rollback or durable provisioning intent.
            return self.managers[key].acquire()

    def _release(self):
        with self.lock:
            self.closing = True
            pending = []
            for key, manager in tuple(self.managers.items()):
                try:
                    manager.release(retain=False)
                except BaseException:
                    pending.append(manager)
                else:
                    del self.managers[key]
            if pending:
                self.pending_cleanup = CleanupPendingError(tuple(pending), self.aclose)
                raise self.pending_cleanup from None
            self.pending_cleanup = None

    async def aclose(self):
        await offload(self._release)
