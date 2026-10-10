"""Offline conformance through real launcher workers and loopback IPC."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from api_agent.providers.progress import component_timeout
from api_agent.testing.collection import collect_tests
from api_agent.testing.integration import run_integration_tests
from api_agent.testing.setup_backend import SetupBackend


_FACTORY = '''
import asyncio
import json
import os
from pathlib import Path
from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.progress import component_timeout

class Provider:
    version = 1
    capabilities = ProviderCapabilities(kind='llm')
    def __init__(self, config):
        self.config = config
        self.emit('provider_load')
    def emit(self, kind):
        with open(self.config['SYNTHETIC_EVENTS'], 'a') as stream:
            stream.write(json.dumps(dict(kind=kind, pid=os.getpid())) + '\\n')
    async def asetup(self, *, interactive, user_index, credentials):
        assert user_index is None and credentials is None
        self.emit('setup_started')
        try:
            async with component_timeout('Synthetic setup gate', 15):
                while not Path(self.config['SYNTHETIC_RELEASE']).exists():
                    await asyncio.sleep(.02)
                await asyncio.sleep(1.1)
        except TimeoutError:
            self.emit('setup_deadline')
            raise
        except asyncio.CancelledError:
            self.emit('setup_cancelled')
            raise
        self.emit('setup_finished')
        provider = self
        class Authentication:
            async def __call__(self):
                provider.emit('auth_checkpoint')
                return object()
            async def aclose(self):
                provider.emit('auth_close')
        return Authentication()
    async def aclose(self):
        self.emit('provider_close')

def factory(config, services):
    return Provider(config)
'''

_SUITE = '''
import asyncio
import json
import os
from pathlib import Path
import pytest
from api_agent.providers.progress import component_timeout

@pytest.mark.parametrize('slot', [0, 1])
def test_shared_readiness(slot, llm_provider, integration_config):
    config = integration_config.values
    suite = Path(__file__).parents[2].name
    with open(config['SYNTHETIC_EVENTS'], 'a') as stream:
        stream.write(json.dumps(dict(kind='worker_request', pid=os.getpid(), suite=suite, slot=slot)) + '\\n')
    async def release_when_both_workers_arrive():
        async with component_timeout('Synthetic worker barrier', 10):
            while True:
                rows = [json.loads(line) for line in Path(config['SYNTHETIC_EVENTS']).read_text().splitlines()]
                requests = [row for row in rows if row['kind'] == 'worker_request' and row['suite'] == suite]
                if len(requests) == 2:
                    Path(config['SYNTHETIC_RELEASE']).touch()
                    return
                await asyncio.sleep(.02)
    asyncio.run(release_when_both_workers_arrive())
    llm_provider.ensure_ready()
    with open(config['SYNTHETIC_EVENTS'], 'a') as stream:
        stream.write(json.dumps(dict(kind='worker_ready', pid=os.getpid(), suite=suite, slot=slot)) + '\\n')
'''


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _write(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def _provider(tmp_path):
    events = tmp_path / 'synthetic-events.jsonl'
    events.touch()
    release = tmp_path / 'synthetic-release'
    factory = tmp_path / 'synthetic-provider' / 'factory.py'
    _write(factory, _FACTORY)
    config = {'LLM_PROVIDER': str(factory) + ':factory', 'DEBUG': False, 'NO_STD_IN': True,
              'SYNTHETIC_EVENTS': str(events), 'SYNTHETIC_RELEASE': str(release)}
    return config, events, release, factory.parent


def _source_environment(monkeypatch):
    source = Path(__file__).resolve().parents[2] / 'src'
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(filter(None, (str(source), os.environ.get('PYTHONPATH')))))


def test_one_backend_across_real_base_pack_provider_suites(tmp_path, monkeypatch):
    _source_environment(monkeypatch)
    config, evidence, _, provider_root = _provider(tmp_path)
    base, pack = tmp_path / 'synthetic-base', tmp_path / 'synthetic-pack'
    base_suite, pack_suite = base / 'tests/integration', pack / 'tests/integration'
    provider_suite = provider_root / 'tests/integration'
    for suite, filename in ((base_suite, 'test_base.py'), (pack_suite, 'test_pack.py'),
                            (provider_suite, 'test_provider.py')):
        _write(suite / filename, _SUITE)
    _write(provider_root / 'cfg/tests.toml', 'version = 1\n[tests]\nintegration = ["tests/integration"]\n')
    context = SimpleNamespace(base_root=base, owner_root=pack,
                              suite_paths=lambda mode: (pack_suite,))
    selected = collect_tests(context, 'integration', config=config)
    assert len(selected) == 6 and len({test.repo for test in selected}) == 3
    assert _rows(evidence) == []  # Declarative collection never loads the factory.
    events, publications = [], []
    def after_run(result):
        publications.append((result, _rows(evidence)))
    result = run_integration_tests(context, selected, accepted=True, config=config,
        parallel_jobs=2, on_event=events.append, after_run=after_run)
    assert result.exit_code == 0, result.output
    assert len(result.tests) == 6 and all(test.status == 'passed' for test in result.tests)
    assert result.inspections == ()
    assert len(publications) == 1
    rows = _rows(evidence)
    kinds = [row['kind'] for row in rows]
    assert kinds.count('provider_load') == kinds.count('setup_started') == kinds.count('setup_finished') == 1
    assert kinds.count('auth_close') == kinds.count('provider_close') == 1
    assert kinds.count('setup_cancelled') == 0
    assert kinds.count('worker_request') == kinds.count('worker_ready') == 6
    owner_rows = [row for row in rows if row['kind'] not in ('worker_request', 'worker_ready')]
    assert {row['pid'] for row in owner_rows} == {os.getpid()}
    requests = [row for row in rows if row['kind'] == 'worker_request']
    assert len({row['pid'] for row in requests}) == 6  # Two fresh processes in each suite.
    assert kinds.index('auth_close') < kinds.index('provider_close')
    assert kinds.index('provider_close') > max(i for i, kind in enumerate(kinds) if kind == 'worker_ready')
    assert publications[0][1] == rows  # Disposal completes before after_run publication.
    assert publications[0][0].exit_code == 0
    assert kinds.count('setup_deadline') == 0
    selected_ids = {test.id for test in selected}
    timing = [event for event in events if event.kind == 'component_progress']
    assert timing and all(event.identity.id in selected_ids and event.repo == event.identity.repo for event in timing)
    shared_reports = {}
    for event in timing:
        if event.component.name == 'Shared provider setup':
            shared_reports.setdefault(event.component.id, set()).add(event.identity.id)
    assert any(len(identities) == 2 for identities in shared_reports.values())
    for identity in selected:
        reports = [event.component for event in timing if event.identity == identity]
        assert any(component.name == 'Shared provider setup' and component.active for component in reports)
        assert any(component.name == 'Shared provider setup' and not component.active for component in reports)
        assert any(component.name == 'Worker shared setup' and component.active for component in reports)
        assert any(component.name == 'Worker shared setup' and not component.active for component in reports)


_SIBLING = '''
import json
from pathlib import Path
import sys
from api_agent.providers.progress import progress_sink
from api_agent.providers.setup_transport import SetupClient

descriptor = json.loads(sys.argv[1])
progress, finished = map(Path, sys.argv[2:])
def report(event):
    if event.name == 'Shared provider setup' and event.active:
        progress.touch()
client = SetupClient(descriptor, worker_id='surviving-sibling', timeout=10)
try:
    with progress_sink(report):
        client.ensure_ready()
    finished.touch()
finally:
    client.close()
'''


def _wait(predicate):
    async def poll():
        async with component_timeout('Synthetic conformance observation', 12):
            while not predicate():
                await asyncio.sleep(.02)
    asyncio.run(poll())


def _communicate(process):
    async def communicate():
        async with component_timeout('Synthetic child completion', 12):
            return await asyncio.to_thread(process.communicate, timeout=12)
    return asyncio.run(communicate())


def _send(stream, descriptor, action, *, timeout=10, **fields):
    frame = dict(version=1, capability=descriptor['capability'], action=action, timeout=timeout, **fields)
    stream.write(json.dumps(frame).encode() + b'\n')
    stream.flush()


def _response(stream):
    async def read():
        async with component_timeout('Synthetic response observation', 12):
            while True:
                frame = json.loads(await asyncio.to_thread(stream.readline))
                if frame['status'] != 'progress':
                    return frame
    return asyncio.run(read())


@pytest.mark.parametrize('abandonment', ['disconnect', 'deadline'])
def test_abandoned_process_lease_preserves_real_sibling_setup(tmp_path, monkeypatch, abandonment):
    _source_environment(monkeypatch)
    config, evidence, release, _ = _provider(tmp_path)
    backend = SetupBackend(config, tmp_path)
    descriptor = backend.start()
    assert _rows(evidence) == []  # Starting the transport remains inert.
    progress, finished = tmp_path / 'sibling-progress', tmp_path / 'sibling-ready'
    connection = socket.create_connection((descriptor['host'], descriptor['port']), timeout=12)
    stream = connection.makefile('rwb')
    sibling = None
    try:
        _send(stream, descriptor, 'register', worker_id='abandoned-worker', attempt=1)
        registered = _response(stream)
        assert registered['status'] == 'registered'
        _send(stream, descriptor, 'ready', timeout=3 if abandonment == 'deadline' else 10,
              binding=registered['binding'])
        _wait(lambda: any(row['kind'] == 'setup_started' for row in _rows(evidence)))
        sibling = subprocess.Popen([sys.executable, '-c', _SIBLING, json.dumps(descriptor),
                                    str(progress), str(finished)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        _wait(progress.exists)  # A backend progress frame proves the sibling is waiting.
        assert not finished.exists() and not release.exists()
        if abandonment == 'disconnect':
            connection.shutdown(socket.SHUT_RDWR)
            stream.close()
            connection.close()
        else:
            response = _response(stream)
            assert response['status'] == 'error'
            stream.close()
            connection.close()
        assert not finished.exists()
        assert not any(row['kind'] == 'setup_cancelled' for row in _rows(evidence))
        release.touch()
        stdout, stderr = _communicate(sibling)
        assert sibling.returncode == 0, stdout + stderr
        assert finished.exists()
    finally:
        stream.close()
        connection.close()
        if sibling is not None and sibling.poll() is None:
            sibling.terminate()
            _communicate(sibling)
        backend.close()
    kinds = [row['kind'] for row in _rows(evidence)]
    assert kinds.count('provider_load') == kinds.count('setup_started') == kinds.count('setup_finished') == 1
    assert kinds.count('setup_cancelled') == kinds.count('setup_deadline') == 0
    assert kinds.count('auth_close') == kinds.count('provider_close') == 1
    assert kinds.index('setup_finished') < kinds.index('auth_close') < kinds.index('provider_close')
    assert backend.closed and not backend.thread.is_alive() and backend.loop.is_closed()
