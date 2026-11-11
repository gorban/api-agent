"""Bounded, data-only conditions for declared workflow edges."""
import ast
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import io
import re
import tokenize
import unicodedata

_NATIVE_ID = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_SCALAR_TYPES = frozenset(('string', 'integer', 'number', 'boolean', 'null'))
_CONTEXT = {'attempt': {'type': 'integer'}, 'max_passes': {'type': 'integer'}}
_SOURCE = '__route_source__'
_CONSTANTS = '__route_constants__'
_VISITS = '__route_visits__'
_EXPIRING = '__route_expiring__'
_EXITS = '__route_exits__'
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
    steps: frozenset = frozenset()
    expiring: frozenset = frozenset()

    def evaluate(self, values, *, attempt=1, max_passes=1, constants=None, visits=None, expired=None):
        """``visits`` maps a step ID to its start count (an unstarted step reads 0) and ``expired`` to
        whether its latest run ended at its own declared timeout (an unstarted step reads false)."""
        counters = {('attempt', step): (visits or {}).get(step, 0) for step in self.steps}
        counters.update({('expired', step): bool((expired or {}).get(step)) for step in self.expiring})
        return _evaluate(self.tree.body, values, self.source_owners,
                         {'attempt': attempt, 'max_passes': max_passes}, constants or {}, counters)


def _normalize(text):
    """Give a leading dot a parseable owner without touching quoted strings."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError) as error:
        raise PredicateError('invalid condition syntax') from error
    result = []
    previous = None
    for token in tokens:
        if token.type == tokenize.NAME and unicodedata.normalize('NFKC', token.string) in (_SOURCE, _CONSTANTS, _VISITS, _EXPIRING, _EXITS):
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
    steps = frozenset() if fields is None else frozenset(fields.get(_VISITS, ()))
    expiring = frozenset() if fields is None else frozenset(fields.get(_EXPIRING, ()))
    exits = {} if fields is None else fields.get(_EXITS, {})

    def contract(node):
        owner, path = _reference(node)
        if path == ('attempt',) and owner in steps:
            return {'type': 'integer'}
        if path == ('expired',) and owner in steps:
            if owner not in expiring:
                raise PredicateError(f'{owner}.expired requires {owner} to declare its own Timeouts row')
            return {'type': 'boolean'}
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
        if rule is None and owner in exits:
            raise PredicateError(f'{owner}.{path[0]} must be declared with one type in the Output schema of every '
                                 f'node that can exit component {owner}: {", ".join(exits[owner]) or "none"}')
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
    return Predicate(text, tree, source_owners, steps, expiring)


def _evaluate(node, values, owners, context, constants, counters):
    if isinstance(node, ast.BoolOp):
        results = (_evaluate(item, values, owners, context, constants, counters) for item in node.values)
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.UnaryOp):
        return not _evaluate(node.operand, values, owners, context, constants, counters)
    if not isinstance(node, ast.Compare):
        result = _value(node, values, owners, context, constants, counters)
        if type(result) is not bool:
            raise PredicateError('condition Boolean field has invalid value')
        return result
    left = _value(node.left, values, owners, context, constants, counters)
    right = _value(node.comparators[0], values, owners, context, constants, counters)
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


def _value(node, values, owners, context, constants, counters):
    if isinstance(node, ast.Constant):
        return node.value
    owner, path = _reference(node)
    if len(path) == 1 and (path[0], owner) in counters:
        return counters[path[0], owner]
    if owner in context and not path:
        return context[owner]
    if not path and owner in constants:
        value = constants[owner]
        if type(value) is not int or not 0 <= value <= 99:
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


def condition_lines(when):
    """Split a condition before each top-level `or` for display; text is unchanged."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(when).readline))
    except (tokenize.TokenError, IndentationError):
        return [when]
    starts, depth = [0], 0
    for token in tokens:
        if token.type == tokenize.OP and token.string in '([{':
            depth += 1
        elif token.type == tokenize.OP and token.string in ')]}':
            depth -= 1
        elif token.type == tokenize.NAME and token.string == 'or' and depth == 0:
            starts.append(token.start[1])
    return [when[start:end].strip() for start, end in zip(starts, [*starts[1:], len(when)])]


def entry_source(scope):
    """The routing source of the ``START`` rows that enter ``scope``: a component ID, or ``None`` for the graph."""
    return 'START' if scope is None else f'START:{scope}'


def edge_source(row):
    """``(source, entering)`` for an Edges row: the key its routing group and conditions use, and
    the scope (``'GRAPH'`` or a component ID) a ``START`` row enters, else ``None``."""
    if row['From'] != 'START':
        return row['From'], None
    scope = row.get('_owner_id')
    return entry_source(scope), 'GRAPH' if scope is None else scope


def entry_problem(layout, row):
    """Why the ``START`` row ``row`` does not enter a direct child of its declaring document, else ``None``."""
    steps = {step.id: step for step in layout.steps}
    if row['To'] not in steps or steps[row['To']].parent != row.get('_owner_id'):
        return 'START must name a direct child of the document that declares it'
    return None


def component_exits(layout):
    """Map each component to the nodes that can exit it: a child with an edge to ``END`` (or the last
    child, which ends an implicit flow), through any component among them."""
    edges = layout.tables.get('Edges', ())
    routed = {row['From'] for row in edges}
    ending = {row['From'] for row in edges if row['To'] == 'END'}

    def leaves(component):
        children = [step for step in layout.steps if step.parent == component]
        exiting = [step for index, step in enumerate(children)
                   if step.id in ending or index == len(children) - 1 and step.id not in routed]
        return tuple(dict.fromkeys(leaf for step in exiting
                                   for leaf in (leaves(step.id) if step.kind == 'component' else (step.id,))))
    return {step.id: leaves(step.id) for step in layout.steps if step.kind == 'component'}


def _declared_type(rule):
    return rule.get('type') if isinstance(rule, Mapping) else None


def predicate_fields(layout, source, entering=None):
    """Output fields visible at a routing source, with unique source-relative paths.

    ``entering`` is ``'GRAPH'`` or a component ID when ``source`` is the ``START`` of that scope:
    only steps before the scope began are visible then. A finished component's result reads as
    ``<component>.<field>`` for each field every node that can exit it declares with one type.
    """
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

    if entering is None:
        boundary = max(index for index, step in enumerate(layout.steps)
                       if step.id == source or belongs_to(step.id, source))
        inside = source
    else:
        boundary = -1 if entering == 'GRAPH' else ordered.index(entering) - 1
        inside = None if entering == 'GRAPH' else entering
    fields = {'START': {}}
    for binding in layout.tables['Bindings']:
        if binding['From'] == 'START' and binding['Field'] != '*':
            target = functions[binding['Node']]['Input schema'].get('properties', {})
            fields['START'][binding['Field']] = target.get(binding['Input'], {})
    fields.update({key: row['Output schema'].get('properties', {}) for key, row in functions.items()
                   if ordered.index(key) <= boundary})
    exits = {}
    for component, leaves in component_exits(layout).items():
        # A component the routing source sits inside has not finished, so it has no result yet.
        if ordered.index(component) > boundary or inside in parents and belongs_to(inside, component):
            continue
        rules = [functions[leaf]['Output schema'].get('properties', {}) for leaf in leaves]
        fields[component] = {key: rule for key, rule in (rules[0].items() if rules else ())
                             if all(key in other and _declared_type(other[key]) == _declared_type(rule)
                                    for other in rules[1:])}
        exits[component] = leaves
    fields[_EXITS] = exits
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
        return inside == component or (inside in parents and belongs_to(inside, component))
    fields[_CONSTANTS] = {row['max_passes_key']: {'type': 'integer'}
                          for row in layout.tables.get('Components', ())
                          if 'max_passes_key' in row and in_component(row['ID'])}
    # A root Limits row is visible to every edge; a component's or node's own row only to edges
    # whose source is that step or inside it (scoped by the edge's source, not its document).
    fields[_VISITS] = {step.id: {'type': 'integer'} for step in layout.steps}
    step_ids = {step.id for step in layout.steps}
    fields[_EXPIRING] = {row['Owner'] for row in layout.tables.get('Timeouts', ()) if row['Owner'] in step_ids}
    fields[_CONSTANTS].update({row['Key']: {'type': 'integer'} for row in layout.tables.get('Limits', ())
                               if row.get('_owner_id') is None or in_component(row['_owner_id'])})
    return fields


def backward_bound(predicate, target, limits):
    """Find ``<target>.attempt < LIMIT`` (or ``<=``) among a condition's top-level conjuncts.

    ``limits`` names the declared Limits keys. Returns ``(key, inclusive)`` for the first
    such comparison, or ``None`` when the condition does not bound re-entering ``target``.
    """
    for node in _conjuncts(predicate.tree.body):
        if (isinstance(node, ast.Compare) and isinstance(node.ops[0], (ast.Lt, ast.LtE))
                and isinstance(node.left, ast.Attribute) and node.left.attr == 'attempt'
                and isinstance(node.left.value, ast.Name) and node.left.value.id == target
                and isinstance(node.comparators[0], ast.Name) and node.comparators[0].id in limits):
            return node.comparators[0].id, isinstance(node.ops[0], ast.LtE)
    return None


def _conjuncts(node):
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        for value in node.values:
            yield from _conjuncts(value)
    else:
        yield node


def _reads_expired(node, step):
    try:
        return _reference(node) == (step, ('expired',))
    except PredicateError:
        return False


@lru_cache(maxsize=1024)
def edge_expiry(when, step):
    """How an edge condition text uses ``<step>.expired``: ``'requires'`` when a top-level conjunct is
    exactly that flag, ``'reads'`` when it appears anywhere else, otherwise ``None``.

    Condition texts that do not parse as predicates (native condition IDs, ``always``) use nothing.
    """
    try:
        tree = ast.parse(_normalize(when), mode='eval').body
    except (PredicateError, SyntaxError):
        return None
    if any(_reads_expired(node, step) for node in _conjuncts(tree)):
        return 'requires'
    return 'reads' if any(_reads_expired(node, step) for node in ast.walk(tree)) else None


def unbounded_edge_message(target):
    return (f'backward edge to {target} requires a bounded When: a conjunction (and) containing '
            f'{target}.attempt < LIMIT or {target}.attempt <= LIMIT for a declared Limits key; '
            'other edges must advance')


def loop_edges(layout):
    """Yield ``(row, bound)`` for each declared edge back to the same or an earlier step.

    ``bound`` is ``backward_bound``'s ``(key, inclusive)``, or ``None`` when the edge's
    condition does not name a declared Limits key.
    """
    limit_keys = {row['Key'] for row in layout.tables.get('Limits', ())}
    position = {step.id: index for index, step in enumerate(layout.steps)}
    for row in layout.tables['Edges']:
        source, target, when = row['From'], row['To'], row['When']
        if source == 'START' or target == 'END' or position[target] > position[source]:
            continue
        bound = None
        if when not in ('always', 'otherwise') and not native_condition_id(when):
            bound = backward_bound(parse_predicate(when, predicate_fields(layout, source)), target, limit_keys)
        yield row, bound


def condition_constants(predicate):
    """Uppercase constant names a parsed condition reads, for declaration checks."""
    return frozenset(node.id for node in ast.walk(predicate.tree)
                     if isinstance(node, ast.Name) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', node.id))


def state_predicate_fields(properties, constants=()):
    """Typed-state fields visible to every route; `.field` reads the current state.

    ``constants`` names the declared integer limits a condition may compare against.
    """
    owners = {}
    def visit(rules, prefix=()):
        for key, rule in rules.items():
            owners[(*prefix, key)] = 'state'
            if _exact_type(rule, 'object'):
                visit(rule.get('properties', {}), (*prefix, key))
    visit(properties)
    return {'state': properties, _SOURCE: {'owners': owners},
            _CONSTANTS: {key: {'type': 'integer'} for key in constants}}


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
