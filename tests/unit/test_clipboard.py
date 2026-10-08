"""Clipboard checks use synthetic values and never touch the system clipboard."""
import asyncio
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import DataTable, Input

from api_agent.tui import clipboard
from api_agent.tui.app import WorkbenchScreen
from test_tui import fixture


@pytest.mark.parametrize('platform,wayland,available,expected', [
    ('darwin', '', '/usr/bin/pbcopy', ['/usr/bin/pbcopy']),
    ('linux', 'wayland-0', '/usr/bin/wl-copy', ['/usr/bin/wl-copy']),
    ('linux', '', '/usr/bin/wl-copy', None),
    ('win32', '', None, None),
    ('darwin', '', None, None),
    ('linux', 'wayland-0', None, None),
])
def test_optional_native_command(monkeypatch, platform, wayland, available, expected):
    monkeypatch.setattr(clipboard.sys, 'platform', platform)
    monkeypatch.setenv('WAYLAND_DISPLAY', wayland)
    which = Mock(return_value=available)
    monkeypatch.setattr(clipboard.shutil, 'which', which)
    assert clipboard.native_command() == expected
    if platform == 'darwin' or platform == 'linux' and wayland:
        which.assert_called_once_with('pbcopy' if platform == 'darwin' else 'wl-copy')
    else:
        which.assert_not_called()


@pytest.mark.parametrize('native_error', [None, OSError('private diagnostic'),
    subprocess.CalledProcessError(1, ['helper'], stderr='private diagnostic'),
    subprocess.TimeoutExpired(['helper'], 2)])
@pytest.mark.parametrize('terminal_error', [False, True])
def test_native_fallback_and_sanitized_errors(monkeypatch, native_error, terminal_error):
    monkeypatch.setattr(clipboard, 'native_command', lambda: ['/synthetic/helper'])
    run = Mock(side_effect=native_error)
    monkeypatch.setattr(clipboard.subprocess, 'run', run)
    app = Mock()
    if terminal_error:
        app.copy_to_clipboard.side_effect = RuntimeError('private diagnostic')
    value = 'synthetic [value] π\nwith newlines'
    if native_error and terminal_error:
        with pytest.raises(clipboard.ClipboardError, match='^Could not send the value to the clipboard.$') as exc:
            asyncio.run(clipboard.copy_text(app, value))
        assert exc.value.__suppress_context__
    else:
        assert asyncio.run(clipboard.copy_text(app, value)) is (native_error is None)
    run.assert_called_once_with(['/synthetic/helper'], input=value.encode('utf-8'),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=True)
    app.copy_to_clipboard.assert_called_once_with(value)


@pytest.mark.parametrize('error', [False, True])
def test_terminal_only_without_native_tools(monkeypatch, error):
    monkeypatch.setattr(clipboard, 'native_command', lambda: None)
    run = Mock()
    monkeypatch.setattr(clipboard.subprocess, 'run', run)
    app = Mock()
    if error:
        app.copy_to_clipboard.side_effect = RuntimeError('not displayed')
        with pytest.raises(clipboard.ClipboardError):
            asyncio.run(clipboard.copy_text(app, ''))
    else:
        assert asyncio.run(clipboard.copy_text(app, '')) is False
    run.assert_not_called()
    app.copy_to_clipboard.assert_called_once_with('')


@pytest.mark.parametrize('mode', [None, 'config-cleanup'])
@pytest.mark.parametrize('platform,key,action,label', [
    ('linux', 'ctrl+c', 'copy_value', '^c'),
    ('darwin', 'super+c', 'copy_selected_text', '⌘c'),
])
def test_copy_selected_effective_values_and_last_shortcut(tmp_path, monkeypatch, mode, platform, key, action, label):
    async def scenario():
        fields = {
            'FLAG': {'type': 'bool', 'default': False},
            'COUNT': {'type': 'int', 'default': 0},
            'TEXT': {'type': 'str', 'default': '[literal] π'},
            'PATH': {'type': 'path', 'anchor': 'base_root', 'default': str(tmp_path / ('long-path-' * 20))},
            'OBJECT': {'type': 'jsonobject', 'default': {'synthetic': [1, 'π']}},
            'EMPTY': {'type': 'str', 'default': 'fallback'},
            'MISSING': {'type': 'str', 'review_optional': True},
            'REQUIRED': {'type': 'str', 'required': True},
        }
        dotenv = tmp_path / '.env'
        dotenv.write_text('# synthetic\n' if mode is None else 'EMPTY=""\n')
        app = fixture(tmp_path, mode, fields)
        app.context.anchors['base_root'] = tmp_path
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one(DataTable)
            if mode is None:
                # Explicit empty values saved during review remain on that screen.
                row = next(i for i in range(table.row_count) if table.get_row_at(i)[0] == 'EMPTY')
                table.move_cursor(row=row)
                await pilot.press('e')
                await pilot.pause()
                app.screen.query_one(Input).value = ''
                await pilot.press('enter')
                await pilot.pause()
                screen = app.screen
                table = screen.query_one(DataTable)
            before = dotenv.read_bytes()
            monkeypatch.setattr('api_agent.tui.app.sys.platform', platform)
            setting_column = 0 if mode is None else 1
            for name, expected in [('FLAG', 'False'), ('COUNT', '0'),
                                    ('TEXT', '[literal] π'), ('EMPTY', ''),
                                    ('PATH', str(tmp_path / ('long-path-' * 20))),
                                    ('OBJECT', '{"synthetic":[1,"π"]}'.replace('π', '\\u03c0'))]:
                row = next(i for i in range(table.row_count)
                           if str(table.get_row_at(i)[setting_column]) == name)
                table.move_cursor(row=row)
                await pilot.pause()
                assert screen.check_action(action, ())
                visible = [b.binding for b in screen.active_bindings.values() if b.binding.show]
                assert visible[-1].action == action
                assert visible[-1].key_display == label
                await pilot.press(key)
                copy.assert_awaited_with(app, expected)
                assert table.cursor_row == row and table.has_focus
                assert app.screen is screen
            for name in ('MISSING', 'REQUIRED'):
                row = next(i for i in range(table.row_count)
                           if str(table.get_row_at(i)[setting_column]) == name)
                table.move_cursor(row=row)
                await pilot.pause()
                assert screen.selected_effective_value() is None
                assert not screen.check_action(action, ())
                count = copy.await_count
                await pilot.press(key)
                await screen.action_copy_value()
                assert copy.await_count == count
            table.clear()
            assert screen.selected_effective_value() is None
            assert dotenv.read_bytes() == before
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', [None, 'config-cleanup'])
def test_mac_config_control_c_warns_instead_of_copying(tmp_path, monkeypatch, mode):
    async def scenario():
        app = fixture(tmp_path, mode, fields={'VALUE': {'type': 'str', 'default': 'synthetic'}})
        copy = AsyncMock()
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test() as pilot:
            await pilot.pause()
            monkeypatch.setattr('api_agent.tui.app.sys.platform', 'darwin')
            notify = Mock()
            monkeypatch.setattr(app, 'notify', notify)
            assert not app.screen.check_action('copy_value', ())
            assert app.screen.check_action('copy_selected_text', ())
            await pilot.press('ctrl+c')
            copy.assert_not_awaited()
            assert any(call.kwargs.get('title') == 'Do you want to quit?' for call in notify.call_args_list)
            await pilot.press('super+c')
            copy.assert_awaited_once_with(app, 'synthetic')
    asyncio.run(scenario())


@pytest.mark.parametrize('native', [True, False])
def test_copy_notification_does_not_include_value(tmp_path, monkeypatch, native):
    async def scenario():
        app = fixture(tmp_path, fields={'VALUE': {'type': 'str', 'default': 'synthetic-private'}})
        copy = AsyncMock(return_value=native)
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test() as pilot:
            await pilot.pause()
            notify = Mock()
            monkeypatch.setattr(app, 'notify', notify)
            await pilot.press('ctrl+c')
            notify.assert_called_once_with('Value copied.' if native else 'Value sent to terminal clipboard.', timeout=2)
            copy.side_effect = clipboard.ClipboardError('synthetic-private')
            await pilot.press('ctrl+c')
            notify.assert_called_with('Could not copy the value.', severity='error', timeout=2)
    asyncio.run(scenario())


def test_contextual_copy_preserves_normal_input_and_modal_behavior(tmp_path, monkeypatch):
    async def scenario():
        app = fixture(tmp_path, fields={'VALUE': {'type': 'str', 'default': 'abc', 'help': 'Description'}})
        copy = AsyncMock()
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        native_input_copy = Mock()
        monkeypatch.setattr(app, 'copy_to_clipboard', native_input_copy)
        async with app.run_test() as pilot:
            await pilot.pause()
            defaults = app.screen
            await pilot.press('?')
            await pilot.pause()
            assert not defaults.check_action('copy_value', ())
            await pilot.press('escape', 'e')
            await pilot.pause()
            assert app.screen.kind == 'input'
            assert app.screen.selected_effective_value() is None
            assert not app.screen.check_action('copy_value', ())
            field = app.screen.query_one(Input)
            field.focus()
            field.action_select_all()
            await pilot.press('ctrl+c')
            native_input_copy.assert_called_once_with('abc')
            copy.assert_not_awaited()
            assert WorkbenchScreen('defaults', 'Not mounted').selected_effective_value() is None
    asyncio.run(scenario())
