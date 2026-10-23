"""Immutable local-operation observations shared by runtime consumers."""
from dataclasses import dataclass
from collections.abc import Mapping


@dataclass(frozen=True)
class LocalComponent:
    name: str
    state: str


@dataclass(frozen=True)
class LocalDestination:
    name: str
    url: str


@dataclass(frozen=True)
class LocalSnapshot:
    phase: str
    components: tuple[LocalComponent, ...] = ()
    destinations: tuple[LocalDestination, ...] = ()
    log_sources: tuple[str, ...] = ('agent',)
    definition_id: str | None = None
    inspection_endpoint: str | None = None


@dataclass(frozen=True)
class LocalEvent:
    kind: str
    snapshot: LocalSnapshot | None = None
    source: str = ''
    text: str = ''
    sequence: int = 0
    diagnostic: Mapping | None = None
