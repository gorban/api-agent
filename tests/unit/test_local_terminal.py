import asyncio
from types import SimpleNamespace as NS

import pytest

from api_agent.runtime.local_terminal import format_log, run_local_terminal


def test_cleanup_status_coalesces_retries_and_reminds_after_interval():
    from api_agent.runtime.local_contracts import LocalComponent, LocalSnapshot
    from api_agent.runtime.local_terminal import _TerminalStatus
    messages = []
    now = [0]
    present = _TerminalStatus(messages.append, lambda: now[0])
    def snapshot(phase, name='agent'):
        return LocalSnapshot(phase, components=(LocalComponent(name, phase),))

    present(snapshot('stopping'))
    for second in range(1, 15):
        now[0] = second
        present(snapshot('cleanup-pending' if second % 2 else 'stopping'))
    assert messages == ['Run local: stopping', 'Cleanup scope: agent', 'Run local: cleanup-pending']

    now[0] = 16
    present(snapshot('stopping'))
    assert messages == ['Run local: stopping', 'Cleanup scope: agent', 'Run local: cleanup-pending',
                        'Run local: cleanup-pending (still waiting)']


def test_cleanup_status_reports_scope_change_immediately_and_resets_for_new_operation():
    from api_agent.runtime.local_contracts import LocalComponent, LocalSnapshot
    from api_agent.runtime.local_terminal import _TerminalStatus
    messages = []
    now = [0]
    def snapshot(phase, name):
        return LocalSnapshot(phase, components=(LocalComponent(name, phase),))
    present = _TerminalStatus(messages.append, lambda: now[0])

    present(snapshot('stopping', 'agent'))
    now[0] = 1
    present(snapshot('cleanup-pending', 'agent'))
    now[0] = 2
    present(snapshot('stopping', 'browser'))
    assert messages[-1] == 'Cleanup scope: browser'
    assert messages.count('Run local: stopping') == 1

    another_operation = _TerminalStatus(messages.append, lambda: now[0])
    another_operation(snapshot('stopping', 'browser'))
    assert messages[-2:] == ['Run local: stopping', 'Cleanup scope: browser']


def test_log_selection_preserves_text_and_labels_each_line():
    event = NS(source='ui', text='first\nsecond\n')
    assert format_log(event, 'agent') == ''
    assert format_log(event, 'ui') == 'first\nsecond\n'
    assert format_log(event, 'all') == '[ui] first\n[ui] second\n'


@pytest.mark.parametrize('failure,expected', [(None, 7), (ValueError('failed'), 1), (asyncio.CancelledError(), 130)])
@pytest.mark.parametrize('kind', ['log', 'diagnostic'])
def test_terminal_owns_same_loop_and_disposes_on_every_outcome(failure, expected, kind):
    actions, output, diagnostics = [], [], []
    class Owner:
        pending_cleanup = None
        def diagnostic(self, error): return str(error)
        def __init__(self, context, resolve, *, argv, on_event):
            self.event = on_event
        async def start(self):
            actions.append(('start', asyncio.get_running_loop()))
            self.event(NS(kind='status', snapshot=None))
            self.event(NS(kind=kind, source='agent', text='ready\n'))
        async def wait(self):
            actions.append(('wait', asyncio.get_running_loop()))
            if failure is not None:
                raise failure
            return 7
        def request_stop(self):
            actions.append(('stop', asyncio.get_running_loop()))
        async def aclose(self):
            actions.append(('close', asyncio.get_running_loop()))
    status = asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'all'}),
        owner_factory=Owner, output=output.append, diagnostic=diagnostics.append))
    assert status == expected
    assert output == ['[agent] ready\n']
    assert actions[-1][0] == 'close'
    assert len({loop for _, loop in actions}) == 1
    assert ('stop' in [name for name, _ in actions]) == (expected == 130)
    assert diagnostics == (['failed'] if expected == 1 else [])


def test_terminal_retains_cleanup_failure():
    from api_agent.providers.control import CleanupPendingError
    async def retry(): pass
    pending = CleanupPendingError(('owner',), retry)
    class Owner:
        pending_cleanup = pending
        def diagnostic(self, error): return str(error)
        def __init__(self, *args, **kwargs): pass
        async def start(self): pass
        async def wait(self): return 0
        async def aclose(self): raise RuntimeError('pending')
    with pytest.raises(CleanupPendingError, match='cleanup pending') as caught:
        asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner))
    assert caught.value.cleanup_pending is pending


def test_default_owner_factory_and_plain_cleanup_error(monkeypatch):
    import sys
    class Owner:
        pending_cleanup = None
        def diagnostic(self, error): return str(error)
        def __init__(self, *args, **kwargs): pass
        async def start(self): pass
        async def wait(self): return 0
        async def aclose(self): raise RuntimeError('dispose')
    monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
    with pytest.raises(RuntimeError, match='dispose'):
        asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'})))


@pytest.mark.parametrize('inspection', [None, 'http://127.0.0.1:9001'])
def test_terminal_status_prints_verified_destinations_and_optional_inspection(inspection):
    from api_agent.runtime.local_contracts import LocalDestination, LocalEvent, LocalSnapshot
    diagnostics = []
    class Owner:
        pending_cleanup = None
        def diagnostic(self, error): return str(error)
        def __init__(self, *args, on_event, **kwargs): self.event = on_event
        async def start(self):
            self.event(LocalEvent('status', LocalSnapshot('ready', destinations=(LocalDestination('Agent', 'https://localhost/'),), inspection_endpoint=inspection)))
        async def wait(self): return 0
        async def aclose(self): pass
    asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner, diagnostic=diagnostics.append))
    assert diagnostics[:2] == ['Run local: ready', 'Agent: https://localhost/']
    assert diagnostics[2:] == ([f'Browser CDP: {inspection}'] if inspection else [])


@pytest.mark.parametrize('handoff,expected', [(None, 7), (5, 5)])
def test_launcher_dispatches_terminal_local_operation_without_stdin(monkeypatch, handoff, expected):
    import os
    from pathlib import Path
    from api_agent.runtime.bootstrap import main
    calls = []
    monkeypatch.setattr(os, 'environ', {})
    monkeypatch.setattr('api_agent.runtime.worktrees.verify_worktrees', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    prepared_groups = []
    def prepare(*args, **kwargs):
        prepared_groups.append(kwargs['groups'])
        return handoff
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', prepare)
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('terminal local operation read stdin'))
    async def run(context, resolve, *, argv, schema, retain_terminal_cleanup):
        assert retain_terminal_cleanup
        calls.append(resolve().values)
        assert resolve().values['RUN_LOG_SOURCE'] == 'all'
        return 7
    monkeypatch.setattr('api_agent.runtime.local_terminal.run_local_terminal', run)
    assert main(['--tui-mode=run-local', '--no-std-in', '--unattended-user-index=0'], base_root=Path(__file__).resolve().parents[2]) == expected
    assert len(calls) == (1 if handoff is None else 0)
    assert prepared_groups == [('browser',)]


@pytest.mark.parametrize('container', [False, True])
def test_host_browser_uses_forwarder_only_inside_container(monkeypatch, container):
    import os
    import shutil
    import subprocess
    from api_agent.runtime.local_terminal import open_host_url
    calls = []
    monkeypatch.setattr(os, 'environ', {'DISTROBOX_ENTER_PATH': '/test'} if container else {'CONTAINER_ID': 'other-container'})
    monkeypatch.setattr(shutil, 'which', lambda name: '/usr/bin/distrobox-host-exec')
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    import webbrowser
    browser_calls = []
    monkeypatch.setattr(webbrowser, 'open', lambda *args, **kwargs: browser_calls.append((args, kwargs)) or True)
    asyncio.run(open_host_url('https://localhost/'))
    if container:
        assert calls[0][0][0] == ['/usr/bin/distrobox-host-exec', 'xdg-open', 'https://localhost/']
        assert calls[0][1]['timeout'] == 10
        assert calls[0][1]['check'] is True
        assert calls[0][1]['stdin'] == subprocess.DEVNULL
        assert browser_calls == []
    else:
        assert calls == []
        assert browser_calls == [(('https://localhost/',), {'new': 2})]


def test_missing_host_forwarder_is_actionable_without_spawning(monkeypatch):
    import os
    import shutil
    import subprocess
    from api_agent.runtime.local_terminal import open_host_url
    monkeypatch.setattr(os, 'environ', {'DISTROBOX_ENTER_PATH': '/test'})
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('must not spawn'))
    with pytest.raises(RuntimeError, match='Host browser forwarding is unavailable'):
        asyncio.run(open_host_url('https://localhost/'))


def test_default_browser_failure_is_actionable(monkeypatch):
    import os
    import webbrowser
    from api_agent.runtime.local_terminal import open_host_url
    monkeypatch.setattr(os, 'environ', {})
    monkeypatch.setattr(webbrowser, 'open', lambda *a, **k: False)
    with pytest.raises(RuntimeError, match='default browser could not open'):
        asyncio.run(open_host_url('https://localhost/'))


@pytest.mark.parametrize('failure,status', [(RuntimeError('synthetic-secret'), 1), (asyncio.CancelledError(), 130)])
def test_terminal_factory_failure_has_safe_fallback_and_no_owner_cleanup(failure, status):
    diagnostics = []
    def failed_factory(*args, **kwargs): raise failure
    assert asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}),
        owner_factory=failed_factory, diagnostic=diagnostics.append)) == status
    assert diagnostics == (['Local operation could not start.'] if status == 1 else [])


@pytest.mark.parametrize('failure', [KeyboardInterrupt('interrupted'), SystemExit(4)])
@pytest.mark.parametrize('retained', [False, True])
def test_terminal_cleanup_preserves_nonordinary_interruption(failure, retained):
    from api_agent.providers.control import CleanupPendingError
    async def retry(): pass
    pending = CleanupPendingError(('owner',), retry) if retained else None
    class Owner:
        pending_cleanup = pending
        def __init__(self, *args, **kwargs): pass
        async def start(self): pass
        async def wait(self): return 0
        async def aclose(self): raise failure
    with pytest.raises(type(failure)) as caught:
        asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner))
    assert caught.value is failure
    if retained:
        assert caught.value.cleanup_pending is pending


def test_clean_base_requirements_need_the_explicit_browser_extra():
    from pathlib import Path
    from packaging.requirements import Requirement
    from api_agent.runtime.dependencies import requirements
    root = Path(__file__).resolve().parents[2]
    context = NS(base_root=root, pack_root=root)
    base_names = {Requirement(raw).name for raw in requirements(context)}
    local_names = {Requirement(raw).name for raw in requirements(context, groups=('browser',))}
    assert 'playwright' not in base_names
    assert 'playwright-stealth' not in base_names
    assert {'playwright', 'playwright-stealth'} <= local_names
    assert base_names <= local_names


@pytest.mark.parametrize('credentials', [None, ('dashboard@example.test', 'terminal-password')])
def test_terminal_dashboard_credentials_use_only_literal_diagnostic(credentials):
    diagnostics, output = [], []
    class Owner:
        pending_cleanup = None
        def __init__(self, *args, **kwargs): pass
        async def start(self): pass
        def interactive_dashboard_credentials(self): return credentials
        async def wait(self): return 0
        async def aclose(self): pass
    assert asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}),
        owner_factory=Owner, output=output.append, diagnostic=diagnostics.append)) == 0
    assert output == []
    assert diagnostics == ([] if credentials is None else [
        'LangFuse dashboard account: dashboard@example.test',
        'LangFuse dashboard password: terminal-password'])


@pytest.mark.parametrize('phase', ['starting', 'stopping'])
def test_terminal_progress_displays_admitted_component_names(phase):
    from api_agent.runtime.local_contracts import LocalComponent, LocalEvent, LocalSnapshot
    diagnostics = []
    class Owner:
        pending_cleanup = None
        def __init__(self, *args, on_event, **kwargs): self.event = on_event
        async def start(self):
            self.event(LocalEvent('status', LocalSnapshot(phase, components=(
                LocalComponent('agent', phase), LocalComponent('service:extension/database', phase),
                LocalComponent('profile:extension-profile', phase)))))
        async def wait(self): return 0
        async def aclose(self): pass
    asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}),
        owner_factory=Owner, output=lambda text: None, diagnostic=diagnostics.append))
    assert diagnostics == [f'Run local: {phase}',
        ('Cleanup scope: ' if phase == 'stopping' else 'Selected components: ') +
        'agent, service:extension/database, profile:extension-profile']


def test_terminal_startup_activity_is_visible_with_other_log_source():
    from api_agent.runtime.local_contracts import LocalComponent, LocalEvent, LocalSnapshot
    diagnostics, output = [], []
    class Owner:
        pending_cleanup = None
        def __init__(self, *args, on_event, **kwargs): self.event = on_event
        async def start(self):
            snapshot = LocalSnapshot('starting', components=(LocalComponent('agent', 'starting'),),
                                     activity='view preparation')
            self.event(LocalEvent('status', snapshot))
            self.event(LocalEvent('status', snapshot))
            self.event(LocalEvent('log', source='agent', text='Local startup: view preparation.\n'))
        async def wait(self): return 0
        async def aclose(self): pass

    asyncio.run(run_local_terminal(None, lambda: NS(values={'RUN_LOG_SOURCE': 'ui'}),
        owner_factory=Owner, output=output.append, diagnostic=diagnostics.append))
    assert diagnostics == ['Run local: starting', 'Selected components: agent',
                           'Local startup: view preparation']
    assert output == ['']


@pytest.mark.parametrize('stall', ['owner', 'writer', 'writer-only'])
def test_repeated_process_sigint_keeps_owned_loop_until_cleanup_acknowledged(tmp_path, stall):
    import os
    from pathlib import Path
    import signal
    import subprocess
    import sys
    import time
    script = tmp_path / 'owned_terminal.py'
    release = tmp_path / 'release'
    log = tmp_path / 'terminal.log'
    script.write_text('''
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from api_agent.runtime.local_run import LocalRunOwner
import api_agent.runtime.local_terminal as terminal
release = Path(sys.argv[1])
stall = sys.argv[2]
async def held():
    while not release.exists():
        await asyncio.sleep(.005)
class Owner(LocalRunOwner):
    CLEANUP_ATTEMPT_SECONDS = .02
    CLEANUP_RETRY_SECONDS = .01
    async def start(self):
        self._started = True
        if stall == 'owner':
            self._stack.push(held)
        self._publish('ready')
    async def wait(self):
        if stall == 'writer-only':
            await self.finish_cleanup()
            return 0
        return await super().wait()
if stall in {'writer', 'writer-only'}:
    original = terminal.TerminalWriter._close
    async def close(self):
        print('writer-cleanup-paused', file=sys.stderr, flush=True)
        await held()
        await original(self)
    terminal.TerminalWriter._close = close
status = asyncio.run(terminal.run_local_terminal(None,
    lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner))
raise SystemExit(status)
''')
    def wait_for_text(child, expected):
        deadline = time.monotonic() + 8
        while expected not in log.read_text() and time.monotonic() < deadline:
            assert child.poll() is None, log.read_text()
            time.sleep(.01)
        assert expected in log.read_text(), log.read_text()
    with log.open('w') as output:
        child = subprocess.Popen([sys.executable, str(script), str(release), stall],
            stdout=output, stderr=output, start_new_session=True)
        try:
            wait_for_text(child, 'Run local: ready')
            if stall != 'writer-only':
                os.kill(child.pid, signal.SIGINT)
            wait_for_text(child, 'Run local: cleanup-pending' if stall == 'owner'
                          else 'writer-cleanup-paused')
            os.kill(child.pid, signal.SIGINT)
            os.kill(child.pid, signal.SIGINT)
            if stall == 'owner':
                wait_for_text(child, 'Cleanup pending; waiting for owned components to stop.')
            else:
                time.sleep(.03)
            assert child.poll() is None
            release.write_text('acknowledge')
            assert child.wait(timeout=8) == 130
        finally:
            release.touch()
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
    text = log.read_text()
    assert 'Run local: stopped' in text
    assert 'asynchronous generator is already running' not in text
    assert 'Traceback' not in text


def test_terminal_signal_owner_cancels_once_reports_repeat_and_restores(monkeypatch):
    from unittest.mock import Mock
    from api_agent.runtime import local_terminal as module
    installed, diagnostics = [], []
    task = NS(cancel=Mock())
    original = object()
    monkeypatch.setattr(module.asyncio, 'current_task', lambda: task)
    monkeypatch.setattr(module.signal, 'getsignal', lambda signum: original)
    monkeypatch.setattr(module.signal, 'signal', lambda signum, handler: installed.append(handler))
    with module._terminal_stop_signals(diagnostics.append) as state:
        assert state == {'requested': False}
        handler = installed[0]
        handler(None, None)
        handler(None, None)
        assert state['requested']
        task.cancel.assert_called_once_with()
    assert diagnostics == ['Cleanup pending; waiting for owned components to stop.']
    assert installed[-1] is original


def test_terminal_signal_owner_leaves_worker_thread_handlers_alone(monkeypatch):
    from api_agent.runtime import local_terminal as module
    monkeypatch.setattr(module.threading, 'current_thread', lambda: object())
    with module._terminal_stop_signals(lambda text: None) as state:
        assert state == {'requested': False}


def test_terminal_returns_interrupt_status_for_owned_signal_during_delivery_cleanup(monkeypatch):
    from contextlib import contextmanager
    from api_agent.runtime import local_terminal as module
    @contextmanager
    def signalled(diagnostic):
        yield {'requested': True}
    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(module, '_terminal_stop_signals', signalled)
    monkeypatch.setattr(module, '_run_local_terminal', interrupted)
    assert asyncio.run(module.run_local_terminal(None, None, output=lambda text: None,
        diagnostic=lambda text: None)) == 130


@pytest.mark.parametrize('retained', ['cleanup_pending', 'terminal_cleanup_error'])
def test_owned_signal_does_not_hide_terminal_cleanup_failure(monkeypatch, retained):
    from contextlib import contextmanager
    from api_agent.runtime import local_terminal as module
    error = asyncio.CancelledError()
    evidence = RuntimeError('retained terminal disposal failure')
    setattr(error, retained, evidence)
    @contextmanager
    def signalled(diagnostic):
        yield {'requested': True}
    async def interrupted(*args, **kwargs):
        raise error
    monkeypatch.setattr(module, '_terminal_stop_signals', signalled)
    monkeypatch.setattr(module, '_run_local_terminal', interrupted)
    with pytest.raises(asyncio.CancelledError) as caught:
        asyncio.run(module.run_local_terminal(None, None, output=lambda text: None,
            diagnostic=lambda text: None))
    assert caught.value is error
    assert getattr(caught.value, retained) is evidence


@pytest.mark.parametrize('interrupt', [False, True])
@pytest.mark.parametrize('allocation_rejected', [False, True])
def test_owning_terminal_keeps_real_descriptor_duty_until_restored(monkeypatch, interrupt, allocation_rejected):
    import os
    import signal
    import sys
    from api_agent.runtime import local_terminal as module
    out_read, out_write = os.pipe()
    err_read, err_write = os.pipe()
    os.set_blocking(out_read, False)
    os.set_blocking(err_read, False)
    original = os.set_blocking
    stdout_inode = os.fstat(out_write).st_ino
    repaired = False
    fault = RuntimeError('cleanup task allocation failed') if allocation_rejected else OSError('descriptor restoration failed')
    restoration = OSError('descriptor restoration failed') if allocation_rejected else fault
    original_task = asyncio.create_task
    def create_task(coroutine, *args, **kwargs):
        if allocation_rejected and coroutine.cr_code.co_name == '_finish_terminal_writer':
            raise fault
        return original_task(coroutine, *args, **kwargs)
    def blocking(fd, enabled):
        if enabled and os.fstat(fd).st_ino == stdout_inode and not repaired:
            raise restoration
        original(fd, enabled)
    class Owner:
        pending_cleanup = None
        def __init__(self, *args, **kwargs): pass
        async def start(self): pass
        async def wait(self): return 0
        async def aclose(self): pass
    async def exercise():
        nonlocal repaired
        with monkeypatch.context() as patch:
            patch.setattr(sys, 'stdout', NS(fileno=lambda: out_write))
            patch.setattr(sys, 'stderr', NS(fileno=lambda: err_write))
            patch.setattr(os, 'set_blocking', blocking)
            patch.setattr(asyncio, 'create_task', create_task)
            task = asyncio.create_task(module.run_local_terminal(None,
                lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner,
                retain_terminal_cleanup=True))
            text = b''
            try:
                deadline = asyncio.get_running_loop().time() + 2
                while b'cleanup pending' not in text and asyncio.get_running_loop().time() < deadline:
                    try:
                        text += os.read(err_read, 4096)
                    except BlockingIOError:
                        pass
                    await asyncio.sleep(.005)
                assert b'Terminal output cleanup failed; cleanup pending' in text
                assert not task.done()
                assert not os.get_blocking(out_write)
                with pytest.raises(BlockingIOError):
                    os.read(out_read, 4096)
                if interrupt:
                    os.kill(os.getpid(), signal.SIGINT)
                    await asyncio.sleep(0)
                    os.kill(os.getpid(), signal.SIGINT)
                assert not task.done()
                repaired = True
                with pytest.raises(type(fault)) as caught:
                    await task
                assert caught.value is fault
                assert caught.value.terminal_cleanup_acknowledged
                assert not hasattr(caught.value, 'cleanup_pending')
                assert not hasattr(caught.value, 'terminal_cleanup_pending')
                assert os.get_blocking(out_write)
                assert os.get_blocking(err_write)
            finally:
                repaired = True
                if not task.done():
                    try:
                        await task
                    except type(fault):
                        pass
                if not task.cancelled():
                    failure = task.exception()
                    receipt = getattr(failure, 'terminal_cleanup_pending', None)
                    if receipt is not None:
                        await receipt.retry()
    try:
        asyncio.run(exercise())
    finally:
        for fd in (out_read, out_write, err_read, err_write):
            os.close(fd)


@pytest.mark.parametrize('chain', ['none', 'nested', 'cycle'])
def test_terminal_descriptor_acknowledgement_preserves_other_cleanup_metadata(monkeypatch, chain):
    from api_agent.runtime import local_terminal as module
    receipt = object()
    fault = OSError('restoration failed')
    other = RuntimeError('other cleanup')
    if chain == 'nested':
        fault.cleanup_pending = other
        other.cleanup_pending = receipt
    elif chain == 'cycle':
        fault.cleanup_pending = fault
    diagnostics = []
    class Writer:
        owned = {1: 2}
        descriptor_cleanup = receipt
        attempts = 0
        async def finish(self): raise fault
        def cleanup_status(self, text): return False
        async def retry_descriptors(self):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError('still pending')
            self.owned = {}
    writer = Writer()
    with pytest.raises(OSError) as caught:
        asyncio.run(module._finish_terminal_writer(writer, True, diagnostics.append))
    assert caught.value is fault
    assert writer.attempts == 2
    assert writer.descriptor_cleanup is None
    assert fault.terminal_cleanup_acknowledged
    assert len(diagnostics) == 2
    if chain == 'nested':
        assert fault.cleanup_pending is other
        assert not hasattr(other, 'cleanup_pending')
    elif chain == 'cycle':
        assert fault.cleanup_pending is fault
