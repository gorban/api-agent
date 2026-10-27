"""A restricted structural Markdown grammar with source-located diagnostics."""
from dataclasses import dataclass
import json
import tomllib
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from markdown_it import MarkdownIt
from referencing.jsonschema import DRAFT202012
from .predicates import PredicateError, parse_predicate, predicate_fields


MAX_COMPONENT_PASSES = 99


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


_COLUMNS = {
    'Components': ('ID', 'Max passes', 'Guard'),
    'Functions': ('ID', 'Registry', 'Input', 'Output'),
    'Bindings': ('Node', 'Input', 'From', 'Field', 'Default'),
    'Edges': ('From', 'To', 'When'),
    'Prompts': ('Node', 'File', 'When'),
    'Timeouts': ('Owner', 'Source', 'Key'),
}
_RESPONSE_COLUMNS = (*_COLUMNS['Functions'], 'Response schema', 'Output mode', 'Fallback')

# Notes cannot supply reference definitions to executable sections.
_MD = MarkdownIt('commonmark').enable('table').disable('reference')
_ID = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')


def _plain(token):
    children = token.children or []
    if any(child.type not in ('text', 'code_inline') for child in children):
        raise ValueError('expected an identifier or plain cell')
    return ''.join(child.content for child in children)


def _link(token, path, pack_root, roots, *, prompt=False):
    children = token.children or []
    if [child.type for child in children] != ['link_open', 'text', 'link_close'] or children[0].markup == 'autolink':
        raise ValueError('expected one inline file link')
    destination = unquote(children[0].attrGet('href'), errors='strict')
    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or parsed.query or (prompt and parsed.fragment):
        raise ValueError('expected a relative local file link')
    name = parsed.path
    if '<resource_pack>' in name:
        if not name.startswith('<resource_pack>/') or (prompt and '<resource_pack>' not in children[1].content):
            raise ValueError('resource_pack must be a leading root token and visible in the label')
        source = pack_root / name.removeprefix('<resource_pack>/')
    else:
        source = path.parent / name
    if not name or Path(name).is_absolute() or '\\' in name or '<' in name.replace('<resource_pack>', ''):
        raise ValueError('invalid local path or unresolved variable')
    from api_agent.config.source_reads import resolve_path
    source = resolve_path(source)
    if not any(source.is_relative_to(root) for root in roots) or not source.is_file():
        raise ValueError('linked file is missing or outside the declared roots')
    if prompt:
        source.read_text(encoding='utf-8')
    return children[1].content, source


def _plain_cell_dash(cell):
    return len(cell.children or ()) == 1 and cell.children[0].type == 'text' and cell.children[0].content == '-'


def _decode_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite JSON')
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    json.dumps(value, allow_nan=False)
    return value


def _json(source):
    return _decode_json(source.read_text(encoding='utf-8'))


def load_layout(path, *, base_root, pack_root):
    """Validate all structural sections and links without importing functions."""
    from api_agent.config.source_reads import resolve_path
    path, base_root, pack_root = map(resolve_path, (path, base_root, pack_root))
    roots = (base_root, pack_root)
    layout = Layout(path, (), {}, ())
    try:
        if not path.is_relative_to(base_root) and not path.is_relative_to(pack_root):
            raise ValueError('layout is outside the declared roots')
        text = path.read_text(encoding='utf-8')
        tokens = _MD.parse(text)
    except (OSError, ValueError, UnicodeError) as error:
        layout.error(str(error))
    sections, current, title = {}, None, False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        line = token.map[0] + 1 if token.map else 1
        if token.type == 'heading_open':
            try:
                heading = _plain(tokens[index + 1])
            except ValueError as error:
                layout.error(str(error), line)
            if token.tag == 'h1' and heading == 'LangGraph Layout v1' and not title and current is None:
                title = True
            elif token.tag == 'h2' and title and heading in {*_COLUMNS, 'Flow', 'Notes'} and heading not in sections:
                current = heading
                sections[current] = []
            else:
                layout.error('unknown, duplicate or misplaced heading', line)
            index += 3
        else:
            if current is None:
                layout.error('content must belong to a declared section', line)
            sections[current].append(token)
            index += 1
    if not title or set(sections) - {'Notes'} != {*_COLUMNS, 'Flow'}:
        layout.error('Layout requires Flow and all fixed table sections')
    tables, files = {}, set()
    for name, columns in _COLUMNS.items():
        body = sections[name]
        if not body:
            tables[name] = []
            continue
        line = body[0].map[0] + 1 if body[0].map else 1
        if body[0].type != 'table_open' or body[-1].type != 'table_close' or sum(token.type == 'table_open' for token in body) != 1:
            layout.error(f'{name} requires exactly one fixed-column table', line)
        rows, row = [], []
        for token in body:
            if token.type == 'inline':
                row.append(token)
            elif token.type == 'tr_close':
                rows.append(row)
                row = []
        try:
            header = tuple(_plain(cell) for cell in rows[0])
            if name == 'Functions' and header == _RESPONSE_COLUMNS:
                columns = _RESPONSE_COLUMNS
            if header != columns:
                raise ValueError(f'{name} has unknown or misplaced columns')
            # markdown-it pads/truncates table rows: reject malformed source first.
            for offset, raw_line in enumerate(text.splitlines()[body[0].map[0]:body[0].map[1]], body[0].map[0] + 1):
                line = offset
                stripped = raw_line.strip()
                cells_source = re.split(r'(?<!\\)\|', stripped.removeprefix('|').removesuffix('|'))
                if len(cells_source) != len(columns):
                    raise ValueError(f'{name} requires exactly {len(columns)} cells per row')
            entries = []
            for cells in rows[1:]:
                values = {}
                line = cells[0].map[0] + 1
                for column, cell in zip(columns, cells):
                    if column == 'Max passes' and name == 'Components' and cell.children and cell.children[0].type == 'link_open':
                        label, source = _link(cell, path, pack_root, roots)
                        files.add(source)
                        values[column] = label
                        values['max_passes_key'] = label
                        values['max_passes_link'] = source
                    elif (column in ('File', 'Source', 'Input', 'Output', 'Response schema') and name != 'Bindings'
                          and not (column == 'Response schema' and _plain_cell_dash(cell))):
                        _, source = _link(cell, path, pack_root, roots, prompt=column == 'File')
                        files.add(source)
                        values[column] = source
                    else:
                        values[column] = _plain(cell)
                values['line'] = line
                entries.append(values)
            tables[name] = entries
        except (OSError, ValueError, UnicodeError) as error:
            layout.error(str(error), line)
    flow = sections['Flow']
    if not flow or flow[0].type != 'ordered_list_open':
        layout.error('Flow requires a nested ordered list')
    if flow[-1].type != 'ordered_list_close' or any(token.type in ('bullet_list_open', 'fence', 'code_block', 'html_block') for token in flow):
        layout.error('Flow requires only a nested ordered list', flow[0].map[0] + 1)
    start = flow[0].map[0]
    stop = flow[0].map[1]
    if any(token.map and token.map[0] >= stop for token in flow):
        layout.error('Flow requires exactly one nested ordered list', stop + 1)
    # Source columns enforce the deliberately stricter grammar over the Markdown AST.
    steps, parents, counts = [], [], {}
    for offset, raw in enumerate(text.splitlines()[start:stop], start + 1):
        if not raw.strip():
            continue
        match = re.fullmatch(r'( *)([1-9][0-9]?)\.( +)(.+)', raw)
        if match is None:
            layout.error('Flow accepts only ordered linked steps numbered 1..99', offset)
        indent, number, spacing, content = match.groups()
        depth = len(indent) // 4
        if len(indent) % 4 or len(number) + 1 + len(spacing) != 4 or depth > len(parents):
            layout.error('steps require four-column markers and four-space nesting', offset)
        parents = parents[:depth]
        parent = parents[-1] if parents else None
        count = counts.get(parent, 0) + 1
        if int(number) != count:
            layout.error('steps must be sequential from 1 at each level', offset)
        counts[parent] = count
        try:
            label, source = _link(_MD.parseInline(content)[0], path, pack_root, roots)
            kind, identifier = label.split(' ')
            if kind not in ('component', 'node') or _ID.fullmatch(identifier) is None:
                raise ValueError('step label must be component ID or node ID')
        except (OSError, ValueError, UnicodeError) as error:
            layout.error(str(error), offset)
        if depth and next(item.kind for item in steps if item.id == parent) != 'component':
            layout.error('only components can own nested steps', offset)
        if identifier in {item.id for item in steps}:
            layout.error('duplicate step ID', offset)
        steps.append(Step(identifier, kind, parent, source, offset))
        files.add(source)
        parents.append(identifier)
    layout = Layout(path, tuple(steps), tables, tuple(sorted(files)))
    _validate(layout)
    return layout


def _validate(layout, resource_bytes=None):
    steps = {item.id: item for item in layout.steps}
    for section, kind in (('Components', 'component'), ('Functions', 'node')):
        entries = layout.tables[section]
        ids = [row['ID'] for row in entries]
        if len(set(ids)) != len(ids) or set(ids) != {key for key, item in steps.items() if item.kind == kind}:
            layout.error(f'{section} must declare each matching step exactly once')
    functions = {row['ID']: row for row in layout.tables['Functions']}
    for row in layout.tables['Functions']:
        try:
            if _ID.fullmatch(row['Registry']) is None:
                raise ValueError('invalid function registry ID')
            for column in ('Input', 'Output'):
                schema = (_decode_json(resource_bytes[row[column]].decode('utf-8'))
                          if resource_bytes is not None else _json(row[column]))
                if _has_unsupported_dialect(schema):
                    raise ValueError('Layout v1 supports only JSON Schema draft 2020-12')
                Draft202012Validator.check_schema(schema)
                if schema.get('type') != 'object' or _has_reference(schema):
                    raise ValueError('schemas must be object contracts without remote references')
                row[column + ' schema'] = schema
            if 'Output mode' in row:
                mode, fallback, response = (row[key] for key in ('Output mode', 'Fallback', 'Response schema'))
                if mode == '-':
                    if (fallback, response) != ('-', '-'):
                        raise ValueError('deterministic response columns must all be -')
                else:
                    if mode not in ('structured', 'json', 'prompted_json', 'text') or fallback not in ('allow', 'reject'):
                        raise ValueError('invalid response output mode or fallback')
                    if (mode == 'text') != (response == '-'):
                        raise ValueError('JSON modes require Response schema; text requires -')
                    if response != '-':
                        schema = (_decode_json(resource_bytes[response].decode('utf-8'))
                                  if resource_bytes is not None else _json(response))
                        if _has_unsupported_dialect(schema):
                            raise ValueError('Layout v1 supports only JSON Schema draft 2020-12')
                        Draft202012Validator.check_schema(schema)
                        from api_agent.providers.rich_inference import response_schema
                        from api_agent.providers.contracts import ContractError
                        try:
                            response_schema(json.dumps(schema))
                        except ContractError as error:
                            raise ValueError('invalid generated-response schema') from error
                        row['Response schema value'] = schema
        except (OSError, ValueError, AttributeError, SchemaError) as error:
            layout.error(str(error), row['line'])
    pass_sources = {}
    for row in layout.tables['Components']:
        if 'max_passes_link' in row:
            key, source = row['max_passes_key'], row['max_passes_link']
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', key):
                layout.error('Max passes link requires an uppercase configuration key', row['line'])
            if key in pass_sources and pass_sources[key] != source:
                layout.error('Max passes key has conflicting configuration sources', row['line'])
            pass_sources[key] = source
            try:
                from api_agent.config.declarations import schema_from_dict, UNSET
                data = (resource_bytes[source] if resource_bytes is not None else source.read_bytes())
                schema = schema_from_dict(tomllib.loads(data.decode('utf-8')))
                field = schema.fields.get(key)
                if field is None or field.type != 'int' or type(field.default) is not int:
                    raise ValueError('Max passes requires a declared integer default')
                maximum = field.default
            except (OSError, UnicodeError, ValueError, KeyError) as error:
                layout.error(f'Max passes declaration: {error}', row['line'])
        else:
            if not re.fullmatch(r'[1-9][0-9]*', row['Max passes']):
                layout.error('Max passes must be a positive integer', row['line'])
            if len(row['Max passes']) > len(str(MAX_COMPONENT_PASSES)):
                layout.error(f'Max passes must be at most {MAX_COMPONENT_PASSES}', row['line'])
            maximum = int(row['Max passes'])
        if not 1 <= maximum <= MAX_COMPONENT_PASSES:
            layout.error(f'Max passes must be at most {MAX_COMPONENT_PASSES} and positive', row['line'])
        row['max_passes_default'] = maximum
        if row['Guard'] == '-':
            if maximum != 1:
                layout.error('Unguarded components require exactly one pass', row['line'])
            continue
        guard = steps.get(row['Guard'])
        if guard is None or guard.kind != 'node' or guard.parent != row['ID']:
            layout.error('Guard must be a directly owned node', row['line'])
        children = [item.id for item in layout.steps if item.parent == row['ID']]
        if children[-1] != guard.id:
            layout.error('Guard must be the final directly owned step', row['line'])
    bound = {key: set() for key in functions}
    ordered = [item.id for item in layout.steps]
    for row in layout.tables['Bindings']:
        target, field, source = row['Node'], row['Input'], row['From']
        if target not in functions or field in bound[target] or field not in functions[target]['Input schema'].get('properties', {}):
            layout.error('unknown or duplicate input binding', row['line'])
        bound[target].add(field)
        if not row['Field'] or (source in ('ATTEMPT', 'MAX_PASSES') and row['Field'] != 'value'):
            layout.error('invalid binding source field', row['line'])
        if source not in ('START', 'PREVIOUS', 'FEEDBACK', 'ATTEMPT', 'MAX_PASSES'):
            if source not in functions or ordered.index(source) >= ordered.index(target):
                layout.error('binding source must be an earlier node', row['line'])
            if row['Field'] != '*' and row['Field'] not in functions[source]['Output schema'].get('properties', {}):
                layout.error('unknown output field', row['line'])
        if row['Default'] != '-':
            try:
                row['default_value'] = _decode_json(row['Default'])
                Draft202012Validator(functions[target]['Input schema']['properties'][field]).validate(row['default_value'])
            except (ValueError, ValidationError) as error:
                layout.error(str(error), row['line'])
    for key, row in functions.items():
        if not set(row['Input schema'].get('required', ())) <= bound[key]:
            layout.error('required inputs must have declared bindings', row['line'])
    seen = set()
    edge_groups = {}
    for row in layout.tables['Edges']:
        source, target = row['From'], row['To']
        guard_ids = {component['Guard'] for component in layout.tables['Components'] if component['Guard'] != '-'}
        if source in guard_ids and (row['When'] != 'always' or target != 'END'):
            layout.error('guard node may only declare always to END', row['line'])
        if source not in steps or (target != 'END' and (target not in steps or steps[target].parent != steps[source].parent)):
            layout.error('edges must connect siblings or their END', row['line'])
        if target != 'END' and ordered.index(target) <= ordered.index(source):
            layout.error('verification loops belong to bounded components; edges must advance', row['line'])
        identity = source, row['When']
        if identity in seen:
            layout.error('duplicate edge condition', row['line'])
        seen.add(identity)
        edge_groups.setdefault(source, []).append(row)
        if row['When'] not in ('always', 'otherwise') and _ID.fullmatch(row['When']) is None:
            try:
                parse_predicate(row['When'], predicate_fields(layout, source))
            except PredicateError as error:
                layout.error(str(error), row['line'])
    for rows in edge_groups.values():
        whens = {row['When'] for row in rows}
        if ('always' in whens and len(rows) > 1) or ('otherwise' in whens and 'always' in whens):
            layout.error('conditional edges require otherwise fallback; always must stand alone', rows[-1]['line'])
    for row in layout.tables['Prompts']:
        if (row['Node'] not in functions or row['When'] == 'otherwise'
                or (row['When'] != 'always' and _ID.fullmatch(row['When']) is None)):
            layout.error('unknown prompt node or condition', row['line'])
    seen = set()
    for row in layout.tables['Timeouts']:
        if row['Owner'] not in {'GRAPH', *steps} or row['Owner'] in seen or _ID.fullmatch(row['Key'].lower()) is None:
            layout.error('unknown or duplicate timeout owner/key', row['line'])
        seen.add(row['Owner'])


def _has_unsupported_dialect(schema):
    """Visit draft 2020-12 schema positions, excluding instance-valued keywords."""
    if not isinstance(schema, dict):
        return False
    if schema.get('$schema', 'https://json-schema.org/draft/2020-12/schema') != 'https://json-schema.org/draft/2020-12/schema':
        return True
    return any(_has_unsupported_dialect(child) for child in _schema_children(schema))


def _schema_children(schema):
    # The installed resource specification identifies active schema positions and
    # legacy definitions. Its traversal performs no reference resolution.
    try:
        children = list(DRAFT202012.subresources_of(schema))
    except (TypeError, AttributeError):
        # Malformed containers are rejected by check_schema immediately afterward.
        return ()
    # The meta-schema also recognizes mixed schema/property-name dependency maps.
    # Derive those compatibility positions from its schema alternative rather
    # than duplicating the resource specification's keyword lists.
    for keyword, declaration in Draft202012Validator.META_SCHEMA['properties'].items():
        alternatives = declaration.get('additionalProperties', {}).get('anyOf', ())
        if {'$dynamicRef': '#meta'} in alternatives:
            values = schema.get(keyword, {})
            if isinstance(values, dict):
                children.extend(values.values())
    return children


def _has_reference(schema):
    """Reject references only where the contract contains a schema."""
    if not isinstance(schema, dict):
        return False
    if '$ref' in schema or '$dynamicRef' in schema:
        return True
    return any(_has_reference(child) for child in _schema_children(schema))
