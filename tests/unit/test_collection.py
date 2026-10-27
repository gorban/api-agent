from pathlib import Path
from types import SimpleNamespace
import subprocess
import pytest

from api_agent.testing import CollectionError, TestIdentity, collect_tests, select_tests


def context(base, owner=None, modes=None):
    owner = owner or base
    modes = modes or {}
    return SimpleNamespace(base_root=base, owner_root=owner, suite_paths=lambda mode: ((base / 'tests/unit',) + tuple(modes.get(mode, ()))) if mode == 'unit' else tuple(modes.get(mode, ())))


def suite(root, name='unit'):
    path = root / 'tests' / name
    path.mkdir(parents=True)
    (root / 'pytest.ini').write_text('[pytest]\nmarkers = mutation: mutation tests\n')
    (path / 'test_tiny.py').write_text('import pytest\n@pytest.mark.mutation\n@pytest.mark.parametrize("value", [1, 2])\ndef test_value(value):\n    raise RuntimeError("must never execute")\n')
    return path


def test_real_collection_combines_deduplicates_and_never_executes(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    units = suite(base)
    extras = suite(pack)
    tests = collect_tests(context(base, pack, {'unit': (units, extras, extras)}), 'unit')
    assert len(tests) == 4
    assert {test.repo for test in tests} == {'base', 'pack'}
    assert all('mutation' in test.tags for test in tests)
    assert [test.node_id for test in tests[:2]] == ['tests/unit/test_tiny.py::test_value[1]', 'tests/unit/test_tiny.py::test_value[2]']
    assert len(collect_tests(context(base, pack, {'unit': (extras,)}), 'unit', ['base::tests/unit/test_tiny.py'])) == 2


@pytest.mark.parametrize("mode", ["integration", "e2e"])
def test_integration_and_e2e_scoped_to_owner(tmp_path, mode):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    suite(base)
    suite(base, mode)
    selected = suite(pack, mode)
    expected = {'base', 'pack'} if mode == 'integration' else {'pack'}
    assert {test.repo for test in collect_tests(context(base, pack, {mode: (selected,)}), mode)} == expected


def test_selectors_union_and_ambiguity():
    tests = [TestIdentity(f'{repo}::a.py::test_x', repo, 'a.py', 'a.py::test_x', frozenset({'tagged'})) for repo in ('base', 'pack')]
    assert select_tests(tests, ['tag:tagged', tests[0].id]) == tests
    assert select_tests(tests, ['pack::a.py']) == [tests[1]]
    assert select_tests(tests, ['base::tag:tagged']) == [tests[0]]
    for selector in ('a.py', 'a.py::test_x', '', 'missing'):
        with pytest.raises(CollectionError):
            select_tests(tests, [selector])


def test_missing_mode_suite_and_pytest_are_actionable(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    with pytest.raises(CollectionError, match='requires unit'):
        collect_tests(ctx, 'local')
    with pytest.raises(CollectionError, match='does not exist'):
        collect_tests(ctx, 'unit')
    suite(tmp_path)
    monkeypatch.setattr('api_agent.testing.collection._run_collection', lambda *args, **kwargs: SimpleNamespace(returncode=2, stderr='Install pytest dev dependencies', stdout=''))
    with pytest.raises(CollectionError, match='Install pytest'):
        collect_tests(ctx, 'unit')


def test_empty_collection_is_error(tmp_path):
    (tmp_path / 'tests/unit').mkdir(parents=True)
    with pytest.raises(CollectionError):
        collect_tests(context(tmp_path), 'unit')


def test_source_imports_resolved_without_editable_install(tmp_path, monkeypatch):
    base, owner = tmp_path / 'base', tmp_path / 'pack'
    base_units = suite(base)
    pack_units = suite(owner)
    for root, module in ((base, 'fresh_base_code'), (owner, 'fresh_pack_code')):
        (root / 'src').mkdir()
        (root / 'src' / f'{module}.py').write_text('VALUE = 42\n')
    (base_units / 'test_import.py').write_text('from fresh_base_code import VALUE\ndef test_import():\n    assert VALUE == 42\n')
    (pack_units / 'test_pack_import.py').write_text('from fresh_base_code import VALUE\nfrom fresh_pack_code import VALUE as PACK\ndef test_import():\n    assert VALUE == PACK\n')
    monkeypatch.delenv('PYTHONPATH', raising=False)
    assert len(collect_tests(context(base, owner, {'unit': (pack_units,)}), 'unit')) == 6


def test_absent_optional_pack_units_and_empty_base(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    suite(base)
    ctx = context(base, pack, {'unit': (pack / 'tests/unit',)})
    assert len(collect_tests(ctx, 'unit')) == 2
    empty_base = tmp_path / 'empty'
    (empty_base / 'tests/unit').mkdir(parents=True)
    pack_units = suite(pack)
    with pytest.raises(CollectionError):
        collect_tests(context(empty_base, pack, {'unit': (pack_units,)}), 'unit')


def test_cancel_collection_import_reaps_owned_child(tmp_path):
    import os
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    path = suite(tmp_path)
    pid_file = tmp_path / 'collector.pid'
    (path / 'test_tiny.py').write_text(
        'import os, signal, time\nfrom pathlib import Path\n'
        'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
        f'Path({str(pid_file)!r}).write_text(str(os.getpid()))\n'
        'time.sleep(60)\ndef test_never():\n    assert False\n'
    )
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect_tests, context(tmp_path), 'unit', (), cancel)
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists(), 'collector must reach fixture import before cancellation'
        cancel.set()
        with pytest.raises(CollectionError, match='cancelled'):
            future.result(timeout=3)
    if os.name == 'posix':
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


def test_successful_collection_reaps_child_with_closed_output(tmp_path):
    import os
    import time
    if os.name != 'posix':
        pytest.skip('POSIX process groups required')
    path = suite(tmp_path)
    pid_file = tmp_path / 'child.pid'
    (path / 'test_tiny.py').write_text(
        'import subprocess, sys\nfrom pathlib import Path\n'
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], '
        'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        f'Path({str(pid_file)!r}).write_text(str(child.pid))\n'
        'def test_one(): pass\n'
    )
    assert len(collect_tests(context(tmp_path), 'unit')) == 1
    stat = Path('/proc') / pid_file.read_text() / 'stat'
    assert not stat.exists() or stat.read_text().split()[2] == 'Z'


def test_successful_collection_reaps_child_holding_output_pipe(tmp_path):
    import os
    import time
    if os.name != 'posix':
        pytest.skip('POSIX process groups required')
    path = suite(tmp_path)
    pid_file = tmp_path / 'child-pipe.pid'
    (path / 'test_tiny.py').write_text(
        'import subprocess, sys\nfrom pathlib import Path\n'
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
        f'Path({str(pid_file)!r}).write_text(str(child.pid))\n'
        'def test_one(): pass\n'
    )
    start = time.monotonic()
    assert len(collect_tests(context(tmp_path), 'unit')) == 1
    assert time.monotonic() - start < 5
    stat = Path('/proc') / pid_file.read_text() / 'stat'
    assert not stat.exists() or stat.read_text().split()[2] == 'Z'


def test_finished_collector_leader_does_not_wait_for_inherited_pipe(tmp_path):
    import os
    import sys
    import time
    from api_agent.testing.collection import _run_collection
    if os.name != 'posix':
        pytest.skip('POSIX process groups required')
    marker = tmp_path / 'pipe-child.pid'
    source = ('import subprocess, sys\nfrom pathlib import Path\n'
              'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
              f'Path({str(marker)!r}).write_text(str(child.pid))\n'
              'print("collected", flush=True)\n')
    start = time.monotonic()
    result = _run_collection([sys.executable, '-c', source], tmp_path, os.environ.copy(), 'repo', None)
    assert result.returncode == 0 and result.stdout.strip() == 'collected'
    assert time.monotonic() - start < 5
    stat = Path('/proc') / marker.read_text() / 'stat'
    assert not stat.exists() or stat.read_text().split()[2] == 'Z'


def test_precancel_does_not_spawn_and_timeout_reaps(tmp_path, monkeypatch):
    import os
    import sys
    import threading
    from api_agent.testing.collection import _run_collection
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CollectionError, match='cancelled'):
        _run_collection([sys.executable, '-c', 'pass'], tmp_path, os.environ.copy(), 'repo', cancel)
    with pytest.raises(CollectionError, match='exceeded'):
        _run_collection([sys.executable, '-c', 'import time; time.sleep(60)'], tmp_path, os.environ.copy(), 'repo', None, timeout=0.05)


@pytest.mark.parametrize("mode", ["integration", "e2e"])
def test_no_pack_suite_uses_base_and_missing_or_empty_owner_fails(tmp_path, mode):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    selected = suite(base, mode)
    assert {test.repo for test in collect_tests(context(base, modes={mode: (selected,)}), mode)} == {'base'}
    ctx = context(base, pack, {mode: (pack / 'tests' / mode,)})
    with pytest.raises(CollectionError, match='does not exist'):
        collect_tests(ctx, mode)
    (pack / 'tests' / mode).mkdir(parents=True)
    with pytest.raises(CollectionError):
        collect_tests(ctx, mode)


def test_empty_optional_pack_units_do_not_hide_missing_base(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    suite(base)
    (pack / 'tests/unit').mkdir(parents=True)
    ctx = context(base, pack, {'unit': (pack / 'tests/unit',)})
    assert {test.repo for test in collect_tests(ctx, 'unit')} == {'base'}
    (base / 'tests/unit/test_tiny.py').unlink()
    with pytest.raises(CollectionError):
        collect_tests(ctx, 'unit')


def test_nested_pack_keeps_its_owner_working_directory(tmp_path):
    base = tmp_path / 'base'
    suite(base)
    owner = base / 'packs' / 'nested'
    units = suite(owner)
    identities = collect_tests(context(base, owner, {'unit': (units,)}), 'unit')
    assert {test.repo for test in identities} == {'base', 'nested'}


@pytest.mark.parametrize('installed,base_name,owner_name,labels', [
    (False, 'same', 'same', ('base', 'pack')),
    (True, 'api_agent', 'external', ('api-agent', 'external')),
    (True, 'api_agent', 'api-agent', ('base', 'pack')),
    (True, 'api_agent', 'api_agent', ('api-agent', 'pack')),
])
def test_collector_implicit_roots_keep_unique_effective_repository_labels(tmp_path, monkeypatch, installed, base_name, owner_name, labels):
    import json
    from api_agent.testing import collection
    base = tmp_path / 'base' / base_name
    owner = tmp_path / 'owner' / owner_name
    suite(base); suite(owner)
    monkeypatch.setattr(collection, '_installed_base_identity', lambda root: installed and root == base)
    def collect(args, *unused):
        Path(args[3]).write_text(json.dumps([dict(node_id='tests/unit/test_tiny.py::test_value[1]', tags=[])]))
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(collection, '_run_collection', collect)
    tests = collect_tests(context(base, owner, {'unit': (owner / 'tests/unit',)}), 'unit')
    assert tuple(test.repo for test in tests) == labels
    assert len({test.id for test in tests}) == 2 and all(test.root is None for test in tests)
    assert collection.repository_label(context(base), base) == ('api-agent' if installed else base.name)
    other = tmp_path / 'provider'; other.mkdir()
    assert collection.repository_label(context(base, owner), other) == ('pack' if base_name == owner_name else other.name)
