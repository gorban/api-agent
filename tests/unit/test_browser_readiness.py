"""Chromium preparation tests use controlled paths and installers only."""
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import subprocess
from unittest.mock import patch

import pytest

from api_agent.runtime.browser import BrowserPreparationError, ensure_chromium_ready, _default_paths, _diagnostic, _install


def _executable(path):
    path.write_text('fake executable')
    path.chmod(0o755)


def test_reuses_matching_headed_executable_without_install(tmp_path):
    browser = tmp_path / 'chromium'
    _executable(browser)
    called = []
    assert ensure_chromium_ready(headed=True, paths=lambda headed: (browser,) if headed else (),
                                 install=lambda *_: called.append(True)) == browser
    assert not called


def test_missing_or_stale_executable_installs_and_rechecks(tmp_path):
    browser = tmp_path / 'headless-shell'
    browser.write_text('stale, not executable')
    def install(timeout, cancelled):
        assert timeout == 3
        assert cancelled is None
        _executable(browser)
    assert ensure_chromium_ready(timeout=3, paths=lambda headed: (browser,), install=install) == browser


def test_install_success_without_binary_is_error(tmp_path):
    with pytest.raises(BrowserPreparationError, match='expected executable is absent'):
        ensure_chromium_ready(paths=lambda headed: (tmp_path / 'missing',), install=lambda *_: None)


def test_cancel_before_install_does_not_start_it(tmp_path):
    with patch('api_agent.runtime.browser.subprocess.Popen') as popen:
        with pytest.raises(BrowserPreparationError, match='cancelled'):
            ensure_chromium_ready(cancelled=lambda: True, paths=lambda headed: (tmp_path / 'missing',))
        popen.assert_not_called()


@pytest.mark.parametrize('reason', ['timeout', 'cancel'])
def test_owned_installer_is_stopped_and_reaped(reason):
    process = SimpleNamespace(pid=123, returncode=None, poll=lambda: None,
                              kill=lambda: None, wait=lambda timeout: None)
    with patch('api_agent.runtime.browser.subprocess.Popen', return_value=process), \
         patch('api_agent.runtime.browser.os.killpg') as killpg, \
         patch('api_agent.runtime.browser.time.monotonic', side_effect=[0, 1]), \
         patch('api_agent.runtime.browser.time.sleep'), \
         patch.object(process, 'wait') as wait:
        with pytest.raises(BrowserPreparationError, match='timed out|cancelled'):
            _install(0.5, (lambda: True) if reason == 'cancel' else None)
        killpg.assert_called_once()
        wait.assert_called_once_with(timeout=5)


def test_installer_failure_is_actionable_without_subprocess_output():
    process = SimpleNamespace(pid=123, returncode=7, poll=lambda: 7, wait=lambda timeout: None)
    with patch('api_agent.runtime.browser.subprocess.Popen', return_value=process), \
         patch('api_agent.runtime.browser.os.killpg'):
        with pytest.raises(BrowserPreparationError, match='exit 7'):
            _install(2, None)


def test_installer_spawn_error_is_reported():
    with patch('api_agent.runtime.browser.subprocess.Popen', side_effect=OSError('private detail')):
        with pytest.raises(BrowserPreparationError, match='Could not start') as error:
            _install(2, None)
    assert 'private detail' not in str(error.value)


def test_installer_progress_and_sanitized_failure():
    process = SimpleNamespace(pid=123, returncode=7, poll=lambda: 7, wait=lambda timeout: None)
    def popen(*args, **kwargs):
        kwargs['stdout'].write(b'GET https://user:secret@example.invalid/file?token=private\nETIMEDOUT\n')
        return process
    updates = []
    with patch('api_agent.runtime.browser.subprocess.Popen', side_effect=popen), \
         patch('api_agent.runtime.browser.os.killpg'):
        with pytest.raises(BrowserPreparationError, match='could not reach the network') as error:
            _install(2, None, updates.append)
    assert updates == ['Preparing Playwright Chromium...']
    assert 'secret' not in str(error.value) and 'private' not in str(error.value)


def test_installer_emits_elapsed_progress():
    polls = iter((None, None, 0))
    process = SimpleNamespace(pid=123, returncode=0, poll=lambda: next(polls))
    updates = []
    with patch('api_agent.runtime.browser.subprocess.Popen', return_value=process), \
         patch('api_agent.runtime.browser.time.monotonic', side_effect=[0, 6, 6.1]), \
         patch('api_agent.runtime.browser.time.sleep'):
        _install(10, None, updates.append)
    assert updates == ['Preparing Playwright Chromium...',
                       'Preparing Playwright Chromium... 6s elapsed', 'Playwright Chromium ready.']


def test_diagnostic_reads_bounded_tail_only():
    import tempfile
    with tempfile.TemporaryFile() as log:
        log.write(b'CERTIFICATE_VERIFY_FAILED' + b'x' * 20000)
        assert _diagnostic(log) == 'installer output did not identify a recognized cause'


@pytest.mark.parametrize(('headed', 'registry_path'), [
    (False, '/cache/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'),
    (False, '/cache/chromium_headless_shell_mac-arm64_special-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell'),
    (True, '/cache/chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'),
])
def test_paths_come_from_playwright_registry_including_platform_overrides(headed, registry_path):
    driver = SimpleNamespace(compute_driver_executable=lambda: ('/driver/node', '/driver/package/cli.js'))
    with patch.dict(sys.modules, {'playwright._impl._driver': driver}), \
         patch('api_agent.runtime.browser.subprocess.run', return_value=SimpleNamespace(stdout=registry_path)) as run:
        assert _default_paths(headed) == (Path(registry_path),)
        command = run.call_args.args[0]
        assert command[0] == '/driver/node'
        assert 'findExecutable' in command[2]
        assert command[-1] == ('chromium' if headed else 'chromium-headless-shell')


def test_unit_dependency_selection_needs_no_optional_browser_or_driver(tmp_path):
    from api_agent.runtime.dependencies import requirements
    (tmp_path / 'pyproject.toml').write_text(
        '[project]\ndependencies=["packaging>=24"]\n'
        '[project.optional-dependencies]\nbrowser=["playwright>=1.63,<1.64"]\n')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path)
    with patch.dict(sys.modules, {'playwright': None, 'playwright._impl._driver': None}), \
         patch('api_agent.runtime.browser.subprocess.run') as run, \
         patch('api_agent.runtime.browser.subprocess.Popen') as install:
        wanted = requirements(context, collect=True)
    assert wanted == ('packaging>=24', 'pytest>=8,<10', 'pytest-cov>=6,<8', 'pytest-xdist>=3,<4')
    run.assert_not_called()
    install.assert_not_called()


def test_missing_optional_playwright_is_actionable():
    with patch.dict(sys.modules, {'playwright._impl._driver': None}):
        with pytest.raises(BrowserPreparationError, match='Playwright is unavailable'):
            _default_paths(False)


@pytest.mark.parametrize('failure', [OSError('private'), subprocess.CalledProcessError(2, 'node'),
                                    subprocess.TimeoutExpired('node', 10)])
def test_registry_failure_is_bounded_and_does_not_expose_details(failure):
    driver = SimpleNamespace(compute_driver_executable=lambda: ('node', '/package/cli.js'))
    with patch.dict(sys.modules, {'playwright._impl._driver': driver}), \
         patch('api_agent.runtime.browser.subprocess.run', side_effect=failure) as run:
        with pytest.raises(BrowserPreparationError, match='Could not resolve') as error:
            _default_paths(False)
        assert run.call_args.kwargs['timeout'] == 10
        assert error.value.__cause__ is failure
        assert 'private' not in str(error.value)


def test_registry_empty_path_is_rejected():
    driver = SimpleNamespace(compute_driver_executable=lambda: ('node', '/package/cli.js'))
    with patch.dict(sys.modules, {'playwright._impl._driver': driver}), \
         patch('api_agent.runtime.browser.subprocess.run', return_value=SimpleNamespace(stdout=' \n')):
        with pytest.raises(BrowserPreparationError, match='no Chromium executable path'):
            _default_paths(True)


@pytest.mark.parametrize(('text', 'expected'), [
    (b'CERTIFICATE_VERIFY_FAILED', 'TLS certificate verification failed'),
    (b'ENOTFOUND', 'could not reach the network'),
    (b'Host system is missing dependencies', 'host browser libraries are missing'),
    (b'Permission denied', 'browser cache is not writable'),
])
def test_diagnostics_classify_known_failures_without_echoing_output(text, expected):
    import tempfile
    with tempfile.TemporaryFile() as log:
        log.write(text + b' https://user:private@example.invalid/?token=secret')
        diagnostic = _diagnostic(log)
    assert expected in diagnostic
    assert all(value not in diagnostic for value in ('private', 'secret', 'example.invalid'))


@pytest.mark.parametrize('exit_code', [None, 0])
def test_group_cleanup_falls_back_only_for_a_live_parent(exit_code):
    from unittest.mock import Mock
    from api_agent.runtime.browser import _stop
    process = SimpleNamespace(pid=123, poll=lambda: exit_code, kill=Mock(), wait=Mock())
    with patch('api_agent.runtime.browser.os.killpg', side_effect=PermissionError):
        _stop(process)
    assert process.kill.call_count == (1 if exit_code is None else 0)
    process.wait.assert_called_once_with(timeout=5)


def test_missing_group_is_reaped_and_cleanup_timeout_is_reported():
    from unittest.mock import Mock
    from api_agent.runtime.browser import _stop
    failure = subprocess.TimeoutExpired('installer', 5)
    process = SimpleNamespace(pid=123, wait=Mock(side_effect=failure))
    with patch('api_agent.runtime.browser.os.killpg', side_effect=ProcessLookupError):
        with pytest.raises(BrowserPreparationError, match='did not exit') as error:
            _stop(process)
    process.wait.assert_called_once_with(timeout=5)
    assert error.value.__cause__ is failure


def test_preparation_rejects_invalid_timeout_and_missing_candidates():
    with pytest.raises(ValueError, match='timeout must be positive'):
        ensure_chromium_ready(timeout=0)
    with pytest.raises(BrowserPreparationError, match='No Chromium executable path'):
        ensure_chromium_ready(paths=lambda _: ())


def test_default_registry_and_default_installer_forward_progress(tmp_path):
    browser = tmp_path / 'browser'
    updates = []
    def install(timeout, cancelled, progress):
        assert timeout == 2 and cancelled is None
        progress('prepared')
        _executable(browser)
    with patch('api_agent.runtime.browser._default_paths', return_value=(tmp_path / 'absent', browser)), \
         patch('api_agent.runtime.browser._install', side_effect=install):
        assert ensure_chromium_ready(timeout=2, progress=updates.append) == browser
    assert updates == ['prepared']


def test_cancel_after_install_prevents_success_and_recheck(tmp_path):
    from unittest.mock import Mock
    cancelled = Mock(side_effect=[False, True])
    paths = Mock(return_value=(tmp_path / 'absent',))
    install = Mock()
    with pytest.raises(BrowserPreparationError, match='preparation cancelled'):
        ensure_chromium_ready(paths=paths, cancelled=cancelled, install=install)
    install.assert_called_once_with(120, cancelled)
    paths.assert_called_once_with(False)
