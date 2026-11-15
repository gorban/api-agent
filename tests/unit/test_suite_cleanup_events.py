"""Cleanup events distinguish borrower counts from completed disposal."""
from unittest.mock import Mock

import pytest

from api_agent.testing.suite_prerequisite_broker import SuitePrerequisiteBroker
from api_agent.testing.suite_prerequisite_pool import PoolPending
from api_agent.testing.suite_prerequisite_provider import validate_prerequisite_event


def test_pool_pending_without_borrowers_reports_incomplete_then_complete(tmp_path, monkeypatch):
    events = []
    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=1, event=events.append)
    failure = PoolPending('owned cleanup remains')
    monkeypatch.setattr(broker._pool, 'close', Mock(side_effect=[failure, None]))

    with pytest.raises(PoolPending) as caught:
        broker.close(timeout=1)
    assert caught.value is failure
    broker.close(timeout=1)

    assert [(event['pending'], event['completed']) for event in events] == [(0, 0), (0, 1)]
    assert all(set(event) == {'phase', 'seconds', 'pending', 'completed'} for event in events)


def test_server_close_failure_reports_incomplete_and_retries(tmp_path):
    events = []
    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=1, event=events.append)
    failure = OSError('synthetic server close failure')
    broker._server = Mock()
    broker._server.close.side_effect = [failure, None]

    with pytest.raises(OSError) as caught:
        broker.close(timeout=1)
    assert caught.value is failure
    broker.close(timeout=1)

    assert [(event['pending'], event['completed']) for event in events] == [(0, 0), (0, 1)]
    assert broker._server.close.call_count == 2


@pytest.mark.parametrize('failure_stage', ['directory', 'descriptor'])
def test_close_reports_incomplete_until_control_storage_is_disposed(tmp_path, monkeypatch, failure_stage):
    events = []
    broker = SuitePrerequisiteBroker(tmp_path, {}, owner_budget=1, event=events.append)
    failure = OSError('synthetic cleanup failure')
    directory = Mock()
    directory.cleanup.side_effect = [failure, None] if failure_stage == 'directory' else None
    broker._directory = directory
    descriptor = tmp_path / 'suite-prerequisite-broker.json'
    descriptor.touch()
    path_type = type(descriptor)
    unlink = path_type.unlink
    failed = False

    def unlink_once(path, *args, **kwargs):
        nonlocal failed
        if failure_stage == 'descriptor' and path == descriptor and not failed:
            failed = True
            raise failure
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(path_type, 'unlink', unlink_once)
    with pytest.raises(OSError) as caught:
        broker.close(timeout=1)
    assert caught.value is failure
    broker.close(timeout=1)

    assert not descriptor.exists()
    assert [(event['pending'], event['completed']) for event in events] == [(0, 0), (0, 1)]


@pytest.mark.parametrize('completed', [0, 1])
def test_event_accepts_content_free_completion_flag(completed):
    event = {'phase': 'cleanup', 'seconds': 0.1, 'pending': 0, 'completed': completed}
    assert validate_prerequisite_event(event) == event


@pytest.mark.parametrize('completed', [True, -1, 2, '1', None])
def test_event_rejects_invalid_completion_flag(completed):
    with pytest.raises(ValueError, match='Invalid prerequisite diagnostic'):
        validate_prerequisite_event({'phase': 'cleanup', 'seconds': 0, 'completed': completed})
