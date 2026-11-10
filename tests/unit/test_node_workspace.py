"""Declared Node workspaces install once, locked, without leaking credentials."""
import os
import threading

import pytest

from api_agent.runtime import node_workspace


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / 'package.json').write_text('{}')
    (tmp_path / 'package-lock.json').write_text('{"packages": {}}')
    monkeypatch.setattr(node_workspace.shutil, 'which', lambda name: '/' + name)
    monkeypatch.setattr(node_workspace, 'ensure_tools', lambda *names, **options: None)
    monkeypatch.setattr(node_workspace, 'LOCK_DIRECTORY', tmp_path / 'locks')
    (tmp_path / 'locks').mkdir()
    return tmp_path


def install_ready(root, name='tool'):
    binary = root / 'node_modules/.bin' / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.touch()


def test_existing_ready_tree_is_trusted_and_untouched(workspace, monkeypatch):
    install_ready(workspace)
    monkeypatch.setattr(node_workspace, '_command', lambda *a, **k: pytest.fail('must not install'))
    node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',))


def test_existing_tree_missing_ready_path_is_never_replaced(workspace, monkeypatch):
    (workspace / 'node_modules').mkdir()
    monkeypatch.setattr(node_workspace, '_command', lambda *a, **k: pytest.fail('must not install'))
    with pytest.raises(ValueError, match='refusing to replace'):
        node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',))


@pytest.mark.parametrize('missing', ['package.json', 'package-lock.json'])
def test_workspace_needs_manifest_and_lockfile(workspace, missing):
    (workspace / missing).unlink()
    with pytest.raises(ValueError, match='package.json and package-lock.json'):
        node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',))


def test_missing_credentials_fail_before_any_install_and_name_only_keys(workspace, monkeypatch):
    monkeypatch.setattr(node_workspace, 'ensure_tools', lambda *a, **k: pytest.fail('no tools before credentials'))
    monkeypatch.setattr(node_workspace, '_command', lambda *a, **k: pytest.fail('must not install'))
    with pytest.raises(ValueError, match='REGISTRY_ONE, REGISTRY_TWO') as error:
        node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',),
            credentials={'REGISTRY_ONE': '', 'REGISTRY_TWO': None, 'REGISTRY_SET': 'secret-value'})
    assert 'secret-value' not in str(error.value) and 'REGISTRY_SET' not in str(error.value)
    assert not (workspace / 'node_modules').exists()


def test_install_passes_credentials_only_to_the_child_and_verifies(workspace, monkeypatch):
    seen = {}
    def command(argv, root, budget, cancellation, env=None):
        seen.update(argv=argv, root=root, env=env)
        install_ready(root)
        return 0, b''
    monkeypatch.setattr(node_workspace, '_command', command)
    before = dict(os.environ)
    node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',),
        credentials={'REGISTRY_TOKEN': 'secret-value'}, environment={'SKIP_HOOKS': '1'})
    assert seen['argv'] == ['/npm', 'ci', '--include=dev', '--no-audit', '--no-fund']
    assert seen['root'] == workspace
    assert seen['env']['REGISTRY_TOKEN'] == 'secret-value' and seen['env']['SKIP_HOOKS'] == '1'
    assert dict(os.environ) == before and 'REGISTRY_TOKEN' not in os.environ


def test_install_without_declared_credentials_or_environment(workspace, monkeypatch):
    monkeypatch.setattr(node_workspace, '_command',
        lambda argv, root, budget, cancellation, env=None: (install_ready(root), (0, b''))[1])
    node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',))


def test_missing_npm_has_manual_guidance(workspace, monkeypatch):
    monkeypatch.setattr(node_workspace.shutil, 'which', lambda name: None)
    with pytest.raises(OSError, match='Install Node.js'):
        node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',))


@pytest.mark.parametrize('failure', ['status', 'not_ready', 'lockfile'])
def test_failed_install_is_a_fixed_diagnostic(workspace, monkeypatch, failure):
    def command(argv, root, budget, cancellation, env=None):
        if failure == 'lockfile':
            (root / 'package-lock.json').write_text('{"changed": true}')
            install_ready(root)
            return 0, b''
        if failure == 'not_ready':
            return 0, b''
        return 1, b'registry says secret-value'
    monkeypatch.setattr(node_workspace, '_command', command)
    with pytest.raises(OSError, match='preparation failed|changed package-lock.json') as error:
        node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',),
            credentials={'REGISTRY_TOKEN': 'secret-value'})
    assert 'secret-value' not in str(error.value)


def test_peer_installation_completed_while_waiting_for_the_lock(workspace, monkeypatch):
    holder_ready, release = threading.Event(), threading.Event()
    def holder():
        with node_workspace._locked(workspace, budget=None, cancellation=None):
            holder_ready.set()
            release.wait(5)
            install_ready(workspace)
    thread = threading.Thread(target=holder)
    thread.start()
    holder_ready.wait(5)
    monkeypatch.setattr(node_workspace, '_command', lambda *a, **k: pytest.fail('peer already installed'))
    threading.Timer(.2, release.set).start()
    node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',))
    thread.join()


def test_cancellation_and_deadline_stop_lock_waiting(workspace):
    from api_agent.providers.progress import report_timeout_sync
    cancelled = threading.Event()
    with node_workspace._locked(workspace, budget=None, cancellation=None):
        cancelled.set()
        with pytest.raises(InterruptedError):
            with node_workspace._locked(workspace, budget=None, cancellation=cancelled):
                pass
        with report_timeout_sync('Node workspace preparation', .05) as budget:
            with pytest.raises(TimeoutError):
                with node_workspace._locked(workspace, budget=budget, cancellation=None):
                    pass


@pytest.mark.parametrize('raised, expected', [
    (TimeoutError('Frontend preparation timed out'), TimeoutError),
    (InterruptedError('Frontend preparation cancelled'), InterruptedError)])
def test_installer_deadline_and_cancellation_use_workspace_wording(workspace, monkeypatch, raised, expected):
    def command(*args, **options):
        raise raised
    monkeypatch.setattr(node_workspace, '_command', command)
    with pytest.raises(expected, match='Node workspace preparation') as error:
        node_workspace.ensure_node_workspace(workspace, ready=('node_modules/.bin/tool',))
    assert 'Frontend' not in str(error.value)


def test_command_environment_reaches_the_child_process(tmp_path, monkeypatch):
    from api_agent.runtime import frontend
    from api_agent.providers.progress import report_timeout_sync
    marker = tmp_path / 'seen.txt'
    with report_timeout_sync('probe', 30) as budget:
        status, _ = frontend._command(['sh', '-c', f'printf %s "$PROBE_VALUE" > {marker}'], tmp_path, budget, None,
                                      env={**os.environ, 'PROBE_VALUE': 'child-only'})
    assert status == 0 and marker.read_text() == 'child-only' and 'PROBE_VALUE' not in os.environ
