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
    root: Path | None = None

    def __post_init__(self):
        object.__setattr__(self, "tags", frozenset(self.tags))
        if self.root is not None:
            object.__setattr__(self, "root", Path(self.root).resolve())


_PLUGIN = '''
import json, sys
try:
    import pytest
except ImportError:
    print("Collection requires pytest; install the api-agent dev dependencies in this Python environment.", file=sys.stderr)
    sys.exit(2)
class Collector:
    def pytest_configure(self, config):
        config.addinivalue_line("markers", "requires_pack_api: selected integration requires pack API/identity preflight")

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


def _validate_provider_suite(root, suite):
    """Reject symlink escapes before pytest can import suite or conftest code."""
    pending = [suite]
    parent = suite
    while parent != root:
        parent = parent.parent
        pending.append(parent / 'conftest.py')
        pending.append(parent / '__init__.py')
    visited = set()
    try:
        while pending:
            path = pending.pop().resolve()
            if not path.is_relative_to(root):
                raise CollectionError(f'Provider integration suite content escapes its root: {path}')
            if path.is_dir() and path not in visited:
                visited.add(path)
                pending.extend(path.iterdir())
    except (OSError, RuntimeError) as exc:
        raise CollectionError(f'Cannot inspect provider integration suite: {suite}') from exc


def provider_integration_suites(context, config):
    """Read selected providers' declarative suites without loading their factories."""
    from api_agent.providers.loading import provider_location, ProviderError
    from .provider_tests import load_test_declaration
    suites = {}
    for name in ('LLM_PROVIDER', 'API_PROVIDER', 'UI_PROVIDER', 'ACCESS_PROVIDER', 'UPSTREAM_API_PROVIDER'):
        selected = config.get(name)
        selected = getattr(selected, 'value', selected)
        if not selected:
            continue
        try:
            factory, unused = provider_location(selected, owner_root=context.owner_root)
        except ProviderError as exc:
            raise CollectionError(f'Invalid selected provider: {name}') from exc
        root = factory.parent
        tests = load_test_declaration(root)
        if tests is None:
            continue
        manifest = root / 'cfg/tests.toml'
        for entry in tests['integration']:
            if not isinstance(entry, str) or not entry.strip() or Path(entry).is_absolute():
                raise CollectionError(f'Provider integration suites require relative paths: {manifest}')
            path = (root / entry).resolve()
            if not path.is_relative_to(root) or not path.is_dir():
                raise CollectionError(f'Provider integration suite must exist inside its root: {path}')
            _validate_provider_suite(root, path)
            suites.setdefault(root, []).append(path)
    return suites


def collect_tests(context, mode, selectors=(), cancel_event=None, *, config=None):
    """Collect each owner's suites using its working directory and pytest config."""
    if mode not in {"unit", "integration", "e2e"}:
        raise CollectionError(f"Collection requires unit, integration, or e2e mode; got {mode!r}")
    base = Path(context.base_root).resolve()
    owner = Path(context.owner_root).resolve()
    suites = {}
    declared = list(context.suite_paths(mode))
    if mode == 'integration' and (base / 'tests/integration').exists():
        declared.insert(0, base / 'tests/integration')
    for raw_path in declared:
        path = Path(raw_path).resolve()
        if not path.exists():
            if mode == "unit" and path != base / "tests/unit":
                continue
            raise CollectionError(f"Declared {mode} suite does not exist: {path}")
        if path == base / f'tests/{mode}':
            root = base
        else:
            root = owner if path.is_relative_to(owner) else base
        if not path.is_relative_to(root):
            raise CollectionError(f"Suite is outside its repository owner: {path}")
        suites.setdefault(root, []).append(path)
    if mode == 'integration':
        for root, paths in provider_integration_suites(context, config if config is not None else getattr(context, 'defaults', {})).items():
            suites.setdefault(root, []).extend(paths)
    if not suites:
        raise CollectionError(f"No {mode} suites are declared")
    child_env = os.environ.copy()
    source_paths = [str(base / "src")]
    if owner != base:
        source_paths.append(str(owner / "src"))
    if child_env.get("PYTHONPATH"):
        source_paths.append(child_env["PYTHONPATH"])
    child_env["PYTHONPATH"] = os.pathsep.join(source_paths)
    labels = {root: repository_label(context, root) for root in suites}
    for root in suites:
        if root not in {base, owner}:
            labels[root] = 'provider:' + str(root)
    collected = {}
    for root, paths in suites.items():
        repo = labels[root]
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
                    identity = TestIdentity(f"{repo}::{node}", repo, node.split("::", 1)[0], node, frozenset(row["tags"]), root if root not in {base, owner} else None)
                    file, _, suffix = node.partition('::')
                    collected.setdefault(((root / file).resolve(), suffix), identity)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise CollectionError(f"Pytest returned invalid collection data for {repo}: {exc}") from exc
    if not collected:
        raise CollectionError(f"No tests collected for {mode}")
    return select_tests(collected.values(), selectors)
