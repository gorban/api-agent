"""Noninteractive, in-memory credential retrieval from existing CLI sessions."""
from __future__ import annotations

import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field


class CredentialError(RuntimeError):
    """An operation failure suitable for presenting to the TUI."""

    exit_code = 1


class CredentialLoginRequiredError(CredentialError):
    """Safe authentication-required failure without helper output."""


class CredentialRecoveryError(CredentialError):
    """Safe recovery failure without input or transcript details."""


DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True)
class CredentialSource:
    manager: str
    cli_path: str
    vault: str
    user: str
    _registry: object | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], *, registry=None) -> CredentialSource:
        fields = ("manager", "cli_path", "vault", "user")
        if not isinstance(value, Mapping) or any(not isinstance(value.get(key), str) or not value[key].strip() for key in fields):
            raise CredentialError("Credential source requires nonempty manager, cli_path, vault and user strings.")
        from .managers import ManagerError, bundled_registry
        registry = bundled_registry() if registry is None else registry
        try:
            registry.get(value['manager'])
        except ManagerError as error:
            raise CredentialError(str(error)) from None
        return cls(**{key: value[key] for key in fields}, _registry=registry)


def resolve_cli_path(cli_path: str) -> str:
    """Resolve exactly the configured executable; never fall back to another CLI."""
    resolved = shutil.which(cli_path)
    if resolved is None:
        raise CredentialError("Configured credential CLI does not exist or is not executable.")
    return os.path.abspath(resolved)


_PROMPT = re.compile(
    r"(?:enter|provide|type)\s+(?:your\s+|the\s+)?(?:master\s+)?(?:password|passphrase|verification code|otp|input)"
    r"|(?:password|passphrase|verification code|otp)\s*:"
    r"|(?:sign[ -]?in|log[ -]?in|unlock|authentication|input).{0,40}(?:required|needed|first)"
    r"|(?:not signed in|not logged in|session expired|press enter|confirm.{0,20}\[y/n\])",
    re.IGNORECASE,
)


def _run(argv: list[str], timeout: float, *, classify_login=None, cancel_event=None) -> str:
    if not math.isfinite(timeout) or timeout <= 0:
        raise CredentialError("Credential CLI timeout must be positive.")
    try:
        from .owned_process import OwnedProcess
        factory = OwnedProcess if os.name == "posix" else subprocess.Popen
        process = factory(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, shell=False)
    except OSError:
        raise CredentialError("Unable to start configured credential CLI.") from None
    terminated = False
    def terminate_owned():
        nonlocal terminated
        if terminated:
            return
        terminated = True
        try:
            if os.name == "posix":
                process.dispose()
            elif process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass

    events: queue.Queue[tuple[int, bytes | None]] = queue.Queue()
    captured = [bytearray(), bytearray()]

    def read_stream(index: int, stream) -> None:
        total = 0
        try:
            while chunk := os.read(stream.fileno(), 4096):
                events.put((index, chunk))
                total += len(chunk)
                if total > 1024 * 1024:
                    break
        finally:
            stream.close()
            events.put((index, None))

    threads = [threading.Thread(target=read_stream, args=(i, stream), daemon=True)
               for i, stream in enumerate((process.stdout, process.stderr))]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    closed = 0
    reason = None
    try:
        while closed < 2:
            if cancel_event is not None and cancel_event.is_set():
                reason = "cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timed out"
                break
            try:
                index, chunk = events.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if chunk is None:
                closed += 1
                continue
            captured[index].extend(chunk)
            if len(captured[index]) > 1024 * 1024:
                reason = "output exceeded its limit"
                break
            if index == 1 and classify_login is not None and classify_login(bytes(captured[1]).decode("utf-8", errors="replace")):
                reason = "login required"
                break
            if index == 1 and _PROMPT.search(bytes(captured[1]).decode("utf-8", errors="replace")):
                reason = "requires authentication or input"
                break
        if reason:
            terminate_owned()
        if os.name == 'posix':
            # Guardian exit acknowledges helper-group disposal and reaping.
            # The guardian itself remains our child until this wait completes.
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                if cancel_event is not None and cancel_event.is_set():
                    reason = 'cancelled'
                if time.monotonic() >= deadline:
                    reason = reason or 'timed out'
                if reason:
                    terminate_owned()
                time.sleep(.01)
            terminate_owned()
            code = process.wait()
        else:
            try:
                code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = reason or 'timed out'
                terminate_owned()
                code = process.wait()
        for thread in threads:
            thread.join(timeout=1)
        while not events.empty():
            index, chunk = events.get_nowait()
            if chunk is not None:
                captured[index].extend(chunk)
        if reason or code:
            if reason == "cancelled":
                raise CredentialRecoveryError("Password manager recovery cancelled.") from None
            if classify_login is not None and classify_login(bytes(captured[1]).decode("utf-8", errors="replace")):
                raise CredentialLoginRequiredError("Keeper login required. Log into Keeper or use interactive recovery.") from None
            raise CredentialError(f"Credential CLI {reason or f'failed with exit code {code}'}. "
                                  f"Log into the CLI first using an interactive session.")
        try:
            return bytes(captured[0]).decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialError("Credential CLI returned invalid UTF-8 output.") from None
    finally:
        if process.returncode is None:
            terminate_owned()
            process.wait()
        if os.name == "posix":
            process.dispose()
        for thread in threads:
            thread.join(timeout=1)


def _retrieve(source: CredentialSource | Mapping[str, object], kind: str, timeout: float, *, interaction=None, recovery_timeout=None) -> str:
    if type(kind) is not str or kind not in {"username", "password", "totp"}:
        raise CredentialError("Unsupported credential kind.")
    if isinstance(source, CredentialSource):
        source = CredentialSource.from_mapping(vars(source), registry=source._registry)
    else:
        source = CredentialSource.from_mapping(source)
    from .managers import ManagerError
    try:
        adapter = source._registry.load(source.manager)
    except ManagerError as error:
        raise CredentialError(str(error)) from None
    cli = resolve_cli_path(source.cli_path)
    try:
        argv = adapter.command(source, kind, cli)
    except ManagerError as error:
        raise CredentialError(str(error)) from None
    except Exception:
        raise CredentialError("Password manager command creation failed.") from None
    if (type(argv) is not list or not argv or argv[0] != cli
            or any(type(argument) is not str or '\0' in argument for argument in argv)):
        raise CredentialError("Password manager returned an invalid credential command.")
    hook = getattr(adapter, 'classify_login', None)
    classify = None
    if hook is not None:
        def classify(stderr):
            try:
                result = hook(stderr)
            except Exception:
                raise CredentialError('Password manager login classification failed.') from None
            if type(result) is not bool:
                raise CredentialError('Password manager returned an invalid login classification.')
            return result
    from api_agent.providers.progress import report_timeout_sync
    if not math.isfinite(timeout) or timeout <= 0:
        raise CredentialError('Credential CLI timeout must be positive.')
    started = time.monotonic()
    try:
        with report_timeout_sync('Password-manager retrieval', timeout) as budget:
            remaining = budget.remaining
            if remaining == 0:
                raise CredentialRecoveryError('Password manager recovery deadline expired.')
            if interaction is not None:
                output = _run(argv, remaining, classify_login=classify, cancel_event=getattr(interaction, 'cancel_event', None))
            else:
                output = _run(argv, remaining, classify_login=classify) if classify is not None else _run(argv, remaining)
    except CredentialLoginRequiredError:
        if interaction is None:
            raise
        from .recovery import recover
        remaining = None if recovery_timeout is None else recovery_timeout - (time.monotonic() - started)
        with report_timeout_sync('Password-manager recovery', None) as budget:
            inherited = budget.remaining
            if inherited is not None:
                remaining = inherited if remaining is None else min(remaining, inherited)
            output = recover(argv, interaction, source._registry.get(source.manager).label, remaining)
    if type(output) is not str:
        raise CredentialError("Credential CLI returned invalid output.")
    try:
        output = adapter.parse(output, kind)
    except ManagerError as error:
        raise CredentialError(str(error)) from None
    except Exception:
        raise CredentialError("Password manager output parsing failed.") from None
    if type(output) is not str:
        raise CredentialError("Password manager returned an invalid credential.")
    if not output or kind == "username" and not output.strip():
        raise CredentialError("Credential CLI returned an empty credential.")
    return output


def get_password(source: CredentialSource | Mapping[str, object], *, timeout: float = DEFAULT_TIMEOUT, interaction=None, recovery_timeout=None) -> str:
    return _coordinated(source, "password", timeout, interaction, recovery_timeout)


def get_username(source: CredentialSource | Mapping[str, object], *, timeout: float = DEFAULT_TIMEOUT, interaction=None, recovery_timeout=None) -> str:
    """Fetch the login identity, distinct from its stored vault entry label."""
    try:
        return _coordinated(source, "username", timeout, interaction, recovery_timeout)
    except (CredentialLoginRequiredError, CredentialRecoveryError):
        raise
    except CredentialError:
        # CLI failures can contain the identity in either output stream.
        raise CredentialError("Username retrieval failed. Check the configured CLI session and login entry.") from None


def get_totp(source: CredentialSource | Mapping[str, object], *, timeout: float = DEFAULT_TIMEOUT, interaction=None, recovery_timeout=None) -> str:
    return _coordinated(source, "totp", timeout, interaction, recovery_timeout)


def _coordinated(source, kind, timeout, interaction, recovery_timeout):
    if interaction is None:
        return _retrieve(source, kind, timeout)
    if recovery_timeout is not None and (not math.isfinite(recovery_timeout) or recovery_timeout <= 0):
        raise CredentialRecoveryError('Password manager recovery deadline expired.')
    from api_agent.providers.progress import report_timeout_sync
    with report_timeout_sync('Password-manager request', recovery_timeout) as budget:
        return _coordinated_interaction(source, kind, timeout, interaction, budget.remaining)


def _coordinated_interaction(source, kind, timeout, interaction, recovery_timeout):
    from .recovery import recovery_lock
    lock = getattr(interaction, 'lock', recovery_lock)
    deadline = None if recovery_timeout is None else time.monotonic() + recovery_timeout
    while not lock.acquire(timeout=.05):
        if getattr(interaction, 'cancel_event', threading.Event()).is_set():
            raise CredentialRecoveryError('Password manager recovery cancelled.')
        if deadline is not None and time.monotonic() >= deadline:
            raise CredentialRecoveryError('Password manager recovery deadline expired.')
    try:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise CredentialRecoveryError('Password manager recovery deadline expired.')
        if getattr(interaction, 'cancel_event', threading.Event()).is_set():
            raise CredentialRecoveryError('Password manager recovery cancelled.')
        retrieval_timeout = timeout if remaining is None else min(timeout, remaining)
        return _retrieve(source, kind, retrieval_timeout, interaction=interaction, recovery_timeout=remaining)
    finally:
        lock.release()
