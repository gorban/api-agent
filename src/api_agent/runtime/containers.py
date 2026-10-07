"""Bounded, Docker-compatible container inventory and explicit stop adapter."""
from __future__ import annotations

from dataclasses import dataclass
import json
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
        try:
            for line in output.splitlines():
                record = json.loads(line)
                identifier = record.get("ID") or record.get("Id")
                names = record.get("Names")
                if isinstance(names, list):
                    names = ", ".join(names)
                if not identifier or not names:
                    raise ValueError("Missing ID or name")
                containers.append(Container(str(identifier), str(names), str(record.get("Image", "")), str(record.get("Status", ""))))
        except (ValueError, TypeError, AttributeError) as error:
            raise ContainerError("Container inventory unavailable: invalid engine response.") from error
        return tuple(containers)

    def stop(self, identifiers):
        identifiers = tuple(dict.fromkeys(identifiers))
        if not identifiers or any(not identifier or identifier.startswith("-") for identifier in identifiers):
            raise ContainerError("Select valid container IDs before stopping.")
        self._run(["stop", *identifiers])
