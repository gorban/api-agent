"""Explicit workflow-view preparation, generation, and read-only checking."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import secrets
import sys
from hashlib import sha256
import json
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import subprocess
from urllib.request import Request, urlopen

from .viewer_repository import CONFIG_PATH, read_repository_identity, RepositoryIdentity, identity_from_origin, repository_config_bytes

from .viewer_render import (
    PLANTUML_SHA256, PlantUMLRenderer, RenderError, _physical_file, _read_regular_jar,
    _route, build_view_bundle, default_renderer, ViewSource, capture_repository_sources,
)


PLANTUML_URL = "https://github.com/plantuml/plantuml/releases/download/v1.2026.9/plantuml-mit-1.2026.9.jar"
_MAX_JAR = 30_000_000
_MAX_VIEW_FILE = 8 * 1024 * 1024
_RECOVERY_SLOTS: dict[int, dict[str, tuple[int, int]]] = {}


def _download_renderer(temporary: Path, timeout: float, url: str, max_bytes: int, channel) -> None:
    """Perform the blocking transfer in a process the caller can terminate."""
    try:
        request = Request(url, headers={"User-Agent": "api-agent-workflow-viewer/1"})
        size = 0
        with urlopen(request, timeout=timeout) as response, temporary.open("wb") as output:
            while chunk := response.read(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise RenderError("PlantUML artifact exceeds size limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        channel.send(None)
    except BaseException as exc:
        channel.send(str(exc)[:256])
    finally:
        channel.close()


def prepare_renderer(*, cache: Path | None = None) -> Path:
    """Install only the pinned optional development JAR in an owned cache."""
    from api_agent.providers.progress import report_timeout_sync

    with report_timeout_sync("Workflow renderer preparation", 60) as budget:
        return _prepare_renderer(cache=cache, budget=budget)


def _prepare_renderer(*, cache: Path | None, budget: object) -> Path:
    location = cache or (Path.home() / ".cache/api-agent/plantuml")
    location.mkdir(parents=True, exist_ok=True)
    target = location / "plantuml-mit-1.2026.9.jar"
    if location.is_symlink() or target.is_symlink():
        raise RenderError("renderer cache symlink rejected")
    if target.exists():
        if sha256(_read_regular_jar(target)).hexdigest() != PLANTUML_SHA256:
            raise RenderError("cached PlantUML digest mismatch")
        return target
    with tempfile.NamedTemporaryFile(dir=location, prefix=".plantuml-", delete=False) as scratch:
        temporary = Path(scratch.name)
    try:
        # Spawn is safe when the caller already has browser or UI threads.
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=_download_renderer,
                                  args=(temporary, min(20, budget.remaining or 0.001), PLANTUML_URL,
                                        _MAX_JAR, child))
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        try:
            while True:
                remaining = budget.remaining
                if remaining is not None and remaining <= 0:
                    raise RenderError("renderer preparation timed out")
                if parent.poll(min(0.05, remaining if remaining is not None else 0.05)):
                    break
            try:
                error = parent.recv()
            except EOFError as exc:
                raise RenderError("renderer preparation worker exited without a result") from exc
            process.join(timeout=max(0, budget.remaining or 0))
            if process.is_alive():
                raise RenderError("renderer preparation timed out")
            if process.exitcode != 0 or error is not None:
                raise RenderError(error or "renderer preparation failed")
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=0.05)
                if process.is_alive():
                    process.kill()
                    process.join()
            parent.close()
        if sha256(temporary.read_bytes()).hexdigest() != PLANTUML_SHA256:
            raise RenderError("PlantUML artifact digest mismatch")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _projection(root: Path):
    from .definition import load_definition
    from .stateful import load_stateful_definition
    from .document_format import canonical_executable_bytes
    from .viewer_projection import project_view

    root = root.resolve()
    entry = root / "flow.md.html"
    data = canonical_executable_bytes(entry.read_bytes(), path=entry)
    loader = load_stateful_definition if data.startswith(b"# Workflow v3\n") else load_definition
    return project_view(loader(entry, base_root=root, pack_root=root, entry_root="base"))


def _repository_inputs(root: Path):
    identities, sources = capture_repository_sources({"base": root, "pack": root})
    return identities or None, sources


def check_views(root: Path) -> None:
    """Read-only check of the supplied clean snapshot or source tree."""
    root = Path(root).resolve()
    projection = _projection(root)
    repositories, repository_sources = _repository_inputs(root)
    build_view_bundle(projection, roots={"base": root, "pack": root},
                      verify=True, repositories=repositories, repository_sources=repository_sources)


def _admitted_snapshot(projection, roots):
    captured = {}
    for document in projection.documents:
        captured[_physical_file(_route(document.logical_path), roots)] = document.raw_bytes
    for resource in projection.resources:
        if resource.logical_path.startswith(("base/", "pack/")):
            path = _physical_file(_route(resource.logical_path), roots)
            if path not in captured:
                captured[path] = resource.content_bytes
    return captured


def _read_regular_view(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RenderError("workflow view file is not an accessible regular file") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_VIEW_FILE:
            raise RenderError("workflow view file must be a bounded regular file")
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as source:
        data = source.read(_MAX_VIEW_FILE + 1)
        if len(data) != info.st_size:
            raise RenderError("workflow view file changed during read")
        return data


def _assert_unchanged(snapshot):
    for path, expected in snapshot.items():
        try:
            current = _read_regular_view(path)
        except RenderError:
            current = None
        if current != expected:
            raise RenderError("admitted workflow source changed during view generation")


def _owned_orphan(path: Path) -> bool:
    data = _read_regular_view(path)
    if path.suffix == ".md":
        match = re.fullmatch(rb'<!-- api-agent-companion-v1 sha256:([0-9a-f]{64}) -->\n([\s\S]+)', data)
        return match is not None and sha256(match[2]).hexdigest().encode() == match[1]
    if path.suffix == ".svg":
        match = re.search(rb'<!-- api-agent-view-v1 (\{[^\n]+\}) -->\n', data)
        if match is None or data.count(b'<!-- api-agent-view-v1 ') != 1:
            return False
        try:
            metadata = json.loads(match[1])
        except ValueError:
            return False
        graphic = data[:match.start()] + data[match.end():]
        return (metadata.get("version") == 1 and
                metadata.get("graphic_sha256") == sha256(graphic).hexdigest())
    return False


def _moved_output_matches(path: Path, expected: bytes, identity: tuple[int, int]) -> bool:
    """Verify the moved inode and bytes without following a raced-in symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        matches = (stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity and
                   info.st_size == len(expected))
    except BaseException:
        os.close(descriptor)
        raise
    if not matches:
        os.close(descriptor)
        return False
    with os.fdopen(descriptor, "rb") as source:
        return source.read(len(expected) + 1) == expected


def _output_snapshot(root: Path, *, exclude=()):
    excluded = set(exclude)
    captured = {}
    config = root / CONFIG_PATH
    if config.exists() or config.is_symlink():
        if config.is_symlink() or config.parent.is_symlink():
            raise RenderError("workflow repository configuration symlink rejected")
        captured[config] = _read_regular_view(config)
    for directory in (root / ".private_resources/diagrams", root / ".private_resources/viewer"):
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path in excluded:
                continue
            if path.is_symlink():
                raise RenderError("workflow view output symlink rejected")
            if not path.is_dir():
                captured[path] = _read_regular_view(path)
    return captured


def _assert_output_unchanged(snapshot, root, *, exclude=()):
    current = _output_snapshot(root, exclude=exclude)
    if current != snapshot:
        raise RenderError("workflow view output changed during generation")


def generate_views(root: Path, *, renderer: PlantUMLRenderer | None = None,
                   fix_orphans: bool = False,
                   confirmed_orphans: tuple[Path, ...] = (),
                   repository_url: str | None = None) -> tuple[Path, ...]:
    """Explicitly write only owned presentation files, leaving Git index untouched."""
    root = Path(root).resolve()
    projection = _projection(root)
    roots = {"base": root, "pack": root}
    admitted = _admitted_snapshot(projection, roots)
    repositories, repository_sources = _repository_inputs(root)
    config_body = None
    if not repository_sources:
        if repository_url is None:
            result = subprocess.run(["git", "-C", str(root), "config", "--get", "remote.origin.url"], capture_output=True, text=True, timeout=5, check=False)
            if result.returncode == 0:
                identity = identity_from_origin(result.stdout.strip())
            else:
                raise RenderError("workflow repository identity missing; supply --repository-url")
        else:
            identity = RepositoryIdentity(repository_url)
        config_body = repository_config_bytes(identity)
        repositories = {"base": identity, "pack": identity}
        repository_sources = (ViewSource("base/" + CONFIG_PATH, "repository", sha256(config_body).hexdigest(), len(config_body), False),)
    else:
        config = _read_regular_view(root / CONFIG_PATH)
        if sha256(config).hexdigest() != repository_sources[0].sha256:
            raise RenderError("workflow repository configuration changed during admission")
        admitted[root / CONFIG_PATH] = config
        if repository_url is not None and RepositoryIdentity(repository_url) != repositories["base"]:
            raise RenderError("workflow repository identity differs from recorded configuration")
    prior_outputs = _output_snapshot(root)
    output_inodes = {}
    for path in prior_outputs:
        info = path.lstat()
        output_inodes[path] = (info.st_dev, info.st_ino)
    bundle = build_view_bundle(projection, roots={"base": root, "pack": root},
                               renderer=renderer or default_renderer(), verify=False,
                               hosted_selected=False, repositories=repositories, repository_sources=repository_sources)
    _assert_unchanged(admitted)
    _assert_output_unchanged(prior_outputs, root)
    planned = []
    if config_body is not None:
        planned.append((_physical_file("r/base/" + CONFIG_PATH, roots), None, config_body))
    for file in bundle.files:
        path = _physical_file(file.path, roots)
        prior = _read_regular_view(path) if path.exists() else None
        if path in admitted and prior != admitted[path]:
            raise RenderError("admitted workflow source changed during view generation")
        planned.append((path, prior, file.body))
    expected = {path for path, _, _ in planned}
    diagram_dir = root / ".private_resources/diagrams"
    orphans = sorted(path for pattern in ("*.svg", "*.md")
                     for path in diagram_dir.rglob(pattern) if path not in expected)
    confirmed = {Path(path).resolve() for path in confirmed_orphans}
    if orphans and (not fix_orphans or not all(path.resolve() in confirmed and _owned_orphan(path)
                                                 for path in orphans)):
        raise RenderError("orphaned or edited diagram output; inspect and use generate --fix-orphans for owned output")
    stages = []
    recovery = Path(tempfile.mkdtemp(prefix=".workflow-view-recovery-", dir=root.parent))
    recovery_fd = os.open(recovery, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    applied = []
    _RECOVERY_SLOTS[recovery_fd] = {}
    keep_recovery = False
    try:
        for path, prior, body in planned:
            if prior != body:
                stages.append(_stage_output(root, path, prior, body, recovery_fd))
        for orphan in orphans:
            stages.append(_stage_output(root, orphan, prior_outputs[orphan], None, recovery_fd))
        excluded = [stage.path.parent / stage.temporary for stage in stages if stage.temporary]
        _assert_unchanged(admitted)
        _assert_output_unchanged(prior_outputs, root, exclude=excluded)
        for stage in stages:
            _assert_unchanged(admitted)
            _assert_output_unchanged(prior_outputs, root, exclude=excluded)
            if stage.body is None and not _owned_orphan(stage.path):
                raise RenderError("orphaned diagram changed during generation")
            applied.append(stage)
            _apply_output(root, stage, recovery_fd)
            if stage.path in admitted:
                admitted[stage.path] = stage.body
            if stage.body is None:
                prior_outputs.pop(stage.path, None)
            elif (stage.path == root / CONFIG_PATH or
                  stage.path.is_relative_to(root / ".private_resources/diagrams") or
                  stage.path.is_relative_to(root / ".private_resources/viewer")):
                prior_outputs[stage.path] = stage.body
        for stage in stages:
            if stage.temporary:
                if not _remove_owned_temporary(stage.parent, stage.temporary, stage.temporary_identity, recovery_fd):
                    keep_recovery = True
                    raise RenderError(f"workflow staged output changed; recover from {recovery}")
                stage.temporary = None
        return tuple(stage.path for stage in applied)
    except BaseException:
        conflicts = [stage.path for stage in reversed(applied) if not _rollback_output(stage, recovery_fd)]
        if conflicts:
            keep_recovery = True
            raise RenderError(f"workflow view rollback conflict; recover original bytes from {recovery}")
        raise
    finally:
        active_error = sys.exc_info()[1]
        cleanup_error = None
        try:
            for stage in stages:
                try:
                    if stage.temporary:
                        if not _remove_owned_temporary(stage.parent, stage.temporary, stage.temporary_identity, recovery_fd):
                            keep_recovery = True
                            cleanup_error = RenderError("staged output cleanup could not settle")
                except BaseException as exc:
                    keep_recovery = True
                    cleanup_error = exc
                finally:
                    os.close(stage.parent)
            if not keep_recovery:
                try:
                    if not _cleanup_recovery(recovery, recovery_fd, stages):
                        cleanup_error = RenderError("recovery cleanup could not settle")
                except BaseException as exc:
                    cleanup_error = exc
        finally:
            _RECOVERY_SLOTS.pop(recovery_fd, None)
            os.close(recovery_fd)
        if cleanup_error is not None:
            raise RenderError(f"workflow recovery cleanup incomplete; retained at {recovery}") from (active_error or cleanup_error)


@dataclass
class _OutputStage:
    path: Path
    parent: int
    prior: bytes | None
    body: bytes | None
    identity: tuple[int, int] | None
    temporary: str | None = None
    temporary_identity: tuple[int, int] | None = None
    backup: str | None = None
    published: tuple[int, int] | None = None


def _open_output_parent(root: Path, path: Path) -> int:
    """Pin each directory inode without following a replaced parent link."""
    parts = path.relative_to(root).parts[:-1]
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            try:
                os.mkdir(part, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        result = descriptor
        descriptor = -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _output_at(parent: int, name: str):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_VIEW_FILE:
            raise RenderError("workflow output must be a bounded regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            data = source.read(_MAX_VIEW_FILE + 1)
        if len(data) != info.st_size:
            raise RenderError("workflow output changed during read")
        return data, (info.st_dev, info.st_ino)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _same_output_parent(root: Path, stage: _OutputStage) -> None:
    current = _open_output_parent(root, stage.path)
    try:
        expected, actual = os.fstat(stage.parent), os.fstat(current)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise RenderError("workflow output parent changed during generation")
    finally:
        os.close(current)


def _stage_output(root: Path, path: Path, prior: bytes | None, body: bytes | None, recovery: int) -> _OutputStage:
    parent = _open_output_parent(root, path)
    temporary = None
    created_identity = None
    try:
        current = _output_at(parent, path.name)
        if (current[0] if current else None) != prior:
            raise RenderError("workflow view changed during generation")
        stage = _OutputStage(path, parent, prior, body, current[1] if current else None)
        if body is not None:
            temporary = ".workflow-view-" + secrets.token_hex(16)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            info = os.fstat(descriptor)
            created_identity = (info.st_dev, info.st_ino)
            stage.temporary_identity = created_identity
            with os.fdopen(descriptor, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            stage.temporary = temporary
        return stage
    except BaseException:
        if temporary is not None and created_identity is not None:
            _remove_owned_temporary(parent, temporary, created_identity, recovery)
        os.close(parent)
        raise


def _recovery_slot(recovery: int) -> str:
    prefix = secrets.token_hex(16)
    counter = 0
    while True:
        directory = prefix + "-" + str(counter)
        try:
            os.mkdir(directory, mode=0o700, dir_fd=recovery)
            info = os.stat(directory, dir_fd=recovery, follow_symlinks=False)
            _RECOVERY_SLOTS.setdefault(recovery, {})[directory] = (info.st_dev, info.st_ino)
            return directory + "/file"
        except FileExistsError:
            counter += 1


def _cleanup_recovery(path: Path, descriptor: int, stages: list[_OutputStage]) -> bool:
    """Delete only recorded backup inodes and known empty recovery directories."""
    for stage in stages:
        if stage.backup is not None:
            current = _output_at(descriptor, stage.backup)
            if current is not None:
                if current != (stage.prior, stage.identity):
                    return False
                os.unlink(stage.backup, dir_fd=descriptor)
    slots = _RECOVERY_SLOTS[descriptor]
    # Some mounted filesystems retain an open directory's earlier enumeration.
    # Reopen the pinned inode, rather than its mutable filesystem pathname.
    inventory_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
    try:
        inventory = set(os.listdir(inventory_fd))
    finally:
        os.close(inventory_fd)
    if inventory != set(slots):
        return False
    for name, identity in slots.items():
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != identity:
            return False
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            if os.listdir(child):
                return False
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=descriptor)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        expected = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            return False
        try:
            os.rmdir(path.name, dir_fd=parent)
        except OSError:
            return False
        return True
    finally:
        os.close(parent)


def _remove_owned_temporary(parent: int, name: str, identity: tuple[int, int], recovery: int) -> bool:
    """Move to an exclusive recovery slot before deciding what may be deleted."""
    slot = _recovery_slot(recovery)
    try:
        os.rename(name, slot, src_dir_fd=parent, dst_dir_fd=recovery)
    except FileNotFoundError:
        return True
    info = os.stat(slot, dir_fd=recovery, follow_symlinks=False)
    if (info.st_dev, info.st_ino) == identity:
        os.unlink(slot, dir_fd=recovery)
        return True
    try:
        os.link(slot, name, src_dir_fd=recovery, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(slot, dir_fd=recovery)
    except OSError:
        pass
    return False


def _publish_link(stage: _OutputStage, path: Path) -> None:
    """Publish only into a still-empty name inside the pinned owner directory."""
    os.link(stage.temporary, path.name, src_dir_fd=stage.parent, dst_dir_fd=stage.parent, follow_symlinks=False)


def _restore_link(recovery: int, name: str, stage: _OutputStage) -> None:
    os.link(name, stage.path.name, src_dir_fd=recovery, dst_dir_fd=stage.parent, follow_symlinks=False)
    os.unlink(name, dir_fd=recovery)


def _apply_output(root: Path, stage: _OutputStage, recovery: int) -> None:
    _same_output_parent(root, stage)
    current = _output_at(stage.parent, stage.path.name)
    expected = None if stage.prior is None else (stage.prior, stage.identity)
    if current != expected:
        raise RenderError("workflow view changed during generation")
    if current is not None:
        backup = _recovery_slot(recovery)
        os.rename(stage.path.name, backup, src_dir_fd=stage.parent, dst_dir_fd=recovery)
        stage.backup = backup
        try:
            moved = _output_at(recovery, stage.backup)
        except (OSError, RenderError):
            moved = None
        if moved != expected:
            message = "orphaned diagram changed during generation" if stage.body is None else "workflow view changed during publication"
            raise RenderError(message)
    if stage.body is not None:
        temporary = _output_at(stage.parent, stage.temporary)
        if temporary is None or temporary[0] != stage.body:
            raise RenderError("workflow staged output changed during publication")
        _publish_link(stage, stage.path)
        stage.published = temporary[1]
        if _output_at(stage.parent, stage.path.name) != temporary:
            raise RenderError("workflow view changed during publication")
    _same_output_parent(root, stage)


def _rollback_output(stage: _OutputStage, recovery: int) -> bool:
    """Restore with exclusive links, retaining every raced-in inode for recovery."""
    if stage.published is not None:
        displaced = _recovery_slot(recovery)
        try:
            os.rename(stage.path.name, displaced, src_dir_fd=stage.parent, dst_dir_fd=recovery)
        except FileNotFoundError:
            return False
        try:
            current = _output_at(recovery, displaced)
        except (OSError, RenderError):
            current = None
        if current != (stage.body, stage.published):
            try:
                _restore_link(recovery, displaced, stage)
            except OSError:
                pass
            return False
        os.unlink(displaced, dir_fd=recovery)
    if stage.backup is not None:
        try:
            _restore_link(recovery, stage.backup, stage)
        except OSError:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "generate", "check"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--fix-orphans", action="store_true")
    parser.add_argument("--repository-url")
    parser.add_argument("--confirm-orphan", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            print(prepare_renderer())
        elif args.command == "generate":
            for path in generate_views(args.root, fix_orphans=args.fix_orphans,
                                       confirmed_orphans=tuple(args.confirm_orphan), repository_url=args.repository_url):
                print(path)
        else:
            check_views(args.root)
    except (RenderError, OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
