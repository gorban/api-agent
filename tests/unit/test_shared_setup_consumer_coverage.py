"""Consumer ownership and optional presentation at shared setup boundaries."""
from dataclasses import asdict
from io import StringIO
import json
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.progress import ComponentProgress, progress_sink
from api_agent.providers.setup_transport import SetupClient
from api_agent.testing import integration, setup_backend
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.inspection_controls import finish_terminal_inspection


def test_progress_without_presentation_sink_is_inert():
    client = SetupClient(dict(version=1, host='127.0.0.1', port=32123, capability='a' * 32))
    event = ComponentProgress('component', 'Shared provider setup', 1, .1, True)
    with progress_sink(None):
        client._progress(dict(version=1, status='progress', component=asdict(event)))
    assert client._loop is None and client._binding is None
    client.close()


@pytest.mark.parametrize('boundary', ['backend_close', 'cancelled_abort'])
def test_fresh_cleanup_interrupt_retains_original_and_backend(tmp_path, monkeypatch, boundary):
    identity = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok', root=tmp_path)
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    cancellation = Event()
    primary = KeyboardInterrupt('first cleanup interruption')
    backend = SimpleNamespace(closed=False, pending=True, lost=False,
        start=lambda: dict(version=1, host='127.0.0.1', port=32123, capability='a' * 32))
    def close():
        raise primary if boundary == 'backend_close' else RuntimeError('cleanup pending')
    backend.close = close
    backend.abort = Mock(side_effect=[primary, KeyboardInterrupt('secondary cleanup interruption')])
    monkeypatch.setattr(setup_backend, 'SetupBackend', lambda *a, **k: backend)
    monkeypatch.setattr(integration, 'collect_tests', lambda *a, **k: (identity,))
    def child(args, **kwargs):
        rows = [dict(kind='report', node=identity.node_id, phase=phase, status='passed',
                     duration=0, traceback='') for phase in ('setup', 'call', 'teardown')]
        from pathlib import Path
        Path(args[3]).write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return SimpleNamespace(stdout=StringIO(), returncode=0, poll=lambda: 0)
    monkeypatch.setattr(integration.subprocess, 'Popen', child)
    monkeypatch.setattr(integration, 'finalize', Mock())
    publications = []
    def publish(result):
        publications.append(result)
        if boundary == 'cancelled_abort':
            cancellation.set()
    with pytest.raises(KeyboardInterrupt) as caught:
        integration.run_integration_tests(context, (identity,), accepted=True, config={},
            after_run=publish, cancel_event=cancellation)
    assert caught.value is primary and caught.value.inspections == (backend,)
    assert len(publications) == 1 and publications[0].tests[0].status == 'passed'
    assert publications[0].inspections == (backend,)
    assert not backend.closed and backend.pending
    assert backend.abort.call_count == (2 if boundary == 'cancelled_abort' else 0)


def test_terminal_pending_backend_does_not_claim_retained_browser(tmp_path):
    identity = TestIdentity('base::test.py::test_ok', 'base', 'test.py', 'test.py::test_ok')
    backend = setup_backend.SetupBackend({}, tmp_path)
    backend.pending = True
    lines = []
    def dispose():
        assert lines[1] == 'Allure publication: PASS'
        assert 'cleanup remains pending' in lines[2]
        assert not any('browser' in line.lower() or 'Debug inspection' in line for line in lines)
        backend.close()
        assert backend.closed and not backend.pending
    backend.wait_closed = Mock(side_effect=dispose)
    backend.end_inspection = Mock()
    execution = integration.IntegrationRunResult(1, (TestResult(identity, 'passed'),), '', inspections=(backend,))
    assert finish_terminal_inspection(SimpleNamespace(execution=execution, persisted=True), lines.append)
    backend.wait_closed.assert_called_once_with()
    backend.end_inspection.assert_not_called()


@pytest.mark.parametrize('primary', [KeyboardInterrupt('original wait'), RuntimeError('original wait')])
def test_terminal_cleanup_does_not_replace_original_wait_error(primary):
    backend = SimpleNamespace(closed=False, lost=False, retains_browser=False,
        wait_closed=Mock(side_effect=primary),
        end_inspection=Mock(side_effect=KeyboardInterrupt('secondary end')),
        abort=Mock(side_effect=KeyboardInterrupt('secondary abort')))
    execution = integration.IntegrationRunResult(1, (), '', inspections=(backend,))
    with pytest.raises(type(primary)) as caught:
        finish_terminal_inspection(SimpleNamespace(execution=execution, persisted=True), lambda line: None)
    assert caught.value is primary
    assert caught.value.inspections == (backend,)
    assert caught.value.inspection_group.handles == (backend,)
    assert not backend.closed
    backend.end_inspection.assert_called_once_with()
    backend.abort.assert_called_once_with()


def test_terminal_first_cleanup_interrupt_is_not_suppressed():
    primary = KeyboardInterrupt('first cleanup interruption')
    backend = SimpleNamespace(closed=False, lost=False, retains_browser=False,
        wait_closed=Mock(), end_inspection=Mock(side_effect=primary), abort=Mock())
    execution = integration.IntegrationRunResult(1, (), '', inspections=(backend,))
    with pytest.raises(KeyboardInterrupt) as caught:
        finish_terminal_inspection(SimpleNamespace(execution=execution, persisted=True), lambda line: None)
    assert caught.value is primary
    assert caught.value.inspections == (backend,)
    assert not backend.closed
    backend.wait_closed.assert_called_once_with()
    backend.end_inspection.assert_called_once_with()
    backend.abort.assert_called_once_with()
