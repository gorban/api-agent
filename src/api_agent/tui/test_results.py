"""Shared presentation for unit, integration and E2E test runs."""
from rich.text import Text


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
                         f"{statuses.count('SKIPPED')} skipped / ",
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


def styled_summary(content, *, failed, neutral_details=False, green_prefixes=()):
    """Mode-specific accounting supplies text; shared rendering applies colors."""
    summary = Text()
    plain = content.plain if isinstance(content, Text) else content
    for index, line in enumerate(plain.splitlines()):
        if index:
            summary.append('\n')
        style = ('green' if any(line.startswith(prefix) for prefix in green_prefixes)
                 else '' if neutral_details and index > 0
                 else 'red' if failed else 'green')
        summary.append(line, style=style)
    return summary
