"""Review regressions use synthetic secrets, diagnostic files, and local pipes."""
import asyncio
import builtins
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from textual import Logger, constants, events, log
from textual.widgets import Input

from api_agent.auth.interaction import Reply
from api_agent.auth.terminal_interaction import TerminalInteraction
from api_agent.tui.private_input import diagnostic_value, install_private_input_logging
from test_recovery_tui import RecoveryApp, open_channel


def test_logger_filter_preserves_other_diagnostics_and_is_idempotent(monkeypatch):
    calls = []
    def original(logger, *objects, **values):
        calls.append((objects, values))
    monkeypatch.setattr(Logger, '__call__', original)
    install_private_input_logging()
    installed = Logger.__call__
    install_private_input_logging()
    assert Logger.__call__ is installed
    field = Input(password=True)
    ordinary = Input()
    marker = object()
    messages = [Input.Changed(field, 'synthetic-secret'),
                Input.Submitted(field, 'synthetic-secret'),
                Input.Blurred(field, 'synthetic-secret'),
                events.Key('s', 's'), events.Paste('synthetic-secret')]
    for message in messages:
        log.event.verbose(message, ordinary=marker, private=message)
        objects, values = calls[-1]
        assert objects == (type(message).__name__ + '(<private input>)',)
        assert values == {'ordinary': marker, 'private': objects[0]}
    plain = Input.Changed(ordinary, 'ordinary-value')
    assert diagnostic_value(plain) is plain
    assert diagnostic_value(marker) is marker


def test_recovery_filter_protects_devtools_sink_as_well_as_file(tmp_path, monkeypatch):
    path = tmp_path / 'devtools.log'
    monkeypatch.setattr(constants, 'LOG_FILE', str(path))
    received = []
    class ConnectedApp:
        _is_devtools_connected = True
        def _log(self, *objects, **values):
            received.append((objects, values))
    app = ConnectedApp()
    logger = Logger(None, app=app)
    install_private_input_logging()
    logger.event(Input.Submitted(Input(password=True), 'synthetic-secret'),
                 event=events.Paste('synthetic-secret'))
    assert len(received) == 1
    assert received[0][0][3:] == ('Submitted(<private input>)',)
    assert received[0][1] == {'event': 'Paste(<private input>)'}
    assert 'synthetic-secret' not in path.read_text()


@pytest.mark.parametrize('entry', ['native', 'paste', 'programmatic'])
def test_native_recovery_diagnostics_never_contain_secret_or_entry_events(tmp_path, monkeypatch, entry):
    path = tmp_path / 'textual.log'
    monkeypatch.setattr(constants, 'LOG_FILE', str(path))
    secret = 'synthetic-private-secret'
    async def scenario():
        app = RecoveryApp()
        async with app.run_test() as pilot:
            channel = await open_channel(app)
            field = app.screen.query_one(Input)
            await asyncio.to_thread(channel.write, 'Password:')
            if entry == 'native':
                await pilot.press(*secret)
            elif entry == 'paste':
                field.post_message(events.Paste(secret))
                await pilot.pause()
            else:
                field.value = secret
            await pilot.press('enter')
            assert channel.read(.01) == Reply('input', secret)
            assert field.value == secret and field.disabled
            await asyncio.to_thread(channel.close)
            assert field.value == ''
            # Events may finish bubbling after the secret screen is dismissed.
            log.event(events.Key('z', 'z'))
            log.info('ordinary diagnostic remains available')
    asyncio.run(scenario())
    contents = path.read_text()
    assert secret not in contents
    assert 'Key(key=' not in contents
    assert 'Paste(text=' not in contents
    assert 'value=' not in '\n'.join(line for line in contents.splitlines()
                                    if 'manager-input' in line)
    assert '(<private input>)' in contents
    assert 'ordinary diagnostic remains available' in contents


@pytest.mark.parametrize('activation', ['--debug', '--start=True', '--start=E2E'])
def test_non_recovery_terminal_dispatch_without_termios(monkeypatch, activation):
    from api_agent.runtime.bootstrap import main
    original_import = builtins.__import__
    def import_without_termios(name, *args, **kwargs):
        if name == 'termios':
            raise ModuleNotFoundError('termios unavailable in platform simulation')
        return original_import(name, *args, **kwargs)
    # Force the actual bootstrap import to execute the relay module afresh.
    monkeypatch.delitem(sys.modules, 'api_agent.auth.terminal_interaction')
    args = ['--no-std-in', '--tui-mode=integration', activation, '--allure-s3-bucket=synthetic']
    if activation == '--debug':
        args.append('--start=True')
    stderr = io.StringIO()
    with patch.dict('os.environ', {}, clear=True), patch('sys.stderr', stderr), \
         patch('api_agent.config.read_dotenv', return_value={}), \
         patch('api_agent.config.dotenv.read_dotenv', return_value={}), \
         patch('api_agent.runtime.worktrees.verify_worktrees'), \
         patch('api_agent.runtime.dependencies.ensure_environment', return_value=None), \
         patch('api_agent.testing.collection.collect_tests', return_value=[object()]), \
         patch('api_agent.testing.launched.run_launched_integration',
               return_value=SimpleNamespace(exit_code=0)) as runner, \
         patch('builtins.__import__', side_effect=import_without_termios):
        assert main(args, base_root=Path(__file__).resolve().parents[2]) == 0
    assert runner.call_count == 1 and not stderr.getvalue()


def test_unsupported_recovery_explains_boundary_before_terminal_io(monkeypatch):
    from api_agent.auth import terminal_interaction as module
    from api_agent.auth.credentials import CredentialRecoveryError
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    with pytest.raises(CredentialRecoveryError, match='requires a POSIX terminal'):
        module.TerminalInteraction(io.StringIO(), io.StringIO()).open('Keeper')


def test_redirected_partial_reply_preserves_large_queued_valid_input():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd) as stream:
            owner = TerminalInteraction(stream, io.StringIO())
            channel = owner.open('Keeper')
            channel.write('Password:')
            os.write(write_fd, b'abcdefgh')
            assert channel.read(.01) is None
            os.write(write_fd, b'\n' + b'answer\n' * 600)
            os.close(write_fd)
            write_fd = None
            assert channel.read(.01) == Reply('input', 'abcdefgh')
            for _ in range(600):
                channel.write('Next:')
                assert channel.read(.01) == Reply('input', 'answer')
                assert len(owner.buffer) <= 4096
            channel.write('Next:')
            assert channel.read(.01) == Reply('eof')
            channel.close()
    finally:
        if write_fd is not None:
            os.close(write_fd)


@pytest.mark.parametrize('payload,expected', [(b'x' * 4095 + b'\n', 'input'),
                                             (b'x' * 4096 + b'\n', 'cancel')])
def test_redirected_reply_size_boundary(payload, expected):
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd) as stream:
            owner = TerminalInteraction(stream, io.StringIO())
            channel = owner.open('Keeper')
            channel.write('Password:')
            os.write(write_fd, payload)
            answer = channel.read(.01)
            assert answer.action == expected
            if expected == 'input':
                assert answer.value == 'x' * 4095
            else:
                assert owner.buffer == b''
            channel.close()
    finally:
        os.close(write_fd)
