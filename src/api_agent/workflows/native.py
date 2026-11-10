"""Source admission and native binding for declared typed state."""
from .run_context import recording_enabled

import ast
from dataclasses import dataclass
from hashlib import sha256
from importlib.abc import Loader, MetaPathFinder
from importlib.util import spec_from_loader
import os
from pathlib import Path
import re
import sys
import tomllib
from threading import Lock, RLock
from types import MappingProxyType
from typing import ClassVar
from urllib.parse import unquote, urlsplit


from .configuration import resolve_state_timeouts as _validated_timeouts
from .execution import _source_manifest
from .model import (NativeCatalog, Layout, LayoutError, Replacement, Resource, Step, ToolUsage, freeze as _freeze,
                    _ID, _MAX_SNAPSHOT_BYTES)


def _read_bounded(path, root):
    # The bounded reader belongs to the source loader; production never reads sources.
    from .definition import _read_bounded as read
    return read(path, root)


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
class TypedDefinition:
    version: ClassVar[int] = 1
    origin: ClassVar[str] = 'source'
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
    tool_usages: tuple[ToolUsage, ...] = ()
    application_binding: tuple[str, Path] | None = None
    imports: tuple = ()
    declaration_entry: Path | None = None

    def verify_sources(self):
        for path, admitted in self.contents.items():
            root = max((root for root in self.roots.values() if path.is_relative_to(root)), key=lambda p: len(p.parts))
            if _read_bounded(path, root) != admitted:
                raise LayoutError(f'{path}:1: resource changed after admission')

    def load_catalog(self, *, packaged_proof=None, config=None):
        """Execute the admitted native sources (source mode only)."""
        return load_native_bindings(self, packaged_proof=packaged_proof, config=config)


def _load_state_definition(entry, *, base_root, pack_root, replacements, entry_root, _root_data):
    """Admit a fully declared typed graph and immutable native source snapshot."""
    # Admission is source-only; production imports the runtime model instead.
    from api_agent.config.source_reads import resolve_path
    from .definition import _declaration_parts, _inline_selection, _sections, _table as _shared_table, _tool_operations
    from .document_format import validate_presentation_location
    entry = resolve_path(entry)
    roots = {'base': resolve_path(base_root), 'pack': resolve_path(pack_root)}
    captured_sizes = {}
    def read(path):
        roots_for_path = [root for root in roots.values() if path.is_relative_to(root)]
        if not roots_for_path:
            raise LayoutError(f'{entry}:1: source outside roots')
        owner = max(roots_for_path, key=lambda p: len(p.parts))
        data = _root_data if path == entry else _read_bounded(path, owner)
        captured_sizes[path] = len(data)
        if sum(captured_sizes.values()) > _MAX_SNAPSHOT_BYTES:
            raise LayoutError(f'{path}:1: workflow snapshot exceeds 64 MiB')
        if path.suffixes[-2:] == ['.md', '.html']:
            validate_presentation_location(data, path=path, root=owner)
        return data
    contents = {entry: read(entry)}
    _, sections = _sections(entry, contents[entry])
    required = {'Graph ID', 'State', 'Flow', 'Edges'}
    if not required <= set(sections) or set(sections) - required - {'Native Sources', 'Native Dependencies', 'Native Dependency Manifests', 'Resources', 'Timeouts', 'Tools', 'Application'}:
        raise LayoutError(f'{entry}:1: missing required section')
    identity = _shared_table(entry, sections['Graph ID'], ('ID',))
    state = _shared_table(entry, sections['State'], ('Registry', 'Source'))
    if len(identity) != 1 or len(state) != 1 or not _ID.fullmatch(identity[0]['ID']) or not _PY_ID.fullmatch(state[0]['Registry']):
        raise LayoutError(f'{entry}:1: invalid graph or state identity')
    replacements = tuple(replacements)
    if any(not isinstance(item, Replacement) for item in replacements) or len({item.target_id for item in replacements}) != len(replacements):
        raise LayoutError(f'{entry}:1: invalid replacements')
    kinds = {entry: 'declaration'}
    def linked(row, *, resolve=True):
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
            target = roots['pack'] / name.removeprefix('<resource_pack>/')
        else:
            target = row.get('_owner', entry).parent / name
        target = resolve_path(target) if resolve else Path(os.path.normpath(target))
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
    tool_usages = []
    def capture_tools(owner, scope_kind, scope, sections):
        ports = set()
        for row in _shared_table(owner, sections.get('Tools', []), ('Port', 'Instance')):
            port = row['Port']
            if not _ID.fullmatch(port) or port in ports:
                raise LayoutError(f'{owner}:{row["line"]}: invalid or duplicate tool port')
            ports.add(port)
            target = linked(dict(row, Source=row['Instance'], _owner=owner), resolve=False)
            if target.suffix != '.toml':
                raise LayoutError(f'{owner}:{row["line"]}: tool instance must be TOML')
            if target not in contents:
                contents[target] = read(target)
            if target in kinds and kinds[target] != 'tool_instance':
                raise LayoutError(f'{owner}:{row["line"]}: tool instance has conflicting resource kind')
            kinds[target] = 'tool_instance'
            tool_usages.append(ToolUsage(scope, port, target, scope_kind, owner, row['line'], _tool_operations(owner, row, scope_kind)))
    capture_tools(entry, 'graph', identity[0]['ID'], sections)
    state_source = source(state[0])
    application_binding = None
    if 'Application' in sections:
        application = _shared_table(entry, sections['Application'], ('Registry', 'Source'))
        if len(application) != 1 or not _PY_ID.fullmatch(application[0]['Registry']):
            raise LayoutError(f'{entry}:1: invalid application binding')
        application_binding = (application[0]['Registry'], source(application[0]))
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
                target = resolve_path(roots[replacement.root_id] / replacement.entry)
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
    def dependency(module, row):
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
    if 'Native Dependencies' in sections:
        for row in _shared_table(entry, sections['Native Dependencies'], ('Module', 'Source')):
            dependency(row['Module'], row)
    for row in _shared_table(entry, sections.get('Native Dependency Manifests', []), ('Source',)):
        manifest = linked(row)
        if manifest.suffix != '.toml' or manifest in kinds:
            raise LayoutError(f'{entry}:{row["line"]}: invalid native dependency manifest')
        contents[manifest] = read(manifest)
        kinds[manifest] = 'configuration'
        try:
            declaration = tomllib.loads(contents[manifest].decode('utf-8'))
        except (ValueError, UnicodeError) as error:
            raise LayoutError(f'{manifest}:1: invalid native dependency manifest') from error
        if (set(declaration) != {'version', 'modules'}
                or type(declaration['version']) is not int or declaration['version'] != 1
                or type(declaration['modules']) is not dict
                or any(type(value) is not str for value in declaration['modules'].values())):
            raise LayoutError(f'{manifest}:1: invalid native dependency manifest')
        for module, location in declaration['modules'].items():
            dependency(module, {'Source': f'[Python]({location})', '_owner': manifest, 'line': 1})
    nodes = []
    steps = []
    selected_declarations = {}
    declaration_fragments = {}
    inline_references = {}
    component_edges = {}
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
                        original = resolve_path(owner.parent / decoded)
                    except (OSError, RuntimeError, ValueError) as error:
                        raise LayoutError(f'{owner}:{number}: invalid declaration link') from error
                    if (not any(original.is_relative_to(root) for root in roots.values())
                            or original.suffixes[-2:] != ['.md', '.html']):
                        raise LayoutError(f'{owner}:{number}: declaration link outside roots or wrong format')
                if replacement.root_id not in roots or Path(replacement.entry).is_absolute() or '..' in Path(replacement.entry).parts:
                    raise LayoutError(f'{owner}:{number}: invalid replacement')
                declaration = resolve_path(roots[replacement.root_id] / replacement.entry)
            elif inline is not None:
                declaration = owner
            else:
                if Path(target).is_absolute() or '#' in target or '?' in target:
                    raise LayoutError(f'{owner}:{number}: invalid declaration link')
                declaration = resolve_path(owner.parent / target)
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
            if title != f'# {kind.title()}: {identifier}':
                raise LayoutError(f'{declaration}:1: declaration identity mismatch')
            steps.append((identifier, kind, parent, declaration))
            selected_declarations[identifier] = (owner, number, declaration)
            capture_tools(declaration, kind, identifier, child)
            if kind == 'component':
                if set(child) - {'Tools', 'Edges', 'Notes'} != {'Flow'}:
                    raise LayoutError(f'{declaration}:1: component requires only Flow')
                if 'Edges' in child:
                    component_edges[identifier] = (declaration, child['Edges'])
                visit(declaration, child['Flow'], identifier, depth + 1)
            else:
                if set(child) - {'Tools'} != {'Native'}:
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
    columns = ('From', 'To', 'Router', 'Source')
    declared_edges = [(None, entry, row) for row in _shared_table(entry, sections['Edges'], columns)]
    for owner_id, (declaration, body) in component_edges.items():
        declared_edges.extend((owner_id, declaration, row) for row in _shared_table(declaration, body, columns))
    for owner_id, declaration, row in declared_edges:
        # A component that declares Edges owns every route from its direct children.
        if (row['From'] not in parents or row['To'] not in (*parents, 'END')
                or (row['To'] != 'END' and parents[row['To']] != parents[row['From']])
                or (row['Router'] != '-' and not _ID.fullmatch(row['Router']))
                or (owner_id is not None and parents[row['From']] != owner_id)
                or (owner_id is None and parents[row['From']] in component_edges)):
            raise LayoutError(f'{declaration}:{row["line"]}: invalid edge declaration')
        if row['Router'] == '-' and row['Source'] != '-':
            raise LayoutError(f'{declaration}:{row["line"]}: static edge source must be -')
        edges.append(dict(row, Source=None if row['Router'] == '-' else source(dict(row, _owner=declaration))))
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
    for path, data in contents.items():
        root_id = max((key for key, root in roots.items() if path.is_relative_to(root)),
                      key=lambda key: (len(roots[key].parts), key == entry_root))
        kind = kinds[path]
        resources.append(Resource(kind, root_id + '/' + path.relative_to(roots[root_id]).as_posix(),
                                  data, sha256(data).hexdigest()))
    resources.sort(key=lambda item: item.logical_path)
    import json
    definition_id = sha256(json.dumps({'version': 1, 'graph_id': identity[0]['ID'],
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
    definition = TypedDefinition(identity[0]['ID'], entry, entry_root, MappingProxyType(roots), replacements,
                                    tuple(resources), MappingProxyType(selected_resources), tuple(helpers),
                                    MappingProxyType(dependencies), tuple(_freeze(row) for row in timeouts),
                                    state[0]['Registry'], state_source,
                                    tuple(_freeze(row) for row in nodes), tuple(_freeze(row) for row in edges),
                                    MappingProxyType(contents), definition_id, layout,
                                    MappingProxyType(selected_declarations), MappingProxyType(declaration_fragments), tuple(tool_usages), application_binding)
    _validated_timeouts(definition)
    return definition








def load_native_bindings(definition, *, packaged_proof=None, config=None):
    """Execute only the selected, still admitted native sources."""
    definition.verify_sources()
    timeout_values, timeout_keys = _validated_timeouts(definition, config)
    if recording_enabled():
        _source_manifest(definition, packaged_proof)
    result = {}
    sources = {definition.schema_source, *(row['Source'] for row in definition.nodes),
               *(row['Source'] for row in definition.edges if row['Source'] is not None)}
    if definition.application_binding is not None:
        sources.add(definition.application_binding[1])
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
    bindings = {(path, name): value for path, namespace in result.items()
                for name, value in namespace.items()}
    return NativeCatalog(definition.definition_id, MappingProxyType(result), timeout_values, timeout_keys,
                           bindings=MappingProxyType(bindings))




def diagram_state(definition):
    """Project only admitted structure; diagram generation never imports native code."""
    lines = ['```mermaid', 'flowchart TD']
    for step in definition.layout.steps:
        lines.append(f'  {step.id}[{step.id}]')
    lines.append(f'  START --> {definition.layout.steps[0].id}')
    for edge in definition.edges:
        label = '' if edge['Router'] == '-' else f'|{edge["Router"]}|'
        lines.append(f'  {edge["From"]} -->{label} {edge["To"]}')
    lines.append('```')
    return '\n'.join(lines) + '\n'


__all__ = ['TypedDefinition', 'NativeCatalog',
           '_load_state_definition', 'load_native_bindings', 'diagram_state']
