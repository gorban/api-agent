"""Read-only Git provenance comparison performed before application startup."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence, TextIO


class WorktreeGateError(RuntimeError):
    """The selected checkout could not be verified or was not acknowledged."""


@dataclass(frozen=True)
class CheckoutReport:
    root: Path
    branch: str
    sha: str
    commit_timestamp: float | None
    dirty_timestamp: float | None
    dirty_path: str | None
    missing_paths: tuple[str, ...]
    selected: bool
    newest: bool = False
    bare: bool = False
    locked: str | None = None
    prunable: str | None = None

    @property
    def timestamp(self) -> float | None:
        values = [v for v in (self.commit_timestamp, self.dirty_timestamp) if v is not None]
        return max(values) if values else None

    @property
    def reason(self) -> str:
        if self.bare:
            return "administrative bare entry; excluded"
        if self.dirty_timestamp is not None and self.dirty_timestamp > (self.commit_timestamp or 0):
            return f"eligible dirty file {self.dirty_path!r}"
        return "HEAD committer timestamp"


@dataclass(frozen=True)
class RepositoryReport:
    selected_root: Path
    checkouts: tuple[CheckoutReport, ...]

    @property
    def stale(self) -> bool:
        return any(c.selected and not c.newest for c in self.checkouts)


def _git(root: Path, *args: str, allowed: tuple[int, ...] = (0,), input: bytes | None = None) -> bytes:
    # Launcher identity comes from explicit paths, not a caller's shell Git context.
    overrides = {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
        "GIT_PREFIX", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
    environment = {key: value for key, value in os.environ.items() if key not in overrides}
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(root), *args], capture_output=True, check=False,
            input=input, env=environment
        )
    except OSError as exc:
        raise WorktreeGateError(f"Cannot run Git for {root}: {exc}") from exc
    if result.returncode not in allowed:
        error = os.fsdecode(result.stderr).strip()
        raise WorktreeGateError(f"Cannot verify {root}: git {' '.join(args)} failed: {error}")
    return result.stdout


def _records(data: bytes) -> list[dict[bytes, bytes]]:
    records: list[dict[bytes, bytes]] = []
    current: dict[bytes, bytes] = {}
    for field in data.split(b"\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = field.partition(b" ")
        if key == b"worktree" and current:
            records.append(current)
            current = {}
        current[key] = value
    if current:
        records.append(current)
    return records


def _dirty(root: Path) -> tuple[float | None, str | None, tuple[str, ...]]:
    # --no-renames exposes both rename endpoints: missing sources have no mtime.
    candidates = _git(root, "diff", "--name-only", "--no-renames", "-z", "--").split(b"\0")
    candidates += _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--").split(b"\0")
    newest: float | None = None
    newest_path: str | None = None
    missing: list[str] = []
    for raw in sorted(set(candidates) - {b""}):
        name = os.fsdecode(raw)
        # --no-index is required to apply ignore rules even to tracked candidates.
        ignored = _git(root, "check-ignore", "--no-index", "-z", "--stdin", allowed=(0, 1), input=raw + b"\0")
        if ignored:
            continue
        try:
            timestamp = (root / name).lstat().st_mtime
        except FileNotFoundError:
            missing.append(name)
            continue
        except OSError as exc:
            raise WorktreeGateError(f"Cannot inspect dirty path {root / name}: {exc}") from exc
        if newest is None or timestamp > newest:
            newest, newest_path = timestamp, name
    return newest, newest_path, tuple(missing)


def inspect_worktrees(roots: Sequence[Path]) -> tuple[RepositoryReport, ...]:
    """Inspect only registered worktrees, deduplicating selected Git roots."""
    if shutil.which("git") is None:
        raise WorktreeGateError("Git CLI is required for the worktree gate; install Git and retry.")
    selected_roots: list[Path] = []
    for root in roots:
        root = Path(root).resolve()
        top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).removesuffix("\n")).resolve()
        if not root.is_relative_to(top):
            raise WorktreeGateError(
                f"Selected launcher path {root} does not belong to discovered Git root {top}."
            )
        if top not in selected_roots:
            selected_roots.append(top)
    if not selected_roots:
        raise WorktreeGateError("No selected repository roots were supplied to the worktree gate.")
    reports: list[RepositoryReport] = []
    for selected in selected_roots:
        common_dir = Path(os.fsdecode(_git(
            selected, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )).removesuffix("\n")).resolve()
        entries = _records(_git(selected, "worktree", "list", "--porcelain", "-z"))
        checkouts: list[CheckoutReport] = []
        for entry in entries:
            if b"worktree" not in entry:
                raise WorktreeGateError(f"Git returned an invalid worktree entry for {selected}.")
            root = Path(os.fsdecode(entry[b"worktree"])).resolve()
            locked = os.fsdecode(entry[b"locked"]) if b"locked" in entry else None
            prunable = os.fsdecode(entry[b"prunable"]) if b"prunable" in entry else None
            if b"bare" in entry:
                checkouts.append(CheckoutReport(root, "bare", "", None, None, None, (), False,
                                                bare=True, locked=locked, prunable=prunable))
                continue
            try:
                actual_top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).removesuffix("\n")).resolve()
                if actual_top != root:
                    raise WorktreeGateError(f"Registered checkout {root} resolves to another Git root {actual_top}.")
                actual_common_dir = Path(os.fsdecode(_git(
                    root, "rev-parse", "--path-format=absolute", "--git-common-dir"
                )).removesuffix("\n")).resolve()
                if actual_common_dir != common_dir:
                    raise WorktreeGateError(
                        f"Registered checkout {root} belongs to an unrelated repository "
                        f"(Git common directory {actual_common_dir}; expected {common_dir})."
                    )
                sha = _git(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
                commit = float(_git(root, "show", "-s", "--format=%ct", "HEAD").strip())
                dirty, path, missing = _dirty(root)
            except (WorktreeGateError, ValueError, UnicodeError) as exc:
                state = f"; locked={locked!r}; prunable={prunable!r}"
                raise WorktreeGateError(f"Registered runnable checkout {root} cannot be verified{state}: {exc}") from exc
            branch = os.fsdecode(entry.get(b"branch", b"detached HEAD"))
            checkouts.append(CheckoutReport(root, branch, sha, commit, dirty, path, missing,
                                            root == selected, locked=locked, prunable=prunable))
        if not any(c.selected for c in checkouts):
            raise WorktreeGateError(f"Selected checkout {selected} is absent from Git's registered runnable worktrees.")
        latest = max(c.timestamp for c in checkouts if not c.bare)
        checkouts = [replace(c, newest=not c.bare and c.timestamp == latest) for c in checkouts]
        reports.append(RepositoryReport(selected, tuple(checkouts)))
    return tuple(reports)


def _date(timestamp: float | None) -> str:
    if timestamp is None:
        return "no runnable timestamp"
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() + " (UTC)"


def format_report(reports: Sequence[RepositoryReport]) -> str:
    lines = ["Worktree provenance (observable commit/file timestamps):"]
    for report in reports:
        lines.append(f"Repository selected: {report.selected_root}")
        for checkout in report.checkouts:
            markers = ", ".join(x for x, enabled in (("SELECTED", checkout.selected), ("NEWEST", checkout.newest)) if enabled)
            lines.append(f"  {str(checkout.root)!r} [{markers}] {checkout.branch} HEAD={checkout.sha or 'n/a'}")
            lines.append(f"    rank={_date(checkout.timestamp)}; reason={checkout.reason}")
            if not checkout.bare:
                lines.append(f"    commit={_date(checkout.commit_timestamp)}; eligible dirty={_date(checkout.dirty_timestamp)}")
            if checkout.locked is not None:
                lines.append(f"    locked: {checkout.locked or 'yes'}")
            if checkout.prunable is not None:
                lines.append(f"    prunable: {checkout.prunable or 'yes'}")
            if checkout.missing_paths:
                lines.append(f"    absent dirty paths {checkout.missing_paths!r}: no recoverable deletion timestamp")
    lines.append("Ordering cannot recover modification dates for deleted files or rename sources.")
    return "\n".join(lines)


def verify_worktrees(
    roots: Sequence[Path], *, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout
) -> None:
    """Require an explicit `y` only when a selected checkout is older."""
    reports = inspect_worktrees(roots)
    if not any(r.stale for r in reports):
        return
    stdout.write(format_report(reports) + "\nSelected checkout is older. Continue using it? Type y: ")
    stdout.flush()
    try:
        answer = stdin.readline() if stdin is not None else ""
    except (OSError, ValueError, AttributeError) as exc:
        raise WorktreeGateError("Stale checkout requires explicit y on stdin; stdin is unavailable.") from exc
    if answer.strip().lower() != "y":
        raise WorktreeGateError("Stale checkout was not acknowledged with explicit y; startup stopped.")
