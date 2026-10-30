"""Call-local credential union for an authenticated setup handoff."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar

from .contracts import ContractError
from api_agent.telemetry.privacy import EmissionBoundary

_CURRENT = ContextVar('provider_call_authority', default=None)


def current_call_authority():
    return _CURRENT.get()


def require_remote_admission(source):
    authority = _CURRENT.get()
    if authority is not None and (not authority.admitted or type(source) is not EmissionBoundary):
        raise ContractError('Caller credential authority unavailable')


@contextmanager
def bind_call_authority(coordinator, caller, local_lease=None):
    if coordinator is not None and type(coordinator) is not EmissionBoundary:
        raise ContractError('Call credential authority unavailable')
    if caller is not None and type(caller) not in (tuple, list):
        raise ContractError('Call credential authority unavailable')
    token = _CURRENT.set(_CallAuthority(coordinator, caller, local_lease))
    try:
        yield
    finally:
        _CURRENT.reset(token)


class _CallAuthority:
    def __init__(self, coordinator, caller, local_lease):
        self._coordinator = coordinator
        self._caller = None if caller is None else tuple(caller)
        self.admitted = coordinator is not None and caller is not None
        self._local_lease = local_lease

    async def register(self, source, values):
        if not self.admitted or type(source) is not EmissionBoundary:
            raise ContractError('Call credential authority unavailable')
        if self._local_lease is not None and self._local_lease.owns(source):
            await self._local_lease.aupgrade(source, values)
            self._caller = self._local_lease.secrets
        else:
            from api_agent.telemetry.privacy import register_secret_async
            for value in values:
                await register_secret_async(source, value)

    def acquire(self, source):
        if not self.admitted or type(source) is not EmissionBoundary:
            raise ContractError('Call credential authority unavailable')
        boundaries = {id(item._authority): item for item in (source, self._coordinator)}
        ordered = [boundaries[key] for key in sorted(boundaries)]
        leases = []
        try:
            with ExitStack() as stack:
                for boundary in ordered:
                    stack.enter_context(boundary._authority.lock)
                for boundary in ordered:
                    leases.append(boundary.begin_evidence_lease())
                values = set(self._caller)
                for boundary in ordered:
                    values.update(boundary._secrets)
                if (any(type(value) is not str or not value for value in values)
                        or len(values) > 64 or sum(len(value.encode('utf-8')) for value in values) > 16384):
                    raise ContractError('Call credential authority exceeds limit')
            return sorted(values), _Leases(leases)
        except BaseException:
            for lease in reversed(leases):
                lease.release()
            raise


class _Leases:
    def __init__(self, leases):
        self._leases = leases

    def release(self):
        for lease in reversed(self._leases):
            lease.release()
        self._leases.clear()
