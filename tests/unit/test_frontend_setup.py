"""Source frontend preparation is locked, bounded, and preserves output."""
import json
from pathlib import Path
import threading

import pytest

from api_agent.runtime import frontend


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools/frontend-assets.json').write_text('{"assets": []}')
    (tmp_path / 'package-lock.json').write_text('{"packages": {"": {}}}')
    monkeypatch.setattr(frontend, 'source_root', lambda: tmp_path)
    monkeypatch.setattr(frontend.shutil, 'which', lambda name: '/' + name)
    return tmp_path


@pytest.fixture
def commands(monkeypatch):
    calls = []
    def command(argv, root, budget, cancellation):
        calls.append((argv, root))
        return (0, b'v24.1.0\n') if argv[1] == '--version' else (0, b'')
    monkeypatch.setattr(frontend, '_command', command)
    return calls


def installed(root):
    (root / 'node_modules').mkdir(exist_ok=True)
    (root / 'node_modules/.package-lock.json').write_text('{"packages": {}}')


def test_distribution_does_not_probe_tools(monkeypatch):
    monkeypatch.setattr(frontend, 'source_root', lambda: None)
    monkeypatch.setattr(frontend.shutil, 'which', lambda name: pytest.fail('tool probe'))
    frontend.ensure_source_frontend()


def test_ready_checkout_does_not_install(checkout, commands, monkeypatch, tmp_path):
    installed(checkout)
    monkeypatch.chdir(checkout / 'tools')
    frontend.ensure_source_frontend()
    assert [argv[1] for argv, _ in commands] == ['--version', 'tools/prepare_frontend_assets.mjs']
    assert all(root == checkout for _, root in commands)


def test_missing_dependencies_install_and_verify(checkout, monkeypatch):
    monkeypatch.setenv('NODE_ENV', 'production')
    calls = []
    def command(argv, root, budget, cancellation):
        calls.append(argv)
        if argv[1] == 'ci':
            installed(root)
        return 0, b'v24.1.0'
    monkeypatch.setattr(frontend, '_command', command)
    before = (checkout / 'package-lock.json').read_bytes()
    frontend.ensure_source_frontend()
    assert calls[1] == ['/npm', 'ci', '--include=dev', '--no-audit', '--no-fund']
    assert calls[-1][-1] == '--check'
    assert (checkout / 'package-lock.json').read_bytes() == before


@pytest.mark.parametrize('tool', ['node', 'npm'])
def test_missing_tool_has_manual_guidance(checkout, commands, monkeypatch, tool):
    monkeypatch.setattr(frontend.shutil, 'which', lambda name: None if name == tool else '/' + name)
    with pytest.raises(OSError, match='Install Node.js 24 or newer and npm'):
        frontend.ensure_source_frontend()


@pytest.mark.parametrize('version', [b'v22.0.0', b'garbage'])
def test_old_or_invalid_node_rejected(checkout, monkeypatch, version):
    monkeypatch.setattr(frontend, '_command', lambda *args: (0, version))
    with pytest.raises(OSError, match='Node.js 24'):
        frontend.ensure_source_frontend()


def test_installer_failure_is_fixed_diagnostic(checkout, monkeypatch):
    monkeypatch.setattr(frontend, '_command', lambda argv, *args:
                        (0, b'v24.0.0') if argv[1] == '--version' else (1, b'secret output'))
    with pytest.raises(OSError, match='Frontend preparation failed') as error:
        frontend.ensure_source_frontend()
    assert 'secret output' not in str(error.value)


def test_postinstall_must_prepare_valid_dependencies(checkout, commands):
    with pytest.raises(OSError, match='Frontend preparation failed'):
        frontend.ensure_source_frontend()


def test_changed_output_is_preserved_before_installer(checkout, commands):
    target = checkout / 'generated.js'
    target.write_text('authored bytes')
    (checkout / 'tools/frontend-assets.json').write_text(json.dumps({'assets': [
        {'target': 'generated.js', 'size': 14, 'sha256': '0' * 64}]}))
    with pytest.raises(ValueError, match='preserve/remove'):
        frontend.ensure_source_frontend()
    assert target.read_text() == 'authored bytes'
    assert not any(argv[1] == 'ci' for argv, _ in commands)


def test_cancelled_setup_starts_no_process(checkout):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(InterruptedError, match='cancelled'):
        frontend.ensure_source_frontend(cancellation=cancelled)


def test_deadline_starts_no_process(checkout):
    with pytest.raises(TimeoutError, match='timed out'):
        frontend.ensure_source_frontend(timeout=.00000001)


def test_installed_lock_detects_changes(tmp_path):
    (tmp_path / 'package-lock.json').write_text('{"packages": {"": {}, "node_modules/a": {"version": "1"}}}')
    installed(tmp_path)
    assert not frontend._installed(tmp_path)


def test_source_root_uses_module_not_working_directory(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    module = root / 'src/api_agent/runtime/frontend.py'
    module.parent.mkdir(parents=True)
    (root / 'tools').mkdir()
    for name in ('launch.py', 'pyproject.toml', 'package.json', 'package-lock.json',
                 'tools/prepare_frontend_assets.mjs', 'tools/frontend-assets.json'):
        (root / name).touch()
    monkeypatch.setattr(frontend, '__file__', str(module))
    monkeypatch.chdir(tmp_path)
    assert frontend.source_root() == root
    (root / 'launch.py').unlink()
    assert frontend.source_root() is None


def test_missing_optional_platform_package_is_ready(tmp_path):
    expected = {'': {}, 'node_modules/a': {'version': '1'},
                'node_modules/platform-addon': {'version': '1', 'optional': True, 'os': ['darwin']}}
    (tmp_path / 'package-lock.json').write_text(json.dumps({'packages': expected}))
    installed(tmp_path)
    package = tmp_path / 'node_modules/a'
    package.mkdir()
    (package / 'package.json').write_text('{"version": "1"}')
    (tmp_path / 'node_modules/.package-lock.json').write_text(json.dumps({'packages': {
        'node_modules/a': expected['node_modules/a']}}))
    assert frontend._installed(tmp_path)
    (package / 'package.json').unlink()
    assert not frontend._installed(tmp_path)


def test_postinstall_output_is_checked(checkout, monkeypatch):
    calls = []
    def command(argv, root, budget, cancellation):
        calls.append(argv)
        if argv[1] == '--version':
            return 0, b'v24.0.0'
        if argv[1] == 'ci':
            installed(root)
            return 0, b''
        return 1, b''
    monkeypatch.setattr(frontend, '_command', command)
    with pytest.raises(OSError, match='Frontend preparation failed'):
        frontend.ensure_source_frontend()
    assert calls[-1][-1] == '--check'


def test_concurrent_preparation_installs_once(checkout, monkeypatch):
    import concurrent.futures
    import time
    installs = []
    def command(argv, root, budget, cancellation):
        if argv[1] == 'ci':
            installs.append(root)
            time.sleep(.05)
            installed(root)
        return 0, b'v24.0.0'
    monkeypatch.setattr(frontend, '_command', command)
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        futures = [executor.submit(frontend.ensure_source_frontend) for _ in range(2)]
        for future in futures:
            future.result(timeout=2)
    assert installs == [checkout]


def test_command_cancellation_reaps_process_group(tmp_path):
    import os
    import sys
    from api_agent.providers.progress import report_timeout_sync
    pid_file = tmp_path / 'child.pid'
    script = ('import subprocess,sys,time; '
              'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
              'open(sys.argv[1],"w").write(str(child.pid)); time.sleep(60)')
    import time
    cancellation = threading.Event()
    def cancel_when_started():
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        cancellation.set()
    watcher = threading.Thread(target=cancel_when_started)
    watcher.start()
    try:
        with report_timeout_sync('Frontend test process', 10) as budget:
            with pytest.raises(InterruptedError):
                frontend._command([sys.executable, '-c', script, str(pid_file)], tmp_path, budget, cancellation)
    finally:
        watcher.join(timeout=6)
    assert not watcher.is_alive()
    from api_agent.testing.processes import _group_running
    # The child shares the leader's group; it must no longer be executing.
    child = int(pid_file.read_text())
    try:
        group = os.getpgid(child)
    except ProcessLookupError:
        return
    assert not _group_running(group)


def test_async_cancellation_waits_for_setup_cleanup(monkeypatch):
    import asyncio
    entered = threading.Event()
    finished = threading.Event()
    def setup(*, cancellation):
        entered.set()
        assert cancellation.wait(2)
        finished.set()
    monkeypatch.setattr(frontend, 'ensure_source_frontend', setup)
    async def run():
        task = asyncio.create_task(frontend.prepare_source_frontend())
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    asyncio.run(run())


def test_installed_module_ignores_marker_rich_parent(tmp_path, monkeypatch):
    root = tmp_path / 'installed'
    root.mkdir()
    (root / 'tools').mkdir()
    for name in ('launch.py', 'pyproject.toml', 'package.json', 'package-lock.json',
                 'tools/prepare_frontend_assets.mjs', 'tools/frontend-assets.json'):
        (root / name).touch()
    module = root / 'site/api_agent/runtime/frontend.py'
    module.parent.mkdir(parents=True)
    monkeypatch.setattr(frontend, '__file__', str(module))
    assert frontend.source_root() is None


@pytest.mark.parametrize('kind', ['changed_definition', 'escaped_package', 'wrong_version', 'unknown_package'])
def test_installed_metadata_rejects_stale_or_unowned_packages(tmp_path, kind):
    package_path = 'node_modules/a' if kind != 'escaped_package' else '../outside'
    details = {'version': '1'}
    expected = {'': {}, package_path: details}
    actual = {package_path: details}
    if kind == 'unknown_package':
        expected.pop(package_path)
    (tmp_path / 'package-lock.json').write_text(json.dumps({'packages': expected}))
    installed(tmp_path)
    (tmp_path / 'node_modules/.package-lock.json').write_text(json.dumps({'packages': actual}))
    if kind == 'changed_definition':
        (tmp_path / 'package.json').write_text('{"devDependencies": {"new": "1"}}')
    if kind == 'wrong_version':
        package = tmp_path / package_path
        package.mkdir()
        (package / 'package.json').write_text('{"version": "2"}')
    assert not frontend._installed(tmp_path)


@pytest.mark.parametrize('kind', ['missing', 'valid', 'symlink', 'directory', 'size', 'digest'])
def test_output_preservation_checks_each_existing_inode(checkout, kind):
    import hashlib
    target = checkout / 'generated.js'
    if kind == 'directory':
        target.mkdir()
    elif kind == 'symlink':
        (checkout / 'other').write_bytes(b'ok')
        target.symlink_to('other')
    elif kind != 'missing':
        target.write_bytes(b'ok' if kind != 'size' else b'larger')
    entry = {'target': 'generated.js', 'size': 2,
             'sha256': hashlib.sha256(b'no' if kind == 'digest' else b'ok').hexdigest()}
    (checkout / 'tools/frontend-assets.json').write_text(json.dumps({'assets': [entry]}))
    if kind in {'missing', 'valid'}:
        frontend._preserve_outputs(checkout)
    else:
        with pytest.raises(ValueError, match='preserve/remove'):
            frontend._preserve_outputs(checkout)


@pytest.mark.parametrize('escape', ['owner', 'target', None])
def test_output_owner_roots_are_scoped(checkout, escape):
    (checkout / 'cfg').mkdir()
    (checkout / 'cfg/workflow-view-roots.json').write_text(json.dumps({'entries': {'flow.md.html': {
        'base_root': '..' if escape == 'owner' else '.', 'pack_root': 'pack'}}}))
    target = '../outside.js' if escape == 'target' else '.private_resources/viewer/marked.js'
    (checkout / 'tools/frontend-assets.json').write_text(json.dumps({'assets': [
        {'target': target, 'size': 2, 'sha256': '0' * 64}]}))
    if escape:
        with pytest.raises(ValueError, match='outside the source repository'):
            frontend._preserve_outputs(checkout)
    else:
        frontend._preserve_outputs(checkout)


def test_changed_lock_is_reported(checkout, monkeypatch):
    def command(argv, root, budget, cancellation):
        if argv[1] == 'ci':
            (root / 'package-lock.json').write_text('{}')
        return 0, b'v24.0.0'
    monkeypatch.setattr(frontend, '_command', command)
    with pytest.raises(ValueError, match='changed package-lock'):
        frontend.ensure_source_frontend()


def test_existing_installed_assets_missing_are_prepared(checkout, monkeypatch):
    installed(checkout)
    (checkout / 'package.json').write_text('{}')
    checks = []
    def command(argv, root, budget, cancellation):
        if argv[-1] == '--check':
            checks.append(argv)
            return (1 if len(checks) == 1 else 0), b''
        return 0, b'v24.0.0'
    monkeypatch.setattr(frontend, '_command', command)
    frontend.ensure_source_frontend()
    assert len(checks) == 2


@pytest.mark.parametrize('capture', [True, False])
def test_command_returns_status_and_only_captures_version(tmp_path, capture):
    import sys
    from api_agent.providers.progress import report_timeout_sync
    argv = [sys.executable, '--version'] if capture else [sys.executable, '-c', 'print("discard")']
    with report_timeout_sync('Frontend test command', 5) as budget:
        status, output = frontend._command(argv, tmp_path, budget, None)
    assert status == 0
    assert output.startswith(b'Python') if capture else output == b''


@pytest.mark.parametrize('entry_point', ['sync', 'async'])
def test_standalone_viewer_prepares_frontend_before_rendering(monkeypatch, entry_point):
    import asyncio
    from api_agent.runtime import view_flow
    calls = []
    def prepare():
        calls.append('frontend')
        raise OSError('frontend prerequisite unavailable')
    async def prepare_async():
        prepare()
    monkeypatch.setattr(frontend, 'ensure_source_frontend', prepare)
    monkeypatch.setattr(frontend, 'prepare_source_frontend', prepare_async)
    monkeypatch.setattr(view_flow, '_prepare_view', lambda context: pytest.fail('rendered before frontend readiness'))
    with pytest.raises(OSError, match='frontend prerequisite unavailable'):
        if entry_point == 'sync':
            view_flow.run_view_flow(object())
        else:
            asyncio.run(view_flow.view_flow(object()))
    assert calls == ['frontend']


def test_source_studio_prepares_frontend_before_launching(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from api_agent.ui import provider
    app = tmp_path / 'apps/api-agent-ui'
    app.mkdir(parents=True)
    (app / 'launch.mjs').touch()
    calls = []
    async def prepare():
        calls.append('frontend')
        raise OSError('frontend prerequisite unavailable')
    monkeypatch.setattr(frontend, 'source_root', lambda: tmp_path)
    monkeypatch.setattr(frontend, 'prepare_source_frontend', prepare)
    monkeypatch.setattr(provider, 'app_directory', lambda: app)
    monkeypatch.setattr(provider.shutil, 'which', lambda name: '/node')
    from types import SimpleNamespace
    launcher = SimpleNamespace(start=AsyncMock())
    owner = provider.DemoUIProvider({'UI_LISTENER_URL': 'http://localhost:9443'}, {'ui_processes': launcher})
    with pytest.raises(OSError, match='frontend prerequisite unavailable'):
        asyncio.run(owner.astart())
    assert calls == ['frontend']
    launcher.start.assert_not_awaited()


def test_default_local_view_prepares_frontend_after_valid_configuration(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from api_agent.runtime.local_run import LocalRunOwner
    from api_agent.runtime import view_flow
    calls = []
    def resolve():
        calls.append('configuration')
        return SimpleNamespace(errors=(), missing=(), values={})
    async def prepare():
        calls.append('frontend')
        raise OSError('frontend prerequisite unavailable')
    monkeypatch.setattr(frontend, 'prepare_source_frontend', prepare)
    monkeypatch.setattr(view_flow, 'load_selected_definition', lambda context: pytest.fail('rendered before frontend readiness'))
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    owner = LocalRunOwner(context, resolve)
    with pytest.raises(OSError, match='frontend prerequisite unavailable'):
        asyncio.run(owner.start())
    assert calls == ['configuration', 'frontend']
    assert owner.snapshot.phase == 'stopped'


@pytest.mark.parametrize('linked', [False, True])
def test_unit_runner_prepares_its_source_base_before_execution(tmp_path, monkeypatch, linked):
    from types import SimpleNamespace
    from api_agent.testing import runner
    calls = []
    selection = (SimpleNamespace(id='base/test.py::test_one'),)
    cancelled = threading.Event()
    monkeypatch.setattr(runner, 'collect_tests', lambda *args, **kwargs: selection)
    monkeypatch.setattr(frontend, 'source_root', lambda: tmp_path)
    def prepare(*, cancellation):
        calls.append(cancellation)
        raise OSError('frontend prerequisite unavailable')
    monkeypatch.setattr(frontend, 'ensure_source_frontend', prepare)
    base = tmp_path
    if linked:
        base = tmp_path / 'source-link'
        base.symlink_to(tmp_path, target_is_directory=True)
    context = SimpleNamespace(base_root=base, owner_root=tmp_path)
    with pytest.raises(OSError, match='frontend prerequisite unavailable'):
        runner.run_unit_tests.__wrapped__(context, selection, cancel_event=cancelled)
    assert calls == [cancelled]


@pytest.mark.parametrize('library_root', ['other', None])
def test_unit_runner_does_not_prepare_an_unrelated_source_base(tmp_path, monkeypatch, library_root):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from api_agent.testing import runner
    selection = (SimpleNamespace(id='base/test.py::test_one'),)
    monkeypatch.setattr(runner, 'collect_tests', lambda *args, **kwargs: selection)
    monkeypatch.setattr(frontend, 'source_root', lambda: tmp_path / library_root if library_root else None)
    prepare = Mock(side_effect=AssertionError('unrelated frontend preparation'))
    monkeypatch.setattr(frontend, 'ensure_source_frontend', prepare)
    monkeypatch.setattr(runner.uuid, 'uuid4', Mock(side_effect=RuntimeError('runner execution reached')))
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    with pytest.raises(RuntimeError, match='runner execution reached'):
        runner.run_unit_tests.__wrapped__(context, selection)
    prepare.assert_not_called()


@pytest.mark.parametrize('library_root', ['other', None])
def test_studio_does_not_prepare_an_installed_or_unrelated_app(tmp_path, monkeypatch, library_root):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.ui import provider
    app = tmp_path / 'packaged-app'
    app.mkdir()
    (app / 'launch.mjs').touch()
    prepare = AsyncMock(side_effect=AssertionError('unrelated frontend preparation'))
    monkeypatch.setattr(frontend, 'source_root', lambda: tmp_path / library_root if library_root else None)
    monkeypatch.setattr(frontend, 'prepare_source_frontend', prepare)
    monkeypatch.setattr(provider, 'app_directory', lambda: app)
    monkeypatch.setattr(provider.shutil, 'which', lambda name: '/node')
    process = object()
    launcher = SimpleNamespace(start=AsyncMock(return_value=process))
    owner = provider.DemoUIProvider({'UI_LISTENER_URL': 'http://localhost:9443'}, {'ui_processes': launcher})
    asyncio.run(owner.astart())
    prepare.assert_not_awaited()
    launcher.start.assert_awaited_once()
    assert owner.process is process
