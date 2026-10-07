"""Validated credential descriptors and shared role-index selection."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .credentials import CredentialError, CredentialSource

DEFAULT_PASSWORD_MANAGER = {"manager": "keeper", "cli_path": "keeper"}


def _canonical_shape(value: Mapping) -> bool:
    """Distinguish envelope fields from legacy role labels with those names."""
    return (("roles" in value and isinstance(value["roles"], Mapping)) or
            ("password_manager" in value and isinstance(value["password_manager"], Mapping)))


def _manager(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"manager", "cli_path"}:
        raise ValueError("password_manager requires exactly manager and cli_path.")
    manager, path = value["manager"], value["cli_path"]
    if not isinstance(manager, str) or manager not in {"keeper", "1password"}:
        raise ValueError("password_manager manager must be keeper or 1password.")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("password_manager cli_path must be a nonempty string.")
    return {"manager": manager, "cli_path": path}


def normalize_auth(value: object) -> dict[str, object]:
    """Return canonical descriptors, migrating uniform legacy entries in memory."""
    if not isinstance(value, Mapping):
        raise ValueError("UNATTENDED_AUTH must be a JSON object.")
    if '_auth_error' in value:
        if value['_auth_error'] == 'manager_conflict':
            raise ValueError('Saved .env profiles use different password managers or CLI paths; choose one shared password manager before saving.')
        raise ValueError('Saved UNATTENDED_AUTH in .env is malformed or invalid; fix that file before editing accounts.')
    if _canonical_shape(value):
        if set(value) - {"password_manager", "roles"}:
            raise ValueError("UNATTENDED_AUTH canonical object supports only password_manager and roles.")
        roles = value.get("roles", {})
        if not isinstance(roles, Mapping):
            raise ValueError("UNATTENDED_AUTH roles must be a JSON object.")
        manager = _manager(value.get("password_manager", DEFAULT_PASSWORD_MANAGER))
        return {"password_manager": manager, "roles": dict(roles)}
    roles = {}
    found_manager = None
    for role, entries in value.items():
        if not isinstance(entries, list):
            raise ValueError(f"Credential role {role!r} must contain an array.")
        converted = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping) or set(entry) != {"manager", "cli_path", "vault", "user"}:
                raise ValueError(f"Legacy credential role {role!r} entry {index} requires manager, cli_path, vault and user.")
            manager = _manager({key: entry[key] for key in ("manager", "cli_path")})
            if found_manager is not None and manager != found_manager:
                raise ValueError("Legacy credential entries use different password managers or CLI paths; choose one shared password manager before saving.")
            found_manager = manager
            converted.append({"vault": entry["vault"], "user": entry["user"]})
        roles[role] = converted
    return {"password_manager": found_manager or dict(DEFAULT_PASSWORD_MANAGER), "roles": roles}


@dataclass(frozen=True)
class CredentialProfiles:
    """Immutable role arrays in the caller's declared role order."""

    roles: tuple[tuple[str, tuple[CredentialSource, ...]], ...]

    def for_role(self, role: str) -> tuple[CredentialSource, ...]:
        for label, sources in self.roles:
            if label == role:
                return sources
        raise ValueError(f"Unknown credential role: {role!r}.")


@dataclass(frozen=True)
class SelectionOption:
    """A display-only account label; no retrieved credential is included."""

    role: str
    index: int
    vault: str
    user: str


class SelectionRequired(CredentialError):
    """Raised when the caller must collect one shared index through its UI."""

    def __init__(self, options: tuple[SelectionOption, ...]):
        self.options = options
        super().__init__("Multiple credential profiles require UNATTENDED_USER_INDEX or a session account choice.")


@dataclass(frozen=True)
class ProfileSelection:
    """Selected sources and roles that must use attended authentication."""

    sources: tuple[tuple[str, CredentialSource], ...]
    attended_roles: tuple[str, ...]
    index: int | None

    def for_role(self, role: str) -> CredentialSource | None:
        for label, source in self.sources:
            if label == role:
                return source
        if role in self.attended_roles:
            return None
        raise ValueError(f"Role was not requested: {role!r}.")


def _labels(labels: Sequence[str], *, name: str) -> tuple[str, ...]:
    if isinstance(labels, (str, bytes)) or not isinstance(labels, Sequence):
        raise ValueError(f"{name} must be a sequence of nonempty role labels.")
    result = tuple(labels)
    if any(not isinstance(label, str) or not label.strip() for label in result):
        raise ValueError(f"{name} must contain nonempty role labels.")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} contains duplicate role labels.")
    return result


def parse_profiles(value: object, enabled_roles: Sequence[str]) -> CredentialProfiles:
    """Validate a parsed JSON object against caller-supplied enabled role labels."""
    labels = _labels(enabled_roles, name="Enabled roles")
    canonical = normalize_auth(value)
    roles = canonical["roles"]
    unknown = roles.keys() - labels
    if unknown:
        raise ValueError("UNATTENDED_AUTH contains an unknown role label.")
    result = []
    fields = {"vault", "user"}
    for role in labels:
        entries = roles.get(role, [])
        if not isinstance(entries, list):
            raise ValueError(f"Credential role {role!r} must contain an array.")
        sources = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise ValueError(f"Credential role {role!r} entry {index} must be an object.")
            if set(entry) != fields:
                raise ValueError(f"Credential role {role!r} entry {index} requires exactly vault and user.")
            try:
                sources.append(CredentialSource.from_mapping({**canonical["password_manager"], **entry}))
            except CredentialError as exc:
                raise ValueError(f"Credential role {role!r} entry {index}: {exc}") from exc
        result.append((role, tuple(sources)))
    return CredentialProfiles(tuple(result))


def _index(value: object, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer.")
    return value


def select_profiles(
    profiles: CredentialProfiles,
    requested_roles: Sequence[str],
    *,
    configured_index: object = None,
    session_index: object = None,
) -> ProfileSelection:
    """Select one paired index for requested roles without prompting or CLI calls."""
    requested = _labels(requested_roles, name="Requested roles")
    available = {role for role, _ in profiles.roles}
    if any(role not in available for role in requested):
        raise ValueError("Requested credential role is not enabled.")
    configured = _index(configured_index, "Configured index")
    index = configured if configured is not None else _index(session_index, "Session index")
    arrays = tuple((role, profiles.for_role(role)) for role in requested)
    nonempty = tuple((role, sources) for role, sources in arrays if sources)
    if index is None and any(len(sources) > 1 for _, sources in nonempty):
        options = tuple(SelectionOption(role, i, source.vault, source.user)
                        for role, sources in nonempty for i, source in enumerate(sources))
        raise SelectionRequired(options)
    if index is not None:
        for role, sources in nonempty:
            if index >= len(sources):
                raise ValueError(f"Shared credential index {index} is out of bounds for role {role!r} ({len(sources)} entries).")
    selected_index = index if index is not None else (0 if nonempty else None)
    selected = tuple((role, sources[selected_index]) for role, sources in nonempty)
    attended = tuple(role for role, sources in arrays if not sources)
    return ProfileSelection(selected, attended, selected_index)
