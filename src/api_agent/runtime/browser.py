"""Explicit Chromium preparation for an enabled browser operation.

Call this immediately before browser work, after Playwright's optional Python
dependency has been installed. Normal environment setup never calls it.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


class BrowserPreparationError(RuntimeError):
    """Chromium could not be made ready within the requested bounds."""


def _default_paths(headed):
    # The bundled registry is Playwright's own platform/revision resolver.
    # This adapter is intentionally coupled to the installed Playwright driver.
    try:
        from playwright._impl._driver import compute_driver_executable
    except ImportError as exc:
        raise BrowserPreparationError('Playwright is unavailable; install its optional dependency group first') from exc
    node, cli = compute_driver_executable()
    bundle = Path(cli).parent / 'lib/coreBundle.js'
    name = 'chromium' if headed else 'chromium-headless-shell'
    script = ('const registry = require(process.argv[1]).registry.registry; '
              'const executable = registry.findExecutable(process.argv[2]); '
              'if (!executable) process.exit(2); '
              'process.stdout.write(executable.executablePath());')
    try:
        result = subprocess.run([node, '-e', script, str(bundle), name],
                                capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BrowserPreparationError('Could not resolve the Chromium executable from Playwright') from exc
    if not result.stdout.strip():
        raise BrowserPreparationError('Playwright returned no Chromium executable path')
    return (Path(result.stdout.strip()),)


def _stop(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise BrowserPreparationError('Chromium installer did not exit after termination') from exc


def _diagnostic(log):
    """Classify only known errors; never expose raw installer output or URLs."""
    log.seek(0, os.SEEK_END)
    end = log.tell()
    log.seek(max(0, end - 8192))
    recent = log.read(8192).decode('utf-8', errors='replace').lower()
    if 'certificate_verify_failed' in recent or 'self signed certificate' in recent:
        return 'TLS certificate verification failed while downloading Chromium'
    if 'enotfound' in recent or 'etimedout' in recent or 'network' in recent:
        return 'Chromium download could not reach the network'
    if 'host system is missing dependencies' in recent or 'missing dependencies' in recent:
        return 'host browser libraries are missing; ask an administrator to prepare the host'
    if 'permission denied' in recent or 'eacces' in recent:
        return 'browser cache is not writable'
    return 'installer output did not identify a recognized cause'


def _install(timeout, cancelled, progress=None):
    report = progress or (lambda message: print(message, file=sys.stderr, flush=True))
    report('Preparing Playwright Chromium...')
    with tempfile.TemporaryFile() as log:
        try:
            process = subprocess.Popen([sys.executable, '-m', 'playwright', 'install', 'chromium'],
                                       stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise BrowserPreparationError('Could not start the Chromium installer') from exc
        start = time.monotonic()
        deadline = start + timeout
        next_progress = start + 5
        try:
            while process.poll() is None:
                if cancelled and cancelled():
                    raise BrowserPreparationError('Chromium installation cancelled')
                now = time.monotonic()
                if now >= deadline:
                    raise BrowserPreparationError(f'Chromium installation timed out after {timeout:g}s')
                if now >= next_progress:
                    report(f'Preparing Playwright Chromium... {int(now - start)}s elapsed')
                    next_progress = now + 5
                time.sleep(min(0.1, max(0.0, deadline - now)))
            if process.returncode:
                raise BrowserPreparationError(
                    f'Chromium installation failed (exit {process.returncode}): {_diagnostic(log)}')
            report('Playwright Chromium ready.')
        except BaseException:
            _stop(process)
            raise


def ensure_chromium_ready(*, headed=False, timeout=120, cancelled=None,
                          paths=None, install=None, progress=None):
    """Return an existing executable or install Chromium and recheck it.

    ``cancelled`` is a zero-argument predicate. ``progress(message)`` receives
    status text. ``paths(headed)`` returns
    candidate executable Paths; ``install(timeout, cancelled)`` is an optional
    controlled adapter for callers and tests. A missing file is never trusted.
    """
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    if cancelled and cancelled():
        raise BrowserPreparationError('Chromium preparation cancelled')
    resolve = paths or _default_paths
    candidates = tuple(Path(path) for path in resolve(headed))
    if not candidates:
        raise BrowserPreparationError('No Chromium executable path is available')
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    if install is None:
        _install(timeout, cancelled, progress)
    else:
        install(timeout, cancelled)
    if cancelled and cancelled():
        raise BrowserPreparationError('Chromium preparation cancelled')
    for candidate in resolve(headed):
        candidate = Path(candidate)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise BrowserPreparationError('Chromium installer completed, but the expected executable is absent')
