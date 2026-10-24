"""Explicit locked preparation of the published MCP child asset."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil

from .owner import GraphQLMcpError

_LOCK_FILES = ("package.json", "package-lock.json")
_DIST = Path("node_modules/mcp-graphql/dist/index.js")
_VERSION = "2.0.4"
_PACKAGE_SHA256 = "08062199e8c3aea094d5f1e157344720903ff5da48c46a15c7f976b03e27faa4"
_LOCK_SHA256 = "bfd6c60e99ff90a1bf55120fb6b436cb0321ef2c22f590591801682bad77c8bd"
# Canonical path/content digest of all 2,266 installed files from the locked
# npm ci tree. npm's generated metadata and .bin links are excluded.
_TREE_SHA256 = "9676db5e3c4c4b038acc3ed9242529627ce74cfdb77df87e60743dcb5c97af6c"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == ".package-lock.json" or relative.startswith(".bin/"):
            continue
        if path.is_symlink():
            content = ("SYMLINK:" + str(path.readlink())).encode()
        elif path.is_dir():
            continue
        else:
            content = path.read_bytes()
        digest.update(relative.encode() + b"\0" + len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


async def _drain(stream: asyncio.StreamReader) -> None:
    total = 0
    while chunk := await stream.read(4096):
        total += len(chunk)
        if total > 65536:
            raise GraphQLMcpError("asset_output")


def installed_script(directory: Path) -> Path:
    """Return a prepared shipped JavaScript path, or fail closed."""
    directory = Path(directory)
    package = directory / "node_modules/mcp-graphql/package.json"
    script = directory / _DIST
    try:
        metadata = json.loads(package.read_text())
    except (OSError, ValueError) as exc:
        raise GraphQLMcpError("assets_missing") from exc
    if metadata.get("name") != "mcp-graphql" or metadata.get("version") != _VERSION or not script.is_file():
        raise GraphQLMcpError("assets_invalid")
    resolved = script.resolve()
    if not resolved.is_relative_to(directory.resolve()):
        raise GraphQLMcpError("assets_integrity")
    try:
        if (_sha256(directory / "package.json") != _PACKAGE_SHA256 or
            _sha256(directory / "package-lock.json") != _LOCK_SHA256 or
            _tree_sha256(directory / "node_modules") != _TREE_SHA256):
            raise GraphQLMcpError("assets_integrity")
    except OSError as exc:
        raise GraphQLMcpError("assets_integrity") from exc
    return resolved


async def prepare_assets(directory: Path, *, npm: str | None = None, timeout: float = 120) -> Path:
    """Run controlled npm ci at an explicit setup boundary, never at invocation."""
    if not 0 < timeout <= 300:
        raise GraphQLMcpError("asset_timeout_invalid")
    npm_path = shutil.which(npm or "npm")
    if npm_path is None:
        raise GraphQLMcpError("npm_missing")
    source = Path(__file__).resolve().parent
    if _sha256(source / "package.json") != _PACKAGE_SHA256 or _sha256(source / "package-lock.json") != _LOCK_SHA256:
        raise GraphQLMcpError("lock_integrity")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for filename in _LOCK_FILES:
        shutil.copy2(source / filename, directory / filename)
    try:
        process = await asyncio.create_subprocess_exec(
            str(Path(npm_path).resolve()), "ci", "--ignore-scripts", "--no-audit", "--no-fund",
            cwd=directory, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=65536)
        try:
            async with asyncio.timeout(timeout):
                async with asyncio.TaskGroup() as group:
                    group.create_task(_drain(process.stdout))
                    group.create_task(_drain(process.stderr))
                    group.create_task(process.wait())
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise GraphQLMcpError("asset_timeout") from exc
        except BaseException as exc:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if isinstance(exc, ExceptionGroup):
                if any(isinstance(item, GraphQLMcpError) and item.code == "asset_output"
                       for item in exc.exceptions):
                    raise GraphQLMcpError("asset_output") from exc
                raise GraphQLMcpError("asset_install") from exc
            raise
        if process.returncode:
            raise GraphQLMcpError("asset_install")
    except OSError as exc:
        raise GraphQLMcpError("asset_install") from exc
    return installed_script(directory)
