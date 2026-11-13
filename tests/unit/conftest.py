"""Unit recording fixtures use disposable history rather than the user cache."""
import functools
import sys

from pathlib import Path

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


@functools.cache
def _timeout_settings():
    from api_agent.config import load_shared_schema
    return tuple(name for name in load_shared_schema().fields if name.endswith('_TIMEOUT_SECONDS'))


@pytest.fixture(autouse=True)
def default_build_and_test_timeouts(monkeypatch):
    """A caller's timeout settings bound the suite run, not the code under test."""
    for name in _timeout_settings():
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated_managed_tools(monkeypatch):
    """Only tests that explicitly opt in may prepare managed tools."""
    monkeypatch.delenv('API_AGENT_MISE', raising=False)


@pytest.fixture(autouse=True)
def chromium_preparations(monkeypatch):
    """Record Chromium preparation requests; unit tests never resolve or download the real browser.

    Tests of the preparation itself import ``ensure_chromium_ready`` directly and inject their own adapters.
    """
    requests = []

    def prepare(**options):
        requests.append(options)
        return Path('/unprepared/chromium')
    monkeypatch.setattr('api_agent.runtime.browser.ensure_chromium_ready', prepare)
    monkeypatch.setattr('api_agent.runtime.managed_browser.ensure_chromium_ready', prepare)
    return requests


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
def isolated_renderer_preparation(tmp_path, monkeypatch):
    """Selected views receive a synthetic prepared renderer, never the user's tools."""
    from api_agent.workflows import viewer_build
    jar = tmp_path / 'renderer-tools' / 'plantuml-mit-1.2026.9.jar'
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b'unused renderer')
    monkeypatch.setattr(viewer_build, 'prepare_renderer', lambda: jar)
    monkeypatch.delenv('API_AGENT_PLANTUML_JAR', raising=False)
    return jar


@pytest.fixture
def fake_mise(tmp_path, monkeypatch):
    """A recording mise that resolves synthetic executables and the PlantUML file tool."""
    from api_agent.runtime import mise_tools
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'mise.toml').write_text('''[tools]
"core:node" = "24.22.2"
"aqua:npm/cli" = "11.16.0"
"core:java" = "temurin-21.0.13+9.0.LTS"
"github:allure-framework/allure2" = "2.46.1"
"aqua:FiloSottile/mkcert" = "1.4.4"
"conda:openssl" = "3.5.9"
"github:plantuml/plantuml" = { version = "1.2026.9", asset_pattern = "plantuml-mit-1.2026.9.jar" }
''')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    tool_dir = tmp_path / 'tools'
    tool_dir.mkdir()
    for name in ('node', 'npm', 'mkcert', 'openssl', 'java', 'allure'):
        (tool_dir / name).write_text('synthetic')
    installed = tmp_path / 'installs' / 'plantuml'
    installed.mkdir(parents=True)
    (installed / 'plantuml-mit-1.2026.9.jar').write_bytes(b'synthetic renderer')
    log = tmp_path / 'calls.jsonl'
    program = bin_dir / 'mise'
    program.write_text(f'''#!{sys.executable}
import json, os, sys
with open(os.environ['FAKE_MISE_LOG'], 'a') as stream:
    stream.write(json.dumps([sys.argv[1:], os.getcwd(), os.environ.get('MISE_ENV')]) + '\\n')
if os.environ.get('FAKE_MISE_FAIL'):
    print('sensitive installer output', file=sys.stderr)
    sys.exit(1)
if os.environ.get('FAKE_MISE_WAIT'):
    import time
    open(os.environ['FAKE_MISE_WAIT'], 'w').close()
    time.sleep(5)
if 'which' in sys.argv:
    print(os.environ.get('FAKE_MISE_' + sys.argv[-1].upper(),
                         os.path.join(os.environ['FAKE_MISE_TOOLS'], sys.argv[-1])))
if 'where' in sys.argv:
    print(os.environ.get('FAKE_MISE_WHERE', os.environ['FAKE_MISE_INSTALLED']))
''')
    program.chmod(0o755)
    monkeypatch.setattr(mise_tools, 'ROOT', root)
    monkeypatch.setenv('PATH', str(bin_dir))
    monkeypatch.setenv('FAKE_MISE_LOG', str(log))
    monkeypatch.setenv('FAKE_MISE_TOOLS', str(tool_dir))
    monkeypatch.setenv('FAKE_MISE_INSTALLED', str(installed))
    monkeypatch.setenv('API_AGENT_MISE', '1')
    monkeypatch.setenv('MISE_ENV', 'untrusted')
    monkeypatch.delenv('JAVA_HOME', raising=False)
    return root, bin_dir, tool_dir, log


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
