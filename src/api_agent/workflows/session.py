"""Declared session fields and per-turn value bindings for the binding grammar.

Parser-free, so source admission and compiled-plan loading share one contract.
"""

MERGES = ('replace', 'append')
COMMITS = ('passed', 'partial')


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
    """Return ``(message, row)`` for the first invalid Session row or SESSION/TURN binding.

    ``row`` is the offending table row, so a caller can name the document that declares it.

    ``None`` means the Session table and every SESSION and TURN binding are consistent
    with the declared node schemas. Types are compared only where both sides declare one.
    """
    rows = tables.get('Session')
    functions = {row['ID']: row for row in tables['Functions']}
    declared, merges, seen = {}, {}, set()
    if rows is not None and not rows:
        return 'Session requires at least one row', {'line': 1}
    for row in rows or ():
        field, source, merge = row['Field'], row['Source'], row['Merge']
        node, name = source_parts(source)
        if not field.isidentifier():
            return 'invalid session field name', row
        if merge not in MERGES:
            return 'invalid session merge; use replace or append', row
        if not node or not name:
            return 'invalid session source; use node.field', row
        if node not in functions:
            return 'unknown session source node', row
        properties = functions[node]['Output schema'].get('properties', {})
        if name not in properties:
            return 'unknown session source output field', row
        if (field, source) in seen:
            return 'duplicate session source', row
        seen.add((field, source))
        if merges.setdefault(field, merge) != merge:
            return 'session field declares conflicting merges', row
        value = _types(properties[name])
        if merge == 'append':
            if value is not None and ('array' not in value or not value <= {'array', 'null'}):
                return 'append session source must be an array', row
            value = {'array'}
        declared.setdefault(field, []).append(value)
    invalid = _commit_problem(tables.get('Session Commit'), rows)
    if invalid is not None:
        return invalid
    for row in tables['Bindings']:
        if row['From'] not in ('SESSION', 'TURN'):
            continue
        turn = row['From'] == 'TURN'
        field = row['Field']
        if turn:
            if field == '*':
                return 'TURN requires a named field', row
            produced = [_types(function['Output schema']['properties'][field])
                        for function in functions.values() if field in function['Output schema'].get('properties', {})]
            if not produced:
                return 'no node declares turn field', row
        elif field not in declared:
            return 'undeclared session field', row
        else:
            produced = declared[field]
        target = _types(functions[row['Node']]['Input schema']['properties'][row['Input']])
        if not all(_overlap(value, target) for value in produced):
            return f'{"turn" if turn else "session"} value type does not match the input schema', row
    return None


def _commit_problem(commit, session):
    """The first invalid ``Session Commit`` row as ``(message, row)``, else ``None``."""
    if commit is None:
        return None
    if session is None:
        return 'Session Commit requires a Session table', commit[0] if commit else {'line': 1}
    if not commit:
        return 'Session Commit requires at least one row', {'line': 1}
    seen = set()
    for row in commit:
        if row['Outcome'] not in COMMITS:
            return 'session commit outcome must be passed or partial', row
        if row['Outcome'] in seen:
            return 'duplicate session commit outcome', row
        seen.add(row['Outcome'])
    if 'passed' not in seen:
        return 'session commit must list passed', commit[-1]
    return None


def commit_outcomes(tables):
    """The turn outcomes that save the session: the declared ``Session Commit`` rows, else ``passed``."""
    return tuple(row['Outcome'] for row in tables.get('Session Commit', ())) or ('passed',)


def writes(rows, runs):
    """Yield ``(row, value)`` for each completed run of a source node, in completion order.

    ``runs`` lists ``(node, output)`` for every run that completed and was not discarded
    by a rejected guarded pass; a node that looped appears once per run. A run's rows keep
    declaration order, and a run whose output omits the field writes nothing.
    """
    by_node = {}
    for row in rows:
        node, name = source_parts(row['Source'])
        by_node.setdefault(node, []).append((row, name))
    for node, output in runs:
        for row, name in by_node.get(node, ()):
            if name in output:
                yield row, output[name]
