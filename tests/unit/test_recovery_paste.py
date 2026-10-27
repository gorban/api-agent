"""Private paste reads native values and rejects stale asynchronous results."""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from textual import events
from textual.app import App
from textual.screen import Screen
from textual.widgets import Input
from textual.widgets._input import Selection

from api_agent.tui import clipboard
from api_agent.tui.manager_recovery import ManagerRecoveryScreen


@pytest_asyncio.fixture
async def ui():
    app = App()
    channel = SimpleNamespace(label='Authentication', closed=False, reply=lambda value: None)
    screen = ManagerRecoveryScreen(channel)
    async with app.run_test(size=(85, 30)) as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        yield app, screen, screen.query_one('#manager-input', Input), pilot


@pytest.mark.asyncio
async def test_native_paste_replaces_selection_without_touching_app_buffer(monkeypatch, ui):
    app, screen, field, pilot = ui
    app.copy_to_clipboard('stale URL')
    async def read_text():
        return '\nsecret\r\n'
    monkeypatch.setattr(clipboard, 'read_text', read_text)
    field.value = 'before-after'
    field.selection = Selection(6, 7)
    await pilot.press('ctrl+v')
    await pilot.pause()
    assert field.value == 'before\nsecret\r\nafter'
    assert field.cursor_position == 15
    assert app.clipboard == 'stale URL'


@pytest.mark.asyncio
async def test_terminal_paste_preserves_private_characters(ui):
    app, screen, field, pilot = ui
    field.post_message(events.Paste('\n secret\r\n\t'))
    await pilot.pause()
    assert field.value == '\n secret\r\n\t'
    field.disabled = True
    field.post_message(events.Paste('ignored'))
    await pilot.pause()
    assert field.value == '\n secret\r\n\t'


@pytest.mark.asyncio
async def test_unavailable_native_clipboard_never_pastes_stale_app_value(monkeypatch, ui):
    app, screen, field, pilot = ui
    notices = []
    app.copy_to_clipboard('stale URL')
    monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append(message))
    async def read_text():
        raise clipboard.ClipboardError('private backend detail')
    monkeypatch.setattr(clipboard, 'read_text', read_text)
    await pilot.press('ctrl+v')
    await pilot.pause()
    assert field.value == ''
    assert notices == ['Native clipboard unavailable. Use terminal Paste (commonly Ctrl+Shift+V on Linux).']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['value', 'selection', 'focus', 'dialog', 'pending', 'closed', 'awaiting', 'disabled', 'replace', 'unmount', 'next_prompt', 'next_prompt_real'])
@pytest.mark.parametrize('failure', [False, True])
async def test_late_native_result_is_discarded(monkeypatch, ui, change, failure):
    app, screen, field, pilot = ui
    started, release = asyncio.Event(), asyncio.Event()
    calls, notices = [], []
    monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append(message))
    async def read_text():
        calls.append(True)
        started.set()
        await release.wait()
        if failure:
            raise clipboard.ClipboardError('hidden')
        return 'late secret'
    monkeypatch.setattr(clipboard, 'read_text', read_text)
    field.value = '' if change.startswith('next_prompt') else 'initial'
    initial = field.value
    await pilot.press('ctrl+v')
    await started.wait()
    field.action_paste()  # An in-flight read is not duplicated.
    assert len(calls) == 1
    if change == 'value':
        await pilot.press('x', 'backspace')
        assert field.value == 'initial'
    elif change == 'selection':
        field.cursor_position = 0
        field.cursor_position = 7
    elif change == 'focus':
        screen.query_one('#manager-send').focus()
        await pilot.pause()
        field.focus()
    elif change == 'dialog':
        await app.push_screen(Screen())
        await app.pop_screen()
        field.focus()
    elif change == 'pending':
        screen.pending = True
    elif change == 'closed':
        screen.channel.closed = True
    elif change == 'awaiting':
        screen.awaiting_output = True
    elif change == 'disabled':
        field.disabled = True
    elif change == 'next_prompt_real':
        screen.send_value()
        screen.append_output('Next prompt:')
    elif change == 'next_prompt':
        # Isolate prompt-transition invalidation from the queued blur defense.
        with field.prevent(events.Blur):
            screen.send_value()
            screen.append_output('Next prompt:')
    elif change == 'replace':
        await field.remove()
        await screen.mount(Input(id='manager-input'))
    else:
        await field.remove()
    release.set()
    await pilot.pause()
    assert field.value == initial
    assert notices == []
    if change in ('pending', 'closed', 'awaiting', 'disabled', 'unmount'):
        field.action_paste()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_ordinary_input_keeps_existing_paste_behavior(ui):
    app, screen, field, pilot = ui
    ordinary = Input(id='ordinary-input')
    await screen.mount(ordinary)
    ordinary.focus()
    app.copy_to_clipboard('ordinary')
    await pilot.press('ctrl+v')
    assert ordinary.value == 'ordinary'
    ordinary.post_message(events.Paste(' first\nsecond'))
    await pilot.pause()
    assert ordinary.value == 'ordinary first'


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['ctrl+x', 'super+c', 'direct_copy'])
async def test_private_native_paste_cannot_copy_secret_out(monkeypatch, ui, action):
    app, screen, field, pilot = ui
    app.copy_to_clipboard('stale output URL')
    notices, output_copies = [], []
    monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append(message))
    async def copy_output(app, value):
        output_copies.append(value)
        return True
    monkeypatch.setattr('api_agent.tui.manager_recovery.copy_text', copy_output)
    async def read_text():
        return 'private native value'
    monkeypatch.setattr(clipboard, 'read_text', read_text)
    await pilot.press('ctrl+v')
    await pilot.pause()
    assert field.value == 'private native value'
    field.selection = Selection(0, len(field.value))
    if action == 'direct_copy':
        field.action_copy()
    else:
        await pilot.press(action)
    await pilot.pause()
    assert app.clipboard == 'stale output URL'
    assert field.value == ('' if action == 'ctrl+x' else 'private native value')
    assert notices == []
    assert output_copies == []


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['ctrl+x', 'super+c'])
async def test_ordinary_input_keeps_copy_and_cut(ui, action):
    app, screen, field, pilot = ui
    ordinary = Input(value='ordinary value', id='ordinary-copy')
    await screen.mount(ordinary)
    ordinary.focus()
    ordinary.selection = Selection(0, len(ordinary.value))
    await pilot.press(action)
    assert app.clipboard == 'ordinary value'
    assert ordinary.value == ('' if action == 'ctrl+x' else 'ordinary value')
