"""Shared presentation for unit, integration and E2E test runs."""
from rich.text import Text


def elapsed_time(seconds):
    """Shared compact elapsed/deadline units for live test presentation."""
    if 0 < seconds < 1:
        return f'{seconds:.2g}s'
    minutes, seconds = divmod(int(seconds), 60)
    return (f'{minutes}m' + (f' {seconds}s' if seconds else '')) if minutes else f'{seconds}s'


def completed_summary(content, elapsed):
    """Append the frozen whole-operation clock to the first summary line."""
    content = content.plain if isinstance(content, Text) else content
    first, separator, rest = content.partition('\n')
    return first.rstrip('.') + f' — {elapsed_time(elapsed)} total.' + separator + rest


def compact_cost(value):
    if value is None:
        return '—'
    if 0 < value < .000001:
        return '<0.000001'
    return f'{value:.6f}' if value < 100 else f'{value:.2f}'


def component_timing(components):
    """Show leaf clocks, or their nearest timed ancestor for untimed work."""
    components = tuple(components)
    by_id = {component.id: component for component in components}
    parents = {component.parent_id for component in components}
    leaves = [component for component in components if component.id not in parents]
    visible = {}
    for leaf in leaves or components:
        component, visited = leaf, set()
        while component.timeout is None and component.parent_id in by_id and component.id not in visited:
            visited.add(component.id)
            component = by_id[component.parent_id]
        displayed = component if component.timeout is not None else leaf
        visible[displayed.id] = displayed
    return '; '.join(f'{elapsed_time(int(component.elapsed))} elapsed'
                     + (f'; timeout {elapsed_time(component.timeout)}' if component.timeout is not None else '')
                     + f' — {component.name}'
                     for component in visible.values())


def literal_cell(value):
    """Bracket-bearing data is literal text, never Rich markup."""
    return Text(value) if isinstance(value, str) and "[" in value else value


def progress_summary(mode, rows):
    statuses = [row[0] for row in rows.values()]
    passed = statuses.count('PASSED')
    failed = sum(status in {'FAILED', 'ERROR'} for status in statuses)
    return Text.assemble(f'Running selected {mode} tests… ',
                         (f'{passed} passed', 'green'), ' / ',
                         (f'{failed} failed', 'red' if failed else ''), ' / ',
                         (f"{statuses.count('SKIPPED')} skipped", 'dark_orange' if 'SKIPPED' in statuses else ''), ' / ',
                         f"{statuses.count('RUNNING')} running / {statuses.count('PENDING')} pending")


def ordered_result_rows(rows, *, completed):
    result = list(rows.items())
    if completed:
        result.sort(key=lambda row: 0 if row[1][0] in {'FAILED', 'ERROR'}
                    else 1 if row[1][0] == 'SKIPPED' else 2)
    return result


def reconcile_results_table(table, rows, *, completed):
    """Keep the mounted table; replace rows only when identities/order change."""
    keys = [str(table.get_row_at(i)[1]) for i in range(table.row_count)]
    if keys != [key for key, _ in rows]:
        table.clear()
        for key, cells in rows:
            table.add_row(*(literal_cell(cell) for cell in cells), key=key)
    else:
        for key, cells in rows:
            for column, value in zip(table.columns, cells):
                table.update_cell(key, column, literal_cell(value))
    if completed:
        table.move_cursor(row=0)


def styled_summary(content, *, failed, skipped=False, neutral_details=False, green_prefixes=(), red_prefixes=()):
    """Mode-specific accounting supplies text; shared rendering applies colors."""
    summary = Text()
    plain = content.plain if isinstance(content, Text) else content
    for index, line in enumerate(plain.splitlines()):
        if index:
            summary.append('\n')
        style = ('green' if any(line.startswith(prefix) for prefix in green_prefixes)
                 else 'red' if any(line.startswith(prefix) for prefix in red_prefixes)
                 else '' if neutral_details and index > 0
                 else 'red' if failed else 'dark_orange' if skipped else 'green')
        summary.append(line, style=style)
    return summary
