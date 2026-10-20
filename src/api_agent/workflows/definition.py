"""Versioned workflow declarations with an immutable resource snapshot."""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from urllib.parse import unquote, urlsplit
from weakref import WeakKeyDictionary

from .layout import Layout, LayoutError, Step, _COLUMNS, _ID, _MD, _validate


@dataclass(frozen=True)
class Replacement:
    target_id: str
    entry: Path
    root_id: str


@dataclass(frozen=True)
class Resource:
    kind: str
    logical_path: str
    content_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class InlinePromptRef:
    owner: Path
    id: str

    def as_uri(self):
        return self.owner.as_uri() + '#prompt-' + self.id


@dataclass(frozen=True)
class WorkflowDefinition:
    graph_id: str
    entry: Path
    entry_root: str
    roots: object
    replacements: tuple[Replacement, ...]
    resources: tuple[Resource, ...]
    definition_id: str
    layout: Layout
    contents: object

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


_LINK = re.compile(r'\[([^\]\n]+)\]\(([^()\n]+)\)\Z')
_FLOW = re.compile(r'( *)([1-9][0-9]?)\.( +)(\[([^\]]+)\]\(([^()]+)\))\Z')
_SECTION = re.compile(r'## ([^\n]+)\Z')
_NATIVE = ('Registry', 'Kind', 'Source')
_MAX_STEPS = 4096
_MAX_RESOURCE_BYTES = 8 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
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
                return data
            finally:
                os.close(fd)
        finally:
            os.close(directory)
    except (OSError, ValueError) as error:
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


def _sections(path, data):
    try:
        text = data.decode('utf-8')
    except UnicodeError as error:
        raise LayoutError(f'{path}:1: invalid UTF-8') from error
    lines = text.splitlines()
    if not lines:
        raise LayoutError(f'{path}:1: empty declaration')
    tokens = _MD.parse(text)
    if any(item.type in ('html_block', 'html_inline')
           for token in tokens for item in (token, *(token.children or ()))):
        raise LayoutError(f'{path}:1: HTML is unsupported in workflow declarations')
    headings = []
    for index, token in enumerate(tokens):
        if token.type != 'heading_open':
            continue
        start = token.map[0]
        content = tokens[index + 1]
        if content.type != 'inline' or any(child.type != 'text' for child in content.children or ()):
            raise LayoutError(f'{path}:{start + 1}: heading must be plain text')
        headings.append((start, token.tag, content.content))
    if not headings or headings[0][0] != 0 or headings[0][1] != 'h1':
        raise LayoutError(f'{path}:1: expected version heading')
    title = lines[0]
    sections = {}
    if any(line.strip() for line in lines[1:headings[1][0] if len(headings) > 1 else len(lines)]):
        raise LayoutError(f'{path}:1: content before section')
    for index, (start, tag, heading) in enumerate(headings[1:], 1):
        if tag != 'h2' or not _SECTION.fullmatch(lines[start]):
            raise LayoutError(f'{path}:{start + 1}: unknown heading')
        if heading in sections:
            raise LayoutError(f'{path}:{start + 1}: duplicate section {heading}')
        stop = headings[index + 1][0] if index + 1 < len(headings) else len(lines)
        sections[heading] = [(line + 1, lines[line]) for line in range(start + 1, stop)]
    return title, sections


def _table(path, body, columns):
    nonempty = [(line, value) for line, value in body if value.strip()]
    if not nonempty:
        return []
    if len(nonempty) < 2:
        raise LayoutError(f'{path}:{nonempty[0][0]}: incomplete table')
    tokens = _MD.parse('\n'.join(value for _, value in body))
    if not tokens or tokens[0].type != 'table_open' or tokens[-1].type != 'table_close' or sum(token.type == 'table_open' for token in tokens) != 1:
        raise LayoutError(f'{path}:{nonempty[0][0]}: expected one Markdown table')
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


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def load_definition(entry, *, base_root, pack_root, replacements=(), entry_root=None):
    """Load exactly one graph and all its resources before any native import or call."""
    entry = Path(entry).resolve()
    roots = {'base': Path(base_root).resolve(), 'pack': Path(pack_root).resolve()}
    if not any(entry.is_relative_to(root) for root in roots.values()):
        raise LayoutError(f'{entry}:1: entry outside declared roots')
    if entry_root is None:
        entry_root = ('pack' if entry.is_relative_to(roots['pack']) and
                      entry.parent != roots['base'] and roots['pack'] != roots['base'] else 'base')
    if entry_root not in roots or not entry.is_relative_to(roots[entry_root]):
        raise LayoutError(f'{entry}:1: invalid declared entry root')
    root_map = MappingProxyType(roots)
    captured = {}
    origins = {}
    def capture(path, kind, root_id=None):
        path = Path(path).resolve()
        if not any(path.is_relative_to(root) for root in roots.values()):
            raise LayoutError(f'{path}:1: missing resource or outside declared roots')
        if root_id is None or not path.is_relative_to(roots[root_id]):
            root_id = 'base' if path.is_relative_to(roots['base']) else 'pack'
        if path not in captured:
            data = _read_bounded(path, roots[root_id])
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
    title, root_sections = _sections(entry, captured[entry][1])
    if title != '# Workflow v2':
        raise LayoutError(f'{entry}:1: expected Workflow v2 heading')
    allowed = {'Graph ID', 'Flow', *_COLUMNS, 'Notes'}
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
            declaration = link(owner, f'[{label}]({target})', 'declaration',
                               selected=replacement is None)
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
            title, child_sections = _sections(declaration, captured[declaration][1])
            if title != f'# {kind.title()} v2: {identifier}':
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
    if seen != {item.id for item in resolved if item.id in seen}:
        raise LayoutError(f'{entry}:1: unknown replacement target')
    tables = {name: [] for name in _COLUMNS}
    for name in _COLUMNS:
        tables[name].extend(_table(entry, root_sections.get(name, []), _COLUMNS[name]))
    native = {}
    inline_prompts = {}
    for step in resolved:
        sections = declarations[step.id]
        allowed = {*_COLUMNS, 'Native', 'Notes', 'Flow'}
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
            inline_prompts[prompt_id] = ('\n'.join(raw[1:-1]) + '\n').encode()
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
            source = link(step.source, row['Source'], 'native_source')
            # The existing compiler checks this path against inspect.getsourcefile.
            execution_steps.append(Step(step.id, step.kind, step.parent, source, step.line))
        else:
            execution_steps.append(step)
    for name in ('Functions', 'Prompts', 'Timeouts'):
        for row in tables[name]:
            owner = row.get('_owner', entry)
            for column, kind in ((('Input', 'schema'), ('Output', 'schema')) if name == 'Functions' else
                                 ((('File', 'prompt'),) if name == 'Prompts' else (('Source', 'configuration'),))):
                value = row[column]
                if column == 'File' and value.startswith('@'):
                    prompt_id = value[1:]
                    if prompt_id not in inline_prompts:
                        raise LayoutError(f'{entry}:{row["line"]}: unknown inline prompt')
                    virtual = InlinePromptRef(owner, prompt_id)
                    if virtual not in captured:
                        prompt_bytes = inline_prompts[prompt_id]
                        if sum(len(item[1]) for item in captured.values()) + len(prompt_bytes) > _MAX_SNAPSHOT_BYTES:
                            raise LayoutError(f'{owner}:{row["line"]}: workflow snapshot exceeds 64 MiB')
                        captured[virtual] = ('prompt', prompt_bytes)
                        origins[virtual] = origins[owner]
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
    identity = {'version': 2, 'graph_id': graph_id, 'resources': [(item.kind, item.logical_path, item.sha256) for item in resources],
                'replacements': [(item.target_id, item.root_id, Path(item.entry).as_posix()) for item in replacements]}
    definition_id = sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return WorkflowDefinition(graph_id, entry, entry_root, root_map, replacements, tuple(resources), definition_id,
                              layout, MappingProxyType({path: data for path, (_, data) in captured.items()}))
