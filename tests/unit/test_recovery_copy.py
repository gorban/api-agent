"""Only explicitly selected private output may enter the clipboard."""
import asyncio
import threading

import pytest
from textual.widgets.text_area import Selection
from textual.widgets import Button, Input, Static, TextArea

from api_agent.tui import manager_recovery
from api_agent.tui.clipboard import ClipboardError
from test_recovery_tui import RecoveryApp, open_channel


@pytest.mark.parametrize('result,message', [(True, 'Output copied.'),
    (False, 'Output sent to terminal clipboard.'), (None, 'Could not copy selected output.')])
def test_selected_output_copy_has_safe_feedback(monkeypatch, result, message):
    async def scenario():
        copied, notices = [], []
        async def copy(app, value):
            copied.append(value)
            if result is None:
                raise ClipboardError('sensitive diagnostic')
            return result
        monkeypatch.setattr(manager_recovery, 'copy_text', copy)
        app = RecoveryApp()
        monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append(message))
        async with app.run_test() as pilot:
            channel = await open_channel(app)
            screen = channel.screen
            await asyncio.to_thread(channel.write, 'before https://example.com/login after')
            field = screen.query_one(Input)
            field.value = 'masked private answer'
            output = screen.query_one('#manager-output', TextArea)
            output.selection = Selection((0, 7), (0, 32))
            await pilot.press('ctrl+c')
            assert copied == ['https://example.com/login']
            assert notices == [message]
            assert not screen.pending and not channel.closed
            assert channel.read(.01) is None
            assert field.value == 'masked private answer' and field.password
            assert app.screen is screen
            await asyncio.to_thread(channel.close)
            assert screen.transcript == '' and field.value == ''
    asyncio.run(scenario())


def test_copy_shortcut_and_empty_or_inactive_output_never_cancel(monkeypatch):
    async def scenario():
        copied = []
        async def copy(app, value):
            copied.append(value)
            return True
        monkeypatch.setattr(manager_recovery, 'copy_text', copy)
        app = RecoveryApp()
        async with app.run_test() as pilot:
            channel = await open_channel(app)
            screen = channel.screen
            field = screen.query_one(Input)
            field.value = 'masked private answer'
            await pilot.press('ctrl+c')
            assert not copied and channel.read(.01) is None
            field.select_all()
            await pilot.press('ctrl+c')
            assert not copied and field.value == 'masked private answer'
            await asyncio.to_thread(channel.write, 'selected output')
            output = screen.query_one('#manager-output', TextArea)
            output.selection = Selection((0, 0), (0, 0))
            await screen.action_copy_output()
            assert not copied
            output.selection = Selection((0, 0), (0, 15))
            screen.query_one('#manager-send', Button).focus()
            await pilot.press('ctrl+c')
            assert copied == ['selected output']
            screen.stop('retry')
            assert channel.read(.01).action == 'retry'
            assert not screen.active_bindings['ctrl+c'].binding.show
            await pilot.press('ctrl+c')
            await screen.action_copy_output()
            assert copied == ['selected output'] and app.screen is screen
            assert channel.read(.01) is None
            screen.pending = False
            channel.closed = True
            assert not screen.active_bindings['ctrl+c'].binding.show
            await pilot.press('ctrl+c')
            await screen.action_copy_output()
            assert copied == ['selected output'] and app.screen is screen
            channel.closed = False
            await asyncio.to_thread(channel.close)
    asyncio.run(scenario())


def test_pointer_selection_copies_in_ordinary_configuration_app(tmp_path, monkeypatch):
    from test_tui import fixture
    async def scenario():
        copied = []
        async def copy(app, value):
            copied.append(value)
            return True
        monkeypatch.setattr(manager_recovery, 'copy_text', copy)
        app = fixture(tmp_path)
        async with app.run_test(size=(110, 35)) as pilot:
            transport = manager_recovery.TUIInteraction(app, threading.Event())
            channel = await asyncio.to_thread(transport.open, 'Authentication')
            await pilot.pause()
            await asyncio.to_thread(channel.write, 'https://example.com/login')
            await pilot.pause()
            await pilot.mouse_down('#manager-output', offset=(2, 1))
            await pilot.hover('#manager-output', offset=(27, 1))
            await pilot.mouse_up('#manager-output', offset=(27, 1))
            await pilot.press('ctrl+c')
            assert copied == ['https://example.com/login']
            await pilot.press('ctrl+c')
            assert copied == ['https://example.com/login', 'https://example.com/login']
            assert app.screen is channel.screen and channel.read(.01) is None
            assert not channel.closed and not channel.screen.pending
            await asyncio.to_thread(channel.close)
    asyncio.run(scenario())


@pytest.mark.parametrize('completion', ['active', 'pending', 'closed', 'dialog'])
def test_pending_clipboard_keeps_cancel_responsive_and_suppresses_late_feedback(tmp_path, monkeypatch, completion):
    from test_tui import fixture
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        copied, notices = [], []
        async def copy(app, value):
            copied.append(value)
            entered.set()
            await release.wait()
            return True
        monkeypatch.setattr(manager_recovery, 'copy_text', copy)
        app = fixture(tmp_path)
        monkeypatch.setattr(app, 'notify', lambda message, **kwargs: notices.append(message))
        async with app.run_test(size=(110, 35)) as pilot:
            transport = manager_recovery.TUIInteraction(app, threading.Event())
            channel = await asyncio.to_thread(transport.open, 'Authentication')
            await pilot.pause()
            screen = channel.screen
            await asyncio.to_thread(channel.write, 'selected output')
            output = screen.query_one(TextArea)
            output.selection = Selection((0, 0), (0, 15))
            await pilot.press('ctrl+c')
            await asyncio.wait_for(entered.wait(), 1)
            worker = screen._copy_worker
            await pilot.press('ctrl+c')
            assert screen._copy_worker is worker and copied == ['selected output']
            await pilot.click('#manager-cancel')
            assert app.screen is not screen and not screen.pending
            await screen.action_copy_output()  # A covered screen cannot copy.
            if completion != 'dialog':
                await pilot.press('escape')
            if completion == 'pending':
                screen.stop('retry')
                assert channel.read(.01).action == 'retry'
            elif completion == 'closed':
                channel.closed = True
            release.set()
            await worker.wait()
            assert notices == (['Output copied.'] if completion == 'active' else [])
            assert not transport.cancel_event.is_set()
            if completion == 'dialog':
                await pilot.press('escape')
            channel.closed = False
            await asyncio.to_thread(channel.close)
            await screen.action_copy_output()  # Dismissed channels remain inert.
            assert copied == ['selected output']
    asyncio.run(scenario())


def test_native_login_uses_same_masked_screen_and_preserves_password_manager_sibling(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from test_tui import fixture
    from api_agent.auth.interaction_transport import OwnedInteraction
    from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
    async def scenario():
        copied = []
        async def copy(app, value):
            copied.append(value)
            return True
        monkeypatch.setattr(manager_recovery, 'copy_text', copy)
        app = fixture(tmp_path)
        async with app.run_test(size=(110, 35)) as pilot:
            transport = manager_recovery.TUIInteraction(app, threading.Event())
            owner = OwnedInteraction(transport)
            login = LoginInteraction(SimpleNamespace(interactive=True, interaction=owner))
            await login.open()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, manager_recovery.ManagerRecoveryScreen)
            await login.write('https://example.com/login')
            field = screen.query_one(Input)
            field.value = 'synthetic authorization reply'
            assert field.password and app.focused is field
            output = screen.query_one(TextArea)
            assert output.read_only
            output.selection = Selection((0, 0), (0, 25))
            await pilot.press('ctrl+c')
            assert copied == ['https://example.com/login']
            assert not transport.cancel_event.is_set()
            sibling = await asyncio.to_thread(owner.open, 'Password manager')
            await asyncio.to_thread(sibling.write, 'Password:')
            field.focus()
            await pilot.press('enter')
            assert await login.read() == 'syntheticauthorizationreply'
            assert field.disabled and screen.awaiting_output
            await login.aclose()
            await pilot.pause()
            assert app.screen is sibling.channel.screen and not sibling.closed
            assert screen.transcript == '' and output.text == '' and field.value == ''
            sibling.channel.screen.query_one(Input).value = 'synthetic password'
            await pilot.press('enter')
            assert sibling.read(.01).value == 'synthetic password'
            assert copied == ['https://example.com/login']
            await asyncio.to_thread(sibling.close)
            await asyncio.to_thread(owner.close)
    asyncio.run(scenario())


def test_output_selection_alone_controls_clickable_footer_copy(monkeypatch):
    from textual.widgets._footer import FooterKey
    from api_agent.tui.app import ButtonFooter

    async def scenario():
        copied = []
        async def copy(app, value):
            copied.append(value)
            return True
        monkeypatch.setattr(manager_recovery, 'copy_text', copy)
        app = RecoveryApp()
        async with app.run_test() as pilot:
            channel = await open_channel(app)
            screen = channel.screen
            await pilot.pause()
            footer = screen.query_one(ButtonFooter)
            assert not screen.query('#manager-copy')
            static_text = ' '.join(str(widget.render()) for widget in screen.query(Static))
            assert '^c' not in static_text and 'Copy selected output' not in static_text
            assert not [key for key in footer.query(FooterKey) if key.key == 'ctrl+c']
            field = screen.query_one(Input)
            field.value = 'synthetic private reply'
            field.select_all()
            await pilot.pause()
            assert not [key for key in footer.query(FooterKey) if key.key == 'ctrl+c']
            await pilot.press('ctrl+c')
            assert app.screen is screen and not copied and channel.read(.01) is None
            await asyncio.to_thread(channel.write, 'public output')
            output = screen.query_one(TextArea)
            output.selection = Selection((0, 0), (0, 6))
            await pilot.pause()
            hint, = [key for key in footer.query(FooterKey) if key.key == 'ctrl+c']
            assert hint.key_display == '^c' and hint.description == 'Copy selected output'
            assert not hint.has_class('-disabled')
            hint.on_mouse_down()
            await pilot.pause()
            assert copied == ['public'] and field.value == 'synthetic private reply'
            output.selection = Selection((0, 0), (0, 0))
            await pilot.pause()
            assert not [key for key in footer.query(FooterKey) if key.key == 'ctrl+c']
            await pilot.press('ctrl+c')
            assert app.screen is screen and copied == ['public']
            assert channel.read(.01) is None
            await asyncio.to_thread(channel.close)
    asyncio.run(scenario())
