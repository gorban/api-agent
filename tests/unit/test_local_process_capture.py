"""Bounded owned output capture uses OS pipes and controlled child state."""
import asyncio
import os
import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
from api_agent.ui import process


class PipeChild:
    def __init__(self):
        read, self.writer = os.pipe()
        self.stdout = os.fdopen(read, 'rb')
        self.code = None
    def poll(self): return self.code
    def terminate(self): self.code = 0
    def kill(self): self.code = -9


@pytest.mark.parametrize('payload', [
    b'first-line\nsecond-line',
    b'{"access\\u005ftoken":"encoded-material"}',
    b'first split-secret trailing',
    b'first split-sec',
    'utf8-é-material'.encode(),
    b'\x1b[2Junsafe terminal command',
    b'x' * 20000,
], ids=('multiline', 'encoded-key', 'registered', 'truncated', 'utf8', 'terminal-controls', 'oversized'))
@pytest.mark.parametrize('allow_content', [False, True])
def test_capture_emits_activity_only_for_arbitrary_child_bytes(payload, allow_content):
    child, observed = PipeChild(), []
    boundary = EmissionBoundary(ContentPolicy(allow_content))
    boundary.register_secret('first-line\nsecond-line')
    boundary.register_secret('split-secret')
    async def run():
        owner = process.OwnedStudioProcess(child)
        owner.capture(observed.append, boundary)
        chunk_size = max(5, len(payload) // 8)
        for position in range(0, len(payload), chunk_size):
            os.write(child.writer, payload[position:position + chunk_size])
            await asyncio.sleep(.03)
        await asyncio.sleep(.03)
        os.close(child.writer)
        await owner.aclose()
        await owner.aclose()
        assert observed
        import re
        assert all(re.fullmatch(r'UI process output: [1-9][0-9]{0,3} bytes received; content omitted.\n', text)
                   for text in observed)
        assert owner._reader.done() and child.stdout.closed
    asyncio.run(run())


def test_live_capture_requires_emission_boundary():
    with pytest.raises(ValueError, match='boundary'):
        process.StudioProcesses(ssl_context=ssl.create_default_context(), log_callback=lambda text: None)


def test_launch_registers_token_before_first_output(monkeypatch):
    child, observed, options = PipeChild(), [], []
    boundary = EmissionBoundary(ContentPolicy())
    boundary.register_secret = Mock(wraps=boundary.register_secret)
    monkeypatch.setattr(process, 'token_urlsafe', lambda length: 'launch-token')
    def spawn(*args, **kwargs):
        boundary.register_secret.assert_called_once_with('launch-token')
        options.append(kwargs)
        os.write(child.writer, b'launch-token output\n')
        return child
    monkeypatch.setattr(process.subprocess, 'Popen', spawn)
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def head(self, url): return SimpleNamespace(status_code=200, headers={'X-UI-Launch-Token': 'launch-token'})
    monkeypatch.setattr(process.httpx, 'AsyncClient', lambda **kwargs: Client())
    async def run():
        launcher = process.StudioProcesses(ssl_context=ssl.create_default_context(),
            log_callback=observed.append, emissions=boundary)
        owner = await launcher.start(('synthetic',), cwd='.', host='localhost', port=8000, scheme='http', env={})
        await asyncio.sleep(.03)
        await owner.aclose()
        os.close(child.writer)
        assert options[0]['stdout'] == process.subprocess.PIPE
        assert options[0]['stderr'] == process.subprocess.STDOUT
        assert observed and 'launch-token' not in observed[0]
    asyncio.run(run())


def test_pending_reader_retains_pipe_until_cleanup_retry():
    child = PipeChild()
    async def run():
        owner = process.OwnedStudioProcess(child, timeout=.01)
        gate = asyncio.Event()
        owner._reader = asyncio.create_task(gate.wait())
        owner._output_owned = True
        with pytest.raises(CleanupPendingError) as pending: await owner.aclose()
        assert not child.stdout.closed
        gate.set()
        await pending.value.retry()
        os.close(child.writer)
        assert child.stdout.closed
    asyncio.run(run())


def test_failed_reader_does_not_retain_finished_child():
    child = PipeChild()
    async def run():
        owner = process.OwnedStudioProcess(child)
        async def fail(): raise ValueError('consumer failed')
        owner._reader = asyncio.create_task(fail())
        owner._output_owned = True
        await owner.aclose()
        os.close(child.writer)
        assert child.stdout.closed
    asyncio.run(run())


def test_cancelled_reader_and_capture_setup_failure_close_owned_pipe(monkeypatch):
    child = PipeChild()
    async def run():
        owner = process.OwnedStudioProcess(child)
        owner._output_owned = True
        owner._reader = asyncio.create_task(asyncio.sleep(60))
        owner._reader.cancel()
        await owner.aclose()
        os.close(child.writer)
        assert child.stdout.closed
        failed = PipeChild()
        owned = process.OwnedStudioProcess(failed)
        monkeypatch.setattr(process.os, 'set_blocking', Mock(side_effect=OSError('setup failed')))
        with pytest.raises(OSError): owned.capture(lambda text: None, EmissionBoundary(ContentPolicy()))
        await owned.aclose()
        os.close(failed.writer)
        assert failed.stdout.closed
    asyncio.run(run())


def test_continuously_readable_output_yields_to_peer_tasks(monkeypatch):
    child = PipeChild()
    reads, peer_observations = [], []
    async def run():
        async def peer(): peer_observations.append(len(reads))
        def read(descriptor, count):
            reads.append(descriptor)
            if len(reads) == 1: asyncio.create_task(peer())
            return b'x' if len(reads) <= 1024 else b''
        monkeypatch.setattr(process.os, 'read', read)
        owner = process.OwnedStudioProcess(child)
        owner.capture(lambda text: None, EmissionBoundary(ContentPolicy()))
        await owner._reader
        assert peer_observations == [1]
        await owner.aclose()
        os.close(child.writer)
    asyncio.run(run())
