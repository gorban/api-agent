"""Explicit workflow-view preparation, generation, and read-only checking."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.request import Request, urlopen

from .viewer_render import (
    PLANTUML_SHA256, PlantUMLRenderer, RenderError, _physical_file, _read_regular_jar,
    _route, build_view_bundle, default_renderer,
)


PLANTUML_URL = "https://github.com/plantuml/plantuml/releases/download/v1.2026.9/plantuml-mit-1.2026.9.jar"
_MAX_JAR = 30_000_000
_MAX_VIEW_FILE = 8 * 1024 * 1024


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


def check_views(root: Path) -> None:
    """Read-only check of the supplied clean snapshot or source tree."""
    root = Path(root).resolve()
    projection = _projection(root)
    build_view_bundle(projection, roots={"base": root, "pack": root},
                      verify=True)


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
                   confirmed_orphans: tuple[Path, ...] = ()) -> tuple[Path, ...]:
    """Explicitly write only owned presentation files, leaving Git index untouched."""
    root = Path(root).resolve()
    projection = _projection(root)
    roots = {"base": root, "pack": root}
    admitted = _admitted_snapshot(projection, roots)
    prior_outputs = _output_snapshot(root)
    output_inodes = {}
    for path in prior_outputs:
        info = path.lstat()
        output_inodes[path] = (info.st_dev, info.st_ino)
    bundle = build_view_bundle(projection, roots={"base": root, "pack": root},
                               renderer=renderer or default_renderer(), verify=False,
                               hosted_selected=False)
    _assert_unchanged(admitted)
    _assert_output_unchanged(prior_outputs, root)
    planned = []
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
    staged = []
    recovery = Path(tempfile.mkdtemp(prefix=".workflow-view-recovery-", dir=root.parent))
    applied = []
    keep_recovery = False
    try:
        for path, prior, body in planned:
            if prior == body:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".workflow-view-", delete=False) as scratch:
                temporary = Path(scratch.name)
                scratch.write(body)
                scratch.flush()
                os.fsync(scratch.fileno())
            staged.append(temporary)
        _assert_unchanged(admitted)
        _assert_output_unchanged(prior_outputs, root, exclude=staged)
        for path, prior, body in planned:
            if prior == body:
                continue
            _assert_unchanged(admitted)
            _assert_output_unchanged(prior_outputs, root, exclude=staged)
            if (_read_regular_view(path) if path.exists() else None) != prior:
                raise RenderError("workflow view changed during generation")
            backup = recovery / str(len(applied))
            if prior is not None:
                backup.write_bytes(prior)
            temporary = staged.pop(0)
            os.replace(temporary, path)
            applied.append((path, prior, body, backup))
            if path in admitted:
                admitted[path] = body
            prior_outputs[path] = body
        for orphan in orphans:
            _assert_unchanged(admitted)
            _assert_output_unchanged(prior_outputs, root, exclude=staged)
            if not _owned_orphan(orphan):
                raise RenderError("orphaned diagram changed during generation")
            backup = recovery / str(len(applied))
            os.replace(orphan, backup)
            expected_bytes = prior_outputs[orphan]
            if not _moved_output_matches(backup, expected_bytes, output_inodes[orphan]):
                try:
                    os.link(backup, orphan, follow_symlinks=False)
                    backup.unlink()
                except OSError as exc:
                    keep_recovery = True
                    raise RenderError(f"orphan changed during move; recover from {recovery}") from exc
                raise RenderError("orphaned diagram changed during generation")
            applied.append((orphan, expected_bytes, None, backup))
            prior_outputs.pop(orphan, None)
        return tuple(path for path, prior, body, _ in applied)
    except BaseException:
        conflicts = []
        for path, prior, body, backup in reversed(applied):
            try:
                current = _read_regular_view(path) if path.exists() else None
            except RenderError:
                current = None
            if path.is_symlink():
                conflicts.append(path)
                continue
            if current != body:
                conflicts.append(path)
                continue
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(prior)
        if conflicts:
            keep_recovery = True
            raise RenderError(f"workflow view rollback conflict; recover original bytes from {recovery}")
        raise
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)
        if not keep_recovery:
            shutil.rmtree(recovery)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "generate", "check"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--fix-orphans", action="store_true")
    parser.add_argument("--confirm-orphan", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            print(prepare_renderer())
        elif args.command == "generate":
            for path in generate_views(args.root, fix_orphans=args.fix_orphans,
                                       confirmed_orphans=tuple(args.confirm_orphan)):
                print(path)
        else:
            check_views(args.root)
    except (RenderError, OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
