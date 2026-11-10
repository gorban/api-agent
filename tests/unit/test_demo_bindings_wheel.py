"""The checked-in compiler input must be the current, minimal bindings wheel."""
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
WHEEL = ROOT / 'demo/.private_resources/wheels/api_agent_demo_bindings-0.1.0-py3-none-any.whl'


def test_checked_in_demo_bindings_wheel_is_fresh(tmp_path):
    subprocess.run([sys.executable, str(ROOT / 'scripts/build_demo_bindings_wheel.py'),
                    '--output', str(tmp_path)], cwd=ROOT, check=True)

    assert WHEEL.read_bytes() == (tmp_path / WHEEL.name).read_bytes()


def test_wheel_contains_only_current_binding_and_python_metadata():
    from zipfile import ZipFile

    with ZipFile(WHEEL) as wheel:
        assert set(wheel.namelist()) == {
            'api_agent/demo/simple/functions.py',
            'api_agent_demo_bindings-0.1.0.dist-info/METADATA',
            'api_agent_demo_bindings-0.1.0.dist-info/WHEEL',
            'api_agent_demo_bindings-0.1.0.dist-info/RECORD',
        }
        assert wheel.read('api_agent/demo/simple/functions.py') == (ROOT / 'demo/simple/functions.py').read_bytes()
        assert b'Requires-Python: >=3.13' in wheel.read('api_agent_demo_bindings-0.1.0.dist-info/METADATA')


def test_compiled_plan_imports_the_installed_binding(tmp_path):
    import os
    import shutil
    from zipfile import ZipFile

    site = tmp_path / 'site'
    shutil.copytree(ROOT / 'src/api_agent', site / 'api_agent')
    with ZipFile(WHEEL) as wheel:
        wheel.extractall(site)
    script = '''\
from pathlib import Path
from api_agent.workflows.artifact import load_artifact
from api_agent.workflows.definition import load_definition
import api_agent.demo.simple.functions as binding
root = Path(ROOT)
source = load_definition(root / 'flow.md.html', base_root=root, pack_root=root, entry_root='base')
compiled = load_artifact((root / 'qualification/simple_chat.json').read_bytes())
assert source.definition_id == compiled.definition_id
assert Path(binding.__file__).resolve() == Path(SITE) / 'api_agent/demo/simple/functions.py'
assert {row['module'] for row in compiled.bindings} == {'api_agent.demo.simple.functions'}
'''
    env = dict(os.environ, PYTHONPATH=str(site))
    subprocess.run([sys.executable, '-c', f'ROOT={str(ROOT)!r}; SITE={str(site)!r}\n' + script],
                   cwd=tmp_path, env=env, check=True)
