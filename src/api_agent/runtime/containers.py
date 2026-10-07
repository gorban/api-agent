"""Bounded, Docker-compatible container inventory and explicit stop adapter."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess


class ContainerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Container:
    id: str
    name: str
    image: str
    status: str


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


class ContainerRuntime:
    def __init__(self, executable=None, scope=None):
        self.executable = executable
        self.scope = scope

    def _run(self, arguments):
        executable = self.executable or shutil.which("docker") or shutil.which("podman")
        if not executable:
            raise ContainerError("Container inventory unavailable: docker and podman are absent from PATH.")
        try:
            result = subprocess.run([executable, *arguments], capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ContainerError(f"Container engine unavailable: {type(error).__name__}.") from error
        if result.returncode:
            # Do not expose engine stderr, which can include credentials in remote URLs.
            raise ContainerError(f"Container engine command failed (exit {result.returncode}).")
        return result.stdout

    def _inspect(self, identifiers):
        try:
            records = json.loads(self._run(["inspect", *identifiers]))
            if not isinstance(records, list):
                raise ValueError("Expected inspect array")
            for record in records:
                if (not isinstance(record, dict)
                        or not isinstance(record.get("Id"), str)
                        or not record["Id"]
                        or not isinstance(record.get("Name"), str)
                        or not isinstance(record.get("Config"), dict)
                        or not isinstance(record.get("State"), dict)):
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
            "ps", "--quiet", "--no-trunc",
            "--filter", "label=api-agent.managed=true",
            "--filter", f"label=api-agent.scope={self.scope.id}",
            "--filter", f"label=com.docker.compose.project={self.scope.project}",
        ]).split()
        if not identifiers:
            return ()
        protected = session_container_identity()
        return tuple(Container(record["Id"], record["Name"].lstrip("/"),
                               str(record["Config"].get("Image", "")),
                               str(record["State"].get("Status", "")))
                     for record in self._inspect(identifiers)
                     if self._eligible(record, protected) and record["State"].get("Running") is True)

    def stop(self, identifiers):
        if self.scope is None:
            raise ContainerError("Container ownership scope is required before stopping.")
        identifiers = tuple(dict.fromkeys(identifiers))
        if not identifiers or any(not isinstance(identifier, str) or not identifier or identifier.startswith("-") for identifier in identifiers):
            raise ContainerError("Select valid container IDs before stopping.")
        protected = session_container_identity()
        if any(is_session_container(identifier, identifier, protected) for identifier in identifiers):
            raise ContainerError("Cannot stop the container hosting this session.")
        records = self._inspect(identifiers)
        if (len(records) != len(identifiers)
                or {record["Id"] for record in records} != set(identifiers)
                or not all(self._eligible(record, protected) for record in records)):
            raise ContainerError("Selected containers are no longer owned by this stack scope.")
        self._run(["stop", *identifiers])
