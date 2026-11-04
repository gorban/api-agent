"""Bounded POSIX process-group cleanup; other systems reap the leader only."""
import os
from pathlib import Path
import signal
import subprocess
import time


def _group_running(group, *, timeout=5):
    """Return whether a POSIX group has live members, excluding zombies."""
    if not Path('/proc').is_dir():
        try:
            report = subprocess.run(['/bin/ps', '-A', '-o', 'pgid=', '-o', 'state='],
                                    capture_output=True, text=True, check=True, timeout=timeout)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f'Cannot verify owned pytest process group {group}: ps failed') from exc
        for line in report.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or not fields[0].isdecimal() or not fields[1]:
                raise RuntimeError(f'Cannot verify owned pytest process group {group}: invalid ps output')
            if int(fields[0]) == group and fields[1][0] != 'Z':
                return True
        return False
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group and fields[0] != 'Z':
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def disposal_complete(process):
    """Confirmed leader/group disposal belongs to this exact process handle."""
    return getattr(process, '_api_agent_disposal_complete', False) is True


def finalize(process, *, communicate=False):
    """Stop the owned group and wait briefly for leader and live descendants."""
    def send(*, force=False):
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            pass

    send()
    wait = process.communicate if communicate else process.wait
    leader_reaped = False
    primary = None
    try:
        try:
            result = wait(timeout=.5)
        except subprocess.TimeoutExpired:
            send(force=True)
            result = wait(timeout=5)
        leader_reaped = True
        return result
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            if os.name == 'posix':
                # The leader may already have exited while a child ignored TERM.
                send(force=True)
                deadline = time.monotonic() + 5
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'Owned pytest process group {process.pid} did not stop after SIGKILL')
                    if not _group_running(process.pid, timeout=remaining):
                        break
                    time.sleep(min(.01, remaining))
        except BaseException as error:
            if primary is None:
                raise
            primary.cleanup_error = error
        else:
            # Popen.wait can reap during its interrupt grace wait, then raise.
            # Persist that success before the original interruption propagates.
            if leader_reaped or type(getattr(process, 'returncode', None)) is int:
                process._api_agent_disposal_complete = True
