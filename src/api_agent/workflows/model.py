"""Parser-free workflow definition model and the protocol the engines consume.

Both engines (``execution.Workflow`` and ``stateful.StatefulWorkflow``), the
journal and the identity helpers depend only on the names in this module.
Source loaders (``definition`` and ``stateful``) produce *parsed* definitions
from ``.md.html`` declarations; ``artifact`` produces *compiled* definitions
from a canonical JSON plan. Neither the Markdown parser nor source ``exec`` is
reachable from here, which keeps a production process free of both.
"""
from dataclasses import dataclass
import inspect
from pathlib import Path
import re
from types import MappingProxyType
from typing import ClassVar, Mapping, Protocol, runtime_checkable

MAX_COMPONENT_PASSES = 99
_ID = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class LayoutError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    id: str
    kind: str
    parent: str | None
    source: Path
    line: int


@dataclass(frozen=True)
class Layout:
    path: Path
    steps: tuple[Step, ...]
    tables: dict
    files: tuple[Path, ...]

    def error(self, message, line=1):
        raise LayoutError(f'{self.path}:{line}: {message}')


@dataclass(frozen=True)
class Replacement:
    target_id: str
    entry: Path
    root_id: str


@dataclass(frozen=True)
class Resource:
    """One admitted resource.

    ``content_bytes`` is ``None`` for a compiled definition's declaration and
    native-source resources, which are identified by ``sha256`` and ``size``
    only and never carried into production.
    """

    kind: str
    logical_path: str
    content_bytes: bytes | None
    sha256: str
    size: int | None = None


@dataclass(frozen=True)
class InlinePromptRef:
    owner: Path
    id: str

    def as_uri(self):
        return self.owner.as_uri() + '#prompt-' + self.id


@dataclass(frozen=True)
class ToolUsage:
    """One declared port binding in a selected graph scope."""

    scope: str
    port: str
    instance: Path
    scope_kind: str
    declaration: Path
    line: int
    operations: tuple[str, ...] = ()


def freeze(value):
    """Recursively replace dicts with read-only mappings and lists with tuples."""
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def declares_source(function, path):
    """Whether ``function`` was compiled from the file ``path`` (the declared source link)."""
    try:
        source = inspect.getsourcefile(function)
        return source is not None and Path(source).samefile(path)
    except (TypeError, OSError):
        return False


def require_source_provenance(definition):
    """Provenance proves checkout/package documents, which a compiled plan does not carry."""
    if getattr(definition, 'origin', 'source') != 'source':
        raise LayoutError('source provenance is unavailable for a compiled plan')


@runtime_checkable
class Definition(Protocol):
    """What every engine, the journal and the identity helpers need.

    ``version`` selects the engine family (2: ``Workflow``, 3:
    ``StatefulWorkflow``). ``origin`` is ``'source'`` for definitions parsed
    from declarations and ``'compiled'`` for definitions loaded from a plan.
    ``contents`` maps resource keys (paths or inline-prompt references) to the
    admitted bytes; a compiled definition omits declaration and native source.
    """

    version: ClassVar[int]
    origin: ClassVar[str]
    graph_id: str
    definition_id: str
    entry: Path
    entry_root: str
    roots: Mapping
    replacements: tuple
    resources: tuple
    layout: Layout
    contents: Mapping
    selected_declarations: Mapping | None
    declaration_fragments: Mapping | None
    tool_usages: tuple

    def verify_sources(self) -> None:
        """Raise ``LayoutError`` unless the admitted native/source closure is unchanged."""


@runtime_checkable
class GraphDefinition(Definition, Protocol):
    """Protocol of a v2 definition run by ``execution.Workflow``."""

    def native_rejection(self, function, step) -> str | None:
        """Return why ``function`` is not the admitted binding for ``step``, else ``None``."""


@runtime_checkable
class StateDefinition(Definition, Protocol):
    """Protocol of a v3 definition run by ``stateful.StatefulWorkflow``."""

    helper_sources: tuple
    native_dependencies: Mapping
    timeouts: tuple
    schema_name: str
    schema_source: Path
    nodes: tuple
    edges: tuple
    declared_resources: Mapping

    def load_catalog(self, *, packaged_proof=None, config=None):
        """Return the ``StatefulCatalog`` that supplies this definition's bindings."""


__all__ = ['MAX_COMPONENT_PASSES', 'LayoutError', 'Step', 'Layout', 'Replacement', 'Resource',
           'InlinePromptRef', 'ToolUsage', 'freeze', 'declares_source', 'require_source_provenance', 'Definition', 'GraphDefinition', 'StateDefinition']
