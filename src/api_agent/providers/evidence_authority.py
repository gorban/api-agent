"""Snapshot and commit a per-call union of live evidence authorities."""
from contextlib import ExitStack

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from .contracts import ContractError


class EvidenceAuthority:
    """Credential-only union; this grants no destination publication permission.

    The caller must independently admit its destination before using a snapshot.
    Keep the frozen caller snapshot local to this call, never shared globally.
    """
    def __init__(self, boundaries, caller_secrets):
        if (not boundaries or any(type(item) is not EmissionBoundary for item in boundaries)
                or type(caller_secrets) not in (list, tuple)
                or any(type(item) is not str or not item for item in caller_secrets)):
            raise ContractError('Evidence authority unavailable')
        self._authorities = tuple(sorted({id(item._authority): item._authority for item in boundaries}.values(), key=id))
        self._caller = tuple(caller_secrets)
        self._check(self._caller)

    @staticmethod
    def _check(values):
        if len(values) > 64 or sum(len(value.encode('utf-8')) for value in values) > 16384:
            raise ContractError('Evidence authority exceeds limit')

    def snapshot(self):
        with ExitStack() as stack:
            for authority in self._authorities:
                stack.enter_context(authority.lock)
            generation = tuple(authority.generation for authority in self._authorities)
            values = set(self._caller)
            for authority in self._authorities:
                values.update(authority.secrets)
            self._check(values)
        return generation, EmissionBoundary(ContentPolicy(True), secrets=values, preserve_json_text=True)

    def commit_if_current(self, generation, callback):
        if type(generation) is not tuple or len(generation) != len(self._authorities):
            raise ContractError('Invalid evidence authority generation')
        with ExitStack() as stack:
            for authority in self._authorities:
                stack.enter_context(authority.lock)
            if any(authority.generation != value for authority, value in zip(self._authorities, generation)):
                return False
            callback()
            return True
