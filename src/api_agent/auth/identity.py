"""Pack-neutral request identity contract; imports perform no authentication."""
from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable


class IdentityError(ValueError):
    """A public, non-sensitive authentication failure."""

    def __init__(self, code='invalid_credentials', *, status=401):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class VerifiedPrincipal:
    """Stable owner key plus private adapter-owned upstream credentials."""
    key: str
    credential: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key.strip():
            raise ValueError('Principal key must be nonempty')


@runtime_checkable
class IdentityAdapterV1(Protocol):
    version: int

    async def resolve_request(self, *, authorization: str | None,
                              cookies: Mapping[str, str]) -> VerifiedPrincipal | None: ...

    async def login_hint(self, principal: VerifiedPrincipal) -> str | None: ...

    async def match_llm_identity(self, principal: VerifiedPrincipal, llm_auth_context: object) -> bool: ...
