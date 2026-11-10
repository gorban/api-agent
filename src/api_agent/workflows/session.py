"""Declared session fields and per-turn value bindings for the binding grammar.

Parser-free, so source admission and compiled-plan loading share one contract.
"""

MERGES = ('replace', 'append')


def source_parts(source):
    """Split ``node.output_field`` at its first dot; output fields may contain dots."""
    node, _, name = source.partition('.')
    return node, name


def _types(schema):
    declared = schema.get('type')
    if declared is None:
        return None
    return {declared} if isinstance(declared, str) else set(declared)


def _overlap(left, right):
    if left is None or right is None:
        return True
    numeric = {'integer', 'number'}
    return bool(left & right or left & numeric and right & numeric)


def problem(tables):
    """Return ``(message, line)`` for the first invalid Session row or SESSION/TURN binding.

    ``None`` means the Session table and every SESSION and TURN binding are consistent
    with the declared node schemas. Types are compared only where both sides declare one.
    """
    rows = tables.get('Session')
    functions = {row['ID']: row for row in tables['Functions']}
    declared, merges, seen = {}, {}, set()
    if rows is not None and not rows:
        return 'Session requires at least one row', 1
    for row in rows or ():
        field, source, merge, line = row['Field'], row['Source'], row['Merge'], row['line']
        node, name = source_parts(source)
        if not field.isidentifier():
            return 'invalid session field name', line
        if merge not in MERGES:
            return 'invalid session merge; use replace or append', line
        if not node or not name:
            return 'invalid session source; use node.field', line
        if node not in functions:
            return 'unknown session source node', line
        properties = functions[node]['Output schema'].get('properties', {})
        if name not in properties:
            return 'unknown session source output field', line
        if (field, source) in seen:
            return 'duplicate session source', line
        seen.add((field, source))
        if merges.setdefault(field, merge) != merge:
            return 'session field declares conflicting merges', line
        value = _types(properties[name])
        if merge == 'append':
            if value is not None and 'array' not in value:
                return 'append session source must be an array', line
            value = {'array'}
        declared.setdefault(field, []).append(value)
    for row in tables['Bindings']:
        if row['From'] not in ('SESSION', 'TURN'):
            continue
        turn = row['From'] == 'TURN'
        field = row['Field']
        if turn:
            if field == '*':
                return 'TURN requires a named field', row['line']
            produced = [_types(function['Output schema']['properties'][field])
                        for function in functions.values() if field in function['Output schema'].get('properties', {})]
            if not produced:
                return 'no node declares turn field', row['line']
        elif field not in declared:
            return 'undeclared session field', row['line']
        else:
            produced = declared[field]
        target = _types(functions[row['Node']]['Input schema']['properties'][row['Input']])
        if not all(_overlap(value, target) for value in produced):
            return f'{"turn" if turn else "session"} value type does not match the input schema', row['line']
    return None


def writes(rows, order, values):
    """Yield ``(row, value)`` for each source that produced output, in execution order.

    ``order`` maps node IDs to flow positions; rows of one node keep declaration order.
    """
    for row in sorted(rows, key=lambda item: order[source_parts(item['Source'])[0]]):
        node, name = source_parts(row['Source'])
        output = values.get(node)
        if output is not None and name in output:
            yield row, output[name]
