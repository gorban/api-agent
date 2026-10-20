"""Recorded, owner-relative repository identity for portable workflow links."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import stat
import re
import tomllib
from urllib.parse import quote

CONFIG_PATH = "cfg/workflow-view.toml"
BRANCH_PLACEHOLDER = "_____switch_branch_____"


@dataclass(frozen=True)
class RepositoryIdentity:
    repository_url: str
    path_prefix: str = ""

    def __post_init__(self):
        if not re.fullmatch(r"//github\.com/[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})/[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})", self.repository_url):
            raise ValueError("invalid workflow repository URL")
        if self.repository_url.endswith(".git"):
            raise ValueError("workflow repository URL must be canonical")
        if self.path_prefix and (len(self.path_prefix) > 512 or any(
                not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."}
                for part in self.path_prefix.split("/"))):
            raise ValueError("invalid workflow repository path prefix")

    @property
    def link_prefix(self) -> str:
        suffix = self.path_prefix + "/" if self.path_prefix else ""
        return f"{self.repository_url}/blob/{BRANCH_PLACEHOLDER}/{suffix}"

    def link(self, owner_relative_path: str) -> str:
        parts = owner_relative_path.split("/")
        if not parts or any(p in {"", ".", ".."} or any(ord(c) < 32 or c in "\\?#" for c in p) for p in parts):
            raise ValueError("invalid workflow repository target")
        return self.link_prefix + quote(owner_relative_path, safe="/-._~")


def read_repository_identity(root: Path) -> tuple[RepositoryIdentity, bytes] | None:
    """Read recorded metadata only; never discover a remote or mutate files."""
    path = Path(root).resolve() / CONFIG_PATH
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("workflow repository configuration symlink rejected")
    try:
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        finally:
            os.close(directory)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError("workflow repository configuration must be a bounded regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            data = source.read(4097)
            if len(data) != info.st_size:
                raise ValueError("workflow repository configuration changed during read")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    document = tomllib.loads(data.decode("utf-8"))
    if set(document) - {"version", "repository_url", "path_prefix"} or type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("invalid workflow repository configuration")
    if not isinstance(document.get("repository_url"), str) or not isinstance(document.get("path_prefix", ""), str):
        raise ValueError("invalid workflow repository configuration")
    return RepositoryIdentity(document["repository_url"], document.get("path_prefix", "")), data


def identity_from_origin(origin: str) -> RepositoryIdentity:
    """Normalize a credential-free GitHub remote during explicit initialization."""
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", origin)
    if match is None:
        raise ValueError("origin is not a safe GitHub repository URL")
    return RepositoryIdentity("//github.com/" + match[1])


def repository_config_bytes(identity: RepositoryIdentity) -> bytes:
    suffix = f'path_prefix = "{identity.path_prefix}"\n' if identity.path_prefix else ""
    return f'version = 1\nrepository_url = "{identity.repository_url}"\n{suffix}'.encode()
