"""Failure navigation keeps the selected test identity across shared result modes."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable

from api_agent.testing import TestIdentity as Identity
from api_agent.testing.runner import UnitRunResult
from test_tui import fixture


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_failure_return_preserves_selected_identity(tmp_path, mode):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.mode = mode
            tests = tuple(SimpleNamespace(identity=Identity(f'test_{i}', 'base', 'file.py', f'test_{i}'),
                          status=status, duration=1, traceback=f'Failure {i}')
                          for i, status in enumerate(('failed', 'error')))
            app.unit_result = UnitRunResult(1, tests, (), '')
            app.unit_rows = {test.identity.id: (test.status.upper(),
                             test.identity.id, '1s') for test in tests}
            app.show_unit_results()
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.focus()
            await pilot.press('down', 'enter')
            await pilot.pause()
            assert app.screen.kind == 'test-failure'
            assert 'Failure 1' in app.screen.body.plain
            await pilot.press('c')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.ordered_rows[table.cursor_row].key.value == tests[1].identity.id
            assert table.has_focus
            app.show_unit_results()
            await pilot.pause()
            assert table.cursor_row == 1
            await pilot.press('up', 'enter')
            await pilot.pause()
            assert 'Failure 0' in app.screen.body.plain
            await pilot.press('c')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.cursor_row == 0
            table.move_cursor(row=1)
            app.unit_rows = dict(reversed(tuple(app.unit_rows.items())))
            app.show_unit_results()
            await pilot.pause()
            assert table.ordered_rows[table.cursor_row].key.value == tests[1].identity.id
            app.unit_rows.pop(tests[1].identity.id)
            app.show_unit_results()
            await pilot.pause()
            assert table.cursor_row == 0
            app.unit_rows = {test.identity.id: (test.status.upper(), test.identity.id, '1s')
                             for test in tests}
            app.show_unit_results()
            await pilot.pause()
            table.move_cursor(row=1)
            app.unit_result = UnitRunResult(1, tests, (), 'Replacement run')
            app.show_unit_results()
            await pilot.pause()
            assert table.cursor_row == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('remove_viewed', [False, True])
def test_detail_return_restores_scroll_or_reveals_missing_identity_fallback(tmp_path, remove_viewed):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.mode = 'unit'
            tests = tuple(SimpleNamespace(identity=Identity(f'test_{i}', 'base', 'file.py', f'test_{i}'),
                          status='failed', duration=1, traceback=f'Failure {i}') for i in range(40))
            app.unit_result = UnitRunResult(1, tests, (), '')
            app.unit_rows = {test.identity.id: ('FAILED', test.identity.id, '1s') for test in tests}
            app.show_unit_results()
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=30)
            table.focus()
            await pilot.pause()
            saved_scroll = table.scroll_y
            assert saved_scroll > 0
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen.kind == 'test-failure'
            assert 'Failure 30' in app.screen.body.plain
            if remove_viewed:
                app.unit_rows.pop(tests[30].identity.id)
            await pilot.press('c')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.has_focus
            assert table.cursor_row == (0 if remove_viewed else 30)
            assert table.scroll_y == (0 if remove_viewed else saved_scroll)
            app.show_unit_results()
            await pilot.pause()
            assert table.cursor_row == (0 if remove_viewed else 30)
            assert table.scroll_y == (0 if remove_viewed else saved_scroll)
            table.move_cursor(row=30)
            await pilot.pause()
            app.unit_result = UnitRunResult(1, tests, (), 'New run')
            app.show_unit_results()
            await pilot.pause()
            assert table.cursor_row == 0
            assert table.scroll_y == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_passed_local_diagnostic_opens_without_token_history(tmp_path, mode):
    from api_agent.testing.contracts import TestResult
    from api_agent.telemetry.usage_output import has_tokens

    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.mode = mode
            tests = tuple(TestResult(Identity(f'passed_{i}', 'base', 'file.py', f'passed_{i}'),
                          'passed', 1, local_diagnostic='RuntimeError: synthetic cleanup failure')
                          for i in range(2))
            app.unit_result = UnitRunResult(0, tests, (), '')
            app.unit_rows = {test.identity.id: ('PASSED', test.identity.id, '1s') for test in tests}
            assert app.cost_estimator is None
            assert not has_tokens(app.suite_usage, tests[1].identity.id)
            app.show_unit_results()
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.focus()
            await pilot.press('down', 'enter')
            await pilot.pause()
            assert app.screen.kind == 'test-failure'
            assert 'RuntimeError: synthetic cleanup failure' in app.screen.body.plain
            await pilot.press('c')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.ordered_rows[table.cursor_row].key.value == tests[1].identity.id
            assert table.has_focus
            assert tests[1].status == 'passed'
    asyncio.run(scenario())
