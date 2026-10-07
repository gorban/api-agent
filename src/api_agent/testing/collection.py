"""Collect pytest identities without running tests or starting prerequisites."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import time
import sys
import tempfile
from .processes import finalize


class CollectionError(ValueError):
    """Discovery or a requested selector cannot produce a valid selection."""


@dataclass(frozen=True)
class TestIdentity:
    __test__ = False

    id: str
    repo: str
    file: str
    node_id: str
    tags: frozenset[str] = frozenset()

    def __post_init__(self):
        object.__setattr__(self, "tags", frozenset(self.tags))


_PLUGIN = '''
import json, sys
try:
    import pytest
except ImportError:
    print("Collection requires pytest; install the api-agent dev dependencies in this Python environment.", file=sys.stderr)
    sys.exit(2)
class Collector:
    def pytest_collection_finish(self, session):
        rows = [{"node_id": item.nodeid, "tags": sorted({mark.name for mark in item.iter_markers()})} for item in session.items]
        with open(sys.argv[1], "w", encoding="utf-8") as stream:
            json.dump(rows, stream)
sys.exit(pytest.main(sys.argv[2:], plugins=[Collector()]))
'''


def repository_label(context, root):
    """Use the same repository identity for collection and recording."""
    base, owner, root = (Path(path).resolve() for path in
                         (context.base_root, context.owner_root, root))
    if base != owner and base.name == owner.name:
        return 'base' if root == base else 'pack'
    return root.name


def select_tests(tests, selectors=()):
    """Resolve a de-duplicated union of repository-qualified or bare selectors."""
    tests = list(tests)
    if not selectors:
        return tests
    selected = set()
    repos = {test.repo for test in tests}
    for selector in selectors:
        if not isinstance(selector, str) or not selector.strip():
            raise CollectionError("Test selectors must be nonempty strings")
        repo = None
        value = selector
        prefix, separator, remainder = selector.partition("::")
        if separator and prefix in repos:
            repo, value = prefix, remainder
        matches = [test for test in tests if (repo is None or test.repo == repo) and (
            value[4:] in test.tags if value.startswith("tag:") else
            value in {test.file, test.node_id, test.id}
        )]
        if not matches:
            raise CollectionError(f"Unknown or empty test selector: {selector}")
        if repo is None and not value.startswith("tag:") and len({test.repo for test in matches}) > 1:
            raise CollectionError(f"Ambiguous test selector {selector!r}; qualify it with a repository name followed by ::")
        selected.update(test.id for test in matches)
    return [test for test in tests if test.id in selected]


def _stop_collection(process):
    """Terminate only this collector's process group and reap its leader."""
    return finalize(process, communicate=True)


def _run_collection(args, root, env, repo, cancel_event, timeout=120):
    if cancel_event is not None and cancel_event.is_set():
        raise CollectionError(f"Pytest collection cancelled for {repo}")
    try:
        process = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   start_new_session=os.name == "posix")
    except OSError as exc:
        raise CollectionError(f"Cannot start pytest collection for {repo}: {exc}") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CollectionError(f"Pytest collection cancelled for {repo}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollectionError(f"Pytest collection for {repo} exceeded {timeout} seconds; inspect collection hooks/imports")
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                _stop_collection(process)
                return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if process.poll() is not None:
                    stdout, stderr = _stop_collection(process)
                    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
                continue
    except BaseException:
        _stop_collection(process)
        raise


def collect_tests(context, mode, selectors=(), cancel_event=None):
    """Collect each owner's suites using its working directory and pytest config."""
    if mode not in {"unit", "integration", "e2e"}:
        raise CollectionError(f"Collection requires unit, integration, or e2e mode; got {mode!r}")
    base = Path(context.base_root).resolve()
    owner = Path(context.owner_root).resolve()
    suites = {}
    for raw_path in context.suite_paths(mode):
        path = Path(raw_path).resolve()
        if not path.exists():
            if mode == "unit" and path != base / "tests/unit":
                continue
            raise CollectionError(f"Declared {mode} suite does not exist: {path}")
        if mode == 'unit' and path == base / 'tests/unit':
            root = base
        else:
            root = owner if path.is_relative_to(owner) else base
        if not path.is_relative_to(root):
            raise CollectionError(f"Suite is outside its repository owner: {path}")
        suites.setdefault(root, []).append(path)
    if not suites:
        raise CollectionError(f"No {mode} suites are declared")
    child_env = os.environ.copy()
    source_paths = [str(base / "src")]
    if owner != base:
        source_paths.append(str(owner / "src"))
    if child_env.get("PYTHONPATH"):
        source_paths.append(child_env["PYTHONPATH"])
    child_env["PYTHONPATH"] = os.pathsep.join(source_paths)
    collected = {}
    for root, paths in suites.items():
        repo = repository_label(context, root)
        with tempfile.TemporaryDirectory(prefix="api-agent-collect-") as temporary:
            output = Path(temporary) / "collection.json"
            args = [sys.executable, "-c", _PLUGIN, str(output), "--collect-only", "-q", "-o", "addopts=", "--rootdir", str(root)]
            args.extend(str(path) for path in dict.fromkeys(paths))
            result = _run_collection(args, root, child_env, repo, cancel_event)
            if result.returncode == 5 and mode == "unit" and root != base:
                continue
            if result.returncode:
                detail = (result.stderr + result.stdout).strip()
                raise CollectionError(f"Pytest collection failed for {repo} (exit {result.returncode}):\n{detail}")
            try:
                rows = json.loads(output.read_text(encoding="utf-8"))
                if not rows and root == base and mode == "unit":
                    raise CollectionError(f"No tests collected in base unit suite: {base / 'tests/unit'}")
                for row in rows:
                    node = row["node_id"]
                    identity = TestIdentity(f"{repo}::{node}", repo, node.split("::", 1)[0], node, frozenset(row["tags"]))
                    collected.setdefault(identity.id, identity)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise CollectionError(f"Pytest returned invalid collection data for {repo}: {exc}") from exc
    if not collected:
        raise CollectionError(f"No tests collected for {mode}")
    return select_tests(collected.values(), selectors)
