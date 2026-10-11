"""Shared setup failures retain safe evidence through pytest and Allure."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import traceback
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.control import CleanupPendingError, OperationalError
from api_agent.providers.setup_transport import SetupClient, SetupServer, _diagnostic, _error, _raise_error
from test_setup_transport import CAPABILITY, Registry, descriptor


@pytest.mark.parametrize('error,kind', [
    (OperationalError('authorization'), 'authorization'),
    (TimeoutError('private body'), 'deadline'),
    (OSError('private body'), 'unavailable'),
    (ContractError('private body'), 'contract'),
    (RuntimeError('private body'), 'unknown'),
])
def test_backend_classification_does_not_transfer_arbitrary_messages(error, kind):
    frame = _error(error, CAPABILITY)
    assert 'Backend failure category: ' + kind in frame['message']
    assert '<no captured stack>' in frame['message']
    assert 'private body' not in json.dumps(frame)
    with pytest.raises(ContractError) as caught:
        _raise_error(frame)
    assert str(caught.value) == frame['message']


@pytest.mark.parametrize('invalid', [['private body'], 'private body'])
def test_diagnostic_rejects_mutated_operational_kind(invalid):
    error = OperationalError('unknown')
    error.kind = invalid
    assert 'failure category: unknown' in _diagnostic(error, 'Backend')
    assert 'private body' not in _diagnostic(error, 'Backend')


@pytest.mark.parametrize('filename,expected', [
    (str(Path(__import__('api_agent.providers.setup_transport', fromlist=['x']).__file__).parent / 'sessions.py'), 'api_agent/providers/sessions.py'),
    ('/private/path/private-body.py', '<external code>'),
    (str(Path(__import__('api_agent.providers.setup_transport', fromlist=['x']).__file__).parent / '../private-body.py'), '<external code>'),
])
def test_stack_locations_exclude_source_locals_and_external_paths(filename, expected):
    namespace = {}
    exec(compile("def fail():\n    secret = 'private body'\n    raise RuntimeError(secret)\n", filename, 'exec'), namespace)
    try:
        namespace['fail']()
    except RuntimeError as error:
        diagnostic = _diagnostic(error, 'Backend')
    assert expected + ':3' in diagnostic
    assert 'private body' not in diagnostic and 'private-body' not in diagnostic
    assert '/private/path' not in diagnostic and 'raise RuntimeError' not in diagnostic


def test_stack_is_bounded_and_contains_innermost_location():
    def recurse(n):
        if n:
            return recurse(n - 1)
        raise RuntimeError('private body')
    try:
        recurse(20)
    except RuntimeError as error:
        diagnostic = _diagnostic(error, 'Backend')
    assert len(diagnostic.splitlines()) == 10
    assert len(diagnostic) <= 2048
    assert 'private body' not in diagnostic


def test_remote_failure_and_local_transport_failure_keep_safe_causes(monkeypatch):
    async def scenario():
        registry = Registry()
        registry.failure = OperationalError('authorization')
        server = SetupServer(registry, CAPABILITY, timeout=3)
        client = SetupClient(await server.start(), timeout=3)
        try:
            with pytest.raises(ContractError) as caught:
                await asyncio.to_thread(client.ensure_ready)
            cause = caught.value.__cause__
            assert isinstance(cause, ContractError)
            assert 'Backend failure category: authorization' in str(cause)
            evidence = ''.join(traceback.format_exception(caught.value))
            assert 'Backend failure category: authorization' in evidence
            assert 'setup_transport.py:' in evidence
            assert client._disposed and client._loop.is_closed()
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
    asyncio.run(scenario())
    monkeypatch.setattr(asyncio, 'open_connection', AsyncMock(side_effect=OSError('private body')))
    client = SetupClient(descriptor())
    with pytest.raises(ContractError) as caught:
        client.ensure_ready()
    assert 'Worker transport failure category: unavailable' in str(caught.value.__cause__)
    assert 'private body' not in ''.join(traceback.format_exception(caught.value))
    assert client._disposed and client._loop.is_closed()


def test_pending_cleanup_retains_primary_diagnostics(monkeypatch):
    client = SetupClient(descriptor())
    async def fail(action, request=None):
        assert request is None
        assert action == "ready"
        raise ContractError('Safe protocol rejection')
    monkeypatch.setattr(client, '_ensure', fail)
    pending = CleanupPendingError((client,), lambda: None)
    original_close = client.close
    monkeypatch.setattr(client, 'close', lambda: (_ for _ in ()).throw(pending))
    try:
        with pytest.raises(CleanupPendingError) as caught:
            client.ensure_ready()
        assert caught.value is pending
        assert pending.__cause__ is pending.operation_error
        assert str(pending.operation_error.__cause__) == 'Safe protocol rejection'
        assert 'Safe protocol rejection' in ''.join(traceback.format_exception(pending))
    finally:
        original_close()


def test_real_pytest_failure_publishes_backend_diagnostics_without_secret(tmp_path):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.recorded import run_recorded_integration
    from test_recorded_integration import Storage
    root = tmp_path / 'fixture'
    suite = root / 'tests/integration'
    suite.mkdir(parents=True)
    (root / 'diagnostic_fixture.py').write_text('''
import asyncio
from api_agent.providers.setup_transport import SetupClient, SetupServer
class Registry:
    def register(self, worker, attempt):
        return dict(version=1, operation_id='operation', selected_identity='selection', worker_id=worker, attempt=attempt)
    async def ensure_ready(self, binding):
        raise RuntimeError('private-provider-secret')
    async def revoke(self, binding):
        pass
async def fail_setup():
    server = SetupServer(Registry(), 'public-synthetic-capability-value-000000', timeout=3)
    client = SetupClient(await server.start(), timeout=3)
    try:
        await asyncio.to_thread(client.ensure_ready)
    finally:
        await asyncio.to_thread(client.close)
        await server.aclose()
''')
    (suite / 'test_setup.py').write_text('import asyncio\nfrom diagnostic_fixture import fail_setup\ndef test_setup():\n    asyncio.run(fail_setup())\n')
    context = SimpleNamespace(base_root=root, owner_root=root, suite_paths=lambda mode: (suite,))
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': tmp_path / 'results', 'TEST_PARALLEL_JOBS': 0}
    storage = Storage()
    result = run_recorded_integration(context, collect_tests(context, 'integration'), config,
                                      accepted=True, storage=storage)
    assert result.exit_code == 1 and result.persisted
    assert result.manifest['status'] == 'failed' and len(storage.calls) == 1
    outcome = result.execution.tests[0]
    assert outcome.status == 'failed'
    assert 'Backend failure category: unknown' in outcome.traceback
    assert 'Stack locations (innermost last)' in outcome.traceback
    assert 'private-provider-secret' not in outcome.traceback
    row = json.loads(next(result.directory.glob('*-result.json')).read_text())
    assert row['statusDetails']['trace'] == outcome.traceback
    assert 'private-provider-secret' not in json.dumps(row)
