"""Controlled HTTP proof covers the server child, not just the pytest driver."""
import json
import os
from pathlib import Path
import subprocess
import sys

from api_agent.testing.e2e_coverage import prepare, finalize, function_body_candidates


def test_parallel_coverage_captures_real_python_http_child_and_missed_function(tmp_path):
    source = tmp_path / 'application'
    source.mkdir()
    module = source / 'native.py'
    module.write_text('''from http.server import BaseHTTPRequestHandler, HTTPServer

def application_native():
    return b"application reached"

def uncalled_native():
    return b"uncalled"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(application_native())

server = HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.handle_request()
server.server_close()
''')
    driver = tmp_path / 'test_driver.py'
    driver.write_text('''import subprocess, sys, urllib.request

def test_browser_backend_boundary():
    child = subprocess.Popen([sys.executable, '-c', 'import native'], stdout=subprocess.PIPE, text=True)
    port = int(child.stdout.readline())
    assert urllib.request.urlopen(f'http://127.0.0.1:{port}/').read() == b'application reached'
    assert child.wait(timeout=20) == 0
''')
    args = [sys.executable, '-m', 'pytest', '-q', '-o', 'addopts=', str(driver), '-n', '8']
    env = dict(os.environ, PYTHONPATH=str(source), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    args.extend(['-p', 'xdist.plugin', '-p', 'pytest_cov.plugin'])
    directory = tmp_path / 'coverage'
    prepare(directory, [source], args, env)
    result = subprocess.run(args, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = finalize(directory, selection=['controlled-http'], parallel_jobs=8, e2e_reruns=0, complete=True)
    data = json.loads((directory / 'coverage.json').read_text())
    row = next(row for name, row in data['files'].items() if name.endswith('native.py'))
    assert 4 in row['executed_lines']
    assert 7 in row['missing_lines']
    assert receipt['parallel_jobs'] == 8 and receipt['e2e_reruns'] == 0
    assert receipt['coverage_gate'] is False
    assert 'launcher coordinator' in receipt['uninstrumented']
    assert [item['function'] for item in receipt['function_body_candidates']] == ['uncalled_native']
    assert (directory / 'html/index.html').is_file()
    failed = finalize(directory, selection=['controlled-http'], parallel_jobs=8, e2e_reruns=0,
                      complete=True, successful=False)
    assert failed['owned_shutdown_complete'] is True
    assert failed['status'] == 'incomplete' and failed['reason'] == 'scenario_failed'


def test_candidates_ignore_import_only_misses_and_missing_sources(tmp_path):
    source = tmp_path / 'module.py'
    source.write_text('import json\n\ndef called():\n    return 1\n')
    assert function_body_candidates({'files': {str(source): {'missing_lines': [1], 'executed_lines': [3, 4]},
        str(tmp_path / 'missing.py'): {'missing_lines': [2]}}}) == []


def test_prepare_rejects_missing_application_scope(tmp_path):
    import pytest
    with pytest.raises(ValueError, match='declared application sources'):
        prepare(tmp_path / 'coverage', [], [], {})


def test_candidates_exclude_docstrings_and_functions_without_executable_evidence(tmp_path):
    source = tmp_path / 'functions.py'
    source.write_text('def documentation_only():\n    """No executable body."""\n\ndef missed():\n    """Documentation must not count as execution."""\n    return 2\n\ndef unmeasured():\n    return 3\n')
    result = function_body_candidates({'files': {str(source): {'missing_lines': [6], 'executed_lines': [1, 4, 8]}}})
    assert [row['function'] for row in result] == ['missed']
    assert result[0]['missed_body_lines'] == [6]


def test_canonical_e2e_executor_retains_separate_application_report(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    from types import SimpleNamespace
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    application = tmp_path / 'src'
    application.mkdir()
    (application / 'native.py').write_text('def called():\n    return 7\n\ndef missed():\n    return 8\n')
    suite = tmp_path / 'owned_e2e'
    suite.mkdir()
    (suite / 'test_boundary.py').write_text('import pytest\nfrom native import called\n'
        '@pytest.mark.requires_owned_ui_session\n@pytest.mark.e2e\n'
        'def test_native():\n    assert called() == 7\n')
    (tmp_path / 'pytest.ini').write_text('[pytest]\npythonpath = src\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: (suite,) if mode == 'e2e' else ())
    selected = collect_tests(context, 'e2e')
    result = run_integration_tests(context, selected, accepted=True,
        config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 0, 'E2E_COVERAGE': True}, parallel_jobs=8)
    assert result.exit_code == 0, result.output
    reports = list((tmp_path / 'artifacts/e2e-coverage').glob('*/diagnostic.json'))
    assert len(reports) == 1
    receipt = json.loads(reports[0].read_text())
    assert receipt['parallel_jobs'] == 8 and receipt['owned_shutdown_complete'] is True
    assert receipt['selection'] == [selected[0].id]
    assert [row['function'] for row in receipt['function_body_candidates']] == ['missed']
    assert not (tmp_path / 'artifacts/unit').exists()


import pytest

@pytest.mark.parametrize('jobs,reruns,debug,message', [
    (0, 0, False, 'eight parallel workers'),
    (8, 2, False, 'e2e-reruns 0'),
    (8, 0, True, 'owned shutdown'),
])
def test_diagnostic_constraints_reject_before_running(jobs, reruns, debug, message):
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.integration import run_integration_tests
    case = TestIdentity('suite::test_case', 'suite', 'test_case.py', 'test_case.py::test_case',
        tags=frozenset({'requires_owned_ui_session'}))
    with pytest.raises(ValueError, match=message):
        run_integration_tests(object(), [case], accepted=True, parallel_jobs=jobs,
            config={'TUI_MODE': 'e2e', 'E2E_RERUNS': reruns, 'E2E_COVERAGE': True, 'DEBUG': debug})


def test_pending_receipt_survives_collector_process_exit(tmp_path):
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'native.py').write_text('def f():\n    return 1\n')
    script = '''import os, sys
from api_agent.testing.e2e_coverage import prepare
prepare(sys.argv[1], [sys.argv[2]], [], {}, selection=['suite::case'],
        parallel_jobs=8, e2e_reruns=0, flow_identities=['flow-digest'])
os._exit(9)
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / 'src'))
    env.pop('COVERAGE_PROCESS_START', None)
    env.pop('COVERAGE_PROCESS_CONFIG', None)
    child = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'coverage'), str(source)], env=env)
    assert child.returncode == 9
    receipt = json.loads((tmp_path / 'coverage' / 'diagnostic.json').read_text())
    manifest = json.loads((tmp_path / 'coverage' / 'source-manifest.json').read_text())
    assert receipt['status'] == 'pending'
    assert receipt['selection'] == ['suite::case']
    assert manifest['flow_identities'] == ['flow-digest']
    assert str(source / 'native.py') in manifest['source_hashes']


def test_empty_coverage_is_incomplete_and_does_not_raise(tmp_path):
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'native.py').write_text('def f():\n    return 1\n')
    directory = tmp_path / 'coverage'
    prepare(directory, [source], [], {}, selection=['case'], parallel_jobs=8, e2e_reruns=0)
    receipt = finalize(directory, selection=['case'], parallel_jobs=8, e2e_reruns=0, complete=False)
    assert receipt['status'] == 'incomplete'
    assert receipt['owned_shutdown_complete'] is False
    assert receipt['function_body_candidates'] == []
    assert receipt['report'] is None


def test_changed_source_rejects_stale_coverage_baseline(tmp_path):
    source = tmp_path / 'src'
    source.mkdir()
    native = source / 'native.py'
    native.write_text('def f():\n    return 1\n')
    directory = tmp_path / 'coverage'
    prepare(directory, [source], [], {}, selection=['case'], parallel_jobs=8, e2e_reruns=0)
    native.write_text('def f():\n    return 2\n')
    receipt = finalize(directory, selection=['case'], parallel_jobs=8, e2e_reruns=0, complete=True)
    assert receipt['status'] == 'incomplete'
    assert receipt['reason'] == 'source_identity_changed'
    assert receipt['function_body_candidates'] == []


def test_nested_inline_execution_does_not_create_false_outer_candidate(tmp_path):
    source = tmp_path / 'native.py'
    source.write_text('def outer():\n    def inner(): return 1\n    return inner\n')
    # The shared line may execute only the retained inner function in this window.
    result = function_body_candidates({'files': {str(source): {
        'missing_lines': [3], 'executed_lines': [2]}}})
    assert all(row['function'] != 'outer' for row in result)


def test_nested_function_body_does_not_hide_unexecuted_outer_body(tmp_path):
    source = tmp_path / 'native.py'
    source.write_text('def outer():\n    def inner():\n        return 1\n    return inner\n')
    result = function_body_candidates({'files': {str(source): {
        'missing_lines': [2, 4], 'executed_lines': [3]}}})
    assert [(row['function'], row['missed_body_lines']) for row in result] == [('outer', [2, 4])]


def test_diagnostic_report_failure_does_not_change_scenario_outcome(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from api_agent.testing import e2e_coverage
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[2] / 'src'))
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'native.py').write_text('def f():\n    return 1\n')
    suite = tmp_path / 'owned_e2e'
    suite.mkdir()
    (suite / 'test_boundary.py').write_text('import pytest\n'
        '@pytest.mark.requires_owned_ui_session\n@pytest.mark.e2e\n'
        'def test_native():\n    from native import f\n    assert f() == 1\n')
    (tmp_path / 'pytest.ini').write_text('[pytest]\npythonpath = src\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: (suite,) if mode == 'e2e' else ())
    def fail_report(*args, **kwargs):
        raise RuntimeError('controlled report failure')
    monkeypatch.setattr(e2e_coverage, 'finalize', fail_report)
    selected = collect_tests(context, 'e2e')
    result = run_integration_tests(context, selected, accepted=True,
        config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 0, 'E2E_COVERAGE': True}, parallel_jobs=8)
    assert result.exit_code == 0, result.output
    assert 'coverage' in result.output.lower()


def test_prepare_failure_preserves_test_launch_and_outcome(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from api_agent.testing import e2e_coverage, integration
    from api_agent.testing.collection import collect_tests

    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[2] / 'src'))
    monkeypatch.setenv('DIAGNOSTIC_PREPARE_SENTINEL', 'original')
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'native.py').write_text('def f():\n    return 1\n')
    suite = tmp_path / 'owned_e2e'
    suite.mkdir()
    (suite / 'test_boundary.py').write_text('import os, pytest\n'
        '@pytest.mark.requires_owned_ui_session\n@pytest.mark.e2e\n'
        'def test_native():\n    from native import f\n    assert f() == 1\n'
        '    assert os.environ["DIAGNOSTIC_PREPARE_SENTINEL"] == "original"\n')
    (tmp_path / 'pytest.ini').write_text('[pytest]\npythonpath = src\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
        suite_paths=lambda mode: (suite,) if mode == 'e2e' else ())
    captured = {}

    def fail_prepare(directory, sources, args, env, **kwargs):
        captured['args'], captured['env'] = list(args), dict(env)
        args.append('--invalid-prepare-option')
        env['DIAGNOSTIC_PREPARE_SENTINEL'] = 'changed'
        raise RuntimeError('private prepare failure ' + 'x' * 10000)

    popen = subprocess.Popen
    def record_launch(args, **kwargs):
        captured['launch_args'], captured['launch_env'] = list(args), dict(kwargs['env'])
        return popen(args, **kwargs)

    monkeypatch.setattr(e2e_coverage, 'prepare', fail_prepare)
    monkeypatch.setattr(e2e_coverage, 'finalize', lambda *args, **kwargs: pytest.fail('finalize called'))
    selected = collect_tests(context, 'e2e')
    monkeypatch.setattr(integration.subprocess, 'Popen', record_launch)
    result = integration.run_integration_tests(context, selected, accepted=True,
        config={'TUI_MODE': 'e2e', 'E2E_RERUNS': 0, 'E2E_COVERAGE': True}, parallel_jobs=8)

    assert result.exit_code == 0, result.output
    assert captured['launch_args'] == captured['args']
    assert all(captured['launch_env'][key] == value for key, value in captured['env'].items())
    assert 'E2E diagnostic coverage preparation unavailable; test outcome unchanged.' in result.output
    assert 'private prepare failure' not in result.output
    assert not list((tmp_path / 'artifacts/e2e-coverage').glob('*/diagnostic.json'))


def test_mismatched_selection_cannot_become_baseline(tmp_path):
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'native.py').write_text('def f():\n    return 1\n')
    directory = tmp_path / 'coverage'
    prepare(directory, [source], [], {}, selection=['original'], parallel_jobs=8, e2e_reruns=0)
    receipt = finalize(directory, selection=['other'], parallel_jobs=8, e2e_reruns=0, complete=True)
    assert receipt['status'] == 'incomplete'
    assert receipt['reason'] == 'selection_changed'
    assert receipt['function_body_candidates'] == []


@pytest.mark.parametrize('jobs,reruns', [(4, 0), (8, 1)])
def test_worker_or_confirmation_change_is_not_a_baseline(tmp_path, jobs, reruns):
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'native.py').write_text('def f():\n    return 1\n')
    directory = tmp_path / 'coverage'
    prepare(directory, [source], [], {}, selection=['case'], parallel_jobs=8, e2e_reruns=0,
            flow_identities=[{'graph_id': 'graph', 'definition_id': 'digest', 'compiled_plan_identity': 'plan'}])
    manifest = json.loads((directory / 'source-manifest.json').read_text())
    assert manifest['flow_identities'][0]['compiled_plan_identity'] == 'plan'
    receipt = finalize(directory, selection=['case'], parallel_jobs=jobs, e2e_reruns=reruns, complete=True)
    assert receipt['reason'] == 'selection_changed'


def test_deleted_source_cannot_match_snapshot(tmp_path):
    source = tmp_path / 'src'
    source.mkdir()
    native = source / 'native.py'
    native.write_text('def f():\n    return 1\n')
    directory = tmp_path / 'coverage'
    prepare(directory, [source], [], {}, selection=['case'], parallel_jobs=8, e2e_reruns=0)
    native.unlink()
    receipt = finalize(directory, selection=['case'], parallel_jobs=8, e2e_reruns=0, complete=True)
    assert receipt['reason'] == 'source_identity_changed'


def test_candidate_qualified_names_distinguish_same_named_methods(tmp_path):
    source = tmp_path / 'native.py'
    source.write_text('class First:\n    def f(self):\n        return 1\n'
                      'class Second:\n    def f(self):\n        return 2\n')
    result = function_body_candidates({'files': {str(source): {
        'missing_lines': [3, 6], 'executed_lines': [1, 2, 4, 5]}}})
    assert [row['function'] for row in result] == ['First.f', 'Second.f']


def test_source_change_during_report_generation_invalidates_candidates(tmp_path, monkeypatch):
    import coverage
    source = tmp_path / 'src'
    source.mkdir()
    native = source / 'native.py'
    native.write_text('def old():\n    return 1\n')
    directory = tmp_path / 'coverage'
    prepare(directory, [source], [], {}, selection=['case'], parallel_jobs=8, e2e_reruns=0)

    class ControlledCoverage:
        def __init__(self, **kwargs): pass
        def set_option(self, *args): pass
        def load(self): pass
        def combine(self, **kwargs): pass
        def save(self): pass
        def json_report(self, *, outfile):
            Path(outfile).write_text(json.dumps({'files': {str(native): {
                'executed_lines': [1], 'missing_lines': [2]}}}))
        def html_report(self, **kwargs):
            native.write_text('def new():\n    return 2\n')
    monkeypatch.setattr(coverage, 'Coverage', ControlledCoverage)
    receipt = finalize(directory, selection=['case'], parallel_jobs=8, e2e_reruns=0, complete=True)
    assert receipt['status'] == 'incomplete'
    assert receipt['reason'] == 'source_identity_changed'
    assert receipt['report'] is None
    assert receipt['function_body_candidates'] == []
