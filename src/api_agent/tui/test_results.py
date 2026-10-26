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


def test_details(result, usage, cost, *, historical_tokens=False, accounting_pending=False):
    from api_agent.telemetry.usage_output import tokens_text, displayed_cost, historical_average, QUOTA_RESET_NOTE
    from api_agent.telemetry.estimation import quota_reset_observed
    local_diagnostic = getattr(result, 'local_diagnostic', '')
    details = local_diagnostic or result.traceback or (
        'ERROR — No failure diagnostic was captured for this test.'
        if result.status == 'error' else result.status.upper())
    failure = bool(local_diagnostic) or result.status in {'failed', 'error', 'broken'}
    lines = [result.identity.id, '']
    if failure:
        lines.extend((details, ''))
    lines.append(f'Execution time: {result.duration:.3f}s')
    if accounting_pending:
        lines.append('Accounting is pending suite finalization; reopen details after completion for final costs and tokens.')
    else:
        lines.extend([f"This test cost: {displayed_cost(cost.get('estimated_cost_usd'), usage)}",
             f"  Historical average: {displayed_cost(historical_average((cost,)), usage)}"])
        if historical_tokens:
            lines.append('Earlier compatible runs have recorded token usage.')
        if quota_reset_observed(usage):
            lines.append(QUOTA_RESET_NOTE)
        if cost.get('basis') and cost['basis'] != 'unavailable':
            lines.append(f"Cost basis: {cost['basis']}")
        lines.append(tokens_text(usage, result.identity.id))
    if not failure:
        lines.extend(('', details))
    text = Text('\n'.join(lines))
    if not (local_diagnostic or result.traceback):
        text.stylize('green' if result.status == 'passed' else 'dark_orange'
                     if result.status == 'skipped' else 'red',
                     len(result.identity.id) + 2 if failure else len(text) - len(details),
                     len(result.identity.id) + 2 + len(details) if failure else len(text))
    return text


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


def append_usage_line(text, line, *, style=''):
    """Keep accounting data literal; only a confirmed quota error is red."""
    prefix, marker, error = line.partition('quota used: Error: ')
    if marker:
        text.append(prefix + 'quota used: ', style=style)
        text.append('Error: ' + error, style='red')
    else:
        text.append(line, style=style)


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
    keys = [row.key.value for row in table.ordered_rows]
    selected = keys[table.cursor_row] if keys else None
    scroll_y = table.scroll_y
    first_completion = completed and not getattr(table, 'results_completed', False)
    new_keys = [key for key, _ in rows]
    if keys != new_keys:
        table.clear()
        for key, cells in rows:
            table.add_row(*(literal_cell(cell) for cell in cells), key=key)
    else:
        for key, cells in rows:
            for column, value in zip(table.columns, cells):
                table.update_cell(key, column, literal_cell(value))
    if first_completion or selected not in new_keys:
        table.move_cursor(row=0)
    else:
        table.move_cursor(row=new_keys.index(selected), scroll=False)
        table.scroll_to(y=scroll_y, animate=False)
    table.results_completed = completed


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
        append_usage_line(summary, line, style=style)
    return summary


def details_belong_to_run(screen, result, lifecycle):
    previous, _, _ = getattr(screen, 'result_return', (None, None, None))
    return (previous is not None and previous is result or
            previous is None and lifecycle is not None
            and getattr(screen, 'result_lifecycle', None) is lifecycle)
