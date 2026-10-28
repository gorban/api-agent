from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from api_agent.testing.collection import collect_tests
from api_agent.testing.integration import run_integration_tests
from api_agent.testing.runner import run_unit_tests


@pytest.mark.parametrize('mode', ['unit', 'integration'])
def test_uneven_parallel_work_is_stolen_and_reported(tmp_path, monkeypatch, mode):
    pytest.importorskip('xdist')
    suite = tmp_path / 'tests' / mode
    suite.mkdir(parents=True)
    marker = tmp_path / 'stolen'
    (suite / 'test_work.py').write_text(f'''
import os
from pathlib import Path
import time
import pytest

@pytest.mark.parametrize('case', range(12))
def test_work(case):
    marker = Path({str(marker)!r})
    if case == 0:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert marker.exists(), "queued work was not stolen"
    if case == 5:
        marker.write_text(os.environ.get('PYTEST_XDIST_WORKER', 'serial'))
''')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
                              suite_paths=lambda _: [suite])
    selected = collect_tests(context, mode)
    events = []
    real_popen = subprocess.Popen
    launches = []

    def record(args, *args_rest, **kwargs):
        launches.append(args)
        return real_popen(args, *args_rest, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', record)
    if mode == 'unit':
        result = run_unit_tests(context, selected, parallel_jobs=2, on_event=events.append)
    else:
        result = run_integration_tests(context, selected, accepted=True,
                                       parallel_jobs=2, on_event=events.append)

    assert result.exit_code == 0
    assert any('--dist=worksteal' in args for args in launches)
    assert marker.read_text().startswith('gw')
    assert {row.identity.id for row in result.tests} == {item.id for item in selected}
    assert all(row.status == 'passed' for row in result.tests)
    assert {event.identity.id for event in events if event.kind == 'test_finished'} == {
        item.id for item in selected
    }


@pytest.mark.parametrize('mode', ['unit', 'integration'])
def test_serial_execution_does_not_start_xdist(tmp_path, monkeypatch, mode):
    suite = tmp_path / 'tests' / mode
    suite.mkdir(parents=True)
    (suite / 'test_work.py').write_text('def test_work():\n    pass\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
                              suite_paths=lambda _: [suite])
    selected = collect_tests(context, mode)
    real_popen = subprocess.Popen
    launches = []

    def record(args, *args_rest, **kwargs):
        launches.append(args)
        return real_popen(args, *args_rest, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', record)
    if mode == 'unit':
        result = run_unit_tests(context, selected, parallel_jobs=0)
    else:
        result = run_integration_tests(context, selected, accepted=True, parallel_jobs=0)

    assert result.exit_code == 0
    assert all('-n' not in args and '--dist=worksteal' not in args for args in launches)
    assert len(result.tests) == 1 and result.tests[0].status == 'passed'
