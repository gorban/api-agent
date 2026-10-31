from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from api_agent.testing.collection import collect_tests, CollectionError
from api_agent.testing.runner import CoverageArea, run_unit_tests, unit_parallel_jobs


@pytest.mark.parametrize('config, expected', [
    ({}, 8),
    ({'TEST_PARALLEL_JOBS': 11}, 11),
    ({'TEST_PARALLEL_JOBS': 11, 'UNIT_TEST_PARALLEL_JOBS': None}, 11),
    ({'TEST_PARALLEL_JOBS': 11, 'UNIT_TEST_PARALLEL_JOBS': 0}, 0),
    ({'TEST_PARALLEL_JOBS': 11, 'UNIT_TEST_PARALLEL_JOBS': 12}, 12),
])
def test_unit_worker_override_preserves_empty_inheritance_and_serial_zero(config, expected):
    assert unit_parallel_jobs(config) == expected


def fixture_repo(root, code='VALUE = 1\n', test='from tiny import VALUE\ndef test_ok():\n    assert VALUE == 1\n'):
    (root / 'src').mkdir(parents=True)
    (root / 'src/tiny.py').write_text(code)
    (root / 'tests/unit').mkdir(parents=True)
    (root / 'tests/unit/test_tiny.py').write_text(test)
    (root / 'pytest.ini').write_text('[pytest]\naddopts = --invalid-option\n')
    return root


def context(base, pack=None):
    return SimpleNamespace(base_root=base, owner_root=pack or base,
                           suite_paths=lambda mode: [base / 'tests/unit'] + ([pack / 'tests/unit'] if pack else []))


def test_real_owner_execution_results_output_and_coverage(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    pack = fixture_repo(tmp_path / 'pack', test='from pathlib import Path\ndef test_owner():\n    assert Path.cwd().name == "pack"\n')
    ctx = context(base, pack)
    events, output = [], []
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'), on_event=events.append, output=output.append)
    assert {r.identity.repo for r in result.tests} == {'base', 'pack'}
    assert all(r.status == 'passed' for r in result.tests)
    assert result.exit_code == 1  # pack production was never imported
    assert {a.repo for a in result.coverage} == {'base', 'pack'}
    assert {report.repo for report in result.coverage_reports} == {'base', 'pack'}
    assert len({report.json_path.parent for report in result.coverage_reports}) == 2
    assert all(report.json_path.is_relative_to(pack / 'artifacts/unit')
               and report.json_path.is_file() and report.html_path.is_file()
               for report in result.coverage_reports)
    assert ''.join(output) == result.output
    assert 'passed' in result.output
    assert {e.kind for e in events} >= {'started', 'test_started', 'test_finished', 'output', 'finished'}


def test_cli_test_cannot_redirect_runner_events_by_changing_argv(tmp_path):
    base = fixture_repo(tmp_path / 'base', test='import sys\nfrom tiny import VALUE\n'
        'def test_cli(monkeypatch):\n'
        '    monkeypatch.setattr(sys, "argv", ["app", "unexpected-events.jsonl"])\n'
        '    assert VALUE == 1\n')
    ctx = context(base)
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'))
    assert result.exit_code == 0 and [test.status for test in result.tests] == ['passed']
    assert all(area.complete for area in result.coverage)
    assert not (base / 'unexpected-events.jsonl').exists()


def test_fixed_branch_gate_and_subset_keeps_unimported_files(tmp_path):
    base = fixture_repo(tmp_path / 'base', 'def choose(value):\n    if value:\n        return 1\n    return 2\n', 'from tiny import choose\ndef test_ok():\n    assert choose(True) == 1\n')
    (base / 'src/unused.py').write_text('UNUSED = 1\n')
    ctx = context(base)
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'))
    assert result.exit_code == 1
    assert any(a.file.endswith('unused.py') and a.covered_lines == 0 for a in result.coverage)
    assert any(a.num_branches and not a.complete for a in result.coverage)
    assert not CoverageArea('repo', 'file', 9999, 10000, 0, 0).complete


def test_failure_traceback_and_no_source_pack(tmp_path):
    base = fixture_repo(tmp_path / 'base', test='def test_bad():\n    assert False, "deliberate failure"\n')
    ctx = context(base)
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'))
    assert result.exit_code == 1
    assert result.tests[0].status == 'failed'
    assert 'deliberate failure' in result.tests[0].traceback
    assert 'deliberate failure' in result.output


def test_full_coverage_success_and_optional_missing_pack(tmp_path, monkeypatch):
    from api_agent.testing import recording, storage
    def forbidden(*args, **kwargs):
        pytest.fail('Unit execution must not initialize Allure recording/storage')
    monkeypatch.setattr(recording, 'RunRecorder', forbidden)
    monkeypatch.setattr(storage, 'RunStorage', forbidden)
    latest = tmp_path / 'latest-integration-manifest.json'
    latest.write_text('{"run_id": "prior-integration"}')
    base = fixture_repo(tmp_path / 'base')
    # Distributed provider integration checks are test code, not production.
    contributed = base / 'src/provider/tests/integration'
    contributed.mkdir(parents=True)
    (contributed / 'test_remote.py').write_text('def test_remote():\n    assert False\n')
    ctx = context(base, tmp_path / 'absent')
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'))
    assert result.exit_code == 0
    assert all(a.complete for a in result.coverage)
    assert all('test_remote.py' not in area.file for area in result.coverage)
    assert latest.read_text() == '{"run_id": "prior-integration"}'
    assert not list(base.rglob('*-result.json'))
    assert not (base / 'artifacts/allure').exists()
    with pytest.raises(CollectionError, match='empty'):
        run_unit_tests(ctx, [])


def test_cancellation_reaps_pytest_and_its_child(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    base = fixture_repo(tmp_path / 'base', test='import os, subprocess, sys, time\nfrom pathlib import Path\ndef test_sleep():\n    Path("scratch-path").write_text(os.environ["TMPDIR"])\n    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n    Path("child.pid").write_text(str(child.pid))\n    time.sleep(60)\n')
    ctx = context(base)
    tests = collect_tests(ctx, 'unit')
    cancel = threading.Event()
    def cancel_when_started():
        deadline = time.monotonic() + 15
        while not (base / 'child.pid').exists() and time.monotonic() < deadline:
            time.sleep(.03)
        cancel.set()
    thread = threading.Thread(target=cancel_when_started)
    thread.start()
    result = run_unit_tests(ctx, tests, cancel_event=cancel)
    thread.join()
    assert result.cancelled and result.exit_code == 130
    scratch = Path((base / "scratch-path").read_text())
    assert scratch.parent == tmp_path and not scratch.exists()
    pid = int((base / 'child.pid').read_text())
    status = Path(f'/proc/{pid}/stat')
    assert not status.exists() or status.read_text().split()[2] == 'Z'


def test_unselected_owner_still_measures_all_production(tmp_path, monkeypatch):
    from api_agent.testing import runner
    real_scratch = runner._unit_scratch
    created = []
    def track_scratch(prefix):
        owner = real_scratch(prefix)
        created.append((prefix, Path(owner.name)))
        return owner
    monkeypatch.setattr(runner, "_unit_scratch", track_scratch)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    base = fixture_repo(tmp_path / 'base')
    pack = fixture_repo(tmp_path / 'pack')
    ctx = context(base, pack)
    selected = [test for test in collect_tests(ctx, 'unit') if test.repo == 'base']
    result = run_unit_tests(ctx, selected)
    assert result.exit_code == 1
    assert [(prefix, path.parent) for prefix, path in created] == [
        ('api-agent-unit-', tmp_path), ('api-agent-unselected-', tmp_path)]
    assert all(not path.exists() for _, path in created)
    assert any(area.repo == 'pack' and area.covered_lines == 0 for area in result.coverage)
    assert {report.repo for report in result.coverage_reports} == {'base', 'pack'}
    assert all(report.json_path.is_file() and report.html_path.is_file()
               for report in result.coverage_reports)
    from api_agent.testing.runner import format_unit_summary
    summary = format_unit_summary(result)
    assert '1/1 passed' in summary and 'FAIL' in summary
    assert 'files below 100%' in summary and 'branches' in summary


def test_owner_coverage_omissions_cannot_hide_namespace_files(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    (base / 'src/namespace').mkdir()
    (base / 'src/namespace/hidden.py').write_text('HIDDEN = 42\n')
    (base / '.coveragerc').write_text('[run]\nomit = */hidden.py\n[report]\nomit = */hidden.py\n')
    ctx = context(base)
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'))
    assert result.exit_code == 1
    assert any(area.file.endswith('hidden.py') and area.num_statements == 1 and area.covered_lines == 0 for area in result.coverage)


def test_parallel_execution_keeps_identity_events(tmp_path):
    pytest.importorskip('xdist')
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    events = []
    selected = collect_tests(ctx, 'unit')
    result = run_unit_tests(ctx, selected, parallel_jobs=2, on_event=events.append)
    assert result.exit_code == 0
    assert result.tests[0].identity == selected[0]
    assert any(event.kind == 'test_finished' and event.identity == selected[0] for event in events)


@pytest.mark.parametrize("inherited_pipe", [True, False])
def test_finished_pytest_reaps_child_holding_output_pipe(tmp_path, inherited_pipe):
    import os
    if os.name != 'posix':
        pytest.skip('POSIX process groups required')
    marker = tmp_path / 'child.pid'
    test = "import subprocess, sys\ndef test_child(capfd):\n    with capfd.disabled():\n        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n        open(" + repr(str(marker)) + ", 'w').write(str(child.pid))\n"
    if not inherited_pipe:
        test = test.replace("    with capfd.disabled():\n", "").replace("        ", "    ")
    base = fixture_repo(tmp_path / 'base', test=test)
    ctx = context(base)
    start = time.monotonic()
    run_unit_tests(ctx, collect_tests(ctx, 'unit'))
    assert time.monotonic() - start < 5
    stat = Path('/proc') / marker.read_text() / 'stat'
    def running():
        try:
            return stat.read_text().split()[2] != 'Z'
        except (FileNotFoundError, ProcessLookupError):
            return False
    deadline = time.monotonic() + .5
    while running() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not running()


def test_summary_omits_complete_files_and_reports_one_full_coverage_line():
    from api_agent.testing.runner import format_unit_summary, UnitRunResult
    complete = CoverageArea('base', 'src/empty.py', 0, 0, 0, 0)
    incomplete = CoverageArea('base', 'src/partial.py', 1, 2, 1, 2)
    summary = format_unit_summary(UnitRunResult(1, (), (complete, incomplete), ''))
    assert 'empty.py' not in summary
    assert 'partial.py' in summary
    summary = format_unit_summary(UnitRunResult(1, (), (complete, complete), ''))
    assert summary.count('Code coverage: lines 100% · branches 100%.') == 1
    assert 'Production coverage' not in summary
    assert 'requires every measured file' not in summary
    assert 'empty.py' not in summary


def test_partial_selection_reports_coverage_without_failing_passed_tests(tmp_path):
    base = fixture_repo(tmp_path / 'base', code='def unused():\n    return 1\n', test='def test_ok():\n    assert True\n')
    ctx = context(base)
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'), require_full_coverage=False)
    assert result.exit_code == 0
    assert not result.require_full_coverage
    assert any(not area.complete for area in result.coverage)
    from api_agent.testing.runner import format_unit_summary
    assert 'Unit results: PASS' in format_unit_summary(result)



def test_summary_distinguishes_coverage_only_failure_from_test_failure():
    from api_agent.testing.runner import format_unit_summary, UnitRunResult, TestResult
    from api_agent.testing.collection import TestIdentity
    one = TestIdentity('base::test.py::one', 'base', 'test.py', 'test.py::one')
    two = TestIdentity('base::test.py::two', 'base', 'test.py', 'test.py::two')
    coverage = (CoverageArea('base', 'src/partial.py', 1, 2, 0, 0),)
    results = (TestResult(one, 'passed'), TestResult(two, 'passed'))
    assert format_unit_summary(UnitRunResult(1, results, coverage, '')).splitlines()[0] == (
        'Unit results: Coverage FAIL — 2/2 passed.')
    results = (TestResult(one, 'passed'), TestResult(two, 'failed'))
    assert format_unit_summary(UnitRunResult(1, results, coverage, '')).splitlines()[0] == (
        'Unit results: FAIL — 1/2 passed; 1 did not pass.')
    assert format_unit_summary(UnitRunResult(130, results, coverage, '', True)).startswith('Unit results: CANCELLED')


def test_unit_worker_delivers_component_progress(tmp_path):
    base = fixture_repo(tmp_path / 'base', test="""
import asyncio
from tiny import VALUE
from api_agent.providers.progress import report_timeout
def test_component():
    async def run():
        async with report_timeout('Synthetic wait', 3):
            await asyncio.sleep(.01)
    asyncio.run(run())
    assert VALUE == 1
""")
    ctx = context(base)
    events = []
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'), on_event=events.append)
    assert result.exit_code == 0
    progress = [event for event in events if event.kind == 'component_progress']
    assert len(progress) == 2 and progress[0].component.active and not progress[-1].component.active
    assert progress[0].identity == result.tests[0].identity
    assert progress[0].component.id == progress[-1].component.id


def test_large_selection_uses_file_instead_of_os_argument_budget(tmp_path, monkeypatch):
    """Long parameter IDs must not consume the OS exec argument allowance."""
    import io
    import json
    from api_agent.testing import runner
    from api_agent.testing.collection import TestIdentity
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    nodes = ['tests/unit/test_tiny.py::test_ok[' + str(index) + '-' + 'x' * 3000 + ']' for index in range(1000)]
    tests = [TestIdentity('base::' + node, 'base', 'tests/unit/test_tiny.py', node) for node in nodes]
    monkeypatch.setattr(runner, 'collect_tests', lambda *args, **kwargs: tests)
    launches = []
    def launch(args, **options):
        assert sum(len(arg.encode()) + 1 for arg in args) < 10000
        selected = Path(options['env']['API_AGENT_UNIT_SELECTION'])
        assert json.loads(selected.read_text()) == nodes
        launches.append(selected)
        return SimpleNamespace(stdout=io.StringIO(), returncode=1, poll=lambda: 1,
                               _api_agent_disposal_complete=True)
    monkeypatch.setattr(runner.subprocess, 'Popen', launch)
    monkeypatch.setattr(runner, '_stop_execution', lambda process: None)
    result = run_unit_tests(ctx, tests)
    assert len(result.tests) == len(nodes)
    assert len(launches) == 1 and not launches[0].exists()


@pytest.mark.parametrize('installed,base_name,owner_name', [
    (False, 'same', 'same'), (True, 'api_agent', 'external'),
    (True, 'api_agent', 'api-agent'), (True, 'api_agent', 'api_agent')])
def test_unit_executor_uses_collected_labels_for_each_actual_child_cwd(tmp_path, monkeypatch, installed, base_name, owner_name):
    from api_agent.testing import collection
    def build(path):
        return fixture_repo(path, test='from pathlib import Path\ndef test_cwd():\n    assert Path.cwd() == Path(' + repr(str(path)) + ')\n')
    base = build(tmp_path / 'base' / base_name)
    owner = build(tmp_path / 'owner' / owner_name)
    monkeypatch.setattr(collection, '_installed_base_identity', lambda root: installed and root == base)
    ctx = context(base, owner)
    tests = collect_tests(ctx, 'unit')
    assert len({test.id for test in tests}) == 2 and all(test.root is None for test in tests)
    result = run_unit_tests.__wrapped__(ctx, tests, require_full_coverage=False)
    assert result.exit_code == 0
    assert len(result.tests) == 2 and all(row.status == 'passed' for row in result.tests)


@pytest.mark.parametrize('explicit, failing', [(False, False), (True, False), (False, True)])
def test_unit_scratch_owned_by_runner_and_child(tmp_path, monkeypatch, explicit, failing):
    import json
    cache = tmp_path / 'cache'
    chosen = tmp_path / 'chosen'
    chosen.mkdir()
    monkeypatch.setenv('XDG_CACHE_HOME', str(cache))
    if explicit:
        monkeypatch.setenv('TMPDIR', str(chosen))
    else:
        monkeypatch.delenv('TMPDIR', raising=False)
    base = fixture_repo(tmp_path / 'base', test=(
        'import json, os, tempfile\nfrom pathlib import Path\n'
        'def test_scratch(tmp_path, pytestconfig):\n'
        '    Path("scratch.json").write_text(json.dumps([os.environ["TMPDIR"], tempfile.gettempdir(), str(tmp_path), str(pytestconfig.getoption("basetemp"))]))\n'
        + ('    assert False\n' if failing else '    assert True\n')))
    ctx = context(base)
    import os, tempfile
    prior_tempdir = tempfile.tempdir
    prior_env = os.environ.get('TMPDIR')
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'), require_full_coverage=False)
    assert tempfile.tempdir == prior_tempdir and os.environ.get('TMPDIR') == prior_env
    scratch, temp, item, basetemp = map(Path, json.loads((base / 'scratch.json').read_text()))
    parent = chosen if explicit else cache / 'api-agent/unit-scratch'
    assert scratch.parent == parent and temp == scratch
    assert item.is_relative_to(scratch) and basetemp.is_relative_to(scratch)
    assert not scratch.exists() and parent.exists()
    assert result.tests[0].status == ('failed' if failing else 'passed')


def test_unit_scratch_concurrent_and_unsafe_cache(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from api_agent.testing.runner import _unit_scratch
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        owners = list(pool.map(lambda _: _unit_scratch('api-agent-unit-'), range(2)))
    paths = [Path(owner.name) for owner in owners]
    try:
        assert paths[0] != paths[1] and all(path.is_dir() for path in paths)
        assert all(path.stat().st_mode & 0o777 == 0o700 for path in paths)
    finally:
        for owner in owners:
            owner.cleanup()
    assert not any(path.exists() for path in paths)
    parent = tmp_path / 'api-agent/unit-scratch'
    parent.rmdir()
    parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError, match='unit scratch.*TMPDIR'):
        _unit_scratch('api-agent-unit-')


def test_unit_scratch_rejects_unavailable_explicit_directory(tmp_path, monkeypatch):
    from api_agent.testing.runner import _unit_scratch
    missing = tmp_path / 'missing'
    monkeypatch.setenv('TMPDIR', str(missing))
    with pytest.raises(OSError, match='Cannot create unit scratch.*TMPDIR'):
        _unit_scratch('api-agent-unit-')
    assert not missing.exists()


def test_unit_scratch_rejects_shared_writable_parent(tmp_path, monkeypatch):
    import os
    from api_agent.testing.runner import _unit_scratch
    if not hasattr(os, 'getuid'):
        pytest.skip('POSIX ownership check')
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    app_cache = tmp_path / 'api-agent'
    app_cache.mkdir(mode=0o777)
    app_cache.chmod(0o777)
    with pytest.raises(OSError, match='unit scratch.*TMPDIR'):
        _unit_scratch('api-agent-unit-')
    assert app_cache.exists()


def test_unit_scratch_retained_if_process_disposal_unconfirmed(tmp_path, monkeypatch):
    import shutil
    from api_agent.testing import runner
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    tests = collect_tests(ctx, 'unit')
    original = runner._stop_execution
    def failed_disposal(process):
        original(process)  # Real child is reaped; simulate failure to confirm disposal.
        process._api_agent_disposal_complete = False
        raise RuntimeError('synthetic disposal failure')
    monkeypatch.setattr(runner, '_stop_execution', failed_disposal)
    try:
        with pytest.raises(RuntimeError, match='synthetic disposal failure') as error:
            run_unit_tests(ctx, tests)
        retained = list(tmp_path.glob('api-agent-unit-*'))
        assert len(retained) == 1 and retained[0].is_dir()
        assert any(str(retained[0]) in note for note in error.value.__notes__)
    finally:
        for path in tmp_path.glob('api-agent-unit-*'):
            shutil.rmtree(path)


@pytest.mark.parametrize('platform, setting, expected', [
    ('darwin', None, 'Library/Caches'),
    ('win32', 'local', 'local'),
    ('linux', 'relative', '.cache'),
])
def test_unit_scratch_cache_platform_defaults(tmp_path, monkeypatch, platform, setting, expected):
    from api_agent.testing import runner
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setattr(runner, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setenv('XDG_CACHE_HOME', 'relative')
    if setting == 'local':
        monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    with runner._unit_scratch('api-agent-unit-') as scratch:
        assert Path(scratch).parent == tmp_path / expected / 'api-agent/unit-scratch'


def test_unit_scratch_rejects_symlinked_cache_ancestor(tmp_path, monkeypatch):
    from api_agent.testing.runner import _unit_scratch
    monkeypatch.delenv('TMPDIR', raising=False)
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv('XDG_CACHE_HOME', str(link / 'cache'))
    with pytest.raises(OSError, match='unit scratch.*TMPDIR'):
        _unit_scratch('api-agent-unit-')
    assert not (tmp_path / 'cache').exists()


def test_unit_scratch_without_posix_uid_api(tmp_path, monkeypatch):
    import os
    from api_agent.testing import runner
    monkeypatch.setattr(runner, 'os', SimpleNamespace(environ={'XDG_CACHE_HOME': str(tmp_path)}))
    with runner._unit_scratch('api-agent-unit-') as scratch:
        assert Path(scratch).is_dir()


@pytest.mark.parametrize('value', ['', 'relative'])
def test_unit_scratch_windows_invalid_localappdata_uses_home(tmp_path, monkeypatch, value):
    from api_agent.testing import runner
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', value)
    monkeypatch.setattr(runner, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    with runner._unit_scratch('unit-') as scratch:
        assert Path(scratch).parent == tmp_path / 'AppData/Local/api-agent/unit-scratch'


def test_unit_scratch_rejects_late_untrusted_ancestor(tmp_path, monkeypatch):
    import os
    from types import ModuleType
    from api_agent.testing import runner
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'late' / 'cache'))
    proxy = ModuleType('isolated_os')
    proxy.__dict__.update(vars(os))
    original = os.mkdir
    injected = []
    def mkdir(name, mode=0o777, *, dir_fd=None):
        if name == 'late' and not injected:
            original(name, 0o700, dir_fd=dir_fd)
            os.chmod(tmp_path / 'late', 0o777)
            injected.append(True)
        return original(name, mode, dir_fd=dir_fd)
    proxy.mkdir = mkdir
    monkeypatch.setattr(runner, 'os', proxy)
    with pytest.raises(OSError, match='cache ancestor is not trusted'):
        runner._unit_scratch('unit-')
    assert injected and not (tmp_path / 'late/cache').exists()


def test_unit_scratch_new_intermediates_private_with_open_umask(tmp_path, monkeypatch):
    import os
    from api_agent.testing.runner import _unit_scratch
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'one/two'))
    previous = os.umask(0)
    try:
        with _unit_scratch('unit-'):
            assert (tmp_path / 'one').stat().st_mode & 0o777 == 0o700
            assert (tmp_path / 'one/two').stat().st_mode & 0o777 == 0o700
    finally:
        os.umask(previous)


def test_unit_scratch_without_uid_api_rejects_symlink(tmp_path, monkeypatch):
    from api_agent.testing import runner
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(runner, 'os', SimpleNamespace(environ={'XDG_CACHE_HOME': str(link)}))
    with pytest.raises(OSError, match='cache directory is not trusted'):
        runner._unit_scratch('unit-')


def test_unit_scratch_closes_directory_descriptor_on_interrupt(tmp_path, monkeypatch):
    import os
    from types import ModuleType
    from api_agent.testing import runner
    if not Path('/proc/self/fd').is_dir():
        pytest.skip('descriptor inventory unavailable')
    monkeypatch.delenv('TMPDIR', raising=False)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'interrupted'))
    proxy = ModuleType('isolated_os')
    proxy.__dict__.update(vars(os))
    original = os.mkdir
    def mkdir(name, mode=0o777, *, dir_fd=None):
        if name == 'interrupted':
            raise KeyboardInterrupt()
        return original(name, mode, dir_fd=dir_fd)
    proxy.mkdir = mkdir
    monkeypatch.setattr(runner, 'os', proxy)
    before = len(list(Path('/proc/self/fd').iterdir()))
    with pytest.raises(KeyboardInterrupt):
        runner._unit_scratch('unit-')
    assert len(list(Path('/proc/self/fd').iterdir())) == before
