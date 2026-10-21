"""Explicit, admitted LangGraph state workflows (Workflow v3)."""

import asyncio
import ast
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from importlib.abc import Loader, MetaPathFinder
from importlib.util import spec_from_loader
import inspect
import os
from pathlib import Path
import re
import sys
import time
import tomllib
from threading import Lock, RLock
from types import MappingProxyType
from typing import Annotated, get_args, get_origin, get_type_hints
from typing_extensions import is_typeddict
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from api_agent.providers.progress import bind_timeout_budget, component_timeout
from api_agent.runtime.tasks import offload, settle
from .definition import Replacement, Resource, _declaration_parts, _inline_selection, _freeze, _read_bounded, _sections, _table as _shared_table
from .document_format import canonical_executable_bytes, validate_presentation_location
from .layout import Layout, LayoutError, Step, _ID


_LINK = re.compile(r'\[[^\]\n]+\]\(([^()\n]+)\)\Z')
_PY_ID = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
_LOADED_DEPENDENCIES = {}
_RESERVED_DEPENDENCIES = {}
_ADMITTED_LOADERS = {}
_DEPENDENCY_LOCK = RLock()
_CATALOG_LOCK = Lock()


class _AdmittedDependencyLoader(MetaPathFinder, Loader):
    def __init__(self, definition):
        self.definition = definition

    def find_spec(self, fullname, path=None, target=None):
        source = self.definition.native_dependencies.get(fullname)
        if source is None:
            return None
        with _DEPENDENCY_LOCK:
            if _RESERVED_DEPENDENCIES.get(fullname) != (
                    self.definition.definition_id, source, self.definition.contents[source]):
                return None
        package = source.name == '__init__.py'
        return spec_from_loader(fullname, self, origin=str(source), is_package=package)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        source = self.definition.native_dependencies[module.__name__]
        with _DEPENDENCY_LOCK:
            if _RESERVED_DEPENDENCIES.get(module.__name__) != (
                    self.definition.definition_id, source, self.definition.contents[source]):
                raise LayoutError(f'{source}:1: native dependency reservation changed; restart required')
        module.__file__ = str(source)
        if source.name == '__init__.py':
            module.__path__ = [str(source.parent)]
        exec(compile(self.definition.contents[source], str(source), 'exec'), module.__dict__)
        with _DEPENDENCY_LOCK:
            if _RESERVED_DEPENDENCIES.get(module.__name__) != (
                    self.definition.definition_id, source, self.definition.contents[source]):
                # Admission may have rolled back while package code was running.
                # Raising lets importlib discard its in-flight sys.modules entry.
                raise LayoutError(f'{source}:1: native dependency admission ended during import')
            _LOADED_DEPENDENCIES[module.__name__] = (self.definition.definition_id, source, module)


@dataclass(frozen=True)
class StatefulDefinition:
    graph_id: str
    entry: Path
    entry_root: str
    roots: object
    replacements: tuple
    resources: tuple[Resource, ...]
    declared_resources: object
    helper_sources: tuple[Path, ...]
    native_dependencies: object
    timeouts: tuple
    schema_name: str
    schema_source: Path
    nodes: tuple[dict, ...]
    edges: tuple[dict, ...]
    contents: object
    definition_id: str
    layout: Layout
    selected_declarations: object = None
    declaration_fragments: object = None

    def verify_sources(self):
        for path, admitted in self.contents.items():
            root = max((root for root in self.roots.values() if path.is_relative_to(root)), key=lambda p: len(p.parts))
            if _read_bounded(path, root) != admitted:
                raise LayoutError(f'{path}:1: resource changed after admission')


def load_stateful_definition(entry, *, base_root, pack_root, replacements=(), entry_root=None):
    """Admit a fully declared v3 graph and immutable native source snapshot."""
    entry = Path(entry).resolve()
    roots = {'base': Path(base_root).resolve(), 'pack': Path(pack_root).resolve()}
    if not any(entry.is_relative_to(root) for root in roots.values()):
        raise LayoutError(f'{entry}:1: declaration outside roots')
    if entry_root is None:
        entry_root = 'pack' if entry.is_relative_to(roots['pack']) and roots['pack'] != roots['base'] else 'base'
    if entry_root not in roots or not entry.is_relative_to(roots[entry_root]):
        raise LayoutError(f'{entry}:1: invalid declared entry root')
    def read(path):
        roots_for_path = [root for root in roots.values() if path.is_relative_to(root)]
        if not roots_for_path:
            raise LayoutError(f'{entry}:1: source outside roots')
        owner = max(roots_for_path, key=lambda p: len(p.parts))
        data = _read_bounded(path, owner)
        if path.suffixes[-2:] == ['.md', '.html']:
            validate_presentation_location(data, path=path, root=owner)
        return data
    contents = {entry: read(entry)}
    lines = canonical_executable_bytes(contents[entry], path=entry).decode('utf-8').splitlines()
    if not lines or lines[0] != '# Workflow v3':
        raise LayoutError(f'{entry}:1: expected Workflow v3 heading')
    _, sections = _sections(entry, contents[entry])
    required = {'Graph ID', 'State', 'Flow', 'Edges'}
    if not required <= set(sections) or set(sections) - required - {'Native Sources', 'Native Dependencies', 'Resources', 'Timeouts'}:
        raise LayoutError(f'{entry}:1: missing required section')
    identity = _shared_table(entry, sections['Graph ID'], ('ID',))
    state = _shared_table(entry, sections['State'], ('Registry', 'Source'))
    if len(identity) != 1 or len(state) != 1 or not _ID.fullmatch(identity[0]['ID']) or not _PY_ID.fullmatch(state[0]['Registry']):
        raise LayoutError(f'{entry}:1: invalid graph or state identity')
    replacements = tuple(replacements)
    if any(not isinstance(item, Replacement) for item in replacements) or len({item.target_id for item in replacements}) != len(replacements):
        raise LayoutError(f'{entry}:1: invalid replacements')
    kinds = {entry: 'declaration'}
    def linked(row):
        match = _LINK.fullmatch(row['Source'])
        if match is None:
            raise LayoutError(f'{entry}:{row["line"]}: expected one source link')
        parsed = urlsplit(match[1])
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or re.search(r'%(?:2f|5c)', match[1], re.I):
            raise LayoutError(f'{entry}:{row["line"]}: invalid source link')
        try:
            name = unquote(parsed.path, errors='strict')
        except UnicodeError as error:
            raise LayoutError(f'{entry}:{row["line"]}: invalid encoded source link') from error
        if not name or name.startswith('/') or '\\' in name or '?' in name or '#' in name:
            raise LayoutError(f'{entry}:{row["line"]}: invalid source link')
        if name.startswith('<resource_pack>/'):
            target = (roots['pack'] / name.removeprefix('<resource_pack>/')).resolve()
        else:
            target = (row.get('_owner', entry).parent / name).resolve()
        if not any(target.is_relative_to(root) for root in roots.values()):
            raise LayoutError(f'{entry}:{row["line"]}: source outside declared roots')
        return target
    def source(row):
        target = linked(row)
        if target.suffix != '.py':
            raise LayoutError(f'{entry}:{row["line"]}: native source must be Python')
        if target not in contents:
            contents[target] = read(target)
        kinds[target] = 'native_source'
        return target
    state_source = source(state[0])
    declared_resources = {}
    if 'Resources' in sections:
        for row in _shared_table(entry, sections['Resources'], ('ID', 'Kind', 'Source')):
            identifier, kind = row['ID'], row['Kind']
            if not _ID.fullmatch(identifier) or identifier in declared_resources or kind not in ('prompt', 'configuration', 'data', 'schema', 'asset'):
                raise LayoutError(f'{entry}:{row["line"]}: invalid resource declaration')
            replacement = next((item for item in replacements if item.target_id == identifier), None)
            if replacement is None:
                target = linked(row)
            else:
                if replacement.root_id not in roots or Path(replacement.entry).is_absolute() or '..' in Path(replacement.entry).parts:
                    raise LayoutError(f'{entry}:{row["line"]}: invalid resource replacement')
                target = (roots[replacement.root_id] / replacement.entry).resolve()
            if target not in contents:
                contents[target] = read(target)
            if target in kinds and kinds[target] != kind:
                raise LayoutError(f'{entry}:{row["line"]}: resource has conflicting kinds')
            kinds[target] = kind
            declared_resources[identifier] = target
    helpers = []
    if 'Native Sources' in sections:
        for row in _shared_table(entry, sections['Native Sources'], ('Source',)):
            helpers.append(source(row))
        if len(helpers) != len(set(helpers)):
            raise LayoutError(f'{entry}:1: duplicate native helper source')
    dependencies = {}
    if 'Native Dependencies' in sections:
        for row in _shared_table(entry, sections['Native Dependencies'], ('Module', 'Source')):
            module = row['Module']
            if (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*', module)
                    or module in dependencies):
                raise LayoutError(f'{entry}:{row["line"]}: invalid native dependency module')
            path = source(row)
            if not any(path == (base / Path(*module.split('.'))).with_suffix('.py')
                       or path == base / Path(*module.split('.')) / '__init__.py'
                       for root in roots.values() for base in (root, root / 'src')):
                raise LayoutError(f'{entry}:{row["line"]}: dependency source does not match module')
            kinds[path] = 'native_dependency'
            dependencies[module] = path
    nodes = []
    steps = []
    selected_declarations = {}
    declaration_fragments = {}
    inline_references = {}
    def visit(owner, body, parent=None, depth=0):
        if depth > 64:
            raise LayoutError(f'{owner}:1: component nesting exceeds 64 levels')
        count = 0
        for number, line in body:
            if not line.strip():
                continue
            match = re.fullmatch(r'([1-9][0-9]?)\.  \[(node|component) ([a-z][a-z0-9_]{0,63})\]\(([^()]+)\)', line)
            if match is None or int(match[1]) != count + 1:
                raise LayoutError(f'{owner}:{number}: Flow requires sequential linked steps')
            count += 1
            kind, identifier, target = match[2], match[3], match[4]
            if identifier in {item[0] for item in steps}:
                raise LayoutError(f'{owner}:{number}: duplicate step ID')
            replacement = next((item for item in replacements if item.target_id == identifier), None)
            inline = _inline_selection(owner, contents[owner], target, kind, identifier, parent, number)
            if inline is not None:
                inline_references.setdefault(owner, set()).add(identifier)
            if replacement is not None:
                if inline is None:
                    if (re.search(r'%(?![0-9A-Fa-f]{2})', target)
                            or any(ord(char) < 32 or ord(char) == 127 for char in target)):
                        raise LayoutError(f'{owner}:{number}: invalid declaration link')
                    try:
                        parsed = urlsplit(target)
                    except ValueError as error:
                        raise LayoutError(f'{owner}:{number}: invalid declaration link') from error
                    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                            or re.search(r'%(?:2f|5c)', target, re.I)):
                        raise LayoutError(f'{owner}:{number}: invalid declaration link')
                    try:
                        decoded = unquote(parsed.path, errors='strict')
                    except UnicodeError as error:
                        raise LayoutError(f'{owner}:{number}: invalid declaration link') from error
                    if (not decoded or decoded.startswith('/') or '\\' in decoded or '?' in decoded
                            or '#' in decoded or any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
                        raise LayoutError(f'{owner}:{number}: invalid declaration link')
                    try:
                        original = (owner.parent / decoded).resolve(strict=False)
                    except (OSError, RuntimeError, ValueError) as error:
                        raise LayoutError(f'{owner}:{number}: invalid declaration link') from error
                    if (not any(original.is_relative_to(root) for root in roots.values())
                            or original.suffixes[-2:] != ['.md', '.html']):
                        raise LayoutError(f'{owner}:{number}: declaration link outside roots or wrong format')
                if replacement.root_id not in roots or Path(replacement.entry).is_absolute() or '..' in Path(replacement.entry).parts:
                    raise LayoutError(f'{owner}:{number}: invalid replacement')
                declaration = (roots[replacement.root_id] / replacement.entry).resolve()
            elif inline is not None:
                declaration = owner
            else:
                if Path(target).is_absolute() or '#' in target or '?' in target:
                    raise LayoutError(f'{owner}:{number}: invalid declaration link')
                declaration = (owner.parent / target).resolve()
            if declaration.suffixes[-2:] != ['.md', '.html']:
                raise LayoutError(f'{owner}:{number}: declaration must end .md.html')
            if declaration not in contents:
                contents[declaration] = read(declaration)
            kinds[declaration] = 'declaration'
            if inline is not None and replacement is None:
                title, child = f'# Node: {identifier}', inline
                declaration_fragments[identifier] = 'node-' + identifier
            else:
                title, child = _sections(declaration, contents[declaration])
            if title not in (f'# {kind.title()} v3: {identifier}', f'# {kind.title()}: {identifier}'):
                raise LayoutError(f'{declaration}:1: declaration identity mismatch')
            steps.append((identifier, kind, parent, declaration))
            selected_declarations[identifier] = (owner, number, declaration)
            if kind == 'component':
                if set(child) != {'Flow'}:
                    raise LayoutError(f'{declaration}:1: component requires only Flow')
                visit(declaration, child['Flow'], identifier, depth + 1)
            else:
                if set(child) != {'Native'}:
                    raise LayoutError(f'{declaration}:1: node requires only Native')
                binding = _shared_table(declaration, child['Native'], ('Registry', 'Kind', 'Source'))
                if len(binding) != 1 or not _ID.fullmatch(binding[0]['Registry']) or binding[0]['Kind'] not in ('deterministic', 'llm'):
                    raise LayoutError(f'{declaration}:1: invalid native binding')
                nodes.append(dict(ID=identifier, Registry=binding[0]['Registry'], Kind=binding[0]['Kind'],
                                  Source=source(dict(binding[0], line=binding[0]['line'], _owner=declaration)),
                                  _owner_id=identifier, line=binding[0]['line']))
        if count == 0:
            raise LayoutError(f'{owner}:1: Flow requires steps')
    visit(entry, sections['Flow'])
    for physical, kind in kinds.items():
        if kind == 'declaration':
            _, _, inline_nodes = _declaration_parts(physical, contents[physical])
            if set(inline_nodes) != inline_references.get(physical, set()):
                raise LayoutError(f'{physical}:1: unreferenced inline node declaration')
    if set(declared_resources) & {item[0] for item in steps}:
        raise LayoutError(f'{entry}:1: resource ID conflicts with step ID')
    if {item.target_id for item in replacements} - {item[0] for item in steps} - set(declared_resources):
        raise LayoutError(f'{entry}:1: unknown replacement target')
    edges = []
    parents = {name: parent for name, _, parent, _ in steps}
    for row in _shared_table(entry, sections['Edges'], ('From', 'To', 'Router', 'Source')):
        if (row['From'] not in parents or row['To'] not in (*parents, 'END')
                or (row['To'] != 'END' and parents[row['To']] != parents[row['From']])
                or (row['Router'] != '-' and not _ID.fullmatch(row['Router']))):
            raise LayoutError(f'{entry}:{row["line"]}: invalid edge declaration')
        if row['Router'] == '-' and row['Source'] != '-':
            raise LayoutError(f'{entry}:{row["line"]}: static edge source must be -')
        edges.append(dict(row, Source=None if row['Router'] == '-' else source(row)))
    for name in parents:
        outgoing = [row for row in edges if row['From'] == name]
        if not outgoing or (len(outgoing) > 1 and any(row['Router'] == '-' for row in outgoing)) or len({row['Router'] for row in outgoing}) != 1:
            raise LayoutError(f'{entry}:1: each node needs one static or one declared routed edge set')
        if len({row['To'] for row in outgoing}) != len(outgoing) or len({row['Source'] for row in outgoing}) != 1:
            raise LayoutError(f'{entry}:1: duplicate or mixed route source')
    timeouts = []
    if 'Timeouts' in sections:
        for row in _shared_table(entry, sections['Timeouts'], ('Owner', 'Source', 'Key')):
            if (row['Owner'] not in {'GRAPH', *parents} or row['Owner'] in {item['Owner'] for item in timeouts}
                    or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', row['Key'])):
                raise LayoutError(f'{entry}:{row["line"]}: invalid timeout owner or key')
            path = linked(row)
            if path.suffix != '.toml':
                raise LayoutError(f'{entry}:{row["line"]}: timeout source must be TOML')
            if path not in contents:
                contents[path] = read(path)
            if path in kinds and kinds[path] != 'configuration':
                raise LayoutError(f'{entry}:{row["line"]}: timeout source has conflicting kind')
            kinds[path] = 'configuration'
            timeouts.append(dict(row, Source=path))
    resources = []
    if sum(map(len, contents.values())) > 64 * 1024 * 1024:
        raise LayoutError(f'{entry}:1: workflow snapshot exceeds 64 MiB')
    for path, data in contents.items():
        root_id = max((key for key, root in roots.items() if path.is_relative_to(root)),
                      key=lambda key: (len(roots[key].parts), key == entry_root))
        kind = kinds[path]
        resources.append(Resource(kind, root_id + '/' + path.relative_to(roots[root_id]).as_posix(),
                                  data, sha256(data).hexdigest()))
    resources.sort(key=lambda item: item.logical_path)
    import json
    definition_id = sha256(json.dumps({'version': 3, 'graph_id': identity[0]['ID'],
                                       'resources': [(r.kind, r.logical_path, r.sha256) for r in resources],
                                       'replacements': [(r.target_id, r.root_id, Path(r.entry).as_posix()) for r in replacements]},
                                      sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    layout_steps = tuple(Step(name, kind, parent, next(row['Source'] for row in nodes if row['ID'] == name)
                              if kind == 'node' else path, selected_declarations[name][1]) for name, kind, parent, path in steps)
    layout_edges = tuple(_freeze(dict(row, When='always' if row['Router'] == '-' else row['Router']))
                         for row in edges)
    layout = Layout(entry, layout_steps, MappingProxyType({'Edges': layout_edges}), tuple(sorted(contents)))
    resources_by_path = {r.logical_path: r for r in resources}
    selected_resources = {}
    for identifier, path in declared_resources.items():
        root_id = max((key for key, root in roots.items() if path.is_relative_to(root)),
                      key=lambda key: (len(roots[key].parts), key == entry_root))
        selected_resources[identifier] = resources_by_path[root_id + '/' + path.relative_to(roots[root_id]).as_posix()]
    definition = StatefulDefinition(identity[0]['ID'], entry, entry_root, MappingProxyType(roots), replacements,
                                    tuple(resources), MappingProxyType(selected_resources), tuple(helpers),
                                    MappingProxyType(dependencies), tuple(_freeze(row) for row in timeouts),
                                    state[0]['Registry'], state_source,
                                    tuple(_freeze(row) for row in nodes), tuple(_freeze(row) for row in edges),
                                    MappingProxyType(contents), definition_id, layout,
                                    MappingProxyType(selected_declarations), MappingProxyType(declaration_fragments))
    _validated_timeouts(definition)
    return definition


@dataclass(frozen=True)
class StatefulCatalog:
    definition_id: str
    namespaces: object
    timeout_values: object
    timeout_keys: object


def _validated_timeouts(definition, config=None):
    """Validate declared timeout schema and selected values before native effects."""
    from api_agent.config.declarations import UNSET, schema_from_dict
    config = config or {}
    values = {}
    keys = {}
    for row in definition.timeouts:
        try:
            declaration = schema_from_dict(tomllib.loads(definition.contents[row['Source']].decode('utf-8')))
        except (UnicodeError, ValueError) as error:
            raise LayoutError(f'{row["Source"]}:{row["line"]}: invalid timeout declaration: {error}') from error
        field = declaration.fields.get(row['Key'])
        if field is None or field.type not in ('int', 'float'):
            raise LayoutError(f'{row["Source"]}:{row["line"]}: timeout key must be numeric')
        value = config.get(row['Key'], None if field.default is UNSET else field.default)
        if value is not None and (type(value) not in (int, float) or not 0 < value < float('inf')):
            raise LayoutError(f'{row["Source"]}:{row["line"]}: timeout must be finite positive seconds')
        values[row['Owner']] = value
        keys[row['Owner']] = row['Key']
    return MappingProxyType(values), MappingProxyType(keys)


def _source_manifest(definition, packaged_proof=None):
    """Verify every logical owner before native effects."""
    from .provenance import verified_documents
    return verified_documents(definition, packaged_proof=packaged_proof)


def load_stateful_catalog(definition, *, packaged_proof=None, config=None):
    """Execute only the selected, still admitted native sources."""
    definition.verify_sources()
    timeout_values, timeout_keys = _validated_timeouts(definition, config)
    if os.environ.get('API_AGENT_ALLURE_REPORTING') == '1':
        _source_manifest(definition, packaged_proof)
    result = {}
    sources = {definition.schema_source, *(row['Source'] for row in definition.nodes),
               *(row['Source'] for row in definition.edges if row['Source'] is not None)}
    def local_module_path(module):
        parts = Path(*module.split('.'))
        for root in definition.roots.values():
            for base in (root, root / 'src'):
                for candidate in ((base / parts).with_suffix('.py'), base / parts / '__init__.py'):
                    if candidate.is_file():
                        return candidate.resolve()
        return None
    trusted_host = Path(__file__).resolve().parents[1]
    def module_for_path(path):
        base = max((base for root in definition.roots.values() for base in (root, root / 'src')
                    if path.is_relative_to(base)), key=lambda item: len(item.parts))
        relative = path.relative_to(base).with_suffix('')
        parts = relative.parts[:-1] if relative.name == '__init__' else relative.parts
        return '.'.join(parts)
    for path in (*definition.helper_sources, *sources, *definition.native_dependencies.values()):
        tree = ast.parse(definition.contents[path], filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    owner = module_for_path(path)
                    package = owner if path.name == '__init__.py' else owner.rpartition('.')[0]
                    prefix = package.split('.') if package else []
                    if node.level > len(prefix) + 1:
                        raise LayoutError(f'{path}:1: relative native import leaves package')
                    prefix = prefix[:len(prefix) - node.level + 1]
                    base_name = '.'.join((*prefix, node.module or '')) if node.module else '.'.join(prefix)
                else:
                    base_name = node.module or ''
                names = [base_name, *(base_name + '.' + alias.name for alias in node.names if alias.name != '*')]
            else:
                continue
            for name in names:
                if not name:
                    continue
                parts = name.split('.')
                for length in range(1, len(parts) + 1):
                    module = '.'.join(parts[:length])
                    local = local_module_path(module)
                    host_runtime = (module == 'api_agent' or module.startswith('api_agent.')) and (
                        local is not None and local.is_relative_to(trusted_host))
                    if local is not None and not host_runtime and definition.native_dependencies.get(module) != local:
                        raise LayoutError(f'{path}:1: local native import must be declared as a dependency')
    # Reserve every declared name before native execution, including imports
    # deferred until a later graph turn. The finder stays installed so lazy
    # imports read the admitted bytes without an ambient source-root sys.path.
    with _CATALOG_LOCK:
        with _DEPENDENCY_LOCK:
            for module, source in definition.native_dependencies.items():
                reserved = (definition.definition_id, source, definition.contents[source])
                if (_RESERVED_DEPENDENCIES.get(module, reserved) != reserved
                        or module in sys.modules and _LOADED_DEPENDENCIES.get(module) != (
                            definition.definition_id, source, sys.modules[module])):
                    raise LayoutError(f'{source}:1: native dependency already loaded from another definition; restart required')
            loader = _ADMITTED_LOADERS.get(definition.definition_id)
            fresh = loader is None and bool(definition.native_dependencies)
            if fresh:
                loader = _AdmittedDependencyLoader(definition)
                for module, source in definition.native_dependencies.items():
                    _RESERVED_DEPENDENCIES[module] = (definition.definition_id, source, definition.contents[source])
                _ADMITTED_LOADERS[definition.definition_id] = loader
                sys.meta_path.insert(0, loader)
        try:
            for path in sorted(sources):
                module = module_for_path(path)
                package = module if path.name == '__init__.py' else module.rpartition('.')[0]
                namespace = {'__name__': '<stateful_catalog>', '__file__': str(path), '__package__': package or None}
                for helper in definition.helper_sources:
                    exec(compile(definition.contents[helper], str(helper), 'exec'), namespace)
                exec(compile(definition.contents[path], str(path), 'exec'), namespace)
                result[path] = MappingProxyType(namespace)
        except BaseException:
            if fresh:
                with _DEPENDENCY_LOCK:
                    for module in sorted(definition.native_dependencies, key=lambda name: name.count('.'), reverse=True):
                        loaded = _LOADED_DEPENDENCIES.get(module)
                        if loaded is not None and loaded[0] == definition.definition_id:
                            if sys.modules.get(module) is loaded[2]:
                                del sys.modules[module]
                            del _LOADED_DEPENDENCIES[module]
                        del _RESERVED_DEPENDENCIES[module]
                    del _ADMITTED_LOADERS[definition.definition_id]
                    sys.meta_path.remove(loader)
            raise
    return StatefulCatalog(definition.definition_id, MappingProxyType(result), timeout_values, timeout_keys)


@dataclass(frozen=True)
class StateNodeContext:
    node_id: str
    services: object
    request: object
    resources: object


class StatefulWorkflow:
    """One declared StateGraph with process-local per-thread checkpoints."""

    def __init__(self, definition, *, catalog, reducers, services=None, journal=None, checkpointer=None,
                 config=None):
        definition.verify_sources()
        if not isinstance(catalog, StatefulCatalog) or catalog.definition_id != definition.definition_id:
            raise LayoutError('catalog does not match admitted definition')
        self.definition = definition
        self.services = MappingProxyType(dict(services or {}))
        self.config = MappingProxyType(dict(config or {}))
        requested_timeouts, requested_keys = _validated_timeouts(definition, self.config)
        if requested_timeouts != catalog.timeout_values or requested_keys != catalog.timeout_keys:
            raise LayoutError('catalog timeout configuration does not match selected values')
        self.timeouts = catalog.timeout_values
        self.timeout_keys = catalog.timeout_keys
        self.journal = journal
        self.catalog = catalog
        self.reducers = reducers
        schema = self._binding(definition.schema_source, definition.schema_name)
        if not is_typeddict(schema):
            raise LayoutError('state registry must name a TypedDict')
        hints = get_type_hints(schema, include_extras=True)
        for key, hint in hints.items():
            if get_origin(hint) is Annotated:
                metadata = get_args(hint)[1:]
                if len(metadata) != 1 or reducers.get(key) is not metadata[0]:
                    raise LayoutError(f'unregistered state reducer: {key}')
        if any(key not in hints for key in reducers):
            raise LayoutError('reducer references undeclared state field')
        self.schema = schema
        self._request = ContextVar('stateful_workflow_request', default=None)
        self._services = ContextVar('stateful_workflow_services', default=self.services)
        self._journal = ContextVar('stateful_workflow_journal', default=journal)
        self._turn_state = ContextVar('stateful_workflow_turn_state', default=None)
        self._active_threads = set()
        self._thread_guard = Lock()
        self._parents = {step.id: step.parent for step in definition.layout.steps}
        self.graph = self._compile_scope(None, checkpointer=checkpointer if checkpointer is not None else InMemorySaver())

    def _compile_scope(self, parent, *, checkpointer=None):
        steps = [step for step in self.definition.layout.steps if step.parent == parent]
        graph = StateGraph(self.schema)
        if parent is not None:
            async def entry(_state):
                await self._start_component(parent)
                self._event('edge', owner=parent, source='START', target=steps[0].id, implicit=True)
                return {}
            graph.add_node('__entry', entry)
        for step in steps:
            if step.kind == 'component':
                graph.add_node(step.id, self._compile_scope(step.id))
            else:
                row = next(row for row in self.definition.nodes if row['ID'] == step.id)
                function = self._binding(row['Source'], row['Registry'])
                if not callable(function):
                    raise LayoutError('node registry must be callable')
                graph.add_node(step.id, self._node(row, function))
        graph.add_edge(START, '__entry' if parent is not None else steps[0].id)
        if parent is not None:
            graph.add_edge('__entry', steps[0].id)
        for step in steps:
            edges = [edge for edge in self.definition.edges if edge['From'] == step.id]
            if edges[0]['Router'] == '-':
                graph.add_conditional_edges(step.id, self._static_route(step.id, edges[0]['To'], parent),
                                            {edges[0]['To']: END if edges[0]['To'] == 'END' else edges[0]['To']})
            else:
                router = self._binding(edges[0]['Source'], edges[0]['Router'])
                if not callable(router):
                    raise LayoutError('router registry must be callable')
                graph.add_conditional_edges(step.id, self._route(step.id, router, edges, parent),
                                            {edge['To']: END if edge['To'] == 'END' else edge['To'] for edge in edges})
        return graph.compile(checkpointer=checkpointer)

    def _binding(self, path, name):
        namespace = self.catalog.namespaces.get(path)
        value = namespace.get(name) if namespace is not None else None
        if value is None or getattr(value, '__module__', None) != '<stateful_catalog>':
            raise LayoutError(f'{path}:1: missing admitted native binding {name}')
        return value

    def _event(self, event, **metadata):
        services = self._services.get()
        callback = self._journal.get() or services.get('journal')
        if callback is not None:
            callback.observe(dict(event=event, **metadata))
        observer = services.get('observe')
        if observer is not None:
            observer(dict(event=event, **metadata))

    def _node(self, row, function):
        async def invoke(state):
            parent = self._parents[row['ID']]
            turn = self._turn_state.get()
            parent_scope = (bind_timeout_budget(turn['open_components'][parent]['budget'])
                            if parent is not None else nullcontext())
            with parent_scope:
                async with component_timeout('Workflow ' + row['ID'], self.timeouts.get(row['ID'])) as budget:
                    return await self._run_node(row, function, state, budget)
        return invoke

    async def _run_node(self, row, function, state, budget):
        visit_id = budget.id
        started = time.monotonic()
        wall_started = time.time() * 1000
        self._event('scope_start', scope_id=budget.id, parent_id=budget.parent_id, owner=row['ID'],
                    kind='node', node=row['ID'], visit_id=visit_id, attempt=1, max_passes=1,
                    started_ms=wall_started,
                    declared_timeout=self.timeouts.get(row['ID']), effective_timeout=budget.timeout,
                    timeout_key=self.timeout_keys.get(row['ID']))
        outcome = 'failed'
        try:
            context = StateNodeContext(row['ID'], self._services.get(), self._request.get(),
                                       self.definition.declared_resources)
            from .attribution import bind_workflow_visit
            with bind_workflow_visit(self._journal.get(), budget.id):
                result = (await function(state, context) if inspect.iscoroutinefunction(function)
                          else await offload(function, state, context))
                if inspect.isawaitable(result):
                    result = await result
            if type(result) is not dict or any(key not in self.schema.__annotations__ for key in result):
                raise ValueError('node must return a partial state update')
            outcome = 'passed'
            self._event('node', scope_id=budget.id, node=row['ID'], visit_id=visit_id,
                        elapsed=time.monotonic() - started, kind=row['Kind'])
            return result
        except asyncio.CancelledError:
            outcome = ('failed' if budget.deadline is not None
                       and time.monotonic() >= budget.deadline else 'cancelled')
            raise
        finally:
            self._event('scope_end', scope_id=budget.id, parent_id=budget.parent_id,
                        owner=row['ID'], kind='node', node=row['ID'], visit_id=visit_id,
                        elapsed=time.monotonic() - started, outcome=outcome,
                        started_ms=wall_started, stopped_ms=time.time() * 1000,
                        declared_timeout=self.timeouts.get(row['ID']), effective_timeout=budget.timeout,
                        timeout_key=self.timeout_keys.get(row['ID']))
            self._turn_state.get()['visits'][row['ID']] = visit_id

    async def _start_component(self, parent):
        turn = self._turn_state.get()
        owner_parent = self._parents[parent]
        parent_scope = (bind_timeout_budget(turn['open_components'][owner_parent]['budget'])
                        if owner_parent is not None else nullcontext())
        ready = asyncio.get_running_loop().create_future()
        finish = asyncio.get_running_loop().create_future()
        async def own():
            try:
                with parent_scope:
                    async with component_timeout('Workflow ' + parent, self.timeouts.get(parent)) as budget:
                        started = time.monotonic()
                        wall_started = time.time() * 1000
                        self._event('scope_start', scope_id=budget.id, parent_id=budget.parent_id,
                                    owner=parent, kind='component', visit_id=budget.id,
                                    attempt=1, max_passes=1,
                                    started_ms=wall_started,
                                    declared_timeout=self.timeouts.get(parent),
                                    effective_timeout=budget.timeout,
                                    timeout_key=self.timeout_keys.get(parent))
                        ready.set_result(budget)
                        outcome = 'failed'
                        try:
                            await finish
                            outcome = 'passed'
                        except asyncio.CancelledError:
                            outcome = ('failed' if budget.deadline is not None
                                       and time.monotonic() >= budget.deadline else 'cancelled')
                            raise
                        finally:
                            self._event('scope_end', scope_id=budget.id, parent_id=budget.parent_id,
                                        owner=parent, kind='component', visit_id=budget.id,
                                        elapsed=time.monotonic() - started, outcome=outcome,
                                        started_ms=wall_started, stopped_ms=time.time() * 1000,
                                        declared_timeout=self.timeouts.get(parent),
                                        effective_timeout=budget.timeout,
                                        timeout_key=self.timeout_keys.get(parent))
            except BaseException as error:
                if not ready.done():
                    ready.set_exception(error)
                raise
        task = asyncio.create_task(own())
        try:
            budget = await ready
        except BaseException:
            task.cancel()
            try:
                await settle(task)
            except BaseException:
                pass
            raise
        turn['open_components'][parent] = dict(budget=budget, task=task, finish=finish)

    async def _complete_component(self, parent, outcome='passed'):
        if parent is None:
            return
        turn = self._turn_state.get()
        if parent not in turn['open_components']:
            return
        owned = turn['open_components'][parent]
        if outcome == 'passed' and not owned['finish'].done():
            owned['finish'].set_result(None)
        elif not owned['task'].done():
            owned['task'].cancel()
        try:
            await settle(owned['task'])
        finally:
            # settle only exits after the owned task reaches a terminal state.
            turn['open_components'].pop(parent)
            turn['visits'][parent] = owned['budget'].id

    def _static_route(self, source, target, parent):
        async def route(_state):
            visit_id = self._turn_state.get()['visits'].get(source)
            self._event('edge', owner=parent or 'GRAPH', source=source, target=target,
                        visit_id=visit_id, implicit=False)
            if target == 'END':
                await self._complete_component(parent)
            return target
        return route

    def _route(self, source, router, edges, parent):
        targets = {edge['To'] for edge in edges}
        async def route(state):
            target = router(state, StateNodeContext(source, self._services.get(), self._request.get(),
                                                    self.definition.declared_resources))
            if target not in targets:
                raise ValueError(f'router for {source} returned an undeclared target')
            visit_id = self._turn_state.get()['visits'].get(source)
            self._event('edge', owner=parent or 'GRAPH', source=source, target=target,
                        visit_id=visit_id, condition=edges[0]['Router'], implicit=False)
            if target == 'END':
                await self._complete_component(parent)
            return target
        return route

    @asynccontextmanager
    async def _turn(self, thread_id, request, services, journal, turn_id=None, turn_kind='initial'):
        self.definition.verify_sources()
        selected_services = MappingProxyType({**self.services, **(services or {})})
        selected_journal = (journal if journal is not None else
                            self.journal if self.journal is not None else
                            selected_services.get('journal'))
        journal_turn = getattr(selected_journal, 'turn_id', None)
        if turn_id is not None and journal_turn is not None and turn_id != journal_turn:
            raise ValueError('explicit turn identity disagrees with the active journal')
        recorded = os.environ.get('API_AGENT_ALLURE_REPORTING') == '1'
        if recorded:
            proof = selected_services.get('packaged_proof')
            source_manifest = _source_manifest(self.definition, proof)
            from .journal import WorkflowJournal
            journal = WorkflowJournal(self.definition,
                log_content=self.config.get('LOG_CONTENT') is True,
                source_manifest=source_manifest,
                boundary=selected_services.get('emissions'),
                turn_id=turn_id,
                test_id=os.environ.get('PYTEST_CURRENT_TEST', '').rsplit(' ', 1)[0] or None)
        with self._thread_guard:
            if thread_id in self._active_threads:
                raise RuntimeError('conversation already has an active turn')
            self._active_threads.add(thread_id)
        token = self._request.set(request)
        services_token = self._services.set(selected_services)
        journal_token = self._journal.set(journal if recorded else selected_journal)
        state_token = self._turn_state.set({'visits': {}, 'open_components': {}})
        try:
            selected_journal = self._journal.get()
            active_turn = turn_id or (selected_journal.turn_id if selected_journal is not None else None)
            if active_turn is not None:
                from api_agent.testing.inference_usage import user_turn
            with user_turn(active_turn, kind=turn_kind) if active_turn is not None else nullcontext():
                async with component_timeout('Workflow GRAPH', self.timeouts.get('GRAPH')) as budget:
                    async with self._scope_graph(budget):
                        yield
        finally:
            self._request.reset(token)
            self._services.reset(services_token)
            self._journal.reset(journal_token)
            self._turn_state.reset(state_token)
            with self._thread_guard:
                self._active_threads.remove(thread_id)
            if recorded:
                from .evidence import publish
                publish(journal.finalize())

    @asynccontextmanager
    async def _scope_graph(self, budget):
        started = time.monotonic()
        wall_started = time.time() * 1000
        self._event('scope_start', scope_id=budget.id, parent_id=budget.parent_id,
                    owner='GRAPH', kind='graph', attempt=1, max_passes=1,
                    started_ms=wall_started,
                    declared_timeout=self.timeouts.get('GRAPH'), effective_timeout=budget.timeout,
                    timeout_key=self.timeout_keys.get('GRAPH'))
        self._event('edge', owner='GRAPH', source='START', target=self.definition.layout.steps[0].id,
                    implicit=True)
        outcome = 'failed'
        try:
            yield
            outcome = 'passed'
        except asyncio.CancelledError:
            outcome = ('failed' if budget.deadline is not None
                       and time.monotonic() >= budget.deadline else 'cancelled')
            raise
        finally:
            try:
                for component in tuple(self._turn_state.get()['open_components']):
                    try:
                        await self._complete_component(component, outcome=outcome)
                    except BaseException:
                        if outcome == 'passed':
                            raise
            finally:
                self._event('scope_end', scope_id=budget.id, parent_id=budget.parent_id,
                            owner='GRAPH', kind='graph', elapsed=time.monotonic() - started,
                            started_ms=wall_started, stopped_ms=time.time() * 1000,
                            outcome=outcome, declared_timeout=self.timeouts.get('GRAPH'),
                            effective_timeout=budget.timeout, timeout_key=self.timeout_keys.get('GRAPH'))

    async def arun(self, update, *, thread_id, request=None, services=None, journal=None,
                   turn_id=None, turn_kind='initial'):
        if type(thread_id) is not str or not thread_id or type(update) is not dict:
            raise ValueError('thread_id and partial state update are required')
        async with self._turn(thread_id, request, services, journal, turn_id, turn_kind):
            return await self.graph.ainvoke(update, {'configurable': {'thread_id': thread_id}})

    async def astream_events(self, update, *, thread_id, request=None, services=None, journal=None,
                             turn_id=None, turn_kind='initial'):
        if type(thread_id) is not str or not thread_id or type(update) is not dict:
            raise ValueError('thread_id and partial state update are required')
        async with self._turn(thread_id, request, services, journal, turn_id, turn_kind):
            queue = asyncio.Queue(maxsize=32)
            async def produce():
                async for event in self.graph.astream_events(
                        update, {'configurable': {'thread_id': thread_id}}, version='v2'):
                    await queue.put(event)
            producer = asyncio.create_task(produce())
            try:
                while True:
                    if producer.done():
                        while not queue.empty():
                            yield queue.get_nowait()
                        await producer
                        break
                    pending = asyncio.create_task(queue.get())
                    try:
                        done, _ = await asyncio.wait((pending, producer),
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if pending in done:
                            yield pending.result()
                    finally:
                        if not pending.done():
                            pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
            finally:
                if not producer.done():
                    producer.cancel()
                try:
                    await settle(producer)
                except BaseException:
                    pass


def diagram_stateful(definition):
    """Project only admitted v3 structure; diagram generation never imports native code."""
    lines = ['```mermaid', 'flowchart TD']
    for step in definition.layout.steps:
        lines.append(f'  {step.id}[{step.id}]')
    lines.append(f'  START --> {definition.layout.steps[0].id}')
    for edge in definition.edges:
        label = '' if edge['Router'] == '-' else f'|{edge["Router"]}|'
        lines.append(f'  {edge["From"]} -->{label} {edge["To"]}')
    lines.append('```')
    return '\n'.join(lines) + '\n'


__all__ = ['StatefulDefinition', 'StatefulCatalog', 'StatefulWorkflow', 'StateNodeContext',
           'load_stateful_definition', 'load_stateful_catalog', 'diagram_stateful', 'add_messages']
