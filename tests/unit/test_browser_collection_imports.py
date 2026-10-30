"""Browser test identities remain discoverable before browser preparation."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
FILES = tuple(f'tests/unit/test_gpt_{name}.py' for name in (
    'login_alternatives', 'account_choice', 'direct_authorization',
    'passkey_browser', 'passkey_navigation',
))
COLLECT = r'''
import importlib.abc, json, os, sys
import pytest

class BlockBrowser(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'playwright' or fullname.startswith('playwright.'):
            raise ModuleNotFoundError('unexpected browser import: ' + fullname)

if os.environ.get('BLOCK_BROWSER'):
    sys.meta_path.insert(0, BlockBrowser())

class Capture:
    def pytest_collection_finish(self, session):
        with open(sys.argv[1], 'w', encoding='utf-8') as output:
            json.dump([item.nodeid for item in session.items], output)

sys.exit(pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider',
                      '-o', 'addopts=', *sys.argv[2:]], plugins=[Capture()]))
'''


def test_browser_test_collection_does_not_import_playwright(tmp_path):
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
    identities = []
    for blocked in (False, True):
        result_path = tmp_path / f'{blocked}.json'
        child_env = dict(env)
        if blocked:
            child_env['BLOCK_BROWSER'] = '1'
        else:
            child_env.pop('BLOCK_BROWSER', None)
        result = subprocess.run([sys.executable, '-c', COLLECT, str(result_path), *FILES],
                                cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        identities.append(json.loads(result_path.read_text()))
    assert identities[0]
    assert identities[0] == identities[1]


@pytest.mark.parametrize(('module', 'fixture'), [
    ('test_gpt_login_alternatives', 'alternative_page'),
    ('test_gpt_account_choice', 'choice_pages'),
    ('test_gpt_direct_authorization', 'authorization_page'),
])
def test_browser_fixture_imports_driver_when_used(monkeypatch, module, fixture):
    class DriverRequested(Exception):
        pass

    browser = ModuleType('playwright')
    api = ModuleType('playwright.async_api')
    def request_driver():
        raise DriverRequested
    api.async_playwright = request_driver
    monkeypatch.setitem(sys.modules, 'playwright', browser)
    monkeypatch.setitem(sys.modules, 'playwright.async_api', api)
    use = getattr(importlib.import_module(module), fixture).__wrapped__()
    with pytest.raises(DriverRequested):
        asyncio.run(use(lambda *_: None))
