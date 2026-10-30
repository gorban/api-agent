"""Tracing tests can be inventoried before the optional tracing SDK is installed."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
FILES = ('tests/unit/test_trace_array_types.py', 'tests/unit/test_workflow_observations.py',
         'tests/unit/test_local_trace_failure.py')


def test_trace_suite_collection_does_not_import_sdk():
    script = '''
import importlib.abc, sys
import pytest
class BlockTrace(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'opentelemetry' or fullname.startswith('opentelemetry.'):
            raise ModuleNotFoundError('tracing SDK requested during collection')
sys.meta_path.insert(0, BlockTrace())
try:
    import opentelemetry
except ModuleNotFoundError as error:
    assert 'tracing SDK requested' in str(error)
else:
    raise AssertionError('tracing import blocker failed')
raise SystemExit(pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider',
                             '-o', 'addopts=', *sys.argv[1:]]))
'''
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', script, *FILES], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'test_serialized_arrays_respect_content_policy' in result.stdout
    assert 'test_independent_projections_and_real_serialized_ancestry' in result.stdout
    assert 'test_failed_local_request_preserves_selection' in result.stdout
