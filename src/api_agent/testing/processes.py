"""Bounded POSIX process-group cleanup; other systems reap the leader only."""
import os
import errno
import asyncio
from pathlib import Path
import signal
import selectors
import threading
import subprocess
import time
import sys

_Popen = subprocess.Popen


def _process_snapshot(*, timeout=5):
    """Read one bounded system snapshot with explicit process identities."""
    try:
        report = subprocess.run(
            ['/bin/ps', '-A', '-o', 'pid=', '-o', 'ppid=', '-o', 'pgid=', '-o', 'state='],
            capture_output=True, text=True, check=True, timeout=timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Cannot inspect owned processes: ps failed') from exc
    rows = []
    seen = set()
    for line in report.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 4 or any(not value.isascii() or not value.isdecimal() for value in fields[:3]):
            raise RuntimeError('Cannot inspect owned processes: invalid ps output')
        pid, parent, group = map(int, fields[:3])
        if pid in seen:
            raise RuntimeError('Cannot inspect owned processes: duplicate ps process identity')
        seen.add(pid)
        rows.append((pid, parent, group, fields[3]))
    return tuple(rows)


def _group_running(group, *, timeout=5):
    """Return whether a POSIX group has live members, excluding zombies."""
    if not Path('/proc').is_dir():
        return any(pgid == group and not state.startswith('Z')
                   for _, _, pgid, state in _process_snapshot(timeout=timeout))
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group and fields[0] != 'Z':
                return True
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError) as exc:
            if isinstance(exc, OSError) and exc.errno == errno.ESRCH:
                continue
            raise RuntimeError(f'Cannot verify owned process group {group}: invalid process stat') from exc
    return False


def disposal_complete(process):
    """Confirmed leader/group disposal belongs to this exact process handle."""
    return getattr(process, '_api_agent_disposal_complete', False) is True


def observe_exit(process):
    """Observe a child exit without releasing its PID reservation."""
    if os.name != 'posix' or not isinstance(process, _Popen):
        poll = getattr(process, 'poll', None)
        return poll() if poll is not None else getattr(process, 'returncode', None)
    with _disposal_lock(process):
        if process.returncode is not None:
            return process.returncode
        try:
            result = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            if process.returncode is not None:
                return process.returncode
            raise RuntimeError('Owned process identity is no longer reserved') from None
        if result is None:
            return None
        return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status


def wait_owned(process, *, timeout):
    """Wait for exit observation while retaining the child for final disposal."""
    if os.name != 'posix' or not isinstance(process, _Popen):
        process.wait(timeout=timeout)
        return process.returncode
    deadline = time.monotonic() + timeout
    while True:
        code = observe_exit(process)
        if code is not None:
            return code
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(min(.01, max(0, deadline - time.monotonic())))


def _disposal_lock(process):
    return process.__dict__.setdefault('_api_agent_disposal_lock', threading.RLock())


def _owned_group_terminal(process, *, timeout=5):
    """Prove this reserved child and its entire group are zombies."""
    if not isinstance(process, _Popen):
        return False
    with _disposal_lock(process):
        if process.returncode is not None:
            return False
        observe_exit(process)  # A missing child reservation must not authorize ps evidence.
        rows = _process_snapshot(timeout=timeout)
        leader = next((row for row in rows if row[0] == process.pid), None)
        if leader is None or leader[1] != os.getpid() or leader[2] != process.pid:
            return False
        # The identity checks ensure the leader is included in this nonempty group.
        return all(state.startswith('Z') for _, _, group, state in rows if group == process.pid)


def inspect_owned(process, inspector):
    """Inspect the actual child only while its identity remains reserved."""
    if isinstance(process, asyncio.subprocess.Process):
        raise RuntimeError('Async child watcher cannot reserve process identity')
    with _disposal_lock(process):
        if process.returncode is not None or observe_exit(process) is not None:
            return None
        try:
            return inspector(process.pid)
        except OSError as error:
            if sys.platform == 'darwin' and error.errno == errno.ESRCH:
                try:
                    terminal = _owned_group_terminal(process)
                except RuntimeError:
                    raise error from None
                if terminal:
                    return None
            raise


def signal_owned_group(process, signum):
    """Signal only a reserved child group; latch terminal absence on its handle."""
    if isinstance(process, asyncio.subprocess.Process):
        raise RuntimeError('Async child watcher cannot reserve process identity')
    with _disposal_lock(process):
        if getattr(process, '_api_agent_group_disposal_complete', False) is True:
            return True
        if type(getattr(process, 'returncode', None)) is int:
            # An already-reaped PID cannot authorize even the first signal.
            if _group_running(process.pid):
                raise RuntimeError('Cannot signal an unreserved process group')
            process._api_agent_group_disposal_complete = True
            return True
        if isinstance(process, _Popen):
            observe_exit(process)
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            process._api_agent_group_disposal_complete = True
            return True
        except PermissionError:
            if not _owned_group_terminal(process):
                raise
            process._api_agent_group_disposal_complete = True
            return True
        return False


def finalize(process, *, communicate=False):
    """Dispose under one handle lock; retain child identity through final signal."""
    with _disposal_lock(process):
        return _finalize(process, communicate=communicate)


def _finalize(process, *, communicate=False):
    if disposal_complete(process):
        return (None, None) if communicate else process.returncode
    primary = None
    leader_reaped = False
    group_disposed = False

    def send(force=False):
        nonlocal group_disposed
        if group_disposed or getattr(process, '_api_agent_group_signals_complete', False) is True:
            return
        try:
            if os.name == 'posix':
                group_disposed = signal_owned_group(process, signal.SIGKILL if force else signal.SIGTERM)
                if force:
                    process._api_agent_group_signals_complete = True
            elif force:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            group_disposed = True

    try:
        send()
        if os.name == 'posix' and isinstance(process, _Popen):
            grace = time.monotonic() + .5
            while observe_exit(process) is None and time.monotonic() < grace:
                time.sleep(.01)
            # No wait/poll/communicate may release the leader before this signal.
            send(True)
        wait = process.communicate if communicate else process.wait
        try:
            result = wait(timeout=.5)
        except subprocess.TimeoutExpired:
            send(True)
            result = wait(timeout=5)
        leader_reaped = True
        return result
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if os.name == 'posix':
                # A failed wait may need disposal, but a reaped handle can only
                # verify absence and never authorize a released numeric group.
                send(True)
                deadline = time.monotonic() + 5
                while not group_disposed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'Owned pytest process group {process.pid} did not stop after SIGKILL')
                    if not _group_running(process.pid, timeout=remaining):
                        group_disposed = True
                        process._api_agent_group_disposal_complete = True
                        break
                    time.sleep(min(.01, remaining))
        except BaseException as error:
            if primary is None:
                raise
            primary.cleanup_error = error
        else:
            if leader_reaped or type(getattr(process, 'returncode', None)) is int:
                process._api_agent_disposal_complete = True


def communicate_owned(process, payload=None, *, timeout=None, check=None):
    """Drain pipes while retaining the leader, then dispose before reaping it."""
    if check is not None:
        check()
    if os.name != 'posix' or not isinstance(process, _Popen):
        return process.communicate(timeout=timeout) if payload is None else process.communicate(payload, timeout=timeout)
    deadline = None if timeout is None else time.monotonic() + timeout
    output = {name: bytearray() for name in ('stdout', 'stderr')}
    selector = selectors.DefaultSelector()
    data = payload.encode(process.encoding or 'utf-8') if isinstance(payload, str) else payload or b''
    position = 0
    try:
        for name in ('stdout', 'stderr'):
            pipe = getattr(process, name)
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
        if process.stdin is not None:
            if data:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            else:
                process.stdin.close()
        while selector.get_map() or observe_exit(process) is None:
            if check is not None:
                check()
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout)
            if observe_exit(process) is not None:
                finalize(process)
            events = selector.select(.01)
            for key, _ in events:
                if key.data == 'stdin':
                    try:
                        position += os.write(key.fd, data[position:position + 65536])
                    except BrokenPipeError:
                        position = len(data)
                    if position == len(data):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    chunk = os.read(key.fd, 65536)
                    if chunk:
                        output[key.data].extend(chunk)
                    else:
                        selector.unregister(key.fileobj)
            if not events and disposal_complete(process):
                # Selector silence is not EOF. Sweep each registered pipe rather
                # than returning a potentially truncated successful response.
                for key in list(selector.get_map().values()):
                    if key.data == 'stdin':
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        raise RuntimeError('Owned process output did not close after disposal') from None
                    if chunk:
                        output[key.data].extend(chunk)
                    else:
                        selector.unregister(key.fileobj)
        finalize(process)
        result = []
        for name in ('stdout', 'stderr'):
            pipe = getattr(process, name)
            captured = bytes(output[name])
            result.append(None if pipe is None else captured.decode(process.encoding or 'utf-8', process.errors or 'strict').replace('\r\n', '\n').replace('\r', '\n')
                          if process.text_mode else captured)
        captured_result = tuple(result)
    finally:
        selector.close()
        try:
            finalize(process)
        finally:
            for name in ('stdin', 'stdout', 'stderr'):
                pipe = getattr(process, name)
                if pipe is not None:
                    pipe.close()
    if deadline is not None and time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired(process.args, timeout)
    return captured_result
