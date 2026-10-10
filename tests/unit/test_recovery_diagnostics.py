"""Native parser privacy and diagnostic attribution use synthetic local data."""
import inspect
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from types import ModuleType

import pytest
from textual import Logger, constants, events
from textual._context import active_app
from textual._xterm_parser import XTermParser
from textual.app import App

from api_agent.tui.private_input import install_private_input_logging


def test_existing_and_future_debug_parsers_stop_raw_logging_without_changing_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = XTermParser(debug=True)
    list(parser.feed('ordinary'))
    before = (tmp_path / 'keys.log').read_text()
    install_private_input_logging()
    secret = 'synthetic-private-secret'
    emitted = list(parser.feed(secret))
    assert ''.join(message.character for message in emitted) == secret
    list(parser.feed(''))
    future = XTermParser(debug=True)
    emitted = list(future.feed('\x1b[200~' + secret + '\x1b[201~\r'))
    assert any(isinstance(message, events.Paste) and message.text == secret for message in emitted)
    list(future.feed(''))
    text = (tmp_path / 'keys.log').read_text()
    assert secret not in text
    assert text == before


@pytest.mark.parametrize('route', ['app', 'active', 'custom'])
def test_devtools_original_callsite_and_payload_survive_filter(monkeypatch, route):
    received = []
    class Payload:
        def __init__(self, content, *, caller):
            self.content, self.caller = content, caller
    class Devtools:
        is_connected = verbose = True
        def log(self, payload, group, verbosity):
            received.append((payload, group, verbosity))
    class Connected:
        _is_devtools_connected = True
        _log = App._log
        devtools = Devtools()
        def _handle_exception(self, error):
            raise error
    parent = ModuleType('textual_dev')
    parent.__path__ = []
    client = ModuleType('textual_dev.client')
    client.DevtoolsLog = Payload
    monkeypatch.setitem(sys.modules, 'textual_dev', parent)
    monkeypatch.setitem(sys.modules, 'textual_dev.client', client)
    monkeypatch.setattr(constants, 'LOG_FILE', None)
    monkeypatch.setattr(Logger, '__call__', inspect.unwrap(Logger.__call__))
    app = Connected()
    logger = Logger(app._log if route == 'custom' else None, app=None if route == 'active' else app)
    token = active_app.set(app)
    def emit(multiple):
        if multiple:
            line = sys._getframe().f_lineno + 1
            logger.info('ordinary', 'diagnostic', count=1)
        else:
            line = sys._getframe().f_lineno + 1
            logger.info('ordinary diagnostic')
        return line
    try:
        baseline = []
        for multiple in (False, True):
            line = emit(multiple)
            payload, group, verbosity = received[-1]
            baseline.append((payload.content, group, verbosity, line, payload.caller.filename))
        install_private_input_logging()
        for multiple, (content, group, verbosity, line, filename) in zip((False, True), baseline):
            assert emit(multiple) == line
            payload, actual_group, actual_verbosity = received[-1]
            assert payload.content == content
            assert (actual_group, actual_verbosity) == (group, verbosity)
            assert payload.caller.filename == filename
            assert os.path.samefile(payload.caller.filename, __file__)
            assert payload.caller.function == 'emit'
            assert payload.caller.lineno == line
            assert logger._log == (app._log if route == 'custom' else None)
    finally:
        active_app.reset(token)


_NATIVE_CHILD = r'''
import asyncio, json, os
from pathlib import Path
from textual import log
from api_agent.tui.app import ApiAgentApp
from api_agent.tui.manager_recovery import ManagerRecoveryScreen
from test_tui import fixture
root = Path(os.environ['PROBE_ROOT'])
class Channel:
    label = 'Keeper'
    closed = False
    def __init__(self, app): self.app = app
    def reply(self, reply):
        (root / 'reply.json').write_text(json.dumps({'action': reply.action, 'value': reply.value}))
        log.info('ordinary diagnostic remains available')
        self.app.exit(0)
class ProbeApp(ApiAgentApp):
    async def on_mount(self):
        # Require the real parser to exist before recovery installs its guard.
        for _ in range(100):
            if (root / 'keys.log').exists(): break
            await asyncio.sleep(.01)
        assert (root / 'keys.log').exists()
        self.call_after_refresh(self.show_probe)
    def show_probe(self):
        self.push_screen(ManagerRecoveryScreen(Channel(self)))
        self.call_after_refresh(lambda: (root / 'ready').write_text(type(self._driver).__name__))
app = fixture(root)
app.__class__ = ProbeApp
app.run(mouse=False)
'''


@pytest.mark.parametrize('entry', ['native', 'paste'])
def test_textual_debug_native_driver_recovery_keeps_both_logs_private(tmp_path, entry):
    if os.name != 'posix':
        pytest.skip('Owned POSIX terminal driver regression')
    repo = Path(__file__).resolve().parents[2]
    child = tmp_path / 'child.py'
    child.write_text(_NATIVE_CHILD)
    master, slave = os.openpty()
    environment = {'TERM': 'xterm-256color', 'TEXTUAL_DEBUG': '1',
                   'TEXTUAL_LOG': str(tmp_path / 'textual.log'), 'PROBE_ROOT': str(tmp_path),
                   'PYTHONPATH': os.pathsep.join(map(str, (repo / 'src', repo / 'tests/unit')))}
    process = subprocess.Popen([sys.executable, str(child)], cwd=tmp_path, env=environment,
                               stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = bytearray()
    def drain():
        if select.select([master], [], [], .05)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                pass
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / 'ready').exists() and process.poll() is None and time.monotonic() < deadline:
            drain()
        assert (tmp_path / 'ready').read_text() == 'LinuxDriver', output.decode(errors='replace')
        parser_before = (tmp_path / 'keys.log').read_text()
        secret = 'synthetic-private-secret'
        payload = secret if entry == 'native' else '\x1b[200~' + secret + '\x1b[201~'
        os.write(master, (payload + '\r').encode())
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            drain()
        assert process.wait(timeout=2) == 0, output.decode(errors='replace')
        assert json.loads((tmp_path / 'reply.json').read_text()) == {'action': 'input', 'value': secret}
        parser_log = (tmp_path / 'keys.log').read_text()
        framework_log = (tmp_path / 'textual.log').read_text()
        assert secret not in parser_log and secret not in framework_log
        assert parser_log == parser_before
        assert 'Key(key=' not in framework_log and 'Paste(text=' not in framework_log
        assert 'ordinary diagnostic remains available' in framework_log
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)
