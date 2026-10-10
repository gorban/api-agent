"""Synthetic workers prove ownership survives forced DEBUG worker disposal."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from api_agent.testing.processes import finalize


def _alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


@pytest.fixture
def worker_subreaper():
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert library.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert library.prctl(36, 1, 0, 0, 0) == 0
    guardians = []
    try:
        yield guardians
    finally:
        for pid in guardians:
            deadline = time.monotonic() + 3
            while True:
                try:
                    reaped, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    break
                if reaped:
                    break
                assert time.monotonic() < deadline
                time.sleep(.01)
        assert library.prctl(36, previous.value, 0, 0, 0) == 0


@pytest.mark.parametrize('mode', ['retrieval', 'recovery'])
@pytest.mark.parametrize('force', [False, True])
@pytest.mark.skipif(sys.platform != "linux", reason="Linux disposable test subreaper")
def test_worker_disposal_kills_detached_auth_group(tmp_path, mode, force, worker_subreaper):
    marker = tmp_path / 'owned'
    guardian_marker = tmp_path / 'guardian'
    helper = (
        'import os,signal,subprocess,sys,time\n'
        'signal.signal(signal.SIGHUP, signal.SIG_IGN)\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGHUP,signal.SIG_IGN);signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])\n"
        f'open({str(marker)!r},"w").write(str(os.getpid())+" "+str(child.pid))\n'
        'time.sleep(60)'
    )
    worker = (
        'import signal,sys\n'
        'from api_agent.auth.owned_process import OwnedProcess\n'
        'original_init=OwnedProcess.__init__\n'
        'def init(self,*args,**kwargs):\n'
        ' original_init(self,*args,**kwargs)\n'
        f' open({str(guardian_marker)!r},"w").write(str(self.pid))\n'
        'OwnedProcess.__init__=init\n'
        + ('signal.signal(signal.SIGTERM,signal.SIG_IGN)\n' if force else '')
        + ('from api_agent.auth.credentials import _run\n'
           f'_run([sys.executable,"-c",{helper!r}],30)\n' if mode == 'retrieval' else
           'from api_agent.auth.recovery import recover\n'
           'class Channel:\n'
           ' def read(self,timeout): return None\n'
           ' def write(self,value): pass\n'
           ' def close(self): pass\n'
           'class Interaction:\n'
           ' def open(self,label): return Channel()\n'
           f'recover([sys.executable,"-c",{helper!r}],Interaction(),"synthetic",30)\n')
    )
    process = subprocess.Popen([sys.executable, '-c', worker], start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = []
    try:
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        worker_subreaper.append(int(guardian_marker.read_text()))
        pids = [int(value) for value in marker.read_text().split()]
        assert len(pids) == 2 and all(_alive(pid) for pid in pids)
        finalize(process)
        deadline = time.monotonic() + 3
        while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not any(_alive(pid) for pid in pids)
        if force:
            assert process.returncode == -signal.SIGKILL
    finally:
        if process.poll() is None:
            finalize(process)
        for pid in pids:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize('waiting,missing,code', [(False, False, 0), (True, False, -9), (True, True, 7)])
def test_guard_retains_group_until_disposal(monkeypatch, waiting, missing, code):
    from api_agent.auth import owned_process as owned
    from types import SimpleNamespace
    monkeypatch.setattr(owned, '_enable_subreaper', lambda: None)
    monkeypatch.setattr(owned, '_reap_descendants', lambda: None)
    calls = []
    helper = SimpleNamespace(pid=123, wait=lambda: calls.append('reap') or code)
    monkeypatch.setattr(owned.subprocess, 'Popen', lambda argv, **kwargs: helper)
    monkeypatch.setattr(owned.os, 'close', lambda fd: calls.append(('close', fd)))
    states = iter([None, object()] if waiting else [object()])
    monkeypatch.setattr(owned.os, 'waitid', lambda *args: next(states))
    class Selector:
        def register(self, *args): pass
        def select(self, timeout): return [object()] if missing else []
        def close(self): calls.append('selector close')
    monkeypatch.setattr(owned.selectors, 'DefaultSelector', Selector)
    def kill(pid, sig):
        calls.append(('kill', pid, sig))
        if missing:
            raise ProcessLookupError
    monkeypatch.setattr(owned.os, 'killpg', kill)
    assert owned._guard(44, ['synthetic']) == (code if code >= 0 else 128 - code)
    assert calls.index(('kill', 123, signal.SIGKILL)) < calls.index('reap')
    assert calls[-1] == ('close', 44)


def test_guard_start_failure_is_safe(monkeypatch):
    from api_agent.auth import owned_process as owned
    monkeypatch.setattr(owned, '_enable_subreaper', lambda: None)
    def fail(*args, **kwargs): raise OSError('private command')
    monkeypatch.setattr(owned.subprocess, 'Popen', fail)
    assert owned._guard(44, ['synthetic']) == 127


def test_owner_start_failure_closes_both_pipe_ends(monkeypatch):
    from api_agent.auth import owned_process as owned
    closed = []
    monkeypatch.setattr(owned.os, 'pipe', lambda: (44, 45))
    monkeypatch.setattr(owned.os, 'close', closed.append)
    monkeypatch.setattr(owned, '_enable_subreaper', lambda: None)
    def fail(*args, **kwargs): raise OSError('private command')
    monkeypatch.setattr(owned.subprocess, 'Popen', fail)
    with pytest.raises(OSError): owned.OwnedProcess(['synthetic'])
    assert closed == [44, 45]


def test_guard_script_entrypoint(monkeypatch):
    import runpy
    from api_agent.auth import owned_process as owned
    import ctypes
    from types import SimpleNamespace
    monkeypatch.setattr(ctypes, 'CDLL', lambda *args, **kwargs: SimpleNamespace(prctl=lambda *args: 0))
    monkeypatch.setattr(sys, 'argv', [owned.__file__, '44', 'synthetic'])
    def fail(*args, **kwargs): raise OSError('private command')
    monkeypatch.setattr(subprocess, 'Popen', fail)
    with pytest.raises(SystemExit) as error:
        runpy.run_path(owned.__file__, run_name='__main__')
    assert error.value.code == 127


def test_guard_selector_setup_failure_still_disposes_helper(monkeypatch):
    from api_agent.auth import owned_process as owned
    from types import SimpleNamespace
    monkeypatch.setattr(owned, '_enable_subreaper', lambda: None)
    monkeypatch.setattr(owned, '_reap_descendants', lambda: None)
    calls = []
    monkeypatch.setattr(owned.subprocess, 'Popen', lambda *args, **kwargs:
                        SimpleNamespace(pid=123, wait=lambda: calls.append('reap') or 0))
    monkeypatch.setattr(owned.os, 'close', lambda fd: calls.append(('close', fd)))
    monkeypatch.setattr(owned.os, 'killpg', lambda *args: calls.append('kill'))
    def fail(): raise OSError('selector setup failed')
    monkeypatch.setattr(owned.selectors, 'DefaultSelector', fail)
    with pytest.raises(OSError, match='selector setup'):
        owned._guard(44, ['synthetic'])
    assert calls[-3:] == ['kill', 'reap', ('close', 44)]


@pytest.mark.parametrize('platform,result', [('linux', 0), ('linux', -1), ('darwin', 0)])
def test_guardian_subreaper_setup(monkeypatch, platform, result):
    from api_agent.auth import owned_process as owned
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(owned.sys, 'platform', platform)
    monkeypatch.setattr(owned.ctypes, 'CDLL', lambda *args, **kwargs:
                        SimpleNamespace(prctl=lambda *args: calls.append(args) or result))
    if result:
        with pytest.raises(OSError, match='own helper descendants'):
            owned._enable_subreaper()
    else:
        owned._enable_subreaper()
    assert bool(calls) == (platform == 'linux')


@pytest.mark.parametrize('expires', [False, True])
def test_guardian_reaps_adopted_descendants_with_bound(monkeypatch, expires):
    from api_agent.auth import owned_process as owned
    results = iter([(12, 9), (0, 0)])
    def wait(*args):
        try: return next(results)
        except StopIteration: raise ChildProcessError
    monkeypatch.setattr(owned.os, 'waitpid', wait)
    times = iter([0, 2 if expires else .1])
    monkeypatch.setattr(owned.time, 'monotonic', lambda: next(times))
    sleeps = []
    monkeypatch.setattr(owned.time, 'sleep', sleeps.append)
    owned._reap_descendants()
    assert sleeps == ([] if expires else [.01])


def test_owner_reader_close_fault_disposes_and_acknowledges_spawn(monkeypatch):
    from api_agent.auth import owned_process as owned
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(owned.os, 'pipe', lambda: (44, 45))
    def close(fd):
        calls.append(('close', fd))
        if fd == 44:
            raise InterruptedError('reader already closed')
    monkeypatch.setattr(owned.os, 'close', close)
    monkeypatch.setattr(owned.subprocess, 'Popen', lambda *args, **kwargs:
                        SimpleNamespace(wait=lambda: calls.append('guardian reaped')))
    with pytest.raises(InterruptedError, match='reader already closed'):
        owned.OwnedProcess(['synthetic'])
    assert calls == [('close', 44), ('close', 45), 'guardian reaped']


def test_owner_preserves_housekeeping_fault_when_acknowledgment_fails(monkeypatch):
    from api_agent.auth import owned_process as owned
    from types import SimpleNamespace
    monkeypatch.setattr(owned.os, 'pipe', lambda: (44, 45))
    def close(fd):
        if fd == 44: raise InterruptedError('reader already closed')
    def wait(): raise OSError('guardian acknowledgment failed')
    monkeypatch.setattr(owned.os, 'close', close)
    monkeypatch.setattr(owned.subprocess, 'Popen', lambda *args, **kwargs: SimpleNamespace(wait=wait))
    with pytest.raises(InterruptedError) as failure:
        owned.OwnedProcess(['synthetic'])
    assert str(failure.value.cleanup_error) == 'guardian acknowledgment failed'
