"""Process-local request ownership, independent of provider authentication."""
from dataclasses import dataclass, field
import secrets
import time
from urllib.parse import urlsplit


class SessionError(ValueError):
    pass


def _origin(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or port == 0):
            raise ValueError
        canonical = 'https://' + parsed.hostname.lower()
        if port is not None:
            canonical += ':' + str(port)
        if value.rstrip('/') != canonical:
            raise ValueError
        return canonical
    except (TypeError, AttributeError, ValueError):
        raise ValueError('invalid_origin') from None


@dataclass(frozen=True)
class SessionHandle:
    key: str = field(repr=False)
    principal_key: str | None


@dataclass(frozen=True)
class OpenedSession:
    handle: SessionHandle
    cookie_value: str | None = field(default=None, repr=False)
    set_cookie: bool = False


class SessionStore:
    def __init__(self, pack_id, callback_origin, *, clock=time.monotonic,
                 session_ttl=3600, max_sessions=1024):
        if not isinstance(pack_id, str) or not pack_id.strip():
            raise ValueError('invalid_pack_id')
        if type(max_sessions) is not int or max_sessions < 1:
            raise ValueError('invalid_capacity')
        if type(session_ttl) not in (int, float) or not 0 < session_ttl < float('inf'):
            raise ValueError('invalid_session_ttl')
        self.pack_id = pack_id
        self.callback_origin = _origin(callback_origin)
        self.clock, self.session_ttl, self.max_sessions = clock, session_ttl, max_sessions
        self._sessions = {}
        self._closed = False

    def open(self, cookie_value, principal_key, *, create=False):
        if self._closed:
            raise SessionError('store_closed')
        if principal_key is not None and (not isinstance(principal_key, str) or not principal_key):
            raise SessionError('invalid_principal')
        now = self.clock()
        self._sessions = {key: entry for key, entry in self._sessions.items() if entry[1] > now}
        entry = self._sessions.get(cookie_value) if isinstance(cookie_value, str) else None
        if entry is not None:
            handle, _ = entry
            if handle.principal_key != principal_key:
                raise SessionError('session_owner_mismatch')
            return OpenedSession(handle)
        if not create:
            raise SessionError('invalid_session')
        if len(self._sessions) >= self.max_sessions:
            raise SessionError('session_capacity')
        key = secrets.token_urlsafe(32)
        handle = SessionHandle(key, principal_key)
        self._sessions[key] = (handle, now + self.session_ttl)
        return OpenedSession(handle, key, True)

    async def aclose(self):
        self._closed = True
        self._sessions.clear()
