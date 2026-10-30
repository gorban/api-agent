"""Bounded JSON-only capture of declared workflow values."""
from collections.abc import Mapping
import math
import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable

from .journal import _schema_field, _provider_field

_OMIT = object()


def project_values(values, schema, boundary, *, limit=None, report=None, max_bytes=65536,
                   _content_wrapper=False, _provider_content=False):
    """Project schema fields without inspecting arbitrary Python objects.

    Empty child schemas permit JSON values (including JSON message parts), but
    explicit object properties exclude undeclared fields. Privacy projection
    precedes clipping so credentials cannot straddle a display boundary.
    """
    report = report if report is not None else set()
    if type(_content_wrapper) is not bool:
        raise ValueError('Invalid internal content wrapper')
    if type(_provider_content) is not bool:
        raise ValueError('Invalid provider content projection')
    if _content_wrapper and (type(values) is not dict or set(values) != {'values'}
                             or type(values['values']) is not dict or schema != {'type': 'object'}):
        raise ValueError('Invalid internal content wrapper')
    # The transport wrapper is not part of the logical evidence value.
    root_depth = -1 if _content_wrapper else 0
    # Privacy must see original JSON carriers before schemas remove fields or
    # limits clip text. Bound the entire representation without fragmenting a
    # JSON container; unknown runtime objects cannot be serialized as JSON.
    admission_budget = [max_bytes, 2048 + int(_content_wrapper)]
    overflow = [False]
    def original(value, seen, depth):
        admission_budget[1] -= 1
        if depth > 8 or admission_budget[1] < 0:
            overflow[0] = True
            return _OMIT
        if type(value) is str:
            admission_budget[0] -= len(value)
            if admission_budget[0] < 0:
                overflow[0] = True
                return _OMIT
            return value
        if value is None or type(value) is bool:
            return value
        if type(value) is int and abs(value) <= (1 << 53) - 1:
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if type(value) in (int, float):
            overflow[0] = True
            return _OMIT
        if type(value) not in (dict, list, tuple):
            report.add('unsupported')
            return _OMIT
        if id(value) in seen:
            overflow[0] = True
            return _OMIT
        seen = seen | {id(value)}
        if type(value) is dict:
            if len(value) > 2048:
                overflow[0] = True
                return _OMIT
            result = {}
            for key, item in value.items():
                if type(key) is not str:
                    overflow[0] = True
                    return _OMIT
                if len(key) > 128:
                    overflow[0] = True
                    return _OMIT
                child = original(item, seen, depth + 1)
                if child is not _OMIT:
                    result[key] = child
            return result
        if len(value) > 2048:
            overflow[0] = True
            return _OMIT
        result = []
        for item in value:
            child = original(item, seen, depth + 1)
            if child is not _OMIT:
                result.append(child)
        return result
    snapshot = original(values, set(), root_depth)
    if overflow[0]:
        report.add('truncated')
        return {}
    if snapshot is _OMIT:
        return {}
    try:
        encoded_size = len(json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except UnicodeError:
        report.add('unsupported')
        return {}
    if encoded_size > max_bytes:
        report.add('truncated')
        return {}
    exported = boundary.content_export({'values': snapshot})
    if type(exported) is not dict or 'values' not in exported:
        report.add('unsupported')
        return {}
    values = exported['values']
    budget = [2048 + int(_content_wrapper)]
    validator = Draft202012Validator(schema, registry=Registry())
    def matches_type(value, rule):
        try:
            return validator.evolve(schema=rule).is_valid(value)
        except (Unresolvable, RecursionError, SchemaError, TypeError, AttributeError):
            return False

    def select(value, rule, depth):
        budget[0] -= 1
        if depth > 8 or budget[0] < 0:
            report.add('truncated')
            return _OMIT
        if rule is True:
            rule = {}
        if not isinstance(rule, Mapping):
            report.add('unsupported')
            return _OMIT
        if '$ref' in rule:
            ref = rule['$ref']
            target = schema
            if type(ref) is not str or not ref.startswith('#/'):
                report.add('unsupported')
                return _OMIT
            for part in ref[2:].split('/'):
                target = target.get(part.replace('~1', '/').replace('~0', '~'), _OMIT) if isinstance(target, Mapping) else _OMIT
            value = select(value, target, depth + 1)
            if value is _OMIT:
                return _OMIT
        if 'allOf' in rule:
            branches = rule['allOf']
            if not all(matches_type(value, branch) for branch in branches):
                report.add('unsupported')
                return _OMIT
            if type(value) is dict:
                # Properties declared by sibling allOf clauses form one object
                # contract. A clause's explicit additionalProperties restriction
                # still constrains fields declared only by another clause.
                clauses = [rule, *branches]
                if any(not isinstance(branch, Mapping) or any(key in branch for key in
                       ('$ref', 'allOf', 'anyOf', 'oneOf')) for branch in branches):
                    report.add('unsupported')
                    return _OMIT
                fields = {key for branch in clauses for key in branch.get('properties', {})}
                properties = {}
                for key in fields:
                    contracts = []
                    for branch in clauses:
                        declared = branch.get('properties', {})
                        if key in declared:
                            contracts.append(declared[key])
                        elif 'additionalProperties' in branch:
                            contracts.append(branch['additionalProperties'])
                    properties[key] = {'allOf': contracts}
                rule = {key: item for key, item in rule.items() if key != 'allOf'}
                if fields:
                    rule['properties'] = properties
            elif type(value) is list:
                clauses = [rule, *branches]
                if any(not isinstance(branch, Mapping) or any(key in branch for key in
                       ('$ref', 'allOf', 'anyOf', 'oneOf')) for branch in branches):
                    report.add('unsupported')
                    return _OMIT
                # Each position inherits every clause's effective item schema.
                # This also preserves prefixItems alongside ordinary items.
                positions = []
                for index in range(len(value)):
                    contracts = []
                    for branch in clauses:
                        prefix = branch.get('prefixItems', ())
                        contracts.append(prefix[index] if index < len(prefix)
                                         else branch.get('items', {}))
                    positions.append({'allOf': contracts})
                rule = {key: item for key, item in rule.items() if key != 'allOf'}
                rule.update(prefixItems=positions, items=False)
        for composition in ('anyOf', 'oneOf'):
            if composition in rule:
                matches = [branch for branch in rule[composition] if matches_type(value, branch)]
                if not matches or composition == 'oneOf' and len(matches) != 1:
                    report.add('unsupported')
                    return _OMIT
                if composition == 'anyOf' and len(matches) > 1:
                    report.add('unsupported')
                value = select(value, matches[0], depth + 1)
                if value is _OMIT:
                    return _OMIT
        if any(key in rule for key in ('if', 'then', 'else', 'not', 'patternProperties', 'dependentSchemas')):
            report.add('unsupported')
            return _OMIT
        if ('const' in rule and value != rule['const']
                or 'enum' in rule and value not in rule['enum']):
            report.add('unsupported')
            return _OMIT
        kind = rule.get('type')
        if type(kind) in (list, tuple):
            kinds = [item for item in kind if matches_type(value, {'type': item})]
            kind = kinds[0] if kinds else False
        if type(value) is dict and kind in (None, 'object'):
            properties = rule.get('properties')
            result = {}
            for key, item in value.items():
                # Provider-returned plaintext reasoning is content, not an
                # arbitrary workflow field declaration. The privacy boundary
                # above still excludes credentials and internal/encrypted state.
                if not (_provider_field(key) if _provider_content else _schema_field(key)):
                    continue
                if isinstance(properties, Mapping) and key not in properties:
                    continue
                child = properties[key] if isinstance(properties, Mapping) else rule.get('additionalProperties', {})
                admitted = select(item, child, depth + 1)
                if admitted is not _OMIT:
                    result[key] = admitted
            return result
        if type(value) in (list, tuple) and kind in (None, 'array'):
            result = []
            for index, item in enumerate(value):
                prefix = rule.get('prefixItems', ())
                child_rule = prefix[index] if index < len(prefix) else rule.get('items', {})
                admitted = select(item, child_rule, depth + 1)
                if admitted is not _OMIT:
                    result.append(admitted)
            return result
        if type(value) is str and kind in (None, 'string'):
            return value
        if value is None and kind in (None, 'null'):
            return value
        if type(value) is bool and kind in (None, 'boolean'):
            return value
        if type(value) is int and kind in (None, 'integer', 'number') :
            return value
        if type(value) is float and kind in (None, 'number'):
            return value
        report.add('unsupported')
        return _OMIT
    selected = select(values, schema, root_depth)
    if selected is _OMIT:
        return {}
    exported = boundary.content_export({'values': selected})
    if type(exported) is not dict or 'values' not in exported:
        report.add('unsupported')
        return {}
    safe = exported['values']
    try:
        encoded_size = len(json.dumps(safe, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except UnicodeError:
        report.add('unsupported')
        return {}
    if encoded_size > max_bytes:
        report.add('truncated')
        return {}
    def clip(value):
        if type(value) is str:
            if len(value) > limit:
                report.add('truncated')
            return value[:limit]
        if type(value) is dict:
            return {key: clip(item) for key, item in value.items()}
        if type(value) is list:
            return [clip(item) for item in value]
        return value
    if (not snapshot and type(snapshot) is type(selected) is type(safe)
            and snapshot == selected == safe and not report):
        report.add('valid_empty')
    return clip(safe) if limit is not None else safe


def capture_reason(values, report, upstream=None):
    if 'truncated' in report or upstream == 'truncated':
        return 'truncated'
    loss = report - {'valid_empty'}
    if (not values and ('valid_empty' not in report or loss)) or upstream == 'unavailable':
        return 'unavailable'
    return 'partial' if loss or upstream == 'partial' else 'captured'


def annotation_schema(annotation, *, depth=0):
    """Resolve declared state types into a conservative JSON capture schema."""
    from types import UnionType
    from typing import Annotated, Any, Literal, NotRequired, Required, Union, get_args, get_origin, get_type_hints
    from typing_extensions import is_typeddict
    if depth > 8:
        return False
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Annotated, Required, NotRequired):
        return annotation_schema(args[0], depth=depth + 1)
    if annotation is Any:
        return {}
    scalars = {str: 'string', bool: 'boolean', int: 'integer', float: 'number', type(None): 'null'}
    for scalar, kind in scalars.items():
        if annotation is scalar:
            return {'type': kind}
    if is_typeddict(annotation):
        return {'type': 'object', 'properties': {
            key: annotation_schema(hint, depth=depth + 1)
            for key, hint in get_type_hints(annotation, include_extras=True).items()}}
    if origin in (Union, UnionType):
        return {'anyOf': [annotation_schema(item, depth=depth + 1) for item in args]}
    if origin is Literal:
        return {'enum': list(args)}
    if origin is list or annotation is list:
        return {'type': 'array', 'items': annotation_schema(args[0], depth=depth + 1) if args else {}}
    if origin is tuple or annotation is tuple:
        if not args:
            return {'type': 'array'}
        if args[-1] is Ellipsis:
            return {'type': 'array', 'items': annotation_schema(args[0], depth=depth + 1)}
        return {'type': 'array', 'prefixItems': [annotation_schema(item, depth=depth + 1) for item in args], 'items': False}
    if origin is dict or annotation is dict:
        if args and args[0] is not str:
            return False
        return {'type': 'object', 'additionalProperties': annotation_schema(args[1], depth=depth + 1) if args else {}}
    return False
