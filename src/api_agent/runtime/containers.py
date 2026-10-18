"""Bounded, Docker-compatible container inventory and explicit stop adapter."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
import json
import os
import re
import time
from pathlib import Path
import shlex
import shutil
import subprocess

from .compose import ENGINE_ENVIRONMENT_KEYS, controlled_engine_environment


class ContainerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    image: str
    status: str
    running: bool = True


def session_container_identity():
    """Read local container metadata without executing it as shell code."""
    identities = {os.environ.get(key, "") for key in ("CONTAINER_ID", "CONTAINER_NAME")}
    try:
        metadata = Path("/run/.containerenv").read_text()
    except FileNotFoundError:
        metadata = ""
    except OSError as error:
        raise ContainerError("Cannot identify the container hosting this session.") from error
    for line in metadata.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"id", "name"}:
            try:
                identities.update(shlex.split(value))
            except ValueError as error:
                raise ContainerError("Invalid session-container identity metadata.") from error
    return frozenset(value.lstrip("/") for value in identities if value)


def is_session_container(identifier, names, identities):
    identifier = identifier.lstrip("/")
    names = {name.strip().lstrip("/") for name in names.split(",")}
    for protected in identities:
        if protected in names or identifier == protected:
            return True
        # Engine inventories commonly abbreviate full hexadecimal IDs.
        if (all(char in "0123456789abcdef" for char in identifier.lower())
                and all(char in "0123456789abcdef" for char in protected.lower())
                and (protected.startswith(identifier) or identifier.startswith(protected))):
            return True
    return False


def _engine_environment():
    """Keep the supported engine/wrapper transport without provider context."""
    return controlled_engine_environment({key: value for key, value in os.environ.items()
                                          if key in ENGINE_ENVIRONMENT_KEYS})


class ContainerRuntime:
    def __init__(self, executable=None, scope=None, *, ownership_directory=None):
        self.executable = executable
        self.scope = scope
        self.ownership_directory = ownership_directory

    def _run(self, arguments, *, timeout=5):
        executable = self.executable or shutil.which("docker") or shutil.which("podman")
        if not executable:
            raise ContainerError("Container inventory unavailable: docker and podman are absent from PATH.")
        try:
            result = subprocess.run([executable, *arguments], capture_output=True, text=True, timeout=timeout, check=False,
                                    env=_engine_environment())
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ContainerError(f"Container engine unavailable: {type(error).__name__}.") from error
        if result.returncode:
            # Do not expose engine stderr, which can include credentials in remote URLs.
            raise ContainerError(f"Container engine command failed (exit {result.returncode}).")
        return result.stdout

    def _inspect(self, identifiers, *, reader=None):
        try:
            records = json.loads((reader or self._run)(["inspect", *identifiers]))
            if not isinstance(records, list):
                raise ValueError("Expected inspect array")
            for record in records:
                if (not isinstance(record, dict)
                        or not isinstance(record.get("Id"), str)
                        or not record["Id"]
                        or not isinstance(record.get("Name"), str)
                        or not isinstance(record.get("Config"), dict)
                        or not isinstance(record.get("State"), dict)
                        or type(record["State"].get("Running")) is not bool):
                    raise ValueError("Invalid inspect record")
            return records
        except (ValueError, TypeError) as error:
            raise ContainerError("Container inventory unavailable: invalid engine response.") from error

    def _eligible(self, record, protected):
        return (self.scope.owns(record["Config"].get("Labels"))
                and not is_session_container(record["Id"], record["Name"], protected)
                and not any(key.startswith(("com.github.containers.toolbox", "distrobox", "io.github.distrobox"))
                            or (key == "manager" and value == "distrobox")
                            for key, value in record["Config"]["Labels"].items()))

    def inventory(self):
        if self.scope is None:
            return ()
        identifiers = self._run([
            "ps", "--all", "--quiet", "--no-trunc",
            "--filter", "label=api-agent.managed=true",
        ]).split()
        if not identifiers:
            return ()
        if len(set(identifiers)) != len(identifiers) or any(not re.fullmatch('[0-9a-f]{64}', identifier) for identifier in identifiers):
            raise ContainerError('Container inventory unavailable: invalid container IDs.')
        records = self._inspect(identifiers)
        if len(records) != len(identifiers) or {record['Id'] for record in records} != set(identifiers):
            raise ContainerError('Container inventory unavailable: identities changed.')
        if any(self.scope.is_legacy(record['Config'].get('Labels')) for record in records):
            raise ContainerError('Previous path-based stack scope exists; review and release its leases and containers before migration.')
        protected = session_container_identity()
        return tuple(Container(record["Id"], record["Name"].lstrip("/"),
                               str(record["Config"].get("Image", "")),
                               str(record["State"].get("Status", "")), record["State"]["Running"])
                     for record in records if self._eligible(record, protected))

    def stop(self, identifiers):
        if self.scope is None:
            raise ContainerError("Container ownership scope is required before stopping.")
        identifiers = tuple(dict.fromkeys(identifiers))
        if not identifiers or any(not isinstance(identifier, str) or not identifier or identifier.startswith("-") for identifier in identifiers):
            raise ContainerError("Select valid container IDs before stopping.")
        protected = session_container_identity()
        if any(is_session_container(identifier, identifier, protected) for identifier in identifiers):
            raise ContainerError("Cannot stop the container hosting this session.")
        from .prerequisites import guarded_cleanup, PrerequisiteError
        guard = (guarded_cleanup(self.ownership_directory, self.scope)
                 if self.ownership_directory is not None else nullcontext(frozenset()))
        try:
            with guard as leased:
                if leased.intersection(identifiers):
                    raise ContainerError("Selected containers are in use by an active invocation.")
                records = self._inspect(identifiers)
                if (len(records) != len(identifiers)
                        or {record["Id"] for record in records} != set(identifiers)
                        or not all(self._eligible(record, protected) for record in records)):
                    raise ContainerError("Selected containers are no longer owned by this stack scope.")
                # Engines may stop each selected service in sequence. Give each
                # its normal graceful interval, with a finite batch ceiling.
                self._run(["stop", "--time", "10", *identifiers],
                          timeout=min(120, 5 + 12 * len(identifiers)))
        except PrerequisiteError:
            raise ContainerError("Cannot verify active prerequisite leases; cleanup refused.") from None


    def logs(self, identifier, *, cancelled=lambda: False, scrubber=None):
        """Read one freshly verified object's finite tail; never follow output."""
        from api_agent.providers.progress import report_timeout_sync
        from api_agent.testing.diagnostics import CredentialScrubber
        from .prerequisites import run_command, PrerequisiteError
        if self.scope is None or not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{64}', identifier):
            raise ContainerError('Select a managed container before viewing logs.')
        executable = self.executable or shutil.which('docker') or shutil.which('podman')
        if not executable:
            raise ContainerError('Container logs unavailable: docker and podman are absent from PATH.')
        scrubber = scrubber or CredentialScrubber(dict(os.environ))
        with report_timeout_sync('Container logs', 5) as budget:
            def command(arguments, *, combine_output=False):
                return run_command((executable, *arguments), environment=_engine_environment(),
                    directory=Path.cwd(), deadline=time.monotonic() + budget.remaining,
                    cancelled=cancelled, combine_output=combine_output, output_scrubber=scrubber,
                    output_limit=65536 if combine_output else None)
            try:
                records = self._inspect((identifier,), reader=command)
                protected = session_container_identity()
                if (len(records) != 1 or records[0]['Id'] != identifier
                        or not self._eligible(records[0], protected)):
                    raise ContainerError('Selected container is no longer owned by this stack scope.')
                text = command(('logs', '--tail', '200', identifier), combine_output=True)
            except PrerequisiteError as error:
                # Keep a shared collector's exact disposal capability, without
                # exposing engine diagnostics or transport addresses in the UI.
                safe = ContainerError('Container logs unavailable or cancelled.')
                if getattr(error, 'cleanup_pending', None) is not None:
                    safe.cleanup_pending = error.cleanup_pending
                raise safe from None
        return text
