"""Native tab traversal includes meaningful controls, not their scroll containers."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Input, Static

from api_agent.testing import TestIdentity as Identity
from test_tui import fixture


async def assert_native_focus_cycle(app, pilot, controls):
    assert app.screen.focus_chain == controls
    controls[0].focus()
    await pilot.press('shift+tab')
    assert app.focused is controls[-1]
    await pilot.press('tab')
    assert app.focused is controls[0]
    for control in controls:
        assert app.focused is control
        await pilot.press('tab')
    assert app.focused is controls[0]


@pytest.mark.parametrize('mode', [None, 'run-local', 'unit', 'integration', 'e2e',
                                 'allure', 'cleanup-containers'])
def test_operation_screens_have_no_container_tab_stops(tmp_path, mode):
    async def scenario():
        identity = Identity('base::test_example.py::test_one', 'base',
                            'test_example.py', 'test_example.py::test_one')
        app = fixture(tmp_path, mode, collector=lambda *args: [identity])
        app.container_runtime.inventory = lambda: (SimpleNamespace(
            id='owned-container', name='Owned test prerequisite', image='example', status='running'),)
        async with app.run_test(size=(75, 22)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            controls = [widget for widget in app.screen.query('DataTable, Button')
                        if not widget.disabled]
            assert controls
            assert not app.screen.query_one('#workbench-content').can_focus
            if mode in ('unit', 'integration', 'e2e', 'cleanup-containers'):
                assert isinstance(controls[0], DataTable)
                assert any(isinstance(widget, Button) for widget in controls)
            else:
                assert all(isinstance(widget, Button) for widget in controls)
            await assert_native_focus_cycle(app, pilot, controls)
            await pilot.resize_terminal(60, 16)
            await pilot.pause()
            await assert_native_focus_cycle(app, pilot, controls)
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', [None, 'config-cleanup'])
def test_configuration_tables_are_the_only_tab_target(tmp_path, mode):
    async def scenario():
        app = fixture(tmp_path, mode, fields={'COUNT': {'type': 'int', 'default': 4}})
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            await assert_native_focus_cycle(app, pilot, [table])
    asyncio.run(scenario())


@pytest.mark.parametrize('help_text', ['[red]Human-readable purpose[/red]\nAdditional details.', '', '  '])
def test_configuration_edit_displays_literal_description_above_type(tmp_path, help_text):
    async def scenario():
        app = fixture(tmp_path, fields={'VALUE': {'type': 'str', 'default': 'existing',
                                                'help': help_text}})
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            await pilot.press('e')
            await pilot.pause()
            screen = app.screen
            assert screen.kind == 'input'
            assert screen.query_one(Input).value == 'existing'
            assert str(screen.query_one('#body', Static).content).startswith('Declared type: str.')
            if help_text.strip():
                description = screen.query_one('#config-description', Static)
                assert str(description.content) == help_text
                assert str(description.render()) == help_text
                assert description.region.y - screen.query_one('#heading').region.bottom == 1
                assert screen.query_one('#body').region.y - description.region.bottom == 1
            else:
                assert not screen.query('#config-description')
            await assert_native_focus_cycle(app, pilot, [screen.query_one(Input), screen.query_one(Button)])
            await pilot.press('escape')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert not (tmp_path / '.env').exists()
    asyncio.run(scenario())


def test_standalone_failure_details_retain_keyboard_scroll_target(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            app.show('test-failure', 'Failure', '\n'.join(f'Traceback line {i}' for i in range(80)))
            await pilot.pause()
            pane = app.screen.query_one(VerticalScroll)
            assert app.screen.focus_chain == [pane]
            pane.focus()
            await pilot.press('j')
            await pilot.pause()
            assert pane.scroll_y > 0
    asyncio.run(scenario())
