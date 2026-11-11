"""Parser-free definition features consumed by the workflow engine.

Source admission captures declarations; the compiled-plan loader supplies the
same runtime contract without importing the declaration parser.
"""
from dataclasses import dataclass, field
import inspect
from pathlib import Path
import re
from types import MappingProxyType
from typing import ClassVar, Mapping, Protocol, runtime_checkable

MAX_COMPONENT_PASSES = 99
_ID = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_PY_ID = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
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

    def row_error(self, row, message):
        """Raise ``message`` at ``row``, in the document that declares it."""
        raise LayoutError(f'{row.get("_owner", self.path)}:{row.get("line")}: {message}')


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


def timeout_owner_problem(rows, steps, tool_ports):
    """The first Timeouts row with an invalid owner or key as ``(row, message)``, else ``None``.

    An owner is GRAPH, a step, or ``<node>.<port>`` naming a port of that node's own Tools table.
    ``steps`` maps ids to steps; ``tool_ports`` maps each node to its declared ports.
    """
    seen = set()
    for row in rows:
        owner = row['Owner']
        node, dotted, port = owner.partition('.')
        bad_key = _ID.fullmatch(row['Key'].lower()) is None
        if dotted:
            message = ('timeout port owner must name a node' if node not in steps or steps[node].kind != 'node'
                       else 'timeout port is not declared in the node Tools table' if port not in tool_ports.get(node, ())
                       else 'duplicate timeout port' if owner in seen
                       else 'invalid timeout key' if bad_key else None)
        else:
            message = ('unknown or duplicate timeout owner/key'
                       if owner not in {'GRAPH', *steps} or owner in seen or bad_key else None)
        if message is not None:
            return row, message
        seen.add(owner)
    return None


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


@dataclass(frozen=True)
class NativeCatalog:
    definition_id: str
    namespaces: object
    timeout_values: object
    timeout_keys: object
    admitted_modules: frozenset = frozenset({'<stateful_catalog>'})
    bindings: object = field(default_factory=lambda: MappingProxyType({}))
    binding_proofs: object = field(default_factory=lambda: MappingProxyType({}))



@runtime_checkable
class Definition(Protocol):
    """The shared workflow, journal and identity contract.

    ``version`` is the initial serialization contract. ``origin`` is ``'source'`` for definitions parsed
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
    """Declared node-binding features of the workflow contract."""

    def native_rejection(self, function, step) -> str | None:
        """Return why ``function`` is not the admitted binding for ``step``, else ``None``."""


@runtime_checkable
class StateDefinition(Definition, Protocol):
    """Declared typed-state features of the workflow contract."""

    helper_sources: tuple
    native_dependencies: Mapping
    timeouts: tuple
    schema_name: str
    schema_source: Path
    nodes: tuple
    edges: tuple
    declared_resources: Mapping

    def load_catalog(self, *, packaged_proof=None, config=None):
        """Return the ``NativeCatalog`` that supplies this definition's bindings."""


__all__ = ['MAX_COMPONENT_PASSES', 'LayoutError', 'Step', 'Layout', 'Replacement', 'Resource',
           'InlinePromptRef', 'ToolUsage', 'timeout_owner_problem', 'freeze', 'declares_source', 'require_source_provenance', 'Definition', 'GraphDefinition', 'StateDefinition', 'NativeCatalog']
