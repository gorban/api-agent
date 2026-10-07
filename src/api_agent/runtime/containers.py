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
    def __init__(self, executable=None):
        self.executable = executable

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

    def inventory(self):
        output = self._run(["ps", "--format", "{{json .}}"])
        containers = []
        protected = session_container_identity()
        try:
            for line in output.splitlines():
                record = json.loads(line)
                identifier = record.get("ID") or record.get("Id")
                names = record.get("Names")
                if isinstance(names, list):
                    names = ", ".join(names)
                if not identifier or not names:
                    raise ValueError("Missing ID or name")
                if is_session_container(str(identifier), str(names), protected):
                    continue
                containers.append(Container(str(identifier), str(names), str(record.get("Image", "")), str(record.get("Status", ""))))
        except (ValueError, TypeError, AttributeError) as error:
            raise ContainerError("Container inventory unavailable: invalid engine response.") from error
        return tuple(containers)

    def stop(self, identifiers):
        identifiers = tuple(dict.fromkeys(identifiers))
        if not identifiers or any(not identifier or identifier.startswith("-") for identifier in identifiers):
            raise ContainerError("Select valid container IDs before stopping.")
        protected = session_container_identity()
        if any(is_session_container(identifier, identifier, protected) for identifier in identifiers):
            raise ContainerError("Cannot stop the container hosting this session.")
        self._run(["stop", *identifiers])
