"""The public checker and stable dispatcher run the indexed snapshot."""
import os
from pathlib import Path
import runpy
import signal
import shutil
import subprocess
import sys
import time
from types import ModuleType

import pytest

from tools import checks, pre_commit_dispatcher as dispatcher


ROOT = Path(__file__).resolve().parents[2]


def git(root, *arguments):
    subprocess.run(['git', *arguments], cwd=root, check=True, capture_output=True)


def staged_repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'tools').mkdir()
    shutil.copy2(ROOT / 'tools/checks.py', root / 'tools/checks.py')
    shutil.copy2(ROOT / 'tools/pre_commit_dispatcher.py', root / 'tools/pre_commit_dispatcher.py')
    (root / 'tools/workflow_views.py').write_text('')
    package = root / 'src/api_agent/workflows'
    package.mkdir(parents=True)
    (root / 'src/api_agent/__init__.py').write_text('')
    (package / '__init__.py').write_text('')
    (package / 'viewer_build.py').write_text('def check_views(root):\n    assert (root / "proof.txt").read_text() == "ok\\n"\n')
    (root / 'proof.txt').write_text('ok\n')
    git(root, 'add', '.')
    return root


def test_dispatcher_materializes_real_index_and_ignores_worktree_poison(tmp_path, monkeypatch):
    root = staged_repo(tmp_path)
    monkeypatch.chdir(root)
    assert dispatcher.run() == 0
    (root / 'tools/checks.py').write_text('raise SystemExit(0)\n')
    (root / 'proof.txt').write_text('wrong\n')
    assert dispatcher.run() == 0
    git(root, 'add', 'proof.txt')
    assert dispatcher.run() != 0


def test_dispatcher_starts_isolated_checker(tmp_path, monkeypatch):
    root = staged_repo(tmp_path)
    monkeypatch.chdir(root)
    original = dispatcher._capture
    observed = []

    def capture(command, **kwargs):
        observed.append(tuple(command))
        return original(command, **kwargs)

    monkeypatch.setattr(dispatcher, '_capture', capture)
    assert dispatcher.run() == 0
    assert observed[-1][1] == '-I'
    assert observed[-1][-5:-3] == ('--no-git', '--root')
    assert observed[-1][-2:] == ('--frontend-source', str(root))


def test_dispatcher_honors_verified_relative_temporary_index(tmp_path, monkeypatch):
    root = staged_repo(tmp_path)
    monkeypatch.chdir(root)
    temporary = root / '.git/index.review'
    temporary.write_bytes((root / '.git/index').read_bytes())
    monkeypatch.setenv('GIT_INDEX_FILE', '.git/index.review')
    assert dispatcher.run() == 0
    monkeypatch.setenv('GIT_INDEX_FILE', str(tmp_path / 'outside'))
    assert dispatcher.run() == 2


def test_dispatcher_rejects_missing_checker_and_unsafe_symlink(tmp_path, monkeypatch):
    root = staged_repo(tmp_path)
    monkeypatch.chdir(root)
    git(root, 'rm', '--cached', 'tools/checks.py')
    assert dispatcher.run() == 2
    git(root, 'add', 'tools/checks.py')
    (root / 'link').symlink_to('proof.txt')
    git(root, 'add', 'link')
    assert dispatcher.run() == 2


def test_dispatcher_rejects_malformed_index_and_old_python(tmp_path, monkeypatch):
    root = staged_repo(tmp_path)
    monkeypatch.chdir(root)
    original_git = dispatcher._git

    def malformed(root, environment, deadline, limit, *arguments):
        if arguments[:2] == ('ls-files', '--stage'):
            return b'not-an-index-record\0'
        return original_git(root, environment, deadline, limit, *arguments)

    monkeypatch.setattr(dispatcher, '_git', malformed)
    assert dispatcher.run() == 2
    monkeypatch.setattr(dispatcher, '_git', original_git)

    original_capture = dispatcher._capture
    def old_python(command, **kwargs):
        if len(command) >= 2 and command[1] == '-c':
            return 0, b'3 12\n', b''
        return original_capture(command, **kwargs)

    monkeypatch.setattr(dispatcher, '_capture', old_python)
    assert dispatcher.run() == 2


def test_dispatcher_limits_index_listing_blob_and_aggregate(tmp_path, monkeypatch):
    root = staged_repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(dispatcher, 'INDEX_BYTES', 8)
    assert dispatcher.run() == 2
    monkeypatch.setattr(dispatcher, 'INDEX_BYTES', 8 * 1024 * 1024)
    monkeypatch.setattr(dispatcher, 'MAX_FILES', 1)
    assert dispatcher.run() == 2
    monkeypatch.setattr(dispatcher, 'MAX_FILES', 10_000)
    monkeypatch.setattr(dispatcher, 'BLOB_BYTES', 8)
    assert dispatcher.run() == 2
    monkeypatch.setattr(dispatcher, 'BLOB_BYTES', 8 * 1024 * 1024)
    monkeypatch.setattr(dispatcher, 'TOTAL_BLOB_BYTES', 100)
    assert dispatcher.run() == 2


def test_checker_output_limit_rejects_noisy_staged_checker(tmp_path, monkeypatch, capsys):
    root = staged_repo(tmp_path)
    monkeypatch.chdir(root)
    checker = root / 'tools/checks.py'
    checker.write_text("print('X' * 2000000)\n")
    git(root, 'add', 'tools/checks.py')
    assert dispatcher.run() == 2
    assert len(capsys.readouterr().out) == 0


def test_bounded_capture_kills_owned_process_group_on_limit_and_deadline(tmp_path):
    pid_file = tmp_path / 'child.pid'
    child = ("import os,subprocess,sys,time; "
             "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
             "open(sys.argv[1],'w').write(str(p.pid)); print('X'*2000000,flush=True); time.sleep(60)")
    with pytest.raises(ValueError, match='output exceeds limit'):
        dispatcher._capture([sys.executable, '-c', child, str(pid_file)], cwd=tmp_path,
                            env=os.environ.copy(), deadline=time.monotonic() + 5,
                            seconds=5, output_limit=1024)


@pytest.mark.parametrize('inherit_pipes', [True, False])
def test_capture_stops_descendant_after_launcher_exits(tmp_path, inherit_pipes):
    pid_file = tmp_path / 'descendant.pid'
    script = ("import subprocess,sys; "
              "options={} if sys.argv[2]=='inherit' else "
              "{'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL}; "
              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],**options); "
              "open(sys.argv[1],'w').write(str(child.pid))")
    try:
        if inherit_pipes:
            with pytest.raises(TimeoutError, match='timed out'):
                dispatcher._capture([sys.executable, '-c', script, str(pid_file), 'inherit'],
                                    cwd=tmp_path, env=os.environ.copy(),
                                    deadline=time.monotonic() + .5, seconds=5, output_limit=1024)
        else:
            code, out, err = dispatcher._capture([sys.executable, '-c', script, str(pid_file), 'redirect'],
                                                 cwd=tmp_path, env=os.environ.copy(),
                                                 deadline=time.monotonic() + 5,
                                                 seconds=5, output_limit=1024)
            assert (code, out, err) == (0, b'', b'')
        pid = int(pid_file.read_text())
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True,
                               text=True, timeout=2).stdout.strip()
        assert not state or state.startswith('Z')
    finally:
        if pid_file.exists():
            pid = int(pid_file.read_text())
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('mode', ['timeout', 'error', 'success', 'interrupt'])
def test_checker_command_descendants_are_reaped(tmp_path, mode):
    pid_file = tmp_path / 'checker.pid'
    child_file = tmp_path / 'child.pid'
    checker = tmp_path / 'checker.py'
    checker.write_text(
        'import os,subprocess,sys,time\n'
        f'open({str(pid_file)!r},"w").write(str(os.getpid()))\n'
        'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"],'
        'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
        f'open({str(child_file)!r},"w").write(str(child.pid))\n'
        f'if {mode!r} in ("timeout","interrupt"): time.sleep(60)\n'
        f'if {mode!r} == "error": sys.exit(7)\n')
    try:
        if mode == 'interrupt':
            controller = (
                'import os,sys,time; from pathlib import Path; '
                'sys.path.insert(0,str(Path.cwd())); '
                'from tools import pre_commit_dispatcher as d; '
                "d._capture([sys.executable,'-I',str(Path.cwd()/'checker.py')],"
                'cwd=Path.cwd(),env=os.environ.copy(),'
                'deadline=time.monotonic()+20,seconds=20,output_limit=1048576)')
            (tmp_path / 'tools').mkdir()
            shutil.copy2(ROOT / 'tools/pre_commit_dispatcher.py', tmp_path / 'tools/pre_commit_dispatcher.py')
            leader = subprocess.Popen([sys.executable, '-I', '-c', controller], cwd=tmp_path,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if child_file.exists() or leader.poll() is not None:
                        break
                    time.sleep(.05)
                assert child_file.exists()
                leader.send_signal(signal.SIGINT)
                leader.communicate(timeout=5)
                assert leader.returncode in (-signal.SIGINT, 130)
            finally:
                if leader.poll() is None:
                    leader.kill()
                    leader.communicate(timeout=5)
        elif mode == 'timeout':
            with pytest.raises(TimeoutError, match='timed out'):
                dispatcher._capture([sys.executable, '-I', str(checker)], cwd=tmp_path,
                                    env=os.environ.copy(), deadline=time.monotonic() + 1,
                                    seconds=5, output_limit=1024)
        else:
            code, _, _ = dispatcher._capture([sys.executable, '-I', str(checker)], cwd=tmp_path,
                                              env=os.environ.copy(), deadline=time.monotonic() + 5,
                                              seconds=5, output_limit=1024)
            assert code == (7 if mode == 'error' else 0)
        assert pid_file.exists() and child_file.exists()
        for file in (pid_file, child_file):
            state = subprocess.run(['ps', '-o', 'stat=', '-p', file.read_text()],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            assert not state or state.startswith('Z')
    finally:
        for file in (pid_file, child_file):
            if file.exists():
                try:
                    os.kill(int(file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_capture_prestart_deadline_and_nonzero_git(tmp_path):
    with pytest.raises(TimeoutError, match='deadline exceeded'):
        dispatcher._capture([sys.executable, '-c', 'pass'], cwd=tmp_path, env=os.environ.copy(),
                            deadline=time.monotonic() - 1, seconds=5, output_limit=1024)
    with pytest.raises(ValueError, match='Git index command failed'):
        dispatcher._git(tmp_path, os.environ.copy(), time.monotonic() + 5, 1024, 'rev-parse', '--show-toplevel')


def test_stop_reaps_already_exited_and_failed_group_kill(monkeypatch):
    class Pipe:
        closed = False
        def close(self):
            self.closed = True

    class Process:
        pid = 12345
        stdout = Pipe()
        stderr = None
        killed = False
        waits = 0
        timeout_once = True
        def poll(self):
            return None
        def wait(self, *, timeout):
            self.waits += 1
            if self.waits == 1 and self.timeout_once:
                raise subprocess.TimeoutExpired('fake', timeout)
            return 0
        def kill(self):
            self.killed = True

    process = Process()
    def vanished(pid, sig):
        assert pid == process.pid and sig == signal.SIGKILL
        raise ProcessLookupError
    monkeypatch.setattr(dispatcher.os, 'killpg', vanished)
    dispatcher._stop(process)
    assert process.stdout.closed and process.killed and process.waits == 2
    process.waits = 0
    process.killed = False
    process.timeout_once = False
    dispatcher._stop(process)
    assert process.waits == 1 and not process.killed


def test_finish_enforces_deadline_before_reaping(monkeypatch):
    class Process:
        pid = 12345
    monkeypatch.setattr(dispatcher.os, 'waitid', lambda *args: None)
    with pytest.raises(TimeoutError, match='timed out'):
        dispatcher._finish(Process(), time.monotonic() - 1)


def test_bad_blob_size_and_changed_blob_are_rejected(tmp_path, monkeypatch):
    root = staged_repo(tmp_path)
    monkeypatch.chdir(root)
    original = dispatcher._git
    def bad_size(root, environment, deadline, limit, *arguments):
        if arguments[:2] == ('cat-file', '-s'):
            return b'bad\n'
        return original(root, environment, deadline, limit, *arguments)
    monkeypatch.setattr(dispatcher, '_git', bad_size)
    assert dispatcher.run() == 2
    def changed(root, environment, deadline, limit, *arguments):
        if arguments[:2] == ('cat-file', 'blob'):
            return b''
        return original(root, environment, deadline, limit, *arguments)
    monkeypatch.setattr(dispatcher, '_git', changed)
    assert dispatcher.run() == 2


def test_checker_requires_complete_snapshot_and_calls_packaged_check(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='missing'):
        checks.check(tmp_path)
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools/workflow_views.py').write_text('')
    module = ModuleType('api_agent.workflows.viewer_build')
    selected = []
    module.check_views = lambda root: selected.append(root)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    checks.check(tmp_path)
    assert selected == [tmp_path.resolve()]


def test_checker_cli_modes(monkeypatch, tmp_path):
    monkeypatch.setattr(checks, 'check', lambda root, frontend_source=None: None)
    assert checks.main(['--no-git', '--root', str(tmp_path)]) == 0
    with pytest.raises(SystemExit) as error:
        checks.main(['--no-git'])
    assert error.value.code == 2

    def fail(root, frontend_source=None):
        raise ValueError('stale view')

    monkeypatch.setattr(checks, 'check', fail)
    assert checks.main(['--no-git', '--root', str(tmp_path)]) == 1
    monkeypatch.setattr(dispatcher, 'run', lambda: 7)
    assert checks.main([]) == 7


def test_script_main_guards_are_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'argv', [str(ROOT / 'tools/checks.py'), '--no-git'])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / 'tools/checks.py'), run_name='__main__')
    assert error.value.code == 2
    root = staged_repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, 'argv', [str(ROOT / 'tools/pre_commit_dispatcher.py')])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(ROOT / 'tools/pre_commit_dispatcher.py'), run_name='__main__')
    assert error.value.code == 0
