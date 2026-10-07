"""Bounded POSIX process-group cleanup; other systems reap the leader only."""
import os
from pathlib import Path
import signal
import subprocess
import time


def _group_running(group):
    """Return whether a POSIX group has live members, excluding zombies."""
    if not Path('/proc').is_dir():
        try:
            report = subprocess.run(['ps', '-A', '-o', 'pgid=', '-o', 'state='],
                                    capture_output=True, text=True, check=True, timeout=.5)
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
    try:
        result = wait(timeout=.5)
    except subprocess.TimeoutExpired:
        send(force=True)
        result = wait(timeout=5)
    finally:
        if os.name == 'posix':
            # The leader may already have exited while a child ignored TERM.
            send(force=True)
            deadline = time.monotonic() + 2
            while _group_running(process.pid):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'Owned pytest process group {process.pid} did not stop after SIGKILL')
                time.sleep(.01)
    return result
