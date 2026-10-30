"""Collection proof with external pytest plugins intentionally disabled."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
FILES = ('tests/unit/test_bedrock_completions_shape.py',
         'tests/unit/test_bedrock_responses_tool_shape.py')
SCRIPT = r'''
import importlib.abc, json, os, sys
import pytest

class BlockSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'openai' or fullname.startswith('openai.'):
            raise ModuleNotFoundError('blocked SDK import: ' + fullname)

if os.environ.get('BLOCK_SDK'):
    assert not any(name == 'openai' or name.startswith('openai.') for name in sys.modules)
    sys.meta_path.insert(0, BlockSDK())

class Capture:
    def pytest_collection_finish(self, session):
        with open(sys.argv[1], 'w', encoding='utf-8') as result:
            json.dump([item.nodeid for item in session.items], result)

sys.exit(pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider',
                      '-o', 'addopts=', '--rootdir', sys.argv[2], *sys.argv[3:]], plugins=[Capture()]))
'''
HELPERS = r'''
import importlib.abc, runpy, sys
class BlockSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'openai' or fullname.startswith('openai.'):
            raise ModuleNotFoundError('blocked SDK import: ' + fullname)
sys.meta_path.insert(0, BlockSDK())
for file, helper in zip(sys.argv[1::2], sys.argv[2::2]):
    namespace = runpy.run_path(file)
    try:
        namespace[helper]()
    except ModuleNotFoundError as error:
        assert str(error) == 'blocked SDK import: openai'
    else:
        raise AssertionError(f'{helper} did not import the SDK')
'''


def test_sdk_free_collection_preserves_identities(tmp_path):
    # Isolate this proof from unrelated installed pytest plugins, not application imports.
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    nodes = []
    for blocked in (False, True):
        result = tmp_path / f'{blocked}.json'
        child_env = dict(env)
        if blocked:
            child_env['BLOCK_SDK'] = '1'
        else:
            child_env.pop('BLOCK_SDK', None)
        completed = subprocess.run([sys.executable, '-c', SCRIPT, str(result), str(ROOT), *FILES],
                                   cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=60)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        nodes.append(json.loads(result.read_text()))
    assert all(any(node.startswith(file + '::') for node in nodes[0]) for file in FILES)
    assert nodes[0] == nodes[1]

    eager = tmp_path / 'test_eager.py'
    eager.write_text('import openai\ndef test_eager(): pass\n')
    completed = subprocess.run([sys.executable, '-c', SCRIPT, str(tmp_path / 'eager.json'),
                                str(ROOT), str(eager)], cwd=ROOT,
                               env=dict(env, BLOCK_SDK='1'), capture_output=True, text=True, timeout=60)
    assert completed.returncode != 0
    assert 'blocked SDK import: openai' in completed.stdout + completed.stderr


def test_sdk_helpers_import_sdk_when_invoked():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', HELPERS,
                             FILES[0], 'sdk_completion', FILES[1], 'sdk_shape'],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
