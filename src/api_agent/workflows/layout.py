"""A restricted structural Markdown grammar with source-located diagnostics."""
import json
import tomllib
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from markdown_it import MarkdownIt
from referencing.jsonschema import DRAFT202012
from .model import Layout, LayoutError, MAX_COMPONENT_PASSES, Step, _ID, timeout_owner_problem
from .predicates import (PredicateError, backward_bound, edge_effected, edge_source, entry_problem, parse_predicate,
                         predicate_fields, unbounded_edge_message)
from . import session


_COLUMNS = {
    'Components': ('ID', 'Max passes', 'Guard'),
    'Functions': ('ID', 'Registry', 'Input', 'Output'),
    'Bindings': ('Node', 'Input', 'From', 'Field', 'Default'),
    'Edges': ('From', 'To', 'When'),
    'Prompts': ('Node', 'File', 'When'),
    'Timeouts': ('Owner', 'Source', 'Key'),
    'Limits': ('Key', 'Source'),
}
_SESSION_COLUMNS = ('Field', 'Source', 'Merge')
_SESSION_COMMIT_COLUMNS = ('Outcome',)
_RESPONSE_COLUMNS = (*_COLUMNS['Functions'], 'Response schema', 'Output mode', 'Fallback')
_SUMMARY_COLUMNS = (*_RESPONSE_COLUMNS, 'Display summary')

# Notes cannot supply reference definitions to executable sections.
_MD = MarkdownIt('commonmark').enable('table').disable('reference')


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
    """Project a validated release declaration to its structural layout."""
    from .definition import load_definition
    return load_definition(path, base_root=base_root, pack_root=pack_root).layout


def _validate(layout, resource_bytes=None, tool_ports=None):
    steps = {item.id: item for item in layout.steps}
    for section, kind in (('Components', 'component'), ('Functions', 'node')):
        entries = layout.tables[section]
        ids = [row['ID'] for row in entries]
        if len(set(ids)) != len(ids) or set(ids) != {key for key, item in steps.items() if item.kind == kind}:
            layout.error(f'{section} must declare each matching step exactly once')
    functions = {row['ID']: row for row in layout.tables['Functions']}
    for row in layout.tables['Functions']:
        validate_function_rows([row], resource_bytes, lambda message, line, row=row: layout.row_error(row, message))
    pass_sources = {}
    for row in layout.tables['Components']:
        if 'max_passes_link' in row:
            key, source = row['max_passes_key'], row['max_passes_link']
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', key):
                layout.row_error(row, 'Max passes link requires an uppercase configuration key')
            if key in pass_sources and pass_sources[key] != source:
                layout.row_error(row, 'Max passes key has conflicting configuration sources')
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
                layout.row_error(row, f'Max passes declaration: {error}')
        else:
            if not re.fullmatch(r'[1-9][0-9]*', row['Max passes']):
                layout.row_error(row, 'Max passes must be a positive integer')
            if len(row['Max passes']) > len(str(MAX_COMPONENT_PASSES)):
                layout.row_error(row, f'Max passes must be at most {MAX_COMPONENT_PASSES}')
            maximum = int(row['Max passes'])
        if not 1 <= maximum <= MAX_COMPONENT_PASSES:
            layout.row_error(row, f'Max passes must be at most {MAX_COMPONENT_PASSES} and positive')
        row['max_passes_default'] = maximum
        if row['Guard'] == '-':
            if maximum != 1:
                layout.row_error(row, 'Unguarded components require exactly one pass')
            continue
        guard = steps.get(row['Guard'])
        if guard is None or guard.kind != 'node' or guard.parent != row['ID']:
            layout.row_error(row, 'Guard must be a directly owned node')
        children = [item.id for item in layout.steps if item.parent == row['ID']]
        if children[-1] != guard.id:
            layout.row_error(row, 'Guard must be the final directly owned step')
    validate_limit_rows(layout, pass_sources)
    limit_keys = {row['Key'] for row in layout.tables.get('Limits', ())}
    bound = {key: set() for key in functions}
    ordered = [item.id for item in layout.steps]
    for row in layout.tables['Bindings']:
        target, field, source = row['Node'], row['Input'], row['From']
        if target not in functions or field in bound[target] or field not in functions[target]['Input schema'].get('properties', {}):
            layout.row_error(row, 'unknown or duplicate input binding')
        bound[target].add(field)
        if not row['Field'] or (source in ('ATTEMPT', 'MAX_PASSES') and row['Field'] != 'value'):
            layout.row_error(row, 'invalid binding source field')
        if source not in ('START', 'PREVIOUS', 'FEEDBACK', 'ATTEMPT', 'MAX_PASSES', 'SESSION', 'TURN'):
            if source not in functions or ordered.index(source) >= ordered.index(target):
                layout.row_error(row, 'binding source must be an earlier node')
            if row['Field'] != '*' and row['Field'] not in functions[source]['Output schema'].get('properties', {}):
                layout.row_error(row, 'unknown output field')
        if row['Default'] != '-':
            try:
                row['default_value'] = _decode_json(row['Default'])
                Draft202012Validator(functions[target]['Input schema']['properties'][field]).validate(row['default_value'])
            except (ValueError, ValidationError) as error:
                layout.row_error(row, str(error))
    for key, row in functions.items():
        for reserved, meaning in (('attempt', 'the engine start counter'), ('expired', 'the engine timeout expiry flag'),
                                  ('expirations', 'the engine timeout expiry counter'),
                                  ('effected', 'the engine flag for a timeout expiry after an effect')):
            if reserved in row['Output schema'].get('properties', {}):
                layout.row_error(row, f'Output schema declares reserved field {reserved}; '
                                      f'<step>.{reserved} is {meaning}')
        if not set(row['Input schema'].get('required', ())) <= bound[key]:
            layout.row_error(row, 'required inputs must have declared bindings')
    invalid = session.problem(layout.tables)
    if invalid is not None:
        layout.row_error(invalid[1], invalid[0])
    seen = set()
    edge_groups = {}
    for row in layout.tables['Edges']:
        source, target = row['From'], row['To']
        guard_ids = {component['Guard'] for component in layout.tables['Components'] if component['Guard'] != '-'}
        if source in guard_ids and (row['When'] != 'always' or target != 'END'):
            layout.row_error(row, 'guard node may only declare always to END')
        group, entering = edge_source(row)
        if entering is not None:
            problem = entry_problem(layout, row)
            if problem is not None:
                layout.row_error(row, problem)
        elif source not in steps or (target != 'END' and (target not in steps or steps[target].parent != steps[source].parent)):
            layout.row_error(row, 'edges must connect siblings or their END')
        identity = group, row['When']
        if identity in seen:
            layout.row_error(row, 'duplicate edge condition')
        seen.add(identity)
        edge_groups.setdefault(group, []).append(row)
        predicate = None
        if row['When'] not in ('always', 'otherwise') and _ID.fullmatch(row['When']) is None:
            try:
                predicate = parse_predicate(row['When'], predicate_fields(layout, source, entering))
            except PredicateError as error:
                layout.row_error(row, str(error))
        if entering is None and edge_effected(row['When'], source) and target == source:
            layout.row_error(row, f'{source}.effected may only route to a step other than {source}: '
                                  'a step that expired after an effect is never replayed')
        if (entering is None and target != 'END' and ordered.index(target) <= ordered.index(source)
                and (predicate is None or backward_bound(predicate, source, target, limit_keys) is None)):
            layout.row_error(row, unbounded_edge_message(target))
    for rows in edge_groups.values():
        whens = {row['When'] for row in rows}
        if ('always' in whens and len(rows) > 1) or ('otherwise' in whens and 'always' in whens):
            layout.row_error(rows[-1], 'conditional edges require otherwise fallback; always must stand alone')
    for row in layout.tables['Prompts']:
        if (row['Node'] not in functions or row['When'] == 'otherwise'
                or (row['When'] != 'always' and _ID.fullmatch(row['When']) is None)):
            layout.row_error(row, 'unknown prompt node or condition')
    problem = timeout_owner_problem(layout.tables['Timeouts'], steps, tool_ports or {})
    if problem is not None:
        layout.row_error(*problem)


def validate_limit_rows(layout, pass_sources):
    """Check Limits keys and sources; values are resolved against configuration separately."""
    seen = set()
    for row in layout.tables.get('Limits', ()):
        def reject(message, row=row):
            layout.row_error(row, message)
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', row['Key']):
            reject('invalid limit key')
        if row['Key'] in seen:
            reject('duplicate limit key')
        if row['Key'] in pass_sources:
            reject('limit key conflicts with a component Max passes key')
        if row['Source'].suffix != '.toml':
            reject('limit source must be TOML')
        seen.add(row['Key'])


def validate_function_rows(rows, resource_bytes, reject):
    """Check each Functions row: schemas, response columns and display summary.

    ``rows`` carry captured schema locations in ``Input``/``Output``/``Response schema``;
    ``resource_bytes`` maps them to admitted bytes (``None`` reads the paths). Parsed
    schemas are stored back on each row. ``reject(message, line)`` must raise.
    Shared by the binding grammar and typed nodes.
    """
    for row in rows:
        try:
            if 'Display summary' in row:
                summary = row['Display summary']
                if summary not in ('-', 'native', 'auto', 'explanation'):
                    raise ValueError('invalid node display summary policy')
                if summary != '-' and (row['Output mode'] == '-' or summary != 'native' and row['Response schema'] == '-'):
                    raise ValueError('generated node summaries require a JSON response schema')
            if _ID.fullmatch(row['Registry']) is None:
                raise ValueError('invalid function registry ID')
            for column in ('Input', 'Output'):
                schema = (_decode_json(resource_bytes[row[column]].decode('utf-8'))
                          if resource_bytes is not None else _json(row[column]))
                if _has_unsupported_dialect(schema):
                    raise ValueError('Workflows support only JSON Schema draft 2020-12')
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
                            raise ValueError('Workflows support only JSON Schema draft 2020-12')
                        Draft202012Validator.check_schema(schema)
                        from api_agent.providers.rich_inference import response_schema
                        from api_agent.providers.contracts import ContractError
                        try:
                            response_schema(json.dumps(schema))
                        except ContractError as error:
                            raise ValueError('invalid generated-response schema') from error
                        row['Response schema value'] = schema
        except (OSError, ValueError, AttributeError, SchemaError) as error:
            reject(str(error), row['line'])


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
