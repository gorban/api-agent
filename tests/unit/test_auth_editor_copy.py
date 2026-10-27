"""Account descriptor edits copy through the shared clipboard adapter."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from textual.app import App
from textual.widgets import DataTable, Input
from textual.widgets._footer import FooterKey

from api_agent.tui.clipboard import ClipboardError
from api_agent.tui.auth_setup import AuthInput
from test_auth_setup import WizardApp


def test_unmasked_editor_input_retains_native_selected_text_copy(monkeypatch):
    class NativeInputApp(App):
        def compose(self):
            yield AuthInput(value='Example value', id='descriptor')

    async def scenario():
        app = NativeInputApp()
        native = Mock()
        monkeypatch.setattr(app, 'copy_to_clipboard', native)
        async with app.run_test() as pilot:
            control = app.query_one('#descriptor', Input)
            control.selection = type(control.selection)(0, 7)
            await pilot.press('ctrl+c')
            native.assert_called_once_with('Example')
            assert control.value == 'Example value' and control.selected_text == 'Example'
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['auth-vault', 'auth-user', 'auth-path'])
@pytest.mark.parametrize('selection,expected', [(None, 'Example value'), ((0, 7), 'Example'), (None, '')])
def test_editor_copy_uses_selection_or_current_value(tmp_path, monkeypatch, field, selection, expected):
    async def scenario():
        app = WizardApp(tmp_path)
        copied = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copied)
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            if field == 'auth-path':
                app.screen.query_one('#auth-manager-table', DataTable).focus()
                await pilot.press('e')
            else:
                await pilot.press('a')
            await pilot.pause()
            editor = app.screen
            control = editor.query_one('#' + field, Input)
            control.value = '' if expected == '' else 'Example value'
            control.focus()
            await pilot.pause()
            control.selection = (type(control.selection)(*selection) if selection is not None
                                 else type(control.selection).cursor(len(control.value)))
            await pilot.pause()
            notify = Mock()
            monkeypatch.setattr(app, 'notify', notify)
            hints = [hint for hint in editor.query(FooterKey) if hint.description == 'Copy value']
            assert len(hints) == 1 and hints[0].key_display == '^c'
            before = control.value
            await pilot.press('ctrl+c')
            copied.assert_awaited_once_with(app, expected)
            assert control.value == before and control.selected_text == ''
            assert app.screen is editor and not app.session.dotenv_path.exists()
            notify.assert_called_once_with('Value copied.', timeout=2)
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['failure', 'changed', 'masked'])
def test_editor_copy_preserves_failed_or_changed_selection_and_masks_secrets(tmp_path, monkeypatch, outcome):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            await pilot.press('a')
            await pilot.pause()
            editor = app.screen
            control = editor.query_one('#auth-vault', Input)
            control.value = 'Example value'
            control.selection = type(control.selection)(0, 7)
            original = control.selection
            notify = Mock()
            monkeypatch.setattr(app, 'notify', notify)

            async def copy(app, value):
                if outcome == 'failure':
                    raise ClipboardError('private diagnostic')
                control.value = 'New edit'
                control.selection = type(control.selection)(0, 3)
                return False

            copied = AsyncMock(side_effect=copy)
            monkeypatch.setattr('api_agent.tui.app.copy_text', copied)
            native = Mock()
            monkeypatch.setattr(app, 'copy_to_clipboard', native)
            if outcome == 'masked':
                control.password = True
                editor.refresh_bindings()
                assert not editor.check_action('copy_value', ())
                await pilot.press('ctrl+c')
            else:
                await editor.action_copy_value()
            if outcome == 'masked':
                copied.assert_not_awaited()
                native.assert_not_called()
                assert not [hint for hint in editor.query(FooterKey) if hint.description == 'Copy value']
                assert control.selection == original
            elif outcome == 'failure':
                assert control.selection == original
                notify.assert_called_once_with('Could not copy the value.', severity='error', timeout=2)
            else:
                assert control.value == 'New edit' and control.selected_text == 'New'
                notify.assert_called_once_with('Value sent to terminal clipboard.', timeout=2)
    asyncio.run(scenario())


def test_pending_readonly_copy_preserves_new_mouse_selection(tmp_path, monkeypatch):
    async def scenario():
        app = WizardApp(tmp_path)
        started, complete = asyncio.Event(), asyncio.Event()

        async def clipboard(app, value):
            started.set()
            await complete.wait()
            return True

        copied = AsyncMock(side_effect=clipboard)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copied)
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            await pilot.press('a')
            await pilot.pause()
            editor = app.screen
            await pilot.mouse_down('#auth-first-label', offset=(0, 0))
            await pilot.hover('#auth-first-label', offset=(4, 0))
            await pilot.mouse_up('#auth-first-label', offset=(4, 0))
            assert editor.get_selected_text() == 'Vault'
            action = asyncio.create_task(editor.action_copy_value())
            await asyncio.wait_for(started.wait(), 2)
            await pilot.mouse_down('#auth-login-label', offset=(0, 0))
            await pilot.hover('#auth-login-label', offset=(4, 0))
            await pilot.mouse_up('#auth-login-label', offset=(4, 0))
            assert editor.get_selected_text() == 'Login'
            complete.set()
            await action
            copied.assert_awaited_once_with(app, 'Vault')
            assert editor.get_selected_text() == 'Login'
    asyncio.run(scenario())


def test_pending_readonly_copy_preserves_selection_when_target_text_changes(tmp_path, monkeypatch):
    async def scenario():
        app = WizardApp(tmp_path, external=True)
        started, complete = asyncio.Event(), asyncio.Event()

        async def clipboard(app, value):
            started.set()
            await complete.wait()
            return True

        copied = AsyncMock(side_effect=clipboard)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copied)
        async with app.run_test(size=(150, 55)) as pilot:
            await pilot.pause()
            await pilot.press('a')
            await pilot.pause()
            editor = app.screen
            await pilot.mouse_down('#auth-targets', offset=(0, 1))
            await pilot.hover('#auth-targets', offset=(8, 1))
            await pilot.mouse_up('#auth-targets', offset=(8, 1))
            original_text = editor.get_selected_text()
            assert original_text
            original_selection = dict(editor.selections)
            action = asyncio.create_task(editor.action_copy_value())
            await asyncio.wait_for(started.wait(), 2)
            editor.action_switch_target()
            await pilot.pause()
            current_text = editor.get_selected_text()
            assert editor.selections == original_selection
            assert current_text and current_text != original_text
            complete.set()
            await action
            copied.assert_awaited_once_with(app, original_text)
            assert editor.get_selected_text() == current_text
    asyncio.run(scenario())
