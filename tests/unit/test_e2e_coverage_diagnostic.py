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
    assert [item['function'] for item in receipt['function_body_candidates']] == ['uncalled_native']
    assert (directory / 'html/index.html').is_file()


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
