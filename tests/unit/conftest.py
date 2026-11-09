"""Unit recording fixtures use disposable history rather than the user cache."""
import sys

import pytest


_IMPORTED = pytest.StashKey[dict]()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    item.stash[_IMPORTED] = {name: module for name, module in sys.modules.items()
                             if name.startswith('api_agent')}


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """An evicted module is re-imported later, splitting class identity for other tests."""
    result = yield
    changed = sorted(name for name, module in item.stash.get(_IMPORTED, {}).items()
                     if sys.modules.get(name) is not module)
    if changed:
        raise AssertionError(f'Test evicted or replaced imported modules: {changed}')
    return result


@pytest.fixture(autouse=True)
def isolated_managed_tools(monkeypatch):
    """Only tests that explicitly opt in may prepare managed tools."""
    monkeypatch.delenv('API_AGENT_MISE', raising=False)


@pytest.fixture(autouse=True)
def isolated_playwright_inspector(monkeypatch):
    """An inherited inspector setting forces even headless browsers to be headed."""
    monkeypatch.delenv('PWDEBUG', raising=False)


@pytest.fixture
def isolated_native_login_gate(tmp_path, monkeypatch):
    """Native admission tests never create coordination files in a user home."""
    path = tmp_path / 'native-session' / 'credentials'
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(path))
    return path


@pytest.fixture
def isolated_renderer_cache(tmp_path, monkeypatch):
    """Selected views look up the prepared renderer cache, never the user's."""
    home = tmp_path / 'renderer-home'
    jar = home / '.cache/api-agent/plantuml/plantuml-mit-1.2026.9.jar'
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b'unused renderer')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('API_AGENT_PLANTUML_JAR', raising=False)
    return jar


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
