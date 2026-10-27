"""Authentication clears only Textual's local clipboard cache."""
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.document._document import Selection
from textual.widgets import Input, TextArea

from api_agent.tui import clipboard
from api_agent.tui.manager_recovery import ManagerRecoveryScreen


def test_clear_local_clipboard_uses_real_app_storage_without_copy_protocol(monkeypatch):
    app = App()
    app._clipboard = 'cached value'
    calls, driver_writes = [], []
    app._driver = SimpleNamespace(write=driver_writes.append)
    monkeypatch.setattr(app, 'copy_to_clipboard', lambda value: calls.append(value))
    clipboard.clear_local_clipboard(app)
    assert app.clipboard == ''
    assert calls == []
    assert driver_writes == []


@pytest.mark.asyncio
async def test_screen_mount_clears_cache_without_changing_native_clipboard(monkeypatch):
    app = App()
    app._clipboard = 'old cached URL'
    native = ['browser value']
    copies = []
    monkeypatch.setattr(app, 'copy_to_clipboard', lambda value: copies.append(value))
    screen = ManagerRecoveryScreen(SimpleNamespace(label='Authentication', closed=False))
    async with app.run_test(size=(85, 30)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        assert app.clipboard == ''
        assert native == ['browser value']
        assert copies == []


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['native', 'terminal', 'error'])
async def test_selected_output_copy_clears_only_cache_even_after_failure(monkeypatch, outcome):
    app = App()
    native, copies, notices = ['previous OS value'], [], []
    copy_to_clipboard = app.copy_to_clipboard
    def copy_protocol(value):
        copies.append(value)
        native[0] = value
        copy_to_clipboard(value)
        if outcome == 'error':
            raise RuntimeError('synthetic copy failure')
    def native_write(command, **kwargs):
        native[0] = kwargs['input'].decode()
    async def read_text():
        return native[0]
    monkeypatch.setattr(app, 'copy_to_clipboard', copy_protocol)
    monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append(message))
    monkeypatch.setattr(clipboard, 'native_command', lambda: ['synthetic-copy'] if outcome == 'native' else None)
    monkeypatch.setattr(clipboard.subprocess, 'run', native_write)
    monkeypatch.setattr(clipboard, 'read_text', read_text)
    screen = ManagerRecoveryScreen(SimpleNamespace(label='Authentication', closed=False))
    async with app.run_test(size=(85, 30)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        driver_writes = []
        monkeypatch.setattr(app._driver, 'write', driver_writes.append)
        url = 'https://login.example.test/authorize'
        screen.append_output(url)
        output = screen.query_one('#manager-output', TextArea)
        output.selection = Selection((0, 0), (0, len(url)))
        await pilot.press('ctrl+c')
        await pilot.pause()
        assert native == [url]
        assert app.clipboard == ''
        assert copies == [url]  # No empty copy command can clear the OS clipboard.
        assert '\x1b]52;c;\a' not in driver_writes
        expected = {'native': 'Output copied.', 'terminal': 'Output sent to terminal clipboard.',
                    'error': 'Could not copy selected output.'}
        assert notices == [expected[outcome]]
        native[0] = 'c3ludGhldGlj'  # Browser copy changes only the OS clipboard.
        field = screen.query_one('#manager-input', Input)
        field.focus()
        await pilot.press('ctrl+v')
        await pilot.pause()
        assert field.value == native[0]
        assert app.clipboard == ''
        Input.action_paste(field)  # A future accidental inherited fallback sees empty.
        assert field.value == native[0]
        assert copies == [url]
