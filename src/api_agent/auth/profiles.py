"""Validated credential descriptors and shared role-index selection."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .credentials import CredentialError, CredentialSource


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
    if not isinstance(value, Mapping):
        raise ValueError("UNATTENDED_AUTH must be a JSON object of role arrays.")
    unknown = value.keys() - labels
    if unknown:
        raise ValueError("UNATTENDED_AUTH contains an unknown role label.")
    result = []
    fields = {"manager", "cli_path", "vault", "user"}
    for role in labels:
        entries = value.get(role, [])
        if not isinstance(entries, list):
            raise ValueError(f"Credential role {role!r} must contain an array.")
        sources = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise ValueError(f"Credential role {role!r} entry {index} must be an object.")
            if set(entry) != fields:
                raise ValueError(f"Credential role {role!r} entry {index} requires exactly manager, cli_path, vault and user.")
            try:
                sources.append(CredentialSource.from_mapping(entry))
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
