"""Async fixture scope is explicit rather than a repeated startup warning."""
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_project_async_fixture_scope_has_no_deprecation_warning(tmp_path):
    test = tmp_path / 'test_example.py'
    test.write_text('import pytest\n@pytest.mark.asyncio\nasync def test_example():\n    pass\n')
    result = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q',
        '-p', 'pytest_asyncio.plugin', '-p', 'no:cacheprovider', '-o', 'addopts=',
        '-W', 'error::pytest.PytestDeprecationWarning', '-c', str(ROOT / 'pyproject.toml'),
        str(test)], cwd=tmp_path, env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert '1 test collected' in result.stdout
    assert 'asyncio_default_fixture_loop_scope' not in result.stderr + result.stdout
