"""Bounded, data-only conditions for declared workflow edges."""
import ast
from collections.abc import Mapping
from dataclasses import dataclass
import io
import re
import tokenize
import unicodedata

_NATIVE_ID = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_SCALAR_TYPES = frozenset(('string', 'integer', 'number', 'boolean', 'null'))
_CONTEXT = {'attempt': {'type': 'integer'}, 'max_passes': {'type': 'integer'}}
_SOURCE = '__route_source__'
_CONSTANTS = '__route_constants__'
_COMPARISONS = frozenset(('==', '!=', '<', '<=', '>', '>='))


def native_condition_id(text):
    return _NATIVE_ID.fullmatch(text) is not None


class PredicateError(ValueError):
    pass


@dataclass(frozen=True)
class Predicate:
    text: str
    tree: ast.Expression
    source_owners: Mapping

    def evaluate(self, values, *, attempt=1, max_passes=1, constants=None):
        return _evaluate(self.tree.body, values, self.source_owners,
                         {'attempt': attempt, 'max_passes': max_passes}, constants or {})


def _normalize(text):
    """Give a leading dot a parseable owner without touching quoted strings."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError) as error:
        raise PredicateError('invalid condition syntax') from error
    result = []
    previous = None
    for token in tokens:
        if token.type == tokenize.NAME and unicodedata.normalize('NFKC', token.string) in (_SOURCE, _CONSTANTS):
            raise PredicateError('reserved condition name')
        if token.type == tokenize.OP and token.string == '.' and previous in (
                None, 'not', 'and', 'or', '(', *_COMPARISONS):
            result.append((tokenize.NAME, _SOURCE))
        result.append((token.type, token.string))
        if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER):
            previous = token.string
    return tokenize.untokenize(result)


def _reference(node):
    path = []
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        if isinstance(node, ast.Attribute):
            path.append(node.attr)
            node = node.value
        elif isinstance(node.slice, ast.Constant) and type(node.slice.value) is str:
            path.append(node.slice.value)
            node = node.value
        else:
            raise PredicateError('condition may read only declared scalar output fields')
    if not isinstance(node, ast.Name):
        raise PredicateError('condition may read only declared scalar output fields')
    return node.id, tuple(reversed(path))


def _exact_type(rule, expected):
    """Treat a one-member JSON Schema type array as its scalar spelling."""
    if not isinstance(rule, Mapping):
        return False
    kinds = rule.get('type')
    return kinds == expected or (type(kinds) in (list, tuple) and len(kinds) == 1
                                  and kinds[0] == expected)


def parse_predicate(text, fields):
    """Admit bounded Boolean expressions over declared output contracts."""
    if len(text) > 256:
        raise PredicateError('condition exceeds 256 characters')
    try:
        tree = ast.parse(_normalize(text), mode='eval')
    except SyntaxError as error:
        raise PredicateError('invalid condition syntax') from error
    if len(tuple(ast.walk(tree))) > 40:
        raise PredicateError('condition exceeds 40 syntax nodes')
    source_owners = {} if fields is None else fields.get(_SOURCE, {}).get('owners', {})
    constants = {} if fields is None else fields.get(_CONSTANTS, {})

    def contract(node):
        owner, path = _reference(node)
        if owner == _SOURCE and fields is not None:
            owner = source_owners.get(path)
            if owner is None:
                raise PredicateError('source field requires one declared output')
        if not path and owner in _CONTEXT:
            return _CONTEXT[owner]
        if not path and owner in constants:
            return constants[owner]
        if fields is None and not path and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', owner):
            return {'type': 'integer'}
        if not path:
            raise PredicateError('condition may read only declared scalar output fields')
        if fields is None:
            # Retained journals validate syntax here; the admitted manifest
            # supplies the declaration binding independently.
            return {'type': 'boolean'}
        rule = fields.get(owner, {}).get(path[0])
        for part in path[1:]:
            if not _exact_type(rule, 'object'):
                raise PredicateError('condition may read only declared scalar output fields')
            rule = rule.get('properties', {}).get(part)
        return rule

    def check_value(node, boolean=False):
        if isinstance(node, ast.Constant) and (node.value is None or type(node.value) in (str, int, float, bool)):
            if not boolean:
                return
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
            rule = contract(node)
            if isinstance(rule, Mapping):
                kinds = rule.get('type')
                if (_exact_type(rule, 'boolean') if boolean else
                    (type(kinds) is str and kinds in _SCALAR_TYPES) or
                    (type(kinds) in (list, tuple) and kinds and all(type(k) is str and k in _SCALAR_TYPES for k in kinds))):
                    return
        raise PredicateError('condition may read only declared Boolean fields' if boolean else
                             'condition may read only declared scalar output fields')

    def check(node, depth=0):
        if depth > 8:
            raise PredicateError('condition nesting exceeds 8 levels')
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)) and len(node.values) <= 8:
            for item in node.values:
                check(item, depth + 1)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            check(node.operand, depth + 1)
        elif isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            check_value(node.left)
            check_value(node.comparators[0])
        else:
            check_value(node, boolean=True)

    check(tree.body)
    return Predicate(text, tree, source_owners)


def _evaluate(node, values, owners, context, constants):
    if isinstance(node, ast.BoolOp):
        results = (_evaluate(item, values, owners, context, constants) for item in node.values)
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.UnaryOp):
        return not _evaluate(node.operand, values, owners, context, constants)
    if not isinstance(node, ast.Compare):
        result = _value(node, values, owners, context, constants)
        if type(result) is not bool:
            raise PredicateError('condition Boolean field has invalid value')
        return result
    left = _value(node.left, values, owners, context, constants)
    right = _value(node.comparators[0], values, owners, context, constants)
    operator = node.ops[0]
    numeric = type(left) in (int, float) and type(right) in (int, float)
    if isinstance(operator, (ast.Eq, ast.NotEq)):
        result = (type(left) is type(right) or numeric) and left == right
        return result if isinstance(operator, ast.Eq) else not result
    if not numeric and (type(left) is not str or type(right) is not str):
        return False
    return (left < right if isinstance(operator, ast.Lt) else
            left <= right if isinstance(operator, ast.LtE) else
            left > right if isinstance(operator, ast.Gt) else left >= right)


def _value(node, values, owners, context, constants):
    if isinstance(node, ast.Constant):
        return node.value
    owner, path = _reference(node)
    if owner in context and not path:
        return context[owner]
    if not path and owner in constants:
        value = constants[owner]
        if type(value) is not int or not 1 <= value <= 99:
            raise PredicateError(f'condition constant {owner} has invalid value')
        return value
    if owner == _SOURCE:
        owner = owners.get(path, _SOURCE)
    value = values.get(owner)
    for field in path:
        if type(value) is not dict or field not in value:
            raise PredicateError(f'condition field {owner}.{field} was not produced')
        value = value[field]
    if not path or type(value) not in (str, int, float, bool, type(None)):
        raise PredicateError('condition field has invalid scalar value')
    return value


def edge_label(when):
    return when


def predicate_fields(layout, source):
    """Output fields visible at a routing source, with unique source-relative paths."""
    functions = {row['ID']: row for row in layout.tables['Functions']}
    ordered = [step.id for step in layout.steps]
    parents = {step.id: step.parent for step in layout.steps}

    def belongs_to(step_id, component_id):
        parent = parents[step_id]
        while parent is not None:
            if parent == component_id:
                return True
            parent = parents[parent]
        return False

    boundary = max(index for index, step in enumerate(layout.steps)
                   if step.id == source or belongs_to(step.id, source))
    fields = {'START': {}}
    for binding in layout.tables['Bindings']:
        if binding['From'] == 'START' and binding['Field'] != '*':
            target = functions[binding['Node']]['Input schema'].get('properties', {})
            fields['START'][binding['Field']] = target.get(binding['Input'], {})
    fields.update({key: row['Output schema'].get('properties', {}) for key, row in functions.items()
                   if ordered.index(key) <= boundary})
    # Components have no declared output object. Their children are separate
    # producers and must always be named explicitly in route expressions.
    candidates = [source] if source in functions else []
    owners = {}
    def visit(owner, properties, prefix=()):
        for key, rule in properties.items():
            path = (*prefix, key)
            owners[path] = owner
            if _exact_type(rule, 'object'):
                visit(owner, rule.get('properties', {}), path)
    for candidate in candidates:
        visit(candidate, fields[candidate])
    fields[_SOURCE] = {'owners': owners}
    def in_component(component):
        return source == component or (source in parents and belongs_to(source, component))
    fields[_CONSTANTS] = {row['max_passes_key']: {'type': 'integer'}
                          for row in layout.tables.get('Components', ())
                          if 'max_passes_key' in row and in_component(row['ID'])}
    return fields


def component_controller(component, *, maximum=None):
    if maximum is None:
        maximum = (component['max_passes_default'] if 'max_passes_default' in component
                   else int(component['Max passes']))
    return {'guard': component['Guard'], 'max_passes': maximum,
            'accepted': component['Guard'] != '-',
            'retry': component['Guard'] != '-' and maximum > 1,
            'exhaustion': component['Guard'] != '-'}


def component_decision(controller, accepted, attempt):
    if accepted or not controller['accepted']:
        return 'accepted'
    return 'retry' if attempt < controller['max_passes'] else 'exhausted'
