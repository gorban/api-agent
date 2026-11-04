"""Unit-only applicability outcomes remain neutral and distinct from skips."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import RunnerEvent, TestResult, format_test_summary
from api_agent.testing.terminal_progress import TerminalTestProgress
from api_agent.tui.test_results import ordered_result_rows, progress_summary, test_details as details
from test_tui import fixture


def result(status, index=0, reason='Requires a different host platform'):
    identity = TestIdentity(f'base::test_{index}.py::test_case', 'base', f'test_{index}.py', 'test_case')
    return TestResult(identity, status, traceback=reason if status == 'not_applicable' else '')


@pytest.mark.parametrize('statuses,expected', [
    (['passed', 'not_applicable'], 'PASS — 1/1 passed; 1 not applicable.'),
    (['not_applicable'], 'NOT APPLICABLE — 0 applicable; 1 not applicable.'),
    (['passed', 'skipped', 'not_applicable'], 'FAIL — 1/2 passed; 1 did not pass; 1 not applicable.'),
    (['failed', 'not_applicable'], 'FAIL — 0/1 passed; 1 did not pass; 1 not applicable.'),
])
def test_unit_applicable_denominator_and_neutral_only_selection(statuses, expected):
    assert format_test_summary('unit', [result(status, i) for i, status in enumerate(statuses)]) == 'Unit results: ' + expected


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_nonunit_does_not_accept_applicability_status(mode):
    summary = format_test_summary(mode, [result('passed'), result('not_applicable', 1)])
    assert summary == f'{mode.capitalize()} results: FAIL — 1/2 passed (50.00%).'


def test_terminal_applicability_reason_is_quoted_scrubbed_and_bounded():
    row = result('not_applicable', reason='different\nhost [sensitive]')
    lines = []
    progress = TerminalTestProgress([row.identity], lines.append, scrub=lambda value: value.replace('[sensitive]', '[redacted]'))
    event = RunnerEvent('test_finished', identity=row.identity, status=row.status, traceback=row.traceback)
    progress(event)
    progress(event)
    assert len(lines) == 1
    assert '— not_applicable ' in lines[0]
    assert 'different\\nhost [redacted]' in lines[0]
    assert '\n' not in lines[0]


def test_unit_progress_and_details_show_neutral_applicability():
    rows = {'pass': ('PASSED', '', ''), 'na': ('NOT_APPLICABLE', '', ''), 'skip': ('SKIPPED', '', ''), 'fail': ('FAILED', '', '')}
    text = progress_summary('unit', rows)
    assert '1 not applicable' in text.plain
    start = text.plain.index('1 not applicable')
    assert not any(span.start <= start < span.end and span.style in {'green', 'red', 'dark_orange'} for span in text.spans)
    assert [key for key, _ in ordered_result_rows(rows, completed=True)] == ['fail', 'skip', 'na', 'pass']
    assert [key for key, _ in ordered_result_rows(rows, completed=False)] == list(rows)
    body = details(result('not_applicable'), {}, {})
    assert 'Requires a different host platform' in body.plain
    assert not any(span.style in {'green', 'red', 'dark_orange'} for span in body.spans)
    assert 'not applicable' not in progress_summary('integration', rows).plain


@pytest.mark.parametrize('exit_code', [0, 1])
@pytest.mark.parametrize('live', [False, True])
def test_app_opens_neutral_unit_applicability_reason(tmp_path, exit_code, live):
    async def scenario():
        app = fixture(tmp_path)
        row = result('not_applicable')
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            app.mode = 'unit'
            app.unit_result = SimpleNamespace(tests=(row,), cancelled=False, exit_code=exit_code,
                require_full_coverage=False, output='', line_coverage=None, branch_coverage=None)
            app.unit_rows = {row.identity.id: ('NOT_APPLICABLE', row.identity.id, '0s')}
            app.show_unit_results(summary='Unit results: NOT APPLICABLE — 0 applicable; 1 not applicable.')
            await pilot.pause()
            header = app.screen.query_one('#body', Static).content
            assert any(span.style == 'red' for span in header.spans) is bool(exit_code)
            assert not any(span.style == 'green' for span in header.spans)
            if live:
                app.finished_test_results[row.identity.id] = row
                app.unit_result = None
            app.toggle_row(row.identity.id)
            await pilot.pause()
            assert app.screen.heading == 'Unit test not applicable'
            assert row.traceback in app.screen.query_one('#body', Static).content.plain
    asyncio.run(scenario())


def test_applicability_without_reason_uses_neutral_status_label():
    body = details(result('not_applicable', reason=''), {}, {})
    assert 'NOT APPLICABLE' in body.plain
    assert not any(span.style in {'green', 'red', 'dark_orange'} for span in body.spans)
