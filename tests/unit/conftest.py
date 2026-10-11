"""Unit recording fixtures use disposable history rather than the user cache."""
import pytest


@pytest.fixture(autouse=True)
def isolated_recording_history(tmp_path_factory, monkeypatch):
    from api_agent.testing import cost_history
    from api_agent.telemetry.history import HistoryCache
    path = tmp_path_factory.mktemp('unit-recording-history') / 'recording-history.sqlite3'
    monkeypatch.setattr(cost_history, 'HistoryCache', lambda: HistoryCache(path))
    return path


@pytest.fixture(autouse=True)
def isolated_suite_lock(tmp_path_factory, monkeypatch):
    from api_agent.testing import suite_lock
    path = tmp_path_factory.mktemp('unit-suite-lock') / 'test-suite.lock'
    monkeypatch.setattr(suite_lock, 'cache_path', lambda: path.with_name('cost-history.sqlite3'))
    return path
