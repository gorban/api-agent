"""Selected mise tools are prepared only for the opted-in source launcher."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace

import pytest

from api_agent.runtime import mise_tools
from api_agent.runtime import https
from api_agent.testing import allure_viewer
from api_agent.runtime.https import prepare_local_https
from api_agent.testing.allure_viewer import ManagedAllureViewer


@pytest.fixture
def fake_mise(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'mise.toml').write_text('''[tools]
"core:node" = "24.22.2"
"aqua:npm/cli" = "11.16.0"
"core:java" = "temurin-21.0.13+9.0.LTS"
"github:allure-framework/allure2" = "2.46.1"
"aqua:FiloSottile/mkcert" = "1.4.4"
''')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    tool_dir = tmp_path / 'tools'
    tool_dir.mkdir()
    for name in ('node', 'npm', 'mkcert', 'java', 'allure'):
        (tool_dir / name).write_text('synthetic')
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
''')
    program.chmod(0o755)
    monkeypatch.setattr(mise_tools, 'ROOT', root)
    monkeypatch.setenv('PATH', str(bin_dir))
    monkeypatch.setenv('FAKE_MISE_LOG', str(log))
    monkeypatch.setenv('FAKE_MISE_TOOLS', str(tool_dir))
    monkeypatch.setenv('API_AGENT_MISE', '1')
    monkeypatch.setenv('MISE_ENV', 'untrusted')
    return root, bin_dir, tool_dir, log


def test_only_selected_pinned_tools_are_prepared(fake_mise):
    root, bin_dir, tools, log = fake_mise
    mise_tools.ensure_tools('java', 'allure')
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0][0][-3:] == ['install', 'core:java@temurin-21.0.13+9.0.LTS', 'github:allure-framework/allure2@2.46.1']
    assert [call[0][-1] for call in calls[1:]] == ['java', 'allure']
    assert calls[1][0][-2] == 'core:java@temurin-21.0.13+9.0.LTS'
    assert all(call[1:] == [str(root), ''] for call in calls)
    assert os.environ['PATH'].split(os.pathsep) == [str(tools), str(bin_dir)]
    assert os.environ['JAVA_HOME'] == str(tools.parent)


def test_selected_npm_precedes_node_bundled_npm(fake_mise, tmp_path, monkeypatch):
    node_dir, npm_dir = tmp_path / 'node-bin', tmp_path / 'npm-bin'
    node_dir.mkdir()
    npm_dir.mkdir()
    for path in (node_dir / 'node', node_dir / 'npm', npm_dir / 'npm'):
        path.write_text('synthetic')
        path.chmod(0o755)
    monkeypatch.setenv('FAKE_MISE_NODE', str(node_dir / 'node'))
    monkeypatch.setenv('FAKE_MISE_NPM', str(npm_dir / 'npm'))
    mise_tools.ensure_tools('node', 'npm')
    assert shutil.which('npm') == str(npm_dir / 'npm')
    assert shutil.which('node') == str(node_dir / 'node')


def test_missing_launcher_marker_never_starts_mise(fake_mise, monkeypatch):
    _, _, _, log = fake_mise
    monkeypatch.delenv('API_AGENT_MISE')
    mise_tools.ensure_tools('node', 'npm')
    assert not log.exists()


def test_unknown_tool_rejected_before_mise_runs(fake_mise):
    _, _, _, log = fake_mise
    with pytest.raises(ValueError, match='Unknown managed executable'):
        mise_tools.ensure_tools('unknown')
    assert not log.exists()


def test_empty_tool_selection_rejected(fake_mise):
    with pytest.raises(ValueError, match='Unknown managed executable'):
        mise_tools.ensure_tools()


@pytest.mark.parametrize('missing', ['mise', 'configuration'])
def test_missing_mise_or_configuration_is_actionable(fake_mise, monkeypatch, missing):
    root, _, _, log = fake_mise
    if missing == 'mise':
        monkeypatch.setenv('PATH', str(root))
    else:
        (root / 'mise.toml').unlink()
    with pytest.raises(OSError, match='requires the project mise configuration'):
        mise_tools.ensure_tools('node')
    assert not log.exists()


@pytest.mark.parametrize('contents', [
    '[tools]\n',
    '[tools]\n"core:node" = "bad@version"\n',
    '[tools]\n"core:node" = { lazy = true }\n',
])
def test_missing_or_invalid_pinned_version_is_rejected(fake_mise, contents):
    root, _, _, log = fake_mise
    (root / 'mise.toml').write_text(contents)
    with pytest.raises(OSError, match='version is missing or invalid'):
        mise_tools.ensure_tools('node')
    assert not log.exists()


def test_expired_budget_rejects_before_spawn(fake_mise):
    _, _, _, log = fake_mise
    with pytest.raises(TimeoutError, match='timed out'):
        mise_tools.ensure_tools('node', timeout=1e-15)
    assert not log.exists()


def test_invalid_mise_executable_reports_safe_start_error(fake_mise):
    _, bin_dir, _, _ = fake_mise
    (bin_dir / 'mise').write_text('not an executable format')
    with pytest.raises(OSError, match='could not start'):
        mise_tools.ensure_tools('node')


@pytest.mark.parametrize('value', ['relative/node', '/definitely-not-a-real-mise-tool'])
def test_invalid_resolved_tool_path_is_rejected(fake_mise, monkeypatch, value):
    monkeypatch.setenv('FAKE_MISE_NODE', value)
    with pytest.raises(OSError, match='path is invalid'):
        mise_tools.ensure_tools('node')


def test_preparation_timeout_during_install_is_reported(fake_mise, monkeypatch, tmp_path):
    marker = tmp_path / 'waiting'
    monkeypatch.setenv('FAKE_MISE_WAIT', str(marker))
    with pytest.raises(TimeoutError, match='timed out'):
        mise_tools.ensure_tools('node', timeout=.3)
    assert marker.exists()


def test_cancellation_during_install_stops_preparation(fake_mise, monkeypatch, tmp_path):
    marker = tmp_path / 'waiting'
    monkeypatch.setenv('FAKE_MISE_WAIT', str(marker))
    cancellation = threading.Event()
    errors = []
    def prepare():
        try:
            mise_tools.ensure_tools('node', cancellation=cancellation)
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=prepare)
    worker.start()
    try:
        import time
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists()
        cancellation.set()
        worker.join(5)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
    finally:
        cancellation.set()
        worker.join(5)


def test_cancellation_during_path_validation_does_not_publish(fake_mise, monkeypatch):
    cancellation = threading.Event()
    original_path = os.environ['PATH']
    class CheckedPath:
        def __init__(self, value):
            self.path = Path(value)
        def is_absolute(self):
            return self.path.is_absolute()
        def is_file(self):
            cancellation.set()
            return self.path.is_file()
        @property
        def parent(self):
            return self.path.parent
    monkeypatch.setattr(mise_tools, 'Path', CheckedPath)
    with pytest.raises(InterruptedError, match='cancelled'):
        mise_tools.ensure_tools('node', cancellation=cancellation)
    assert os.environ['PATH'] == original_path


def test_cancelled_preparation_never_starts_mise(fake_mise):
    from threading import Event
    _, _, _, log = fake_mise
    cancellation = Event()
    cancellation.set()
    with pytest.raises(InterruptedError, match='cancelled'):
        mise_tools.ensure_tools('node', cancellation=cancellation)
    assert not log.exists()


def test_installer_output_is_not_in_failure(fake_mise, monkeypatch):
    monkeypatch.setenv('FAKE_MISE_FAIL', '1')
    with pytest.raises(OSError, match='preparation failed') as error:
        mise_tools.ensure_tools('mkcert')
    assert 'sensitive installer output' not in str(error.value)


def test_custom_https_runner_does_not_prepare_mkcert(tmp_path, monkeypatch):
    monkeypatch.setattr(mise_tools, 'ensure_tools', lambda *a, **k: pytest.fail('unexpected mise'))
    def runner(*args, **kwargs):
        raise RuntimeError('custom runner invoked')
    with pytest.raises(RuntimeError, match='custom runner invoked'):
        prepare_local_https(tmp_path, runner=runner)


def test_custom_allure_command_does_not_prepare_managed_tools(monkeypatch):
    monkeypatch.setattr(mise_tools, 'ensure_tools', lambda *a, **k: pytest.fail('unexpected mise'))
    viewer = ManagedAllureViewer(cli=f'{sys.executable} -m custom')
    async def command(*args, **kwargs):
        raise RuntimeError('custom command invoked')
    monkeypatch.setattr(viewer, '_command', command)
    with pytest.raises(RuntimeError, match='custom command invoked'):
        asyncio.run(viewer._ready())


def test_default_https_prepares_only_mkcert(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(mise_tools, 'ensure_tools', lambda *a, **k: calls.append(a))
    def runner(*args, **kwargs):
        raise RuntimeError('default runner invoked')
    monkeypatch.setattr(https, '_default_runner', runner)
    monkeypatch.setattr(https, 'shutil', SimpleNamespace(which=lambda _: '/synthetic'))
    with pytest.raises(RuntimeError, match='default runner invoked'):
        prepare_local_https(tmp_path)
    assert calls == [('mkcert',)]


def test_default_allure_prepares_java_and_allure(monkeypatch):
    calls = []
    monkeypatch.setattr(mise_tools, 'ensure_tools', lambda *a, **k: calls.append(a))
    monkeypatch.setattr(allure_viewer, 'shutil', SimpleNamespace(which=lambda _: sys.executable))
    viewer = ManagedAllureViewer()
    async def command(*args, **kwargs):
        raise RuntimeError('default command invoked')
    monkeypatch.setattr(viewer, '_command', command)
    with pytest.raises(RuntimeError, match='default command invoked'):
        asyncio.run(viewer._ready())
    assert calls == [('java', 'allure')]


def test_allure_cancellation_waits_for_preparation_cleanup(monkeypatch):
    started, observed, release, finished = (threading.Event() for _ in range(4))
    def prepare(*names, cancellation, **kwargs):
        started.set()
        assert cancellation.wait(5)
        observed.set()
        assert release.wait(5)
        finished.set()
    monkeypatch.setattr(mise_tools, 'ensure_tools', prepare)

    async def exercise():
        viewer = ManagedAllureViewer()
        task = asyncio.create_task(viewer._ready())
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        assert await asyncio.to_thread(observed.wait, 5)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    asyncio.run(exercise())


def test_managed_https_failure_has_context(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('preparation failed')
    monkeypatch.setattr(mise_tools, 'ensure_tools', fail)
    with pytest.raises(https.HTTPSPreparationError, match='Unable to prepare mkcert'):
        prepare_local_https(tmp_path)


def test_managed_allure_failure_has_context(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('preparation failed')
    monkeypatch.setattr(mise_tools, 'ensure_tools', fail)
    viewer = ManagedAllureViewer()
    with pytest.raises(allure_viewer.ViewerError, match='Unable to prepare Allure and Java'):
        asyncio.run(viewer._ready())
