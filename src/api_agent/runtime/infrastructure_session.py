"""Timed access to the existing owned prerequisite lifecycle."""
from types import MappingProxyType

from api_agent.providers.progress import report_timeout_sync


class InfrastructureSession:
    """Expose endpoint outputs only after the manager has verified readiness."""

    def __init__(self, manager):
        self.manager = manager

    @property
    def endpoints(self):
        if self.manager.lease is None:
            return MappingProxyType({})
        return MappingProxyType({service.id: service.endpoints
            for service in self.manager.conditional.values()})

    def acquire(self):
        manager = self.manager
        with report_timeout_sync('Infrastructure readiness', manager.timeout) as budget:
            deadline = manager.now() + budget.remaining
            return manager.acquire(_deadline=deadline)

    def release(self, *, retain=False):
        manager = self.manager
        with report_timeout_sync('Infrastructure cleanup', 15) as budget:
            return manager.release(retain=retain, _deadline=manager.now() + budget.remaining)

    def status(self):
        with report_timeout_sync('Infrastructure status', self.manager.timeout) as budget:
            return self.manager.status(_deadline=self.manager.now() + budget.remaining)

    def logs(self, identifier, *, tail=100, output_limit=16384):
        with report_timeout_sync('Infrastructure logs', self.manager.timeout) as budget:
            return self.manager.logs(identifier, tail=tail, output_limit=output_limit,
                _deadline=self.manager.now() + budget.remaining)
