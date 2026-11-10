"""Versioned workflow declarations with an immutable resource snapshot."""

from dataclasses import dataclass, replace
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import ClassVar
from urllib.parse import unquote, urlsplit
from weakref import WeakKeyDictionary

from .layout import _COLUMNS, _RESPONSE_COLUMNS, _SUMMARY_COLUMNS, _ID, _MD, _validate
from .model import (InlinePromptRef, Layout, LayoutError, Replacement, Resource, Step, ToolUsage,
                    declares_source, freeze as _freeze, _MAX_SNAPSHOT_BYTES)
from .document_format import extract_document, validate_presentation_location
from api_agent.tooling.operations import FIXED_OPERATIONS, name_problem


@dataclass(frozen=True)
class WorkflowDefinition:
    version: ClassVar[int] = 1
    origin: ClassVar[str] = 'source'
    graph_id: str
    entry: Path
    entry_root: str
    roots: object
    replacements: tuple[Replacement, ...]
    resources: tuple[Resource, ...]
    definition_id: str
    layout: Layout
    contents: object
    selected_declarations: object = None
    declaration_fragments: object = None
    tool_usages: tuple[ToolUsage, ...] = ()
    imports: tuple = ()
    declaration_entry: Path | None = None

    def verify_native_sources(self):
        """Reject a source change before importing its admitted native binding."""
        for step in self.layout.steps:
            if step.kind != 'node':
                continue
            owner = max((root for root in self.roots.values() if step.source.is_relative_to(root)),
                        key=lambda root: len(root.parts))
            current = _read_bounded(step.source, owner)
            if current != self.contents[step.source]:
                raise LayoutError(f'{step.source}:1: native source changed after definition admission')

    def verify_sources(self):
        self.verify_native_sources()

    def native_rejection(self, function, step):
        """Return why ``function`` is not this node's admitted loader-owned binding."""
        if not declares_source(function, step.source):
            return 'registered function does not match its declared source link'
        if native_origin(function) != (self.definition_id, step.source):
            return 'native callable does not belong to the admitted definition'
        return None


_LINK = re.compile(r'\[([^\]\n]+)\]\(([^()\n]+)\)\Z')
_FLOW = re.compile(r'( *)([1-9][0-9]?)\.( +)(\[([^\]]+)\]\(([^()]+)\))\Z')
_SECTION = re.compile(r'## ([^\n]+)\Z')
_NATIVE = ('Registry', 'Kind', 'Source')
_MAX_STEPS = 4096
_MAX_RESOURCE_BYTES = 8 * 1024 * 1024
_NATIVE_ORIGIN = WeakKeyDictionary()


def _read_bounded(path, root):
    """Read a regular file through root-relative no-follow descriptors."""
    try:
        relative = path.relative_to(root)
        if not relative.parts or any(part in ('', '.', '..') for part in relative.parts):
            raise ValueError('invalid root-relative path')
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in root.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError('resource is not a regular file')
                if before.st_size > _MAX_RESOURCE_BYTES:
                    raise ValueError('resource exceeds 8 MiB')
                chunks = []
                remaining = _MAX_RESOURCE_BYTES + 1
                while remaining:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b''.join(chunks)
                after = os.fstat(fd)
                if len(data) > _MAX_RESOURCE_BYTES:
                    raise ValueError('resource exceeds 8 MiB')
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('resource changed during read')
                from api_agent.config.source_reads import record_file
                record_file(path, root, data)
                return data
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except (OSError, ValueError) as error:
        from api_agent.config.source_reads import record_unreadable
        record_unreadable(path, root)
        raise LayoutError(f'{path}:1: unsafe or unavailable resource: {error}') from error


def native_origin(function):
    """Return loader-owned provenance for a callable, if present."""
    try:
        origin = _NATIVE_ORIGIN.get(function)
    except TypeError:
        return None
    if origin is None or origin[2] is None or getattr(function, '__code__', None) is not origin[2]:
        return None
    return origin[:2]


def load_native_catalog(definition: WorkflowDefinition, source: Path, *, reporting_enabled=False,
                        packaged_proof=None):
    """Explicitly execute a trusted selected catalog from its admitted bytes."""
    source = Path(source).resolve()
    if not isinstance(definition, WorkflowDefinition) or source not in {
            step.source for step in definition.layout.steps if step.kind == 'node'}:
        raise LayoutError(f'{source}:1: source is not a selected native binding')
    if os.environ.get('API_AGENT_ALLURE_REPORTING') == '1' or reporting_enabled:
        from .provenance import verified_documents
        verified_documents(definition, packaged_proof=packaged_proof)
    definition.verify_native_sources()
    namespace = {'__name__': '<workflow_catalog>', '__file__': str(source), '__package__': None}
    code = compile(definition.contents[source], str(source), 'exec')
    exec(code, namespace)
    for value in namespace.values():
        if callable(value) and getattr(value, '__module__', None) == namespace['__name__']:
            try:
                _NATIVE_ORIGIN[value] = (definition.definition_id, source,
                                         getattr(value, '__code__', None))
            except TypeError:
                pass
    return namespace


def _declaration_parts(path, data):
    """Parse once per admitted document; results are read-only and keyed by exact bytes."""
    return _cached_declaration_parts(str(path), bytes(data))


@lru_cache(maxsize=256)
def _cached_declaration_parts(path_text, data):
    title, sections, nodes = _parse_declaration_parts(Path(path_text), data)
    def frozen(mapping):
        return MappingProxyType({key: tuple(body) for key, body in mapping.items()})
    return title, frozen(sections), MappingProxyType({key: frozen(body) for key, body in nodes.items()})


def _parse_declaration_parts(path, data):
    """Parse physical headings once, retaining scoped node sections and lines."""
    extracted = extract_document(data, path=path)
    text = extracted.executable_bytes.decode('utf-8')
    lines = text.splitlines()
    line_map = extracted.line_map
    if not lines:
        raise LayoutError(f'{path}:1: empty declaration')
    tokens = _MD.parse(text)
    if any(item.type in ('html_block', 'html_inline')
           for token in tokens for item in (token, *(token.children or ()))):
        raise LayoutError(f'{path}:{line_map[0]}: HTML is unsupported in workflow declarations')
    headings = []
    for index, token in enumerate(tokens):
        if token.type != 'heading_open':
            continue
        start = token.map[0]
        content = tokens[index + 1]
        if content.type != 'inline' or any(child.type != 'text' for child in content.children or ()):
            raise LayoutError(f'{path}:{line_map[start]}: heading must be plain text')
        headings.append((start, token.tag, content.content))
    if not headings or headings[0][0] != 0 or headings[0][1] != 'h1':
        raise LayoutError(f'{path}:{line_map[0]}: expected version heading')
    title = lines[0]
    component = re.fullmatch(r'# Component: [a-z][a-z0-9_]{0,63}', title)
    sections, nodes = {}, {}
    current = None
    if any(line.strip() for line in lines[1:headings[1][0] if len(headings) > 1 else len(lines)]):
        raise LayoutError(f'{path}:{line_map[0]}: content before section')
    for index, (start, tag, heading) in enumerate(headings[1:], 1):
        stop = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
        body = [(line_map[line], lines[line]) for line in range(start + 1, stop)]
        if tag == 'h2' and heading.startswith('Node: '):
            identifier = heading.removeprefix('Node: ')
            if not component or not _ID.fullmatch(identifier) or identifier in nodes:
                raise LayoutError(f'{path}:{line_map[start]}: invalid or duplicate inline node')
            if any(value.strip() for _, value in body):
                raise LayoutError(f'{path}:{line_map[start]}: content before inline node section')
            current = nodes[identifier] = {}
            continue
        if tag == 'h2' and _SECTION.fullmatch(lines[start]):
            current = None
            target = sections
        elif tag == 'h3' and current is not None and lines[start] == '### ' + heading:
            target = current
        else:
            raise LayoutError(f'{path}:{line_map[start]}: unknown heading')
        if heading in target:
            raise LayoutError(f'{path}:{line_map[start]}: duplicate section {heading}')
        target[heading] = body
    return title, sections, nodes


def _sections(path, data):
    title, sections, _ = _declaration_parts(path, data)
    return title, sections


def _prompt_body(path, data, identifier, *, scope=None):
    """Find one physical prompt scope; duplicate virtual identities are unsafe."""
    title, sections, nodes = _declaration_parts(path, data)
    outer = re.fullmatch(r'# (?:Component|Node): ([a-z][a-z0-9_]{0,63})', title)
    scopes = ((outer.group(1) if outer else None, sections), *nodes.items())
    matches = [body['Prompt: ' + identifier] for owner_id, body in scopes
               if (scope is None or owner_id == scope) and 'Prompt: ' + identifier in body]
    return matches[0] if len(matches) == 1 else None


def _inline_selection(path, data, target, kind, identifier, parent, line):
    if not target.startswith('#'):
        return None
    _, _, nodes = _declaration_parts(path, data)
    if (kind != 'node' or parent is None or target != '#node-' + identifier or
            identifier not in nodes):
        raise LayoutError(f'{path}:{line}: missing or foreign inline node fragment')
    return nodes[identifier]


def _tool_operations(path, row, scope_kind):
    if 'Operations' not in row:
        return ()
    operations = tuple(part.strip() for part in row['Operations'].split(','))
    if (scope_kind != 'node' or not operations or len(set(operations)) != len(operations)
            or any(part not in FIXED_OPERATIONS and name_problem(part) for part in operations)):
        raise LayoutError(f'{path}:{row["line"]}: invalid tool operations; declare node operations separated by commas')
    return operations


def _table(path, body, columns):
    nonempty = [(line, value) for line, value in body if value.strip()]
    if not nonempty:
        return []
    if len(nonempty) < 2:
        raise LayoutError(f'{path}:{nonempty[0][0]}: incomplete table')
    tokens = _MD.parse('\n'.join(value for _, value in body))
    if not tokens or tokens[0].type != 'table_open' or tokens[-1].type != 'table_close' or sum(token.type == 'table_open' for token in tokens) != 1:
        raise LayoutError(f'{path}:{nonempty[0][0]}: expected one Markdown table')
    if columns == ('Port', 'Instance'):
        header = tuple(part.strip() for part in nonempty[0][1].strip('|').split('|'))
        if header == ('Port', 'Instance', 'Operations'):
            columns = header
    if columns == _COLUMNS['Functions']:
        header = tuple(part.strip() for part in nonempty[0][1].strip('|').split('|'))
        if header == _RESPONSE_COLUMNS:
            columns = _RESPONSE_COLUMNS
        if header == _SUMMARY_COLUMNS:
            columns = _SUMMARY_COLUMNS
    def cells(item):
        number, value = item
        if not value.startswith('|') or not value.endswith('|'):
            raise LayoutError(f'{path}:{number}: expected fixed-column table')
        result = tuple(part.replace(r'\|', '|').strip() for part in re.split(r'(?<!\\)\|', value[1:-1]))
        if len(result) != len(columns):
            raise LayoutError(f'{path}:{number}: wrong table column count')
        return result
    if cells(nonempty[0]) != columns or any(not re.fullmatch(r':?-{3,}:?', part) for part in cells(nonempty[1])):
        raise LayoutError(f'{path}:{nonempty[0][0]}: unknown table columns')
    return [dict(zip(columns, cells(item)), line=item[0]) for item in nonempty[2:]]


def load_definition(entry, *, base_root, pack_root, replacements=(), entry_root=None):
    """Admit the workflow contract, including optional declared typed state."""
    return _admit_definition(entry, base_root=base_root, pack_root=pack_root,
                             replacements=replacements, entry_root=entry_root)


def _admit_definition(entry, *, base_root, pack_root, replacements=(), entry_root=None, importing=()):
    from api_agent.config.source_reads import resolve_path
    entry = resolve_path(entry)
    roots = tuple(map(resolve_path, (base_root, pack_root)))
    owners = [root for root in roots if entry.is_relative_to(root)]
    if not owners:
        raise LayoutError(f'{entry}:1: declaration outside declared roots')
    owner = max(owners, key=lambda root: len(root.parts))
    entry_root = entry_root or ('pack' if roots[1] != roots[0] and entry.is_relative_to(roots[1]) else 'base')
    named_roots = dict(zip(('base', 'pack'), roots))
    if entry_root not in named_roots or not entry.is_relative_to(named_roots[entry_root]):
        raise LayoutError(f'{entry}:1: invalid declared entry root')
    data = _read_bounded(entry, owner)
    title, sections = _sections(entry, data)
    if title != '# Workflow':
        raise LayoutError(f'{entry}:1: expected Workflow heading')
    if 'Import' in sections:
        return _import_definition(entry, title, sections, base_root=base_root, pack_root=pack_root,
                                  replacements=replacements, entry_root=entry_root, importing=importing, admitted=data)
    if 'State' in sections:
        from .native import _load_state_definition
        return _load_state_definition(entry, base_root=base_root, pack_root=pack_root,
                                      replacements=replacements, entry_root=entry_root, _root_data=data)
    return _load_binding_definition(entry, base_root=base_root, pack_root=pack_root,
                                    replacements=replacements, entry_root=entry_root, _root_data=data)


def _import_definition(entry, title, sections, *, base_root, pack_root, replacements, entry_root, importing, admitted):
    """Expand an explicit root import without retargeting the imported resources."""
    from api_agent.config.source_reads import resolve_path
    roots = {'base': resolve_path(base_root), 'pack': resolve_path(pack_root)}
    if entry in importing or len(importing) >= 64:
        raise LayoutError(f'{entry}:1: workflow import cycle or nesting limit')
    if not {'Graph ID', 'Import'} <= set(sections) or set(sections) - {'Graph ID', 'Import', 'Notes'}:
        raise LayoutError(f'{entry}:1: workflow import cannot overlay graph sections')
    identity = _table(entry, sections['Graph ID'], ('ID',))
    selection = _table(entry, sections['Import'], ('Root', 'Path'))
    if (len(identity) != 1 or not _ID.fullmatch(identity[0]['ID']) or len(selection) != 1
            or selection[0]['Root'] not in roots):
        raise LayoutError(f'{entry}:1: invalid workflow import identity or root')
    row = selection[0]
    uri = urlsplit(row['Path'])
    name = unquote(uri.path)
    target = None
    if (not uri.scheme and not uri.netloc and not uri.query and not uri.fragment
            and name and not Path(name).is_absolute() and '..' not in Path(name).parts
            and '\\' not in name and name.endswith('.md.html')
            and not re.search(r'%(?:2f|5c)', row['Path'], re.I)
            and not any(ord(char) < 32 or ord(char) == 127 for char in name)):
        target = resolve_path(roots[row['Root']] / name)
    if target is None or not target.is_relative_to(roots[row['Root']]):
        raise LayoutError(f'{entry}:1: invalid workflow import source')
    selected = _admit_definition(target, base_root=base_root, pack_root=pack_root,
        replacements=replacements, entry_root=row['Root'], importing=(*importing, entry))
    data = _read_bounded(entry, roots[entry_root])
    if data != admitted:
        raise LayoutError(f'{entry}:1: workflow import changed during admission')
    validate_presentation_location(data, path=entry, root=roots[entry_root])
    contents = {**selected.contents, entry: data}
    if sum(len(value) for value in contents.values()) > _MAX_SNAPSHOT_BYTES:
        raise LayoutError(f'{entry}:1: workflow import snapshot exceeds 64 MiB')
    logical = entry_root + '/' + entry.relative_to(roots[entry_root]).as_posix()
    resources = tuple(sorted((*selected.resources, Resource('declaration', logical, data, sha256(data).hexdigest())),
                             key=lambda item: item.logical_path))
    graph_id = identity[0]['ID']
    import json
    identity = {'version': 1, 'graph_id': graph_id,
        'resources': [(r.kind, r.logical_path, r.sha256) for r in resources],
        'replacements': [(r.target_id, r.root_id, Path(r.entry).as_posix()) for r in selected.replacements]}
    definition_id = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return replace(selected, graph_id=graph_id, entry=entry, entry_root=entry_root,
        roots=MappingProxyType(roots), resources=resources, contents=MappingProxyType(contents),
        definition_id=definition_id, layout=replace(selected.layout, path=entry, files=tuple(sorted(contents, key=str))),
        tool_usages=tuple(replace(use, scope=graph_id) if use.scope_kind == 'graph' else use for use in selected.tool_usages),
        imports=((target, selected.graph_id), *selected.imports),
        declaration_entry=selected.declaration_entry or target)


def _load_binding_definition(entry, *, base_root, pack_root, replacements, entry_root, _root_data):
    """Load exactly one graph and all its resources before any native import or call."""
    from api_agent.config.source_reads import resolve_path
    entry = resolve_path(entry)
    roots = {'base': resolve_path(base_root), 'pack': resolve_path(pack_root)}
    root_map = MappingProxyType(roots)
    captured = {}
    origins = {}
    def capture(path, kind, root_id=None):
        path = (Path(os.path.normpath(path)) if kind == 'tool_instance' else resolve_path(path))
        if not any(path.is_relative_to(root) for root in roots.values()):
            raise LayoutError(f'{path}:1: missing resource or outside declared roots')
        if root_id is None or not path.is_relative_to(roots[root_id]):
            root_id = 'base' if path.is_relative_to(roots['base']) else 'pack'
        if (path in captured and 'tool_instance' in (kind, captured[path][0])
                and kind != captured[path][0]):
            raise LayoutError(f'{path}:1: tool instance has conflicting resource kind')
        if path not in captured:
            data = _root_data if path == entry else _read_bounded(path, roots[root_id])
            if kind == 'declaration':
                validate_presentation_location(data, path=path, root=roots[root_id])
            if sum(len(item[1]) for item in captured.values()) + len(data) > _MAX_SNAPSHOT_BYTES:
                raise LayoutError(f'{path}:1: workflow snapshot exceeds 64 MiB')
            captured[path] = (kind, data)
            origins[path] = root_id
        return path
    def link(owner, value, kind, *, selected=True):
        match = _LINK.fullmatch(value)
        if match is None:
            raise LayoutError(f'{owner}:1: expected one inline file link')
        encoded = match.group(2).replace(r'\<resource_pack\>', '<resource_pack>')
        parsed = urlsplit(encoded)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or re.search(r'%(?:2f|5c)', encoded, re.I):
            raise LayoutError(f'{owner}:1: invalid resource link')
        try:
            name = unquote(parsed.path, errors='strict')
        except UnicodeError as error:
            raise LayoutError(f'{owner}:1: invalid encoded resource link') from error
        if not name or name.startswith('/') or '\\' in name or '?' in name or '#' in name:
            raise LayoutError(f'{owner}:1: invalid resource link')
        if name.startswith('<resource_pack>/'):
            path = roots['pack'] / name.removeprefix('<resource_pack>/')
            root_id = 'pack'
        else:
            path = owner.parent / name
            root_id = origins[owner]
        if not selected:
            path = Path(os.path.normpath(path))
            if not any(path.is_relative_to(root) for root in roots.values()):
                raise LayoutError(f'{owner}:1: resource link outside declared roots')
            return path
        return capture(path, kind, root_id)
    capture(entry, 'declaration', entry_root)
    _, root_sections = _sections(entry, captured[entry][1])
    allowed = {'Graph ID', 'Flow', 'Tools', *_COLUMNS, 'Notes'}
    if set(root_sections) - allowed or 'Graph ID' not in root_sections or 'Flow' not in root_sections:
        raise LayoutError(f'{entry}:1: unknown or missing root section')
    identity = _table(entry, root_sections['Graph ID'], ('ID',))
    if len(identity) != 1 or not _ID.fullmatch(identity[0]['ID']):
        raise LayoutError(f'{entry}:1: Graph ID requires one stable ID')
    graph_id = identity[0]['ID']
    replacements = tuple(replacements)
    seen = set()
    for replacement in replacements:
        if not isinstance(replacement, Replacement) or replacement.target_id in seen or replacement.root_id not in roots:
            raise LayoutError(f'{entry}:1: invalid or duplicate replacement')
        seen.add(replacement.target_id)
    resolved = []
    declarations = {}
    selected_declarations = {}
    declaration_fragments = {}
    inline_references = {}
    def visit_flow(owner, section, parent=None, depth=0):
        if depth > 64:
            raise LayoutError(f'{owner}:1: component nesting exceeds 64 levels')
        count = 0
        for number, line in section:
            if not line.strip():
                continue
            match = _FLOW.fullmatch(line)
            if match is None or match.group(1):
                raise LayoutError(f'{owner}:{number}: Flow requires direct ordered linked steps')
            _, index, spacing, _, label, target = match.groups()
            count += 1
            if len(index) + 1 + len(spacing) != 4 or int(index) != count:
                raise LayoutError(f'{owner}:{number}: invalid four-column step sequence')
            parts = label.split(' ')
            if len(parts) != 2 or parts[0] not in ('component', 'node') or not _ID.fullmatch(parts[1]):
                raise LayoutError(f'{owner}:{number}: invalid step label')
            kind, identifier = parts
            if len(resolved) >= _MAX_STEPS:
                raise LayoutError(f'{owner}:{number}: workflow exceeds {_MAX_STEPS} steps')
            if any(item.id == identifier for item in resolved):
                raise LayoutError(f'{owner}:{number}: duplicate step ID')
            replacement = next((item for item in replacements if item.target_id == identifier), None)
            inline = _inline_selection(owner, captured[owner][1], target, kind, identifier, parent, number)
            if inline is not None:
                inline_references.setdefault(owner, set()).add(identifier)
            declaration = owner if inline is not None else link(
                owner, f'[{label}]({target})', 'declaration', selected=replacement is None)
            if declaration.suffixes[-2:] != ['.md', '.html']:
                raise LayoutError(f'{owner}:{number}: child declaration must end .md.html')
            if replacement:
                proposed = Path(replacement.entry)
                if proposed.is_absolute() or '..' in proposed.parts:
                    raise LayoutError(f'{entry}:1: replacement must be root-relative')
                if proposed.suffixes[-2:] != ['.md', '.html']:
                    raise LayoutError(f'{entry}:1: replacement declaration must end .md.html')
                declaration = capture(roots[replacement.root_id] / proposed, 'declaration', replacement.root_id)
                if declaration.suffixes[-2:] != ['.md', '.html']:
                    raise LayoutError(f'{entry}:1: replacement declaration must end .md.html')
            step = Step(identifier, kind, parent, declaration, number)
            resolved.append(step)
            selected_declarations[identifier] = (owner, number, declaration)
            if inline is not None and replacement is None:
                title, child_sections = f'# Node: {identifier}', inline
                declaration_fragments[identifier] = 'node-' + identifier
            else:
                title, child_sections = _sections(declaration, captured[declaration][1])
            if title != f'# {kind.title()}: {identifier}':
                raise LayoutError(f'{declaration}:1: declaration identity mismatch')
            declarations[identifier] = child_sections
            if kind == 'component':
                if 'Flow' not in child_sections:
                    raise LayoutError(f'{declaration}:1: component requires Flow')
                visit_flow(declaration, child_sections['Flow'], identifier, depth + 1)
            elif 'Flow' in child_sections:
                raise LayoutError(f'{declaration}:1: node cannot own Flow')
        if count == 0:
            raise LayoutError(f'{owner}:1: Flow requires steps')
    visit_flow(entry, root_sections['Flow'])
    # Resource rows are captured below; every physical snapshot here is a declaration.
    for physical, (_, data) in captured.items():
        _, _, nodes = _declaration_parts(physical, data)
        if set(nodes) != inline_references.get(physical, set()):
            raise LayoutError(f'{physical}:1: unreferenced inline node declaration')
    if seen != {item.id for item in resolved if item.id in seen}:
        raise LayoutError(f'{entry}:1: unknown replacement target')
    tables = {name: [] for name in _COLUMNS}
    for name in _COLUMNS:
        tables[name].extend(_table(entry, root_sections.get(name, []), _COLUMNS[name]))
    native = {}
    inline_prompts = {}
    for step in resolved:
        sections = declarations[step.id]
        allowed = {*_COLUMNS, 'Native', 'Notes', 'Flow', 'Tools'}
        if set(sections) - allowed - {key for key in sections if key.startswith('Prompt: ')}:
            raise LayoutError(f'{step.source}:1: unknown declaration section')
        if step.kind == 'node':
            bindings = _table(step.source, sections.get('Native', []), _NATIVE)
            if len(bindings) != 1:
                raise LayoutError(f'{step.source}:1: node requires one Native binding')
            native[step.id] = bindings[0]
        elif 'Native' in sections:
            raise LayoutError(f'{step.source}:1: component cannot bind native source')
        for name in _COLUMNS:
            rows = _table(step.source, sections.get(name, []), _COLUMNS[name])
            for row in rows:
                row['_owner'] = step.source
                row['_owner_id'] = step.id
            tables[name].extend(rows)
        for name, body in sections.items():
            if not name.startswith('Prompt: '):
                continue
            prompt_id = name.removeprefix('Prompt: ')
            raw = [value for _, value in body]
            while raw and not raw[0].strip():
                raw.pop(0)
            while raw and not raw[-1].strip():
                raw.pop()
            if not _ID.fullmatch(prompt_id) or len(raw) < 2 or raw[0] != '```text' or raw[-1] != '```' or prompt_id in inline_prompts:
                raise LayoutError(f'{step.source}:1: invalid inline prompt')
            inline_prompts[prompt_id] = (step.id, ('\n'.join(raw[1:-1]) + '\n').encode())
    tool_usages = []
    scopes = [('graph', graph_id, entry, root_sections)]
    scopes.extend((step.kind, step.id, step.source, declarations[step.id]) for step in resolved)
    for scope_kind, scope, owner, sections in scopes:
        ports = set()
        for row in _table(owner, sections.get('Tools', []), ('Port', 'Instance')):
            port = row['Port']
            if not _ID.fullmatch(port) or port in ports:
                raise LayoutError(f'{owner}:{row["line"]}: invalid or duplicate tool port')
            ports.add(port)
            instance = link(owner, row['Instance'], 'tool_instance')
            if instance.suffix != '.toml':
                raise LayoutError(f'{owner}:{row["line"]}: tool instance must be TOML')
            tool_usages.append(ToolUsage(scope, port, instance, scope_kind, owner, row['line'], _tool_operations(owner, row, scope_kind)))
    execution_steps = []
    for step in resolved:
        if step.kind == 'node':
            row = native[step.id]
            matching = [item for item in tables['Functions'] if item['ID'] == step.id]
            if len(matching) != 1 or matching[0]['Registry'] != row['Registry']:
                raise LayoutError(f'{step.source}:1: Native registry and Functions must match')
            if row['Kind'] not in ('llm', 'deterministic'):
                raise LayoutError(f'{step.source}:1: unsupported native callable kind')
            matching[0]['Kind'] = row['Kind']
            if ('Output mode' in matching[0]
                    and (row['Kind'] == 'deterministic') != (matching[0]['Output mode'] == '-')):
                raise LayoutError(f'{step.source}:{matching[0]["line"]}: response declaration must match native node kind')
            source = link(step.source, row['Source'], 'native_source')
            # The existing compiler checks this path against inspect.getsourcefile.
            execution_steps.append(Step(step.id, step.kind, step.parent, source, step.line))
        else:
            execution_steps.append(step)
    parents = {step.id: step.parent for step in resolved}
    def belongs_to(scope, identifier):
        while identifier is not None:
            if identifier == scope:
                return True
            identifier = parents.get(identifier)
        return False
    for row in tables['Components']:
        value = row['Max passes']
        if value.startswith('['):
            match = _LINK.fullmatch(value)
            if match is None:
                raise LayoutError(f'{row.get("_owner", entry)}:{row["line"]}: invalid Max passes link')
            row['max_passes_key'] = match.group(1)
            row['max_passes_link'] = link(row.get('_owner', entry), value, 'configuration')
    for name in ('Functions', 'Prompts', 'Timeouts'):
        for row in tables[name]:
            owner = row.get('_owner', entry)
            for column, kind in ((('Input', 'schema'), ('Output', 'schema'),
                                  *((('Response schema', 'schema'),) if row.get('Response schema', '-') != '-' else ())) if name == 'Functions' else
                                 ((('File', 'prompt'),) if name == 'Prompts' else (('Source', 'configuration'),))):
                value = row[column]
                if column == 'File' and value.startswith('@'):
                    prompt_id = value[1:]
                    if prompt_id not in inline_prompts:
                        raise LayoutError(f'{entry}:{row["line"]}: unknown inline prompt')
                    prompt_owner, prompt_bytes = inline_prompts[prompt_id]
                    row_scope = row.get('_owner_id')
                    if ((row_scope is not None and not belongs_to(row_scope, row['Node']))
                            or prompt_owner != (row_scope or row['Node'])):
                        raise LayoutError(f'{owner}:{row["line"]}: foreign inline prompt scope')
                    prompt_document = selected_declarations[prompt_owner][2]
                    virtual = InlinePromptRef(prompt_document, prompt_id)
                    if virtual not in captured:
                        if sum(len(item[1]) for item in captured.values()) + len(prompt_bytes) > _MAX_SNAPSHOT_BYTES:
                            raise LayoutError(f'{prompt_document}:{row["line"]}: workflow snapshot exceeds 64 MiB')
                        captured[virtual] = ('prompt', prompt_bytes)
                        origins[virtual] = origins[prompt_document]
                    row[column] = virtual
                else:
                    row[column] = link(owner, value, kind)
    layout = Layout(entry, tuple(execution_steps), tables, tuple(sorted(captured, key=str)))
    _validate(layout, {path: data for path, (_, data) in captured.items()})
    layout = Layout(entry, layout.steps, _freeze(tables), layout.files)
    resources = []
    for path, (kind, data) in captured.items():
        if isinstance(path, InlinePromptRef):
            logical = f'inline/{origins[path]}/{path.id}'
        else:
            root_id = origins[path]
            logical = root_id + '/' + path.relative_to(roots[root_id]).as_posix()
        resources.append(Resource(kind, logical, data, sha256(data).hexdigest()))
    resources.sort(key=lambda item: item.logical_path)
    for path, (kind, admitted) in captured.items():
        if isinstance(path, InlinePromptRef):
            continue
        try:
            current = _read_bounded(path, roots[origins[path]])
        except LayoutError as error:
            raise LayoutError(f'{path}:1: resource changed during definition admission') from error
        if current != admitted:
            raise LayoutError(f'{path}:1: resource changed during definition admission')
    identity = {'version': 1, 'graph_id': graph_id, 'resources': [(item.kind, item.logical_path, item.sha256) for item in resources],
                'replacements': [(item.target_id, item.root_id, Path(item.entry).as_posix()) for item in replacements]}
    definition_id = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return WorkflowDefinition(graph_id, entry, entry_root, root_map, replacements, tuple(resources), definition_id,
                              layout, MappingProxyType({path: data for path, (_, data) in captured.items()}),
                              MappingProxyType(selected_declarations), MappingProxyType(declaration_fragments), tuple(tool_usages))
