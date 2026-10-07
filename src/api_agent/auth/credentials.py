"""Noninteractive, in-memory credential retrieval from existing CLI sessions."""
from __future__ import annotations

import csv
import io
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass


class CredentialError(RuntimeError):
    """An operation failure suitable for presenting to the TUI."""

    exit_code = 1


@dataclass(frozen=True)
class CredentialSource:
    manager: str
    cli_path: str
    vault: str
    user: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CredentialSource:
        fields = ("manager", "cli_path", "vault", "user")
        if any(not isinstance(value.get(key), str) or not value[key].strip() for key in fields):
            raise CredentialError("Credential source requires nonempty manager, cli_path, vault and user strings.")
        source = cls(**{key: value[key] for key in fields})
        if source.manager not in {"keeper", "1password"}:
            raise CredentialError("Credential manager must be keeper or 1password.")
        return source


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


def _run(argv: list[str], timeout: float) -> str:
    if not math.isfinite(timeout) or timeout <= 0:
        raise CredentialError("Credential CLI timeout must be positive.")
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False,
                                   start_new_session=os.name == "posix")
    except OSError as exc:
        raise CredentialError("Unable to start configured credential CLI.") from exc
    def terminate_owned():
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass

    events: queue.Queue[tuple[int, bytes | None]] = queue.Queue()
    captured = [bytearray(), bytearray()]

    def read_stream(index: int, stream) -> None:
        try:
            while chunk := os.read(stream.fileno(), 4096):
                events.put((index, chunk))
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
            if index == 1 and _PROMPT.search(bytes(captured[1]).decode("utf-8", errors="replace")):
                reason = "requires authentication or input"
                break
        if reason:
            terminate_owned()
        try:
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            reason = reason or "timed out"
            terminate_owned()
            code = process.wait()
        for thread in threads:
            thread.join(timeout=1)
        while not events.empty():
            index, chunk = events.get_nowait()
            if chunk is not None:
                captured[index].extend(chunk)
        if reason or code:
            output = "\n".join(bytes(data).decode("utf-8", errors="replace") for data in captured).strip()
            raise CredentialError(f"Credential CLI {reason or f'failed with exit code {code}'}. "
                                  f"Log into the CLI first using an interactive session.\n{output}")
        try:
            return bytes(captured[0]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialError("Credential CLI returned invalid UTF-8 output.") from exc
    finally:
        if process.poll() is None:
            terminate_owned()
            process.wait()


def _retrieve(source: CredentialSource | Mapping[str, object], kind: str, timeout: float) -> str:
    if type(kind) is not str or kind not in {"username", "password", "totp"}:
        raise CredentialError("Unsupported credential kind.")
    if isinstance(source, CredentialSource):
        source = CredentialSource.from_mapping(vars(source))
    else:
        source = CredentialSource.from_mapping(source)
    cli = resolve_cli_path(source.cli_path)
    if source.manager == "keeper":
        item = f"{source.vault}/{source.user}"
        if kind == "password":
            argv = [cli, "find-password", item]
        elif kind == "username":
            argv = [cli, "clipboard-copy", item, "--field", "login", "--output", "stdout"]
        else:
            argv = [cli, "clipboard-copy", item, "--totp", "--output", "stdout", "-t"]
    else:
        argv = [cli, "item", "get", source.user, "--vault", source.vault]
        if kind == "password":
            argv += ["--fields", "password", "--reveal"]
        elif kind == "username":
            argv += ["--fields", "username"]
        else:
            argv += ["--otp"]
    output = _run(argv, timeout)
    if source.manager == "1password" and kind in {"username", "password"}:
        try:
            rows = list(csv.reader(io.StringIO(output), strict=True))
        except csv.Error as exc:
            raise CredentialError(f"1Password returned invalid {kind} CSV.") from exc
        if len(rows) != 1 or len(rows[0]) != 1:
            raise CredentialError(f"1Password {kind} output must contain exactly one CSV cell.")
        output = rows[0][0]
    else:
        # Keeper appends one line terminator to the credential it prints.
        terminator = "\r\n" if os.name == "nt" else "\n"
        if output.endswith(terminator):
            output = output[:-len(terminator)]
    if not output or kind == "username" and not output.strip():
        raise CredentialError("Credential CLI returned an empty credential.")
    return output


def get_password(source: CredentialSource | Mapping[str, object], *, timeout: float = 15.0) -> str:
    return _retrieve(source, "password", timeout)


def get_username(source: CredentialSource | Mapping[str, object], *, timeout: float = 15.0) -> str:
    """Fetch the login identity, distinct from its stored vault entry label."""
    try:
        return _retrieve(source, "username", timeout)
    except CredentialError:
        # CLI failures can contain the identity in either output stream.
        raise CredentialError("Username retrieval failed. Check the configured CLI session and login entry.") from None


def get_totp(source: CredentialSource | Mapping[str, object], *, timeout: float = 15.0) -> str:
    return _retrieve(source, "totp", timeout)
