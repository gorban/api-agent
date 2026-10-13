"""Pure reporting routing evidence; no authentication or service administration.

A registration must come from a trusted registry outside this module. Creating
one does not establish trust or confer administrative authority. Registry trust,
leases, liveness and lifecycle administration need separate implementations.
"""
from dataclasses import dataclass
from enum import Enum
import ipaddress
import re
from urllib.parse import urlsplit
from uuid import UUID


@dataclass(frozen=True)
class InstallationId:
    value: str

    def __post_init__(self):
        if type(self.value) is not str:
            raise TypeError("Installation ID must be text.")
        try:
            parsed = UUID(self.value)
        except ValueError:
            raise ValueError("Installation ID must be a canonical UUID.") from None
        if str(parsed) != self.value:
            raise ValueError("Installation ID must be a canonical UUID.")


@dataclass(frozen=True)
class ReportingEndpoint:
    """ASCII HTTP(S) URL with an unescaped, case-sensitive path.

    Default ports and an empty root path normalize; other paths remain distinct.
    Percent escapes, dot segments, repeated slashes and noncanonical numeric hosts
    are rejected rather than guessed. DNS names are syntactic routing evidence.
    """
    value: str

    def __post_init__(self):
        if type(self.value) is not str:
            raise TypeError("Reporting endpoint must be text.")
        try:
            canonical = _normalize_endpoint(self.value)
        except ValueError:
            raise ValueError("Invalid reporting endpoint.") from None
        object.__setattr__(self, "value", canonical)


def _normalize_endpoint(value):
    if not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise ValueError
    if any(char in value for char in "\\@?#%"):
        raise ValueError
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.hostname:
        raise ValueError
    host = parts.hostname
    port = parts.port
    if parts.netloc.endswith(":") or port == 0:
        raise ValueError
    if ":" in host:
        host = "[" + str(ipaddress.IPv6Address(host)) + "]"
    elif re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)(?:\.(?:[0-9]+|0x[0-9a-f]+))*", host):
        host = str(ipaddress.IPv4Address(host))
    elif not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", host) or len(host) > 253:
        raise ValueError
    path = parts.path or "/"
    if not re.fullmatch(r"/[A-Za-z0-9_~./-]*", path) or "//" in path or any(segment in (".", "..") for segment in path.split("/")):
        raise ValueError
    authority = host if port is None or port == {"http": 80, "https": 443}[parts.scheme] else f"{host}:{port}"
    return f"{parts.scheme}://{authority}{path}"


def _require_type(value, expected):
    if type(value) is not expected:
        raise TypeError(f"Expected {expected.__name__}.")


@dataclass(frozen=True)
class ManagedSelection:
    installation_id: InstallationId
    endpoint: ReportingEndpoint

    def __post_init__(self):
        _require_type(self.installation_id, InstallationId)
        _require_type(self.endpoint, ReportingEndpoint)


@dataclass(frozen=True)
class ExternalSelection:
    endpoint: ReportingEndpoint

    def __post_init__(self):
        _require_type(self.endpoint, ReportingEndpoint)


@dataclass(frozen=True)
class ReportingRegistration:
    """Routing record supplied by a trusted registry, never a credential."""
    installation_id: InstallationId
    endpoint: ReportingEndpoint

    def __post_init__(self):
        _require_type(self.installation_id, InstallationId)
        _require_type(self.endpoint, ReportingEndpoint)


class ReportingOperation(Enum):
    PUBLISH = "publish"
    RELINQUISH = "relinquish"
    STOP = "stop"
    RECREATE = "recreate"
    DELETE_DATA = "delete_data"


class ReleaseDisposition(Enum):
    RETAIN_SERVICE_AND_DATA = "retain_service_and_data"


@dataclass(frozen=True)
class PublicationAccess:
    selection: ManagedSelection | ExternalSelection
    registration: ReportingRegistration | None = None

    def __post_init__(self):
        if type(self.selection) not in (ManagedSelection, ExternalSelection):
            raise TypeError("Expected an explicit reporting selection.")
        if type(self.selection) is ManagedSelection:
            _validate_binding(self.selection, self.registration)
        elif self.registration is not None:
            raise ValueError("External publication cannot claim a managed registration.")

    def require_operation(self, operation: ReportingOperation) -> None:
        _require_type(operation, ReportingOperation)
        if operation not in (ReportingOperation.PUBLISH, ReportingOperation.RELINQUISH):
            raise PermissionError("Publication access cannot administer reporting services or data.")

    def release(self) -> ReleaseDisposition:
        """Relinquishing access always preserves service and data, including last use."""
        return ReleaseDisposition.RETAIN_SERVICE_AND_DATA


def bind_managed_publication(selection: ManagedSelection, registration: ReportingRegistration) -> PublicationAccess:
    """Check routing against a caller-supplied trusted record; grant publication only."""
    _require_type(selection, ManagedSelection)
    return PublicationAccess(selection, registration)


def _validate_binding(selection, registration):
    _require_type(registration, ReportingRegistration)
    if selection.installation_id != registration.installation_id:
        raise ValueError("Reporting installation does not match registration.")
    if selection.endpoint != registration.endpoint:
        raise ValueError("Reporting endpoint does not match registration.")
