"""Bind a POSIX helper group to its caller's lifetime through a private pipe.

The guardian, rather than the caller, owns and reaps the helper leader. Only
the caller holds the pipe writer, so even SIGKILL causes group disposal.
"""
import ctypes
import os
import selectors
import signal
import subprocess
import sys
import time


# Cleanup inspection retains ownership even when the operation deadline expired.
_GROUP_PROBE_SECONDS = 5


def kill_owned_group(pid: int) -> None:
    """Signal an owned, unreaped group; tolerate only a proved zombie-only group."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        # Some POSIX kernels reject signaling a group containing only zombies.
        # The caller retains its child PID with WNOWAIT throughout this check.
        from api_agent.runtime.processes import list_processes
        members = [(member, state) for member, group, state in
                   list_processes('pid', 'pgid', 'state', timeout=_GROUP_PROBE_SECONDS) if group == pid]
        if not any(member == pid and state.startswith("Z") for member, state in members):
            raise
        if any(not state.startswith("Z") for _, state in members):
            raise


class OwnedProcess:
    def __init__(self, argv, *, keep_session=False, **streams):
        # keep_session: own a process group inside the caller's session, so CLIs
        # that scope authorization to the terminal session (1Password) reuse it.
        reader, self.writer = os.pipe()
        self.process = None
        try:
            try:
                self.process = subprocess.Popen(
                    [sys.executable, __file__, str(reader), '1' if keep_session else '0', *argv],
                    pass_fds=(reader,), **_isolation(keep_session), **streams)
            finally:
                # Never retry close after EINTR: the descriptor may be reused.
                os.close(reader)
        except BaseException as error:
            try:
                try:
                    self.dispose()
                finally:
                    if self.process is not None:
                        self.process.wait()
            except BaseException as cleanup_error:
                error.cleanup_error = cleanup_error
            raise

    def __getattr__(self, name):
        return getattr(self.process, name)

    def dispose(self):
        if self.writer is not None:
            writer, self.writer = self.writer, None
            os.close(writer)


def _enable_subreaper():
    if sys.platform == 'linux':
        library = ctypes.CDLL(None, use_errno=True)
        if library.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), 'Unable to own helper descendants')


def _reap_descendants():
    # Linux adopts only this guardian's orphaned descendants. A bounded wait
    # handles their asynchronous SIGKILL exit without hanging on escaped groups.
    deadline = time.monotonic() + 1
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            if time.monotonic() >= deadline:
                return
            time.sleep(.01)


def _isolation(keep_session):
    return {'process_group': 0} if keep_session else {'start_new_session': True}


def _guard(reader, argv, keep_session=False):
    # The helper never receives the lifetime pipe. Its argv and terminal setup
    # remain exactly those supplied by the adapter/recovery bootstrap.
    try:
        _enable_subreaper()
        helper = subprocess.Popen(argv, close_fds=True, **_isolation(keep_session))
    except OSError:
        return 127
    selector = None
    try:
        os.close(0)
        os.close(1)
        os.close(2)
        selector = selectors.DefaultSelector()
        selector.register(reader, selectors.EVENT_READ)
        while os.waitid(os.P_PID, helper.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            if selector.select(.02):
                break
    finally:
        # An unreaped leader reserves the group identity through disposal.
        kill_owned_group(helper.pid)
        code = helper.wait()
        _reap_descendants()
        if selector is not None:
            selector.close()
        os.close(reader)
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(_guard(int(sys.argv[1]), sys.argv[3:], sys.argv[2] == '1'))
