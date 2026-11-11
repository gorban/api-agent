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


from .configuration import resolve_state_limits as _validated_limits
from .configuration import resolve_state_timeouts as _validated_timeouts
from .execution import _source_manifest
from .predicates import PredicateError, condition_constants, parse_predicate
from .prompt_slots import check_prompt_file
from .model import (NativeCatalog, Layout, LayoutError, Replacement, Resource, Step, ToolUsage, freeze as _freeze,
                    _ID, _PY_ID, _MAX_SNAPSHOT_BYTES)


def _read_bounded(path, root):
    # The bounded reader belongs to the source loader; production never reads sources.
    from .definition import _read_bounded as read
    return read(path, root)


_LINK = re.compile(r'\[[^\]\n]+\]\(([^()\n]+)\)\Z')
_SLOT = re.compile(r'\{\{([^{}\n]{1,64})\}\}')
_LIMIT_KEY = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')
_TIMEOUT_COLUMNS = ('Owner', 'Source', 'Key')
_EXPIRY_COLUMNS = (*_TIMEOUT_COLUMNS, 'Expired', 'Expirations')
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
    prompts: tuple = ()
    prompt_slots: tuple = ()
    functions: tuple = ()
    limits: tuple = ()

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
    from .definition import (_application_row, _declaration_parts, _inline_selection, _sections,
                             _table as _shared_table, _tool_operations)
    from .document_format import validate_presentation_location
    from .layout import validate_function_rows
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
    if not required <= set(sections) or set(sections) - required - {'Native Sources', 'Native Dependencies', 'Native Dependency Manifests', 'Resources', 'Timeouts', 'Limits', 'Tools', 'Application', 'Prompt Slots'}:
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
        application = _application_row(entry, sections['Application'])
        application_binding = (application['Registry'], source(application))
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
    scoped_tables = []
    used_constants = []
    prompts = []
    functions = []
    def capture_functions(declaration, identifier, binding, body):
        # Same Functions columns, schema links and checks as the binding grammar.
        rows = _shared_table(declaration, body, ('ID', 'Registry', 'Input', 'Output'))
        if len(rows) != 1 or rows[0]['ID'] != identifier or rows[0]['Registry'] != binding['Registry']:
            raise LayoutError(f'{declaration}:1: Native registry and Functions must match')
        row = dict(rows[0], Kind=binding['Kind'], _owner=declaration, _owner_id=identifier)
        if 'Output mode' in row and (binding['Kind'] == 'deterministic') != (row['Output mode'] == '-'):
            raise LayoutError(f'{declaration}:{row["line"]}: response declaration must match native node kind')
        for column in ('Input', 'Output', 'Response schema'):
            if row.get(column, '-') == '-':
                continue
            target = linked(dict(row, Source=row[column]))
            if target not in contents:
                contents[target] = read(target)
            if kinds.get(target, 'schema') != 'schema':
                raise LayoutError(f'{declaration}:{row["line"]}: schema has conflicting resource kind')
            kinds[target] = 'schema'
            row[column] = target
        def reject(message, line):
            raise LayoutError(f'{declaration}:{line}: {message}')
        validate_function_rows([row], contents, reject)
        functions.append(row)
    def capture_prompts(declaration, identifier, sections):
        inline = {}
        for name, body in sections.items():
            if not name.startswith('Prompt: '):
                continue
            prompt_id, raw = name.removeprefix('Prompt: '), [value for _, value in body]
            while raw and not raw[0].strip():
                raw.pop(0)
            while raw and not raw[-1].strip():
                raw.pop()
            if not _ID.fullmatch(prompt_id) or len(raw) < 2 or raw[0] != '```text' or raw[-1] != '```':
                raise LayoutError(f'{declaration}:1: invalid inline prompt')
            # Typed prompt bytes are exactly the fenced lines; blank edge lines keep edge newlines.
            inline[prompt_id] = '\n'.join(raw[1:-1])
        rows = _shared_table(declaration, sections.get('Prompts', []), ('Node', 'File', 'When'))
        selected = []
        for row in rows:
            if row['Node'] != identifier or row['When'] == 'otherwise':
                raise LayoutError(f'{declaration}:{row["line"]}: invalid prompt declaration')
            if row['When'] != 'always':
                try:
                    used_constants.append((declaration, row['line'],
                                           condition_constants(parse_predicate(row['When'], None))))
                except PredicateError as error:
                    raise LayoutError(f'{declaration}:{row["line"]}: {error}') from None
            if row['File'].startswith('@'):
                prompt_id = row['File'][1:]
                if prompt_id not in inline:
                    raise LayoutError(f'{declaration}:{row["line"]}: unknown inline prompt')
                text = inline[prompt_id]
            else:
                path = linked(dict(row, Source=row['File'], _owner=declaration))
                declared = linked(dict(row, Source=row['File'], _owner=declaration), resolve=False)
                check_prompt_file(f'{declaration}:{row["line"]}', path, max(
                    (root for root in roots.values() if declared.is_relative_to(root)),
                    key=lambda root: len(root.parts)), text_only=True)
                if path not in contents:
                    contents[path] = read(path)
                if kinds.get(path, 'prompt') != 'prompt':
                    raise LayoutError(f'{declaration}:{row["line"]}: prompt has conflicting resource kind')
                kinds[path] = 'prompt'
                prompt_id, text = path.stem, contents[path].decode('utf-8')
            if prompt_id in selected:
                raise LayoutError(f'{declaration}:{row["line"]}: duplicate node prompt')
            selected.append(prompt_id)
            prompts.append(dict(Node=identifier, Prompt=prompt_id, When=row['When'], Text=text, line=row['line']))
        if set(inline) - {row['File'][1:] for row in rows if row['File'].startswith('@')}:
            raise LayoutError(f'{declaration}:1: unreferenced inline prompt')
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
                if set(child) - {'Tools', 'Edges', 'Notes', 'Timeouts', 'Limits'} != {'Flow'}:
                    raise LayoutError(f'{declaration}:1: component requires only Flow')
                scoped_tables.append((identifier, declaration, child))
                if 'Edges' in child:
                    component_edges[identifier] = (declaration, child['Edges'])
                visit(declaration, child['Flow'], identifier, depth + 1)
            else:
                if set(child) - {'Tools', 'Prompts', 'Functions'} - {key for key in child if key.startswith('Prompt: ')} != {'Native'}:
                    raise LayoutError(f'{declaration}:1: node requires only Native')
                capture_prompts(declaration, identifier, child)
                binding = _shared_table(declaration, child['Native'], ('Registry', 'Kind', 'Source'))
                if len(binding) != 1 or not _ID.fullmatch(binding[0]['Registry']) or binding[0]['Kind'] not in ('deterministic', 'llm'):
                    raise LayoutError(f'{declaration}:1: invalid native binding')
                if 'Functions' in child:
                    capture_functions(declaration, identifier, binding[0], child['Functions'])
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
    routed_columns, condition_columns = ('From', 'To', 'Router', 'Source'), ('From', 'To', 'When')
    def edge_table(path, body):
        header = next((value for _, value in body if value.strip()), '')
        conditional = tuple(part.strip() for part in header.strip().strip('|').split('|')) == condition_columns
        return [dict(row, Router='-', Source='-') if conditional else row
                for row in _shared_table(path, body, condition_columns if conditional else routed_columns)]
    declared_edges = [(None, entry, row) for row in edge_table(entry, sections['Edges'])]
    for owner_id, (declaration, body) in component_edges.items():
        declared_edges.extend((owner_id, declaration, row) for row in edge_table(declaration, body))
    for owner_id, declaration, row in declared_edges:
        # A component that declares Edges owns every route from its direct children,
        # including conditional entry from its START.
        origin = owner_id if row['From'] == 'START' else parents.get(row['From'], ())
        if (origin == () or row['To'] not in (*parents, 'END')
                or (row['From'] == 'START' and (owner_id is None or row['To'] == 'END' or 'When' not in row))
                or (row['To'] != 'END' and parents[row['To']] != origin)
                or (row['Router'] != '-' and not _ID.fullmatch(row['Router']))
                or (owner_id is not None and origin != owner_id)
                or (owner_id is None and origin in component_edges)):
            raise LayoutError(f'{declaration}:{row["line"]}: invalid edge declaration')
        if row['Router'] == '-' and row['Source'] != '-':
            raise LayoutError(f'{declaration}:{row["line"]}: static edge source must be -')
        if 'When' in row and row['When'] not in ('always', 'otherwise'):
            try:
                used_constants.append((declaration, row['line'],
                                       condition_constants(parse_predicate(row['When'], None))))
            except PredicateError as error:
                raise LayoutError(f'{declaration}:{row["line"]}: {error}') from None
        edges.append(dict(row, Source=None if row['Router'] == '-' else source(dict(row, _owner=declaration))))
    def edge_set(outgoing):
        whens = [row['When'] for row in outgoing if 'When' in row]
        if whens and (len(whens) != len(outgoing) or len(set(whens)) != len(whens)
                      or ('always' in whens and len(whens) > 1)
                      or ('always' not in whens and whens.count('otherwise') != 1)):
            raise LayoutError(f'{entry}:{outgoing[-1]["line"]}: conditional edges require one otherwise fallback; always must stand alone')
        if not whens and len({row['To'] for row in outgoing}) != len(outgoing):
            raise LayoutError(f'{entry}:1: duplicate or mixed route source')
    for name in parents:
        outgoing = [row for row in edges if row['From'] == name]
        if not outgoing or (len(outgoing) > 1 and any(row['Router'] == '-' and 'When' not in row for row in outgoing)) or len({row['Router'] for row in outgoing}) != 1:
            raise LayoutError(f'{entry}:1: each node needs one static or one declared routed edge set')
        if len({row['Source'] for row in outgoing}) != 1:
            raise LayoutError(f'{entry}:1: duplicate or mixed route source')
        edge_set(outgoing)
    for owner_id in component_edges:
        entering = [row for row in edges if row['From'] == 'START' and parents[row['To']] == owner_id]
        if entering:
            edge_set(entering)
    timeouts, limits = [], []
    def configuration(declaration, row, noun):
        path = linked(dict(row, _owner=declaration))
        if path.suffix != '.toml':
            raise LayoutError(f'{declaration}:{row["line"]}: {noun} source must be TOML')
        if path not in contents:
            contents[path] = read(path)
        if path in kinds and kinds[path] != 'configuration':
            raise LayoutError(f'{declaration}:{row["line"]}: {noun} source has conflicting kind')
        kinds[path] = 'configuration'
        return path
    def capture_timeouts(declaration, owner_id, body):
        header = next((value for _, value in body if value.strip()), '')
        expiring = tuple(part.strip() for part in header.strip().strip('|').split('|')) == _EXPIRY_COLUMNS
        # The graph names any owner; a component names itself or a direct child.
        owners = ({'GRAPH', *parents} if owner_id is None else
                  {owner_id, *(name for name, parent in parents.items() if parent == owner_id)})
        for row in _shared_table(declaration, body, _EXPIRY_COLUMNS if expiring else _TIMEOUT_COLUMNS):
            if (row['Owner'] not in owners or row['Owner'] in {item['Owner'] for item in timeouts}
                    or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', row['Key'])):
                raise LayoutError(f'{declaration}:{row["line"]}: invalid timeout owner or key')
            expiry = {}
            expired, expirations = row.pop('Expired', '-'), row.pop('Expirations', '-')
            if (expired == '-') != (expirations == '-'):
                raise LayoutError(f'{declaration}:{row["line"]}: expiry columns must both name State fields or both be -')
            if expired != '-':
                if row['Owner'] == 'GRAPH':
                    raise LayoutError(f'{declaration}:{row["line"]}: expiry routing requires a node or component owner')
                if not (_PY_ID.fullmatch(expired) and _PY_ID.fullmatch(expirations)):
                    raise LayoutError(f'{declaration}:{row["line"]}: invalid expiry field name')
                if expired == expirations:
                    raise LayoutError(f'{declaration}:{row["line"]}: expiry fields must differ')
                expiry = dict(Expired=expired, Expirations=expirations)
            path = configuration(declaration, row, 'timeout')
            timeouts.append(dict(row, Source=path, **expiry, **({} if owner_id is None else {'_owner': declaration})))
    def capture_limits(declaration, owner_id, body):
        for row in _shared_table(declaration, body, ('Key', 'Source')):
            if not _LIMIT_KEY.fullmatch(row['Key']):
                raise LayoutError(f'{declaration}:{row["line"]}: invalid limit key')
            if row['Key'] in {item['Key'] for item in limits}:
                raise LayoutError(f'{declaration}:{row["line"]}: duplicate limit key')
            limits.append(dict(row, Source=configuration(declaration, row, 'limit'),
                               **({} if owner_id is None else {'_owner': declaration, '_owner_id': owner_id})))
    for table, capture in (('Timeouts', capture_timeouts), ('Limits', capture_limits)):
        if table in sections:
            capture(entry, None, sections[table])
        for owner_id, declaration, child in scoped_tables:
            if table in child:
                capture(declaration, owner_id, child[table])
    for declaration, line, names in used_constants:
        undeclared = sorted(names - {item['Key'] for item in limits})
        if undeclared:
            raise LayoutError(f'{declaration}:{line}: condition uses undeclared constant {undeclared[0]}')
    slots = []
    for row in _shared_table(entry, sections.get('Prompt Slots', []), ('Slot', 'Registry', 'Source')):
        if (not _ID.fullmatch(row['Slot']) or not _PY_ID.fullmatch(row['Registry'])
                or row['Slot'] in {item['Slot'] for item in slots}):
            raise LayoutError(f'{entry}:{row["line"]}: invalid or duplicate prompt slot')
        slots.append(dict(row, Source=source(row)))
    for row in prompts:
        if set(_SLOT.findall(row['Text'])) - {item['Slot'] for item in slots}:
            raise LayoutError(f'{entry}:{row["line"]}: prompt uses an undeclared slot')
    # Admit the static local import closure; tables and manifests only add modules
    # that no admitted source imports statically.
    pending = [state_source, *helpers, *dependencies.values(), *(row['Source'] for row in nodes),
               *(row['Source'] for row in edges if row['Source'] is not None),
               *(row['Source'] for row in slots),
               *((application_binding[1],) if application_binding is not None else ())]
    scanned, resolved = set(), {}
    while pending:
        path = pending.pop()
        if path in scanned:
            continue
        scanned.add(path)
        for module, local in _local_imports(path, contents[path], roots, resolved):
            if module == 'api_agent' or module.startswith('api_agent.'):
                continue
            if module in dependencies:
                if dependencies[module] != local:
                    raise LayoutError(f'{path}:1: native dependency {module} resolves to another source')
                continue
            if local not in contents:
                contents[local] = read(local)
            kinds[local] = 'native_dependency'
            dependencies[module] = local
            pending.append(local)
    dependencies = dict(sorted(dependencies.items()))
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
    layout_edges = tuple(_freeze(dict(row, When=row.get('When', 'always' if row['Router'] == '-' else row['Router'])))
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
                                    MappingProxyType(selected_declarations), MappingProxyType(declaration_fragments), tuple(tool_usages), application_binding,
                                    prompts=tuple(_freeze(row) for row in prompts), prompt_slots=tuple(_freeze(row) for row in slots),
                                    functions=tuple(_freeze(row) for row in functions),
                                    limits=tuple(_freeze(row) for row in limits))
    _validated_timeouts(definition)
    _validated_limits(definition)
    return definition









def _module_for_path(path, roots):
    base = max((base for root in roots.values() for base in (root, root / 'src')
                if path.is_relative_to(base)), key=lambda item: len(item.parts))
    relative = path.relative_to(base).with_suffix('')
    parts = relative.parts[:-1] if relative.name == '__init__' else relative.parts
    return '.'.join(parts)


def _local_imports(path, data, roots, resolved=None):
    """Yield (module, file) for each repository-local module a native source imports.

    ``resolved`` memoizes module lookups for one admission or catalog load.
    """
    resolved = {} if resolved is None else resolved
    def local_module_path(module):
        if module not in resolved:
            parts = Path(*module.split('.'))
            resolved[module] = next((candidate.resolve() for root in roots.values() for base in (root, root / 'src')
                                     for candidate in ((base / parts).with_suffix('.py'), base / parts / '__init__.py')
                                     if candidate.is_file()), None)
        return resolved[module]
    trusted_host = Path(__file__).resolve().parents[1]
    tree = ast.parse(data, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                owner = _module_for_path(path, roots)
                package = owner if path.name == '__init__.py' else owner.rpartition('.')[0]
                prefix = package.split('.') if package else []
                if node.level > len(prefix):
                    raise LayoutError(f'{path}:1: relative native import leaves package')
                prefix = prefix[:len(prefix) - node.level + 1]
                base_name = '.'.join((*prefix, node.module or '')) if node.module else '.'.join(prefix)
            else:
                base_name = node.module or ''
            names = [base_name, *(base_name + '.' + alias.name for alias in node.names if alias.name != '*')]
        else:
            continue
        for name in names:
            parts = name.split('.')
            for length in range(1, len(parts) + 1):
                module = '.'.join(parts[:length])
                local = local_module_path(module)
                host_runtime = (module == 'api_agent' or module.startswith('api_agent.')) and (
                    local is not None and local.is_relative_to(trusted_host))
                if local is not None and not host_runtime:
                    yield module, local


def load_native_bindings(definition, *, packaged_proof=None, config=None):
    """Execute only the selected, still admitted native sources."""
    definition.verify_sources()
    timeout_values, timeout_keys = _validated_timeouts(definition, config)
    _validated_limits(definition, config)
    if recording_enabled():
        _source_manifest(definition, packaged_proof)
    result = {}
    sources = {definition.schema_source, *(row['Source'] for row in definition.nodes),
               *(row['Source'] for row in definition.edges if row['Source'] is not None),
               *(row['Source'] for row in getattr(definition, 'prompt_slots', ()))}
    if definition.application_binding is not None:
        sources.add(definition.application_binding[1])
    resolved = {}
    for path in (*definition.helper_sources, *sources, *definition.native_dependencies.values()):
        for module, local in _local_imports(path, definition.contents[path], definition.roots, resolved):
            if definition.native_dependencies.get(module) != local:
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
                module = _module_for_path(path, definition.roots)
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
