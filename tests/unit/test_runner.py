from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from api_agent.testing.collection import collect_tests, CollectionError
from api_agent.testing.runner import CoverageArea, run_unit_tests


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
    assert ''.join(output) == result.output
    assert 'passed' in result.output
    assert {e.kind for e in events} >= {'started', 'test_started', 'test_finished', 'output', 'finished'}


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


def test_full_coverage_success_and_optional_missing_pack(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base, tmp_path / 'absent')
    result = run_unit_tests(ctx, collect_tests(ctx, 'unit'))
    assert result.exit_code == 0
    assert all(a.complete for a in result.coverage)
    with pytest.raises(CollectionError, match='empty'):
        run_unit_tests(ctx, [])


def test_cancellation_reaps_pytest_and_its_child(tmp_path):
    base = fixture_repo(tmp_path / 'base', test='import subprocess, sys, time\nfrom pathlib import Path\ndef test_sleep():\n    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n    Path("child.pid").write_text(str(child.pid))\n    time.sleep(60)\n')
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
    pid = int((base / 'child.pid').read_text())
    status = Path(f'/proc/{pid}/stat')
    assert not status.exists() or status.read_text().split()[2] == 'Z'


def test_unselected_owner_still_measures_all_production(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    pack = fixture_repo(tmp_path / 'pack')
    ctx = context(base, pack)
    selected = [test for test in collect_tests(ctx, 'unit') if test.repo == 'base']
    result = run_unit_tests(ctx, selected)
    assert result.exit_code == 1
    assert any(area.repo == 'pack' and area.covered_lines == 0 for area in result.coverage)
    from api_agent.testing.runner import format_unit_summary
    summary = format_unit_summary(result)
    assert '1/1 passed' in summary and 'FAIL' in summary
    assert '[below 100%]' in summary and 'branches' in summary


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
    assert summary.count('Production coverage: 100%') == 1
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
