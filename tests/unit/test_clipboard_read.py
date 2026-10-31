"""Native reads use synthetic subprocesses and never the host clipboard."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.tui import clipboard


@pytest.mark.parametrize(('platform', 'wayland', 'tool', 'command'), [
    ('darwin', '', 'pbpaste', ['/fake/pbpaste']),
    ('linux', 'synthetic', 'wl-paste', ['/fake/wl-paste', '--no-newline']),
    ('linux', '', None, None),
    ('win32', '', None, None),
])
def test_read_command(monkeypatch, platform, wayland, tool, command):
    monkeypatch.setattr(clipboard.sys, 'platform', platform)
    monkeypatch.setenv('WAYLAND_DISPLAY', wayland)
    which = Mock(side_effect=lambda name: '/fake/' + name)
    monkeypatch.setattr(clipboard.shutil, 'which', which)
    assert clipboard.native_read_command() == command
    assert which.call_args_list == ([] if tool is None else [((tool,), {})])


def process(data=b'synthetic', status=0):
    return Mock(stdout=Mock(read=AsyncMock(side_effect=[data, b''])),
                wait=AsyncMock(return_value=status), returncode=status)


def setup(monkeypatch, proc):
    monkeypatch.setattr(clipboard, 'native_read_command', lambda: ['/fake/pbpaste'])
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(clipboard.asyncio, 'create_subprocess_exec', spawn)
    return spawn


def test_read_success_and_exact_limit(monkeypatch):
    proc = process(b'a' * 65536)
    spawn = setup(monkeypatch, proc)
    assert asyncio.run(clipboard.read_text()) == 'a' * 65536
    assert proc.stdout.read.await_args_list == [((65537,), {}), ((1,), {})]
    spawn.assert_awaited_once_with('/fake/pbpaste', stdout=asyncio.subprocess.PIPE,
                                  stderr=asyncio.subprocess.DEVNULL, limit=65537)


@pytest.mark.parametrize('data,status', [pytest.param(b'x' * 65537, 0, id='oversized-output'), (b'\xff', 0), (b'synthetic', 1)])
def test_invalid_output_has_fixed_error(monkeypatch, data, status):
    proc = process(data, status)
    setup(monkeypatch, proc)
    with pytest.raises(clipboard.ClipboardError, match='Use terminal Paste') as error:
        asyncio.run(clipboard.read_text())
    assert 'synthetic' not in str(error.value)


def test_unavailable_helper(monkeypatch):
    monkeypatch.setattr(clipboard, 'native_read_command', lambda: None)
    with pytest.raises(clipboard.ClipboardError, match='Use terminal Paste'):
        asyncio.run(clipboard.read_text())


def test_missing_executable(monkeypatch):
    monkeypatch.setattr(clipboard.sys, 'platform', 'darwin')
    monkeypatch.setattr(clipboard.shutil, 'which', lambda _: None)
    assert clipboard.native_read_command() is None


def test_spawn_error(monkeypatch):
    spawn = setup(monkeypatch, process())
    spawn.side_effect = OSError('synthetic-private')
    with pytest.raises(clipboard.ClipboardError, match='Use terminal Paste'):
        asyncio.run(clipboard.read_text())


@pytest.mark.parametrize('failure', [OSError('synthetic-private'), TimeoutError()])
def test_read_error_kills_and_reaps(monkeypatch, failure):
    proc = process()
    proc.returncode = None
    proc.stdout.read.side_effect = failure
    setup(monkeypatch, proc)
    with pytest.raises(clipboard.ClipboardError, match='Use terminal Paste'):
        asyncio.run(clipboard.read_text())
    proc.kill.assert_called_once_with()
    proc.wait.assert_awaited_once_with()


def test_cancel_kills_and_reaps(monkeypatch):
    proc = process()
    proc.returncode = None
    proc.stdout.read.side_effect = asyncio.CancelledError()
    setup(monkeypatch, proc)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(clipboard.read_text())
    proc.kill.assert_called_once_with()
    proc.wait.assert_awaited_once_with()


def test_exited_during_cleanup(monkeypatch):
    proc = process()
    proc.returncode = None
    proc.stdout.read.side_effect = OSError()
    proc.kill.side_effect = ProcessLookupError()
    setup(monkeypatch, proc)
    with pytest.raises(clipboard.ClipboardError):
        asyncio.run(clipboard.read_text())
    proc.wait.assert_awaited_once_with()


def test_partial_chunks_are_joined(monkeypatch):
    proc = process()
    proc.stdout.read.side_effect = [b'first', b'second', b'']
    setup(monkeypatch, proc)
    assert asyncio.run(clipboard.read_text()) == 'firstsecond'


def test_cancellation_during_launch_still_reaps(monkeypatch):
    async def scenario():
        launched = asyncio.Event()
        release = asyncio.Event()
        proc = process()
        proc.returncode = None

        async def spawn(*args, **kwargs):
            launched.set()
            await release.wait()
            return proc

        setup(monkeypatch, proc)
        monkeypatch.setattr(clipboard.asyncio, 'create_subprocess_exec', spawn)
        task = asyncio.create_task(clipboard.read_text())
        await launched.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        proc.kill.assert_called_once_with()
        proc.wait.assert_awaited_once_with()

    asyncio.run(scenario())


def test_deadline_interrupts_a_stalled_reader(monkeypatch):
    async def scenario():
        proc = process()
        proc.returncode = None
        stalled = asyncio.Event()
        async def read(_):
            await stalled.wait()
        proc.stdout.read.side_effect = read
        setup(monkeypatch, proc)
        timeout = clipboard.component_timeout
        observed = []

        def short_timeout(name, seconds):
            observed.append((name, seconds))
            return timeout(name, 0.01)

        monkeypatch.setattr(clipboard, 'component_timeout', short_timeout)
        with pytest.raises(clipboard.ClipboardError, match='Use terminal Paste'):
            await clipboard.read_text()
        assert observed == [('Clipboard read', 2)]
        proc.kill.assert_called_once_with()
        proc.wait.assert_awaited_once_with()

    asyncio.run(scenario())
