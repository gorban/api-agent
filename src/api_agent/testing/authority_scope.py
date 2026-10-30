"""Per-test enrollment of worker-owned credential authorities.

Enrollment is only complete with an explicit test manifest and every declared
producer joined. This object performs no IPC and grants no publication right.
"""
from contextvars import ContextVar
from contextlib import contextmanager
import asyncio
import math
from threading import RLock

from api_agent.providers.contracts import ContractError
from api_agent.telemetry.privacy import EmissionBoundary

_CURRENT = ContextVar('test_credential_authority_scope', default=None)
_PRODUCERS = frozenset({'ui', 'tools', 'workflow'})
_COORDINATOR_ONLY = 'coordinator'


class TestAuthorityScope:
    __test__ = False

    def __init__(self, manifest):
        if manifest is not None:
            if (type(manifest) not in (tuple, list) or not manifest or any(type(name) is not str for name in manifest)
                    or len(set(manifest)) != len(manifest) or set(manifest) - (_PRODUCERS | {_COORDINATOR_ONLY})
                    or _COORDINATOR_ONLY in manifest and len(manifest) != 1):
                raise ContractError('Invalid test credential authority manifest')
            manifest = frozenset(manifest)
        self._manifest = manifest
        self._owners = {}
        self._closed = False
        self._closing = False
        self._invalid = False
        self._generation = 0
        self._leases = 0
        self._waiters = set()
        self._evidence_jobs = set()
        self._lock = RLock()

    def join(self, name, boundary):
        if name not in _PRODUCERS or type(boundary) is not EmissionBoundary:
            raise ContractError('Invalid test credential authority owner')
        with self._lock:
            if self._closed or self._closing:
                raise ContractError('Test credential authority scope is closed')
            if self._manifest is not None and name not in self._manifest:
                self._invalid = True
                self._generation += 1
                raise ContractError('Undeclared test credential authority owner')
            owners = self._owners.setdefault(name, {})
            identity = id(boundary._authority)
            if identity not in owners:
                if self._leases:
                    raise ContractError('Test credential owner requires asynchronous admission')
                owners[identity] = boundary
                self._generation += 1

    async def ajoin(self, name, boundary, *, timeout=30):
        if name not in _PRODUCERS or type(boundary) is not EmissionBoundary:
            raise ContractError('Invalid test credential authority owner')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError('Invalid authority admission timeout')
        async with asyncio.timeout(timeout):
            while True:
                loop = asyncio.get_running_loop()
                with self._lock:
                    if self._closed or self._closing:
                        raise ContractError('Test credential authority scope is closed')
                    if self._manifest is not None and name not in self._manifest:
                        self._invalid = True
                        self._generation += 1
                        raise ContractError('Undeclared test credential authority owner')
                    if not self._leases or id(boundary._authority) in self._owners.get(name, {}):
                        self.join(name, boundary)
                        return
                    future = loop.create_future()
                    waiter = (loop, future)
                    self._waiters.add(waiter)
                try:
                    await future
                finally:
                    with self._lock:
                        self._waiters.discard(waiter)

    def authorities(self):
        """Return live owners only after explicit and complete admission."""
        with self._lock:
            required = frozenset() if self._manifest == {_COORDINATOR_ONLY} else self._manifest
            if (self._closed or self._invalid or required is None or set(self._owners) != required):
                raise ContractError('Test credential authority scope is unavailable')
            return tuple(boundary for owners in self._owners.values() for boundary in owners.values())

    def snapshot(self):
        with self._lock:
            return self._generation, self.authorities()

    def freeze(self, *, credential_only=False):
        """Acquire a local snapshot lease without holding locks across IPC."""
        if type(credential_only) is not bool:
            raise ContractError('Invalid credential authority lease mode')
        with self._lock:
            boundaries = self.authorities()
            if (not boundaries and self._manifest != {_COORDINATOR_ONLY}) or (not credential_only and any(not boundary.policy.allow_content for boundary in boundaries)):
                raise ContractError('Test credential authority scope is unavailable')
            leases = []
            try:
                for boundary in sorted(boundaries, key=lambda item: id(item._authority)):
                    leases.append(boundary.begin_evidence_lease())
                values = set()
                for boundary in boundaries:
                    with boundary._authority.lock:
                        values.update(boundary._secrets)
                if len(values) > 64 or sum(len(value.encode('utf-8')) for value in values) > 16384:
                    raise ContractError('Test credential authority exceeds limits')
                self._leases += 1
                return _ScopeLease(self, leases, tuple(values))
            except BaseException:
                for lease in reversed(leases):
                    lease.release()
                raise

    def _release(self):
        with self._lock:
            self._leases -= 1
            if self._leases:
                return
            waiters = tuple(self._waiters)
            self._waiters.clear()
        for loop, future in waiters:
            def wake(target=future):
                if not target.done():
                    target.set_result(None)
            try:
                loop.call_soon_threadsafe(wake)
            except RuntimeError:
                pass

    def commit_if_current(self, generation, callback):
        """Commit a short local callback only if enrollment stayed unchanged."""
        with self._lock:
            if self._closed or self._invalid or self._generation != generation:
                return False
            self.authorities()
            callback()
            return True

    def enroll_evidence(self, future):
        with self._lock:
            if self._closed or self._closing:
                raise ContractError('Test credential authority scope is closed')
            self._evidence_jobs.add(future)
        def finished(done):
            with self._lock:
                self._evidence_jobs.discard(done)
        future.add_done_callback(finished)

    def finish_evidence(self):
        with self._lock:
            jobs = tuple(self._evidence_jobs)
        for future in jobs:
            try:
                future.result()
            except BaseException:
                # The publisher reports evidence loss separately.
                pass
        with self._lock:
            self._evidence_jobs.difference_update(jobs)

    def close(self):
        with self._lock:
            self._closing = True
        self.finish_evidence()
        with self._lock:
            self._closed = True
            self._generation += 1
            self._owners.clear()
            waiters = tuple(self._waiters)
            self._waiters.clear()
        for loop, future in waiters:
            def wake(target=future):
                if not target.done():
                    target.set_result(None)
            try:
                loop.call_soon_threadsafe(wake)
            except RuntimeError:
                pass


def current_scope():
    return _CURRENT.get()


def join_current(name, boundary):
    scope = current_scope()
    if scope is not None:
        scope.join(name, boundary)


async def ajoin_current(name, boundary):
    scope = current_scope()
    if scope is not None:
        await scope.ajoin(name, boundary)


class _ScopeLease:
    def __init__(self, scope, leases, secrets):
        self._scope, self._leases = scope, leases
        self.secrets = secrets
        self._released = False

    async def aupgrade(self, boundary, values):
        """Admit worker credentials against this exact held local lease."""
        if type(boundary) is not EmissionBoundary or any(type(value) is not str or not value for value in values):
            raise ContractError('Invalid credential authority upgrade')
        authority = boundary._authority
        while True:
            loop = asyncio.get_running_loop()
            with self._scope._lock:
                if self._released:
                    raise ContractError('Credential authority lease is closed')
                owned = sum(lease._authority is authority and not lease._released for lease in self._leases)
                if not owned:
                    raise ContractError('Credential authority lease does not own source')
                with authority.lock:
                    if set(values) <= authority.secrets or authority.evidence_leases == owned:
                        combined = set(self.secrets) | set(values) | authority.secrets
                        if len(combined) > 64 or sum(len(value.encode('utf-8')) for value in combined) > 16384:
                            raise ContractError('Credential authority exceeds limits')
                        new = set(values) - authority.secrets
                        if new:
                            authority.secrets.update(new)
                            authority.generation += 1
                        self.secrets = tuple(combined)
                        return
                    future = loop.create_future()
                    waiter = (loop, future)
                    authority.registration_waiters.add(waiter)
            try:
                await future
            finally:
                with authority.lock:
                    authority.registration_waiters.discard(waiter)

    def owns(self, boundary):
        with self._scope._lock:
            return not self._released and any(lease._authority is boundary._authority and not lease._released for lease in self._leases)

    def release(self):
        with self._scope._lock:
            if self._released:
                return
            self._released = True
            self.secrets = ()
            for lease in reversed(self._leases):
                lease.release()
            self._leases.clear()
            self._scope._release()


@contextmanager
def authority_scope(manifest):
    scope = TestAuthorityScope(manifest)
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        scope.close()
        _CURRENT.reset(token)
