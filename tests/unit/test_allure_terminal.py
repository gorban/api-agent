"""Terminal history ownership, interruption and safe diagnostics."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from api_agent.runtime.allure_terminal import TerminalAllureOwner, run_allure_terminal
from api_agent.providers.control import CleanupPendingError

@pytest.mark.parametrize('failure,expected', [(None, 0), (ValueError('startup failed'), 1), (asyncio.CancelledError(), 130), (KeyboardInterrupt(), 130)])
def test_terminal_disposes_same_loop_without_stdin(failure, expected, monkeypatch):
    actions, output = [], []
    monkeypatch.setattr('builtins.input', lambda *args: pytest.fail('stdin read'))
    class Owner:
        def __init__(self, *args): pass
        async def start(self):
            actions.append(asyncio.get_running_loop())
            if isinstance(failure, ValueError): raise failure
            return NS(url='http://127.0.0.1:5252/', inspection_endpoint='http://127.0.0.1:9000/', browser_error='secret-token')
        async def aclose(self): actions.append(asyncio.get_running_loop())
    async def wait():
        if failure: raise failure
    assert asyncio.run(run_allure_terminal(None, NS(values={}), owner_factory=Owner, output=output.append, diagnostic=output.append, wait=wait)) == expected
    assert len(actions) == 2 and actions[0] is actions[1]
    assert 'secret-token' not in '\n'.join(output)


def test_owner_retains_failed_viewer_but_also_closes_reporting():
    owner = TerminalAllureOwner(None, NS(values={}))
    viewer = NS(show_archive=AsyncMock(return_value='location'), aclose=AsyncMock(side_effect=ValueError('secret')))
    owner.allure_viewer = viewer
    owner.prepare_allure_reporting = AsyncMock()
    owner.allure_source = lambda: 'source'
    owner.reporting_sessions = NS(aclose=AsyncMock())
    assert asyncio.run(owner.start()) == 'location'
    viewer.show_archive.assert_awaited_once_with('source')
    with pytest.raises(ValueError) as caught: asyncio.run(owner.aclose())
    owner.reporting_sessions.aclose.assert_awaited_once()
    assert caught.value.cleanup_pending is not None
    viewer.aclose.side_effect = None
    asyncio.run(caught.value.cleanup_pending.retry())
    assert viewer.aclose.await_count == 2


def test_cleanup_failure_remains_visible():
    class Owner:
        def __init__(self, *args): pass
        async def start(self): raise ValueError('secret')
        async def aclose(self):
            if not getattr(self, 'attempted', False):
                self.attempted = True
                raise CleanupPendingError((self,), self.aclose)
    with pytest.raises(Exception, match='cleanup pending'):
        asyncio.run(run_allure_terminal(None, NS(values={}), owner_factory=Owner, output=lambda line: None))


def test_repeated_cancellation_drains_cleanup_before_returning():
    async def scenario():
        closing, release = asyncio.Event(), asyncio.Event()
        class Owner:
            def __init__(self, *args): pass
            async def start(self): return NS(url='http://127.0.0.1:5252/', inspection_endpoint=None, browser_error=None)
            async def aclose(self):
                closing.set()
                await release.wait()
        task = asyncio.create_task(run_allure_terminal(None, NS(values={}), owner_factory=Owner, output=lambda line: None))
        await asyncio.sleep(0)
        task.cancel()
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        assert await task == 130
    asyncio.run(scenario())


@pytest.mark.parametrize('handoff', [None, 8])
def test_terminal_dispatch_prepares_dependencies_and_preserves_status(handoff):
    from pathlib import Path
    from unittest.mock import patch
    from api_agent.runtime.bootstrap import main
    root = Path(__file__).resolve().parents[2]
    with patch.dict('os.environ', {}, clear=True), patch('api_agent.config.read_dotenv', return_value={}), \
         patch('api_agent.config.dotenv.read_dotenv', return_value={}), \
         patch('api_agent.runtime.worktrees.verify_worktrees'), \
         patch('api_agent.runtime.dependencies.ensure_environment', return_value=handoff) as prepare, \
         patch('api_agent.runtime.allure_terminal.run_allure_terminal', new_callable=AsyncMock, return_value=130) as run:
        assert main(['--tui-mode=allure', '--no-std-in', '--allure-s3-use-localstack=False'], base_root=root) == (130 if handoff is None else handoff)
    assert prepare.call_args.kwargs['groups'] == ('dev', 'browser')
    assert run.await_count == (1 if handoff is None else 0)


def test_logging_failure_is_fatal_and_still_disposes():
    closed = []
    class Owner:
        def __init__(self, *args): pass
        async def start(self): return NS(url='http://127.0.0.1:5252/', inspection_endpoint=None, browser_error=None)
        async def aclose(self): closed.append(True)
    def output(line): raise OSError('closed output')
    with pytest.raises(OSError, match='closed output'):
        asyncio.run(run_allure_terminal(None, NS(values={}), owner_factory=Owner, output=output))
    assert closed == [True]


@pytest.mark.parametrize('failure', [asyncio.CancelledError(), KeyboardInterrupt()])
def test_startup_interrupt_closes_before_returning(failure):
    closed = []
    class Owner:
        def __init__(self, *args): pass
        async def start(self): raise failure
        async def aclose(self): closed.append(True)
    assert asyncio.run(run_allure_terminal(None, NS(values={}), owner_factory=Owner, output=lambda line: None)) == 130
    assert closed == [True]


def test_real_cleanup_failure_retries_same_owner_and_remains_failure():
    owners, calls, output = [], [], []
    class Owner(TerminalAllureOwner):
        def __init__(self, context, result):
            super().__init__(context, result)
            owners.append(self)
            async def close():
                calls.append(asyncio.get_running_loop())
                if len(calls) == 1: raise OSError('disposal failed')
            self.allure_viewer = NS(aclose=close)
            self.reporting_sessions = NS(aclose=AsyncMock())
        async def start(self): return NS(url='http://127.0.0.1:5252/', inspection_endpoint=None, browser_error=None)
    async def wait(): pass
    with pytest.raises(Exception, match='disposal failed'):
        asyncio.run(run_allure_terminal(None, NS(values={}), owner_factory=Owner, output=output.append, diagnostic=output.append, wait=wait))
    assert len(owners) == 1 and len(calls) == 2 and calls[0] is calls[1]
    owners[0].reporting_sessions.aclose.assert_awaited_once()
    assert sum('retrying owned resources' in line for line in output) == 1


def test_owner_without_viewer_can_close_and_repeated_close_is_noop():
    owner = TerminalAllureOwner(None, NS(values={}))
    owner.reporting_sessions = NS(aclose=AsyncMock())
    asyncio.run(owner.aclose())
    asyncio.run(owner.aclose())
    owner.reporting_sessions.aclose.assert_awaited_once()


@pytest.mark.parametrize('delivery_failure', [False, True])
def test_repeated_pending_cleanup_is_retained_even_if_diagnostics_fail(delivery_failure):
    attempts, output = [], []
    class Owner(TerminalAllureOwner):
        def __init__(self, context, result):
            super().__init__(context, result)
            async def close():
                attempts.append(True)
                if len(attempts) < 3: raise CleanupPendingError((self,), self.aclose)
            self.allure_viewer = NS(aclose=close)
            self.reporting_sessions = NS(aclose=AsyncMock())
        async def start(self): return NS(url='http://127.0.0.1:5252/', inspection_endpoint=None, browser_error=None)
    def write(line):
        output.append(line)
        if delivery_failure and 'retrying' in line: raise OSError('delivery failed')
    async def wait(): pass
    with pytest.raises(Exception, match='delivery failed' if delivery_failure else 'cleanup pending'):
        asyncio.run(run_allure_terminal(None, NS(values={}), owner_factory=Owner, output=write, diagnostic=write, wait=wait))
    assert len(attempts) == 3
    assert sum('retrying' in line for line in output) == 1


def test_unknown_cleanup_failure_is_fatal():
    class Owner:
        def __init__(self, *args): pass
        async def start(self): return NS(url='http://127.0.0.1:5252/', inspection_endpoint=None, browser_error=None)
        async def aclose(self): raise ValueError('fatal disposal')
    async def wait(): pass
    with pytest.raises(Exception, match='fatal disposal'):
        asyncio.run(run_allure_terminal(None, NS(values={}), owner_factory=Owner, output=lambda line: None, wait=wait))


def test_real_repeated_sigint_waits_for_cleanup_acknowledgement(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time
    script = '''
import asyncio, pathlib, sys
from types import SimpleNamespace as NS
from api_agent.runtime.allure_terminal import run_allure_terminal
root = pathlib.Path(sys.argv[1])
class Owner:
    def __init__(self, *args): pass
    async def start(self):
        (root / 'ready').touch()
        return NS(url='http://127.0.0.1:5252/', inspection_endpoint=None, browser_error=None)
    async def aclose(self):
        (root / 'closing').touch()
        while not (root / 'release').exists(): await asyncio.sleep(.01)
        (root / 'acknowledged').touch()
status = asyncio.run(run_allure_terminal(None, NS(values={}), owner_factory=Owner))
(root / 'status').write_text(str(status))
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def await_file(name):
        deadline = time.monotonic() + 10
        while not (tmp_path / name).exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, name
            time.sleep(.01)
    try:
        await_file('ready')
        import selectors
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), 'history URL must flush before shutdown'
            assert process.stdout.readline().startswith('Allure history: http://127.0.0.1:5252/')
        os.kill(process.pid, signal.SIGINT)
        await_file('closing')
        os.kill(process.pid, signal.SIGINT)
        time.sleep(.1)
        assert process.poll() is None
        assert not (tmp_path / 'acknowledged').exists()
        (tmp_path / 'release').touch()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0
        assert (tmp_path / 'acknowledged').exists()
        assert (tmp_path / 'status').read_text() == '130'
        assert 'Cleanup pending' in stderr
        assert 'unhandled' not in stderr.lower() and 'Traceback' not in stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_startup_diagnostic_uses_stderr_and_scrubs_credentials(capsys):
    class Owner:
        def __init__(self, *args): pass
        async def start(self): raise OSError('unavailable: synthetic-secret-value')
        async def aclose(self): pass
    assert asyncio.run(run_allure_terminal(None, NS(values={'API_KEY': 'synthetic-secret-value'}), owner_factory=Owner)) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'Allure history startup' in captured.err
    assert 'unavailable' in captured.err
    assert 'synthetic-secret-value' not in captured.err


def test_signal_notice_delivery_failure_is_deferred_until_cleanup_ack(monkeypatch):
    from contextlib import contextmanager
    notices, actions = [], []
    @contextmanager
    def signals(diagnostic):
        notices.append(diagnostic)
        yield
    monkeypatch.setattr('api_agent.runtime.local_terminal._terminal_stop_signals', signals)
    class Owner:
        def __init__(self, *args): pass
        async def start(self): return NS(url='http://127.0.0.1:5252/', inspection_endpoint=None, browser_error=None)
        async def aclose(self):
            notices[0]('Cleanup pending')
            actions.append('acknowledged')
    def diagnostic(text): raise OSError('signal notice delivery failed')
    async def wait(): pass
    with pytest.raises(OSError, match='signal notice delivery failed'):
        asyncio.run(run_allure_terminal(None, NS(values={}), owner_factory=Owner,
                                        output=lambda line: None, diagnostic=diagnostic, wait=wait))
    assert actions == ['acknowledged']
