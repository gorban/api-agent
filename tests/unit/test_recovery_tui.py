"""Temporary manager screens use no real credential CLI or vault."""
import asyncio
import threading

from textual.app import App
from textual.widgets import Button, Input, Static

from api_agent.auth.interaction import Reply
from api_agent.tui.manager_recovery import ManagerRecoveryScreen, TUIInteraction, transcript_text, _LIMIT


class RecoveryApp(App):
    def __init__(self):
        super().__init__()
        self.transport = TUIInteraction(self, threading.Event())


async def open_channel(app, label='Keeper'):
    channel = await asyncio.to_thread(app.transport.open, label)
    await asyncio.sleep(.05)
    return channel


def test_recovery_send_cancel_focus_and_cleanup():
    async def scenario():
        app = RecoveryApp()
        async with app.run_test(size=(85, 30)) as pilot:
            channel = await open_channel(app)
            screen = app.screen
            assert app.focused.id == 'manager-input'
            assert screen.query_one(Input).password
            await asyncio.to_thread(channel.write, '\x1b[32mPassword:\x1b[0m [literal]')
            assert screen.transcript == 'Password: [literal]'
            screen.query_one(Input).value = 'private-value'
            await pilot.press('enter')
            answer = await asyncio.to_thread(channel.read, .01)
            assert answer == Reply('input', 'private-value')
            assert 'private-value' not in repr(answer)
            assert screen.query_one(Input).value == 'private-value'
            assert screen.query_one(Input).disabled
            assert screen.query_one('#manager-send', Button).disabled
            assert 'private-value' not in screen.transcript
            await pilot.press('enter')
            screen.send_value()
            assert channel.read(.01) is None
            await asyncio.to_thread(channel.write, '')
            await asyncio.to_thread(channel.write, '\x1b[32m')
            assert screen.query_one(Input).disabled
            await asyncio.to_thread(channel.write, 'Next input:')
            assert screen.query_one(Input).value == ''
            assert not screen.query_one(Input).disabled
            assert not screen.query_one('#manager-send', Button).disabled
            assert app.focused.id == 'manager-input'
            for expected in ('manager-send', 'manager-cancel', 'manager-input'):
                await pilot.press('tab')
                assert app.focused.id == expected
            screen.query_one('#manager-send', Button).focus()
            screen.query_one(Input).value = 'again'
            await pilot.press('enter')
            assert channel.read(.01).value == 'again'
            screen.query_one('#manager-cancel', Button).focus()
            await pilot.press('enter')
            await pilot.press('escape')
            assert app.screen is screen and not screen.pending
            await pilot.press('enter')
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            assert channel.read(.01).action == 'cancel'
            assert app.screen is screen and screen.pending
            assert all(widget.disabled for widget in screen.query('Input, Button'))
            assert not screen.check_action('retry', ())
            screen.send_value()
            screen.stop('retry')
            await asyncio.to_thread(channel.write, 'late')
            assert 'late' not in screen.transcript
            await asyncio.to_thread(channel.close)
            assert channel.closed and screen.transcript == ''
            assert app.screen is not screen
            await asyncio.to_thread(channel.close)
            await asyncio.to_thread(channel.write, 'discarded')
            channel.reply(Reply('input', 'discarded'))
            assert channel.read(.01).action == 'cancel'
    asyncio.run(scenario())


def test_output_during_cancel_confirmation_preserves_dialog_focus():
    async def scenario():
        app = RecoveryApp()
        async with app.run_test() as pilot:
            channel = await open_channel(app)
            screen = app.screen
            screen.query_one(Input).value = 'private'
            await pilot.press('enter')
            assert channel.read(.01) == Reply('input', 'private')
            screen.request_cancel()
            await pilot.pause()
            dialog = app.screen
            assert dialog is not screen
            focused = app.focused
            await asyncio.to_thread(channel.write, 'Try again:')
            assert app.screen is dialog and app.focused is focused
            assert not screen.query_one(Input).disabled
            assert screen.query_one(Input).value == ''
            await pilot.press('escape')
            screen.query_one(Input).focus()
            screen.query_one(Input).value = 'private again'
            await pilot.press('enter')
            assert channel.read(.01) == Reply('input', 'private again')
            await pilot.press('ctrl+r')
            assert channel.read(.01).action == 'retry'
            assert screen.pending and screen.query_one(Input).value == ''
            await asyncio.to_thread(channel.close)
    asyncio.run(scenario())


def test_completed_request_clears_readonly_secret_before_dismissal():
    async def scenario():
        app = RecoveryApp()
        async with app.run_test() as pilot:
            channel = await open_channel(app)
            screen = app.screen
            field = screen.query_one(Input)
            field.value = 'private'
            await pilot.press('enter')
            assert channel.read(.01) == Reply('input', 'private')
            assert field.disabled
            await asyncio.to_thread(channel.close)
            assert field.value == ''
            assert screen.transcript == ''
    asyncio.run(scenario())


def test_retry_queue_and_deadline_confirmation_close():
    async def scenario():
        app = RecoveryApp()
        async with app.run_test(size=(85, 30)) as pilot:
            first = await open_channel(app, 'First')
            second = await open_channel(app, 'Second')
            third = await open_channel(app, 'Third')
            assert app.screen is first.screen
            await asyncio.to_thread(second.write, 'queued output')
            assert second.screen.transcript == 'queued output'
            await asyncio.to_thread(third.close)
            assert app.screen is first.screen
            assert third not in app.transport.waiting
            assert first.read(.001) is None
            await pilot.press('ctrl+r')
            assert first.read(.01).action == 'retry'
            assert app.screen is first.screen
            await asyncio.to_thread(first.close)
            await pilot.pause()
            assert app.screen is second.screen
            assert str(second.screen.query_one('#manager-output', Static).content) == 'queued output'
            second.screen.request_cancel()
            second.screen.request_cancel()  # Two dialogs can still be disposed on operation expiry.
            await asyncio.to_thread(second.close)
            assert app.transport.active is None
            assert second.screen not in app.screen_stack
            app.transport.cancel_event.set()
            canceled = await open_channel(app)
            assert canceled.closed and canceled.read(.01).action == 'cancel'
            canceled.screen.request_cancel()
            assert not canceled.screen.pending
    asyncio.run(scenario())


def test_transcript_bounded_and_global_cancel():
    async def scenario():
        app = RecoveryApp()
        async with app.run_test() as pilot:
            channel = await open_channel(app)
            await asyncio.to_thread(channel.write, 'a' * (_LIMIT + 10) + '\x1b]52;clipboard\x07\x00\x7f')
            assert len(channel.screen.transcript) == _LIMIT
            assert set(channel.screen.transcript) == {'a'}
            app.transport.cancel_event.set()
            assert channel.read(.01).action == 'cancel'
            await asyncio.to_thread(channel.close)
    assert transcript_text('a\t\n\r\x00\x7fb') == 'a\t\n\rb'
    asyncio.run(scenario())


def test_running_integration_returns_to_results_after_recovery(tmp_path):
    from types import SimpleNamespace
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.contracts import TestResult, RunnerEvent
    from api_agent.testing.integration import IntegrationRunResult
    from test_tui import fixture
    identity = TestIdentity('base::t.py::test_login', 'base', 't.py', 't.py::test_login')
    answers = []
    def runner(context, tests, config, **options):
        options['on_event'](RunnerEvent('test_started', identity=identity))
        channel = options['interaction'].open('Keeper')
        try:
            channel.write('Password:')
            while (reply := channel.read(.05)) is None:
                pass
            answers.append(reply)
            options['on_event'](RunnerEvent('test_finished', identity=identity, status='passed'))
        finally:
            channel.close()
        return SimpleNamespace(execution=IntegrationRunResult(0, (TestResult(identity, 'passed'),), ''),
                               exit_code=0, persisted=True)
    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [identity])
        app.integration_runner = runner
        async with app.run_test(size=(95, 35)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            await pilot.pause(.3)
            screen = app.screen
            assert screen.channel.label == 'Keeper'
            assert app.test_run_active()
            app.unit_event(RunnerEvent('test_started', identity=identity))
            assert app.screen is screen
            screen.query_one(Input).value = 'synthetic answer'
            await pilot.press('enter')
            await pilot.pause(.3)
            assert answers == [Reply('input', 'synthetic answer')]
            assert app.screen.kind == 'test-results'
            assert 'PASS' in app.screen.body
            assert 'synthetic answer' not in app.screen.body
            assert not app.test_run_active()
    asyncio.run(scenario())


def test_shared_backend_recovery_outlives_startup_deadline(tmp_path, monkeypatch):
    """A real accepted setup connection must not inherit listener startup's limit."""
    import sys
    from types import SimpleNamespace
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.contracts import TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.setup_backend import SetupBackend, _SetupOwner
    from api_agent.providers.setup_transport import SetupClient
    from test_tui import fixture

    cli = tmp_path / 'synthetic-keeper'
    cli.write_text('#!' + sys.executable + '\n'
        'import os,sys\n'
        'if not os.isatty(0):\n'
        ' sys.stderr.write("Keeper Persistent login is not working in this non-interactive environment\\n"); sys.exit(1)\n'
        'sys.stderr.write("Password: "); sys.stderr.flush()\n'
        'if sys.stdin.readline().strip() != "synthetic answer": sys.exit(2)\n'
        'sys.stdout.write("private synthetic credential\\n")\n')
    cli.chmod(0o700)
    config = {'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {
        'password_manager': {'manager': 'keeper', 'cli_path': str(cli)},
        'roles': {'LLM User': [{'vault': 'fixture', 'user': 'fixture'}]}}}
    received = []

    async def credential_readiness(owner):
        # Keep the real shared backend, credential reader, PTY, and TUI transport;
        # substitute only the provider's remote authentication operation.
        received.append(await owner.provider._credentials(0)(0, 'password'))
    monkeypatch.setattr(_SetupOwner, 'ensure_ready', credential_readiness)
    identity = TestIdentity('base::t.py::test_login', 'base', 't.py', 't.py::test_login')

    def runner(context, tests, values, **options):
        backend = SetupBackend(config, tmp_path, interaction=options['interaction'])
        client = SetupClient(backend.start())
        try:
            client.ensure_ready()
        finally:
            client.close()
            backend.close()
        return SimpleNamespace(execution=IntegrationRunResult(
            0, (TestResult(identity, 'passed'),), ''), exit_code=0, persisted=True)

    async def scenario():
        app = fixture(tmp_path, 'integration', collector=lambda *args: [identity])
        app.integration_runner = runner
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(.2)
            await pilot.press('s')
            for _ in range(50):
                if isinstance(app.screen, ManagerRecoveryScreen):
                    break
                await pilot.pause(.1)
            screen = app.screen
            assert isinstance(screen, ManagerRecoveryScreen)
            await pilot.pause(6)
            assert app.screen is screen and app.test_run_active()
            assert 'Password:' in screen.transcript
            assert not received
            screen.query_one(Input).value = 'synthetic answer'
            await pilot.press('enter')
            for _ in range(50):
                if not app.test_run_active():
                    break
                await pilot.pause(.1)
            assert received == ['private synthetic credential']
            assert app.screen.kind == 'test-results'
            assert 'PASS' in app.screen.body
            assert 'synthetic answer' not in app.screen.body
            assert 'private synthetic credential' not in app.screen.body
            assert screen.transcript == ''
    asyncio.run(scenario())
