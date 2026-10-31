"""Verify launcher environment isolation across actual pytest processes."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('isolation_enabled', [True, False])
def test_inherited_mise_is_isolated_and_explicit_opt_in_still_works(tmp_path, isolation_enabled):
    root = Path(__file__).resolve().parents[2]
    unit = tmp_path / 'unit'
    unit.mkdir()
    conftest = (root / 'tests/unit/conftest.py').read_text()
    if not isolation_enabled:
        fixture = '''@pytest.fixture(autouse=True)
def isolated_managed_tools(monkeypatch):
    """Only tests that explicitly opt in may prepare managed tools."""
    monkeypatch.delenv('API_AGENT_MISE', raising=False)


'''
        assert conftest.count(fixture) == 1
        conftest = conftest.replace(fixture, '')
    (unit / 'conftest.py').write_text(conftest)
    (unit / 'test_inherited.py').write_text('''\
import os
from api_agent.runtime import mise_tools

def test_inherited_marker_does_not_prepare_tools(tmp_path, monkeypatch):
    marker = os.environ.get('API_AGENT_MISE')
    assert marker is None
    monkeypatch.setattr(mise_tools, 'ROOT', tmp_path)
    (tmp_path / 'mise.toml').write_text('[tools]\\n"core:node" = "1"\\n')
    def unexpected(*args, **kwargs):
        raise AssertionError('inherited marker started managed tool preparation')
    monkeypatch.setattr(mise_tools.shutil, 'which', unexpected)
    mise_tools.ensure_tools('node')
''')
    env = os.environ.copy()
    env['API_AGENT_MISE'] = '1'
    sentinel = 'synthetic-unit-environment-sentinel-706436'
    env['UNIT_ISOLATION_SENTINEL'] = sentinel
    env.pop('PYTEST_ADDOPTS', None)
    env.pop('PYTEST_CURRENT_TEST', None)
    command = [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-o', 'addopts=',
               str(unit / 'test_inherited.py'),
               str(root / 'tests/unit/test_mise_tools.py') + '::test_only_selected_pinned_tools_are_prepared']
    result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True, timeout=60)
    output_is_safe = sentinel not in result.stdout and sentinel not in result.stderr
    assert output_is_safe
    assert result.returncode == (0 if isolation_enabled else 1)
    expected_summary = ('2 passed' if isolation_enabled else '1 failed, 1 passed') in result.stdout
    assert expected_summary
