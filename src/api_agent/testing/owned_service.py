"""Run a service under an owner process that stops it when the launcher stops or dies."""
import os
import selectors
import signal
import subprocess
import time


class OwnedService:
    """Start ``bash -c script`` in its own process group and wait for its first output line.

    The script prints one line once the service is up, then waits for its standard
    input to close (`read -r _ || true`) and removes the service from an EXIT trap.
    Closing the input, as `stop` does and as this process dying does, ends the wait.
    Startup past ``deadline`` or once ``cancelled()`` is true signals the whole group
    so a running build or pull ends, lets the trap clean up and raises. ``line`` is
    empty when the script exited before printing one; ``stop`` then returns its exit code.
    """

    def __init__(self, script, arguments, *, environment, deadline, cancelled):
        self._process = subprocess.Popen(['bash', '-c', script, 'owned-service', *arguments], env=dict(environment),
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                         start_new_session=True)
        try:
            self.line = self._ready(deadline, cancelled)
        except BaseException:
            os.killpg(self._process.pid, signal.SIGTERM)
            self.stop()
            raise

    def _ready(self, deadline, cancelled):
        with selectors.DefaultSelector() as selector:
            selector.register(self._process.stdout, selectors.EVENT_READ)
            while True:
                if cancelled():
                    raise InterruptedError('Service startup was cancelled')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Service startup passed its deadline')
                if selector.select(min(0.1, remaining)):
                    return self._process.stdout.readline().strip()

    def stop(self):
        """Let the owner clean up and return its exit code."""
        self._process.stdin.close()
        self._process.stdout.close()
        return self._process.wait()
