"""Bounded, data-only conditions for declared workflow edges."""
import ast
from collections.abc import Mapping
from dataclasses import dataclass
import re


_NATIVE_ID = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_SCALAR_TYPES = frozenset(('string', 'integer', 'number', 'boolean', 'null'))


def native_condition_id(text):
    return _NATIVE_ID.fullmatch(text) is not None


class PredicateError(ValueError):
    pass


@dataclass(frozen=True)
class Predicate:
    text: str
    tree: ast.Expression

    def evaluate(self, values):
        return _evaluate(self.tree.body, values)


def parse_predicate(text, fields):
    """Admit a small Boolean expression over known object fields."""
    if len(text) > 256:
        raise PredicateError('condition exceeds 256 characters')
    try:
        tree = ast.parse(text, mode='eval')
    except SyntaxError as error:
        raise PredicateError('invalid condition syntax') from error
    nodes = tuple(ast.walk(tree))
    if len(nodes) > 40:
        raise PredicateError('condition exceeds 40 syntax nodes')

    def check(node, depth=0):
        if depth > 8:
            raise PredicateError('condition nesting exceeds 8 levels')
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)) and len(node.values) <= 8:
            for item in node.values:
                check(item, depth + 1)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            check(node.operand, depth + 1)
        elif isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            for item in (node.left, node.comparators[0]):
                check_value(item)
        else:
            raise PredicateError('condition requires bounded Boolean comparisons')

    def check_value(node):
        if isinstance(node, ast.Constant) and (node.value is None or type(node.value) in (str, int, float, bool)):
            return
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and isinstance(node.slice, ast.Constant) and type(node.slice.value) is str):
            contract = fields.get(node.value.id, {}).get(node.slice.value) if fields is not None else {'type': 'string'}
            if isinstance(contract, Mapping):
                kinds = contract.get('type')
                if type(kinds) is str and kinds in _SCALAR_TYPES:
                    return
                if type(kinds) in (list, tuple) and kinds and all(type(kind) is str and kind in _SCALAR_TYPES for kind in kinds):
                    return
        raise PredicateError('condition may read only declared scalar output fields')

    check(tree.body)
    return Predicate(text, tree)


def _evaluate(node, values):
    if isinstance(node, ast.BoolOp):
        results = (_evaluate(item, values) for item in node.values)
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.UnaryOp):
        return not _evaluate(node.operand, values)
    left = _value(node.left, values)
    right = _value(node.comparators[0], values)
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


def _value(node, values):
    if isinstance(node, ast.Constant):
        return node.value
    owner, field = node.value.id, node.slice.value
    if owner not in values or not isinstance(values[owner], dict) or field not in values[owner]:
        raise PredicateError(f'condition field {owner}[{field!r}] was not produced')
    return values[owner][field]


def edge_label(when):
    """Canonical admitted label used by runtime metadata and renderers."""
    return when


def predicate_fields(layout, source):
    """Scalar output fields visible at a routing source, including bound START fields."""
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
    return fields


def component_controller(component):
    """Pure pass-controller metadata; no synthetic executable graph node."""
    maximum = int(component['Max passes'])
    return {'guard': component['Guard'], 'max_passes': maximum,
            'accepted': component['Guard'] != '-',
            'retry': component['Guard'] != '-' and maximum > 1,
            'exhaustion': component['Guard'] != '-'}


def component_decision(controller, accepted, attempt):
    """Return the controller branch shared by execution and diagram semantics."""
    if accepted or not controller['accepted']:
        return 'accepted'
    return 'retry' if attempt < controller['max_passes'] else 'exhausted'
