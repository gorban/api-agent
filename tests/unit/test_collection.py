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
    assert {test.repo for test in collect_tests(context(base, pack, {mode: (selected,)}), mode)} == {'pack'}


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
