"""The shell launcher supplies tools and preserves the existing CLI boundary."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'checkout space'
    runtime = root / 'src/api_agent/runtime'
    runtime.mkdir(parents=True)
    shutil.copyfile(ROOT / 'launch.sh', root / 'launch.sh')
    shutil.copyfile(ROOT / 'src/api_agent/runtime/entry.py', runtime / 'entry.py')
    (runtime.parent / '__init__.py').write_text('')
    (runtime / '__init__.py').write_text('')
    (runtime / 'bootstrap.py').write_text('''import json, os, sys
def main(*, base_root, _replace_process):
    print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'root': str(base_root),
                      'replace': _replace_process, 'caller_env': os.environ.get('API_AGENT_CALLER_CWD'),
                      'pwd': os.environ.get('PWD')}), flush=True)
    return 37
''')
    tools = tmp_path / 'tools'
    tools.mkdir()
    fake = tools / 'mise'
    fake.write_text('''#!/bin/sh
[ "$1" = --version ] && { printf '%s\\n' 'mise test'; exit 0; }
[ "$1" = --cd ] || exit 90
cd "$2" || exit 93
while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done
[ "$#" -gt 0 ] || exit 91
shift
[ "$1" = python ] || exit 92
shift
exec "$FAKE_PYTHON" "$@"
''')
    fake.chmod(0o755)
    return root, tools


def test_shell_preserves_arguments_cwd_and_status(checkout, tmp_path):
    root, tools = checkout
    caller = tmp_path / 'caller space'
    caller.mkdir()
    args = ['with space', 'line\nbreak', '', '--flag=value']
    env = dict(os.environ, PATH=str(tools) + os.pathsep + '/usr/bin:/bin', FAKE_PYTHON=sys.executable)
    result = subprocess.run(['/bin/sh', str(root / 'launch.sh'), *args], cwd=caller, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 37, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == {'argv': args, 'cwd': str(caller), 'root': str(root),
                       'replace': True, 'caller_env': None, 'pwd': str(caller)}


def test_symlink_launcher_resolves_checkout_but_preserves_caller(checkout, tmp_path):
    root, tools = checkout
    alias = tmp_path / 'alias'
    alias.symlink_to(root / 'launch.sh')
    env = dict(os.environ, PATH=str(tools) + os.pathsep + '/usr/bin:/bin', FAKE_PYTHON=sys.executable)
    result = subprocess.run(['/bin/sh', str(alias), 'from alias'], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 37, result.stderr
    receipt = json.loads(result.stdout)
    assert (receipt['root'], receipt['cwd'], receipt['argv']) == (str(root), str(tmp_path), ['from alias'])


def test_missing_mise_reports_actionable_error(checkout, tmp_path):
    root, _ = checkout
    result = subprocess.run(['/bin/sh', str(root / 'launch.sh')], cwd=tmp_path,
                            env={'PATH': ''}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert 'mise is required' in result.stderr and 'installing-mise' in result.stderr


def test_broken_mise_reports_actionable_error_before_launch(checkout, tmp_path):
    root, tools = checkout
    (tools / 'mise').write_text('#!/bin/sh\nexit 1\n')
    result = subprocess.run(['/bin/sh', str(root / 'launch.sh')], cwd=tmp_path,
                            env={'PATH': str(tools)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert 'mise could not run' in result.stderr and 'installing-mise' in result.stderr
