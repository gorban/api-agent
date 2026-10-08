"""Provider-owned connection records and credential-safe failures."""
from dataclasses import dataclass, field


class AuthError(ValueError):
    """Expose a stable failure code, never upstream secrets or responses."""


@dataclass(frozen=True)
class Connection:
    subject: str
    client_id: str
    host_id: str
    email: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    id_token: str = field(repr=False)
    scopes: tuple[str, ...]
    expires_at: float
    earliest_refresh_at: float = 0
