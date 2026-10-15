"""Readonly selections reuse copy handling without invoking a real clipboard."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import DataTable, Input, Select, Static
from textual.widgets._footer import FooterKey

from api_agent.tui.app import SettingHelpDialog
from test_tui import fixture
from test_auth_setup import WizardApp


@pytest.mark.parametrize('view', ['review-help', 'cleanup-help', 'setting-edit', 'manager-edit'])
@pytest.mark.parametrize('platform,key,label', [('linux', 'ctrl+c', '^c'), ('win32', 'ctrl+c', '^c'), ('darwin', 'super+c', '⌘c')])
def test_readonly_selection_copies_exact_text_and_keeps_editor_state(tmp_path, monkeypatch, view, platform, key, label):
    async def scenario():
        description = 'Use https://openrouter.ai/api/v1 for a matching personal key.'
        app = (WizardApp(tmp_path) if view == 'manager-edit' else
               fixture(tmp_path, 'config-cleanup' if view == 'cleanup-help' else None,
                       fields={'VALUE': {'type': 'str', 'default': 'unchanged', 'help': description}}))
        copied = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copied)
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            if view == 'manager-edit':
                app.screen.query_one('#auth-manager-table', DataTable).focus()
                await pilot.press('e')
                await pilot.pause()
                app.screen.query_one('#auth-manager', Select).value = '1password'
                await pilot.pause()
                selector, editable = '#auth-implementation', '#auth-path'
            elif view == 'setting-edit':
                await pilot.press('e')
                await pilot.pause()
                selector, editable = '#config-description', '#config-value'
            else:
                await pilot.press('?')
                await pilot.pause()
                assert isinstance(app.screen, SettingHelpDialog)
                selector, editable = '#setting-help-text', None
            screen = app.screen
            monkeypatch.setattr('api_agent.tui.app.sys.platform', platform)
            notify = Mock()
            monkeypatch.setattr(app, 'notify', notify)
            screen.refresh_bindings()
            before_values = app.session.result(_review=True).values.copy()
            input_value = screen.query_one(editable, Input).value if editable else None
            assert not screen.check_action('copy_selected_text' if platform == 'darwin' else 'copy_value', ())
            # Actual mouse selection, not a mocked selection getter.
            expected = (str(screen.query_one(selector, Static).content) if view == 'manager-edit'
                        else 'https://openrouter.ai/api/v1')
            # Select the entire implementation path even from a deeply nested checkout.
            await pilot.resize_terminal(max(150, len(expected) + 40), 40)
            await pilot.pause()
            start = 0 if view == 'manager-edit' else 4
            end = start + len(expected) - 1
            await pilot.mouse_down(selector, offset=(start, 0))
            await pilot.hover(selector, offset=(end, 0))
            await pilot.mouse_up(selector, offset=(end, 0))
            await pilot.pause()
            selected = screen.get_selected_text()
            assert selected == expected
            if platform == 'darwin':
                await pilot.press('ctrl+c')
                copied.assert_not_awaited()
                if view == 'setting-edit':
                    assert any(call.kwargs.get('title') == 'Do you want to quit?' for call in notify.call_args_list)
                assert screen.get_selected_text() == selected
            hints = [hint for hint in screen.query(FooterKey) if hint.description == 'Copy value']
            assert len(hints) == 1 and hints[0].key_display == label
            await pilot.press(key)
            copied.assert_awaited_once_with(app, selected)
            assert screen.get_selected_text() is None and app.screen is screen
            assert app.session.result(_review=True).values == before_values
            if editable:
                assert screen.query_one(editable, Input).value == input_value
            else:
                assert screen.query_one('#setting-help-close').has_focus
            assert not screen.check_action('copy_selected_text' if platform == 'darwin' else 'copy_value', ())
            assert not (tmp_path / '.env').exists()
    asyncio.run(scenario())


def test_popup_copy_without_selection_is_inert_and_error_keeps_selection(tmp_path, monkeypatch):
    async def scenario():
        from api_agent.tui.clipboard import ClipboardError
        app = fixture(tmp_path, fields={'VALUE': {'type': 'str', 'default': 'value', 'help': 'Literal description'}})
        copied = AsyncMock(side_effect=ClipboardError('not displayed'))
        monkeypatch.setattr('api_agent.tui.app.copy_text', copied)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press('?')
            await pilot.pause()
            screen = app.screen
            await screen.action_copy_value()
            copied.assert_not_awaited()
            await pilot.mouse_down('#setting-help-text', offset=(0, 0))
            await pilot.hover('#setting-help-text', offset=(6, 0))
            await pilot.mouse_up('#setting-help-text', offset=(6, 0))
            selected = screen.get_selected_text()
            assert selected == 'Literal'
            notify = Mock()
            monkeypatch.setattr(app, 'notify', notify)
            await pilot.press('ctrl+c')
            copied.assert_awaited_once_with(app, selected)
            assert screen.get_selected_text() == selected
            notify.assert_called_once_with('Could not copy the value.', severity='error', timeout=2)
            await pilot.press('escape')
            assert not screen.check_action('copy_value', ())
    asyncio.run(scenario())
