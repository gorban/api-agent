"""Offline installed-wheel proof of the existing file-provider consumer.

The declared dev dependencies build the wheel without isolation or downloads.
API_AGENT_TEST_WHEEL optionally selects a freshly built local wheel. The consumer installs no runtime dependencies because this
loader/configuration surface uses only the standard library. This does not
exercise the application Runtime or dependency preparation.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from api_agent.providers.progress import report_timeout_sync


PROVIDER = '''
import asyncio
from .local import answer
from api_agent.providers.contracts import APIResult, ProviderCapabilities

class Adapter:
    version = 1
    capabilities = ProviderCapabilities('api', ('invoke',))
    def __init__(self, config, services):
        self.config, self.events = config, services['events']
        self.events.append('factory')
    async def astart(self):
        await asyncio.sleep(0)
        self.events.append('start')
    async def ainvoke(self, request, *, auth=None):
        self.events.append('invoke')
        if request.payload == 'fail':
            raise ValueError('synthetic invocation failure')
        return APIResult(answer(request.payload, self.config['SYNTHETIC_VALUE']))
    async def aclose(self):
        await asyncio.sleep(0)
        self.events.append('disposed')

def factory(config, services):
    return Adapter(config, services)
'''

CONSUMER = '''
import asyncio
import importlib.metadata
from pathlib import Path
import sys
import api_agent
from api_agent.providers import loading
from api_agent.config.resolve import resolve_config
from api_agent.providers.contracts import APIRequest
from api_agent.providers.distribution import inspect_relocated_pack, selected_manifests

installed, pack, original, checkout, mode = sys.argv[1:]
installed, pack, original, checkout = map(Path, (installed, pack, original, checkout))
assert not original.exists()
assert not Path.cwd().is_relative_to(checkout)
assert all(not Path(p).resolve().is_relative_to(checkout) for p in sys.path if p)
distribution = importlib.metadata.distribution('api-agent')
assert distribution.version == '0.1.0'
assert Path(distribution.locate_file('')).resolve().is_relative_to(installed)
for module in (api_agent, loading):
    assert Path(module.__file__).resolve().is_relative_to(installed)
base = Path(api_agent.__file__).parent
context, schema = inspect_relocated_pack(base, pack)
assert context.owner_root == pack
assert context.production_paths == (pack / 'src',)
assert schema.fields['SYNTHETIC_VALUE'].default == 'default'
resolved = resolve_config(schema, argv=['--synthetic-value=explicit'],
                          environ={'SYNTHETIC_VALUE': 'environment'},
                          dotenv={'SYNTHETIC_VALUE': 'saved'})
assert resolved.values['SYNTHETIC_VALUE'] == 'explicit'
assert resolved.fields['SYNTHETIC_VALUE'].source == 'cli'
assert not any(name.startswith('_api_agent_provider_') for name in sys.modules)
values = {'API_PROVIDER': 'src/factory.py:factory', 'UI_PROVIDER': 'inactive/factory.py:factory'}
manifests = selected_manifests(values, owner_root=pack, active_kinds={'api'})
assert len(manifests) == 1
assert manifests[0].root == pack / 'src'
assert manifests[0].requirements() == ('synthetic-client>=1',)
assert manifests[0].select_groups('visual').requirements() == ('synthetic-client>=1', 'synthetic-visual>=2')

async def exercise():
    events = []
    location = values['API_PROVIDER']
    if mode == 'invalid_factory':
        location = 'src/factory.py:missing'
    if mode == 'invalid_capability':
        location = 'src/invalid.py:factory'
    before = set(sys.modules)
    if mode in ('invalid_factory', 'invalid_capability'):
        try:
            await loading.aload_provider(location, resolved.values,
                                         {'owner_root': pack, 'events': events}, kind='api')
        except loading.ProviderError as error:
            assert str(error) == 'provider_initialization_failed'
        else:
            raise AssertionError('invalid provider accepted')
        assert events == ([] if mode == 'invalid_factory' else ['factory', 'disposed'])
        assert not any(name.startswith('_api_agent_provider_') for name in set(sys.modules) - before)
        return
    owner = await loading.aload_provider(location, resolved.values,
                                        {'owner_root': pack, 'events': events}, kind='api')
    prefix = owner.module_name
    assert prefix + '.local' in sys.modules
    assert Path(sys.modules[prefix + '.local'].__file__).resolve() == pack / 'src/local.py'
    await owner.astart()
    try:
        result = await owner.require('api', 'invoke').ainvoke(APIRequest('fail' if mode == 'invocation_failure' else 'request'))
        assert mode == 'success'
        assert result.payload == 'request:explicit'
    except ValueError as error:
        assert mode == 'invocation_failure'
        assert str(error) == 'synthetic invocation failure'
    finally:
        await owner.aclose()
    assert events == ['factory', 'start', 'invoke', 'disposed']
    assert owner.closed and owner.disposed
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules)
    try:
        owner.require('api', 'invoke')
    except loading.ProviderError as error:
        assert str(error) == 'provider_closed'
    else:
        raise AssertionError('closed consumer accepted')
    try:
        await owner.astart()
    except loading.ProviderError as error:
        assert str(error) == 'provider_closed'
    else:
        raise AssertionError('closed startup accepted')
    await owner.aclose()
    assert events.count('disposed') == 1

asyncio.run(exercise())
assert not (pack / 'inactive-ran').exists()
print('installed lifecycle verified:' + mode)
'''


def run(command, *, cwd):
    """Run bounded subprocesses with fixture-owned home/cache/temp directories."""
    home, cache, temporary = (cwd / name for name in ('process-home', 'process-cache', 'process-temp'))
    for directory in (home, cache, temporary):
        directory.mkdir(exist_ok=True)
    environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}
    environment.update({
        'PATH': os.defpath,
        'HOME': str(home), 'USERPROFILE': str(home),
        'APPDATA': str(home), 'LOCALAPPDATA': str(cache),
        'XDG_CONFIG_HOME': str(home), 'XDG_DATA_HOME': str(home),
        'XDG_CACHE_HOME': str(cache),
        'TMPDIR': str(temporary), 'TEMP': str(temporary), 'TMP': str(temporary),
        'PIP_CONFIG_FILE': os.devnull, 'PIP_NO_INDEX': '1',
        'PIP_NO_DEPS': '1', 'PIP_NO_CACHE_DIR': '1',
        'PIP_DISABLE_PIP_VERSION_CHECK': '1',
    })
    with report_timeout_sync('Installed provider proof subprocess', 60) as budget:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                timeout=budget.remaining, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture(scope='module')
def installed_wheel(tmp_path_factory):
    repository = Path(__file__).resolve().parents[2]
    temporary = tmp_path_factory.mktemp('installed-provider')
    wheel_setting = os.environ.get('API_AGENT_TEST_WHEEL')
    if wheel_setting:
        wheel = Path(wheel_setting).resolve()
        assert wheel.is_file(), 'API_AGENT_TEST_WHEEL must name a local wheel'
    else:
        output = temporary / 'wheels'
        run([sys.executable, '-I', '-m', 'build', '--no-isolation', '--wheel',
             '--outdir', str(output), str(repository)], cwd=temporary)
        wheel, = output.glob('*.whl')
    environment = temporary / 'consumer-env'
    run([sys.executable, '-I', '-m', 'venv', str(environment)], cwd=temporary)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    run([str(python), '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-cache-dir', str(wheel)], cwd=temporary)
    return python, environment, repository, wheel


@pytest.fixture
def relocated_pack(tmp_path):
    original = tmp_path / 'original-pack'
    for child in ('cfg', 'src', 'inactive'):
        (original / child).mkdir(parents=True)
    (original / 'pack.toml').write_text(
        'version = 1\nid = "synthetic"\nowner = "."\nconfig = "cfg/config.toml"\nproduction = ["src"]\n')
    (original / 'cfg/config.toml').write_text(
        'version = 1\n[fields.SYNTHETIC_VALUE]\ntype = "str"\ndefault = "default"\n'
        '[fields.API_PROVIDER]\ntype = "str"\noverride = true\ndefault = "src/factory.py:factory"\n'
        '[fields.UI_PROVIDER]\ntype = "str"\noverride = true\ndefault = "inactive/factory.py:factory"\n')
    (original / 'src/provider-dependencies.toml').write_text(
        'version = 1\ndependencies = ["synthetic-client>=1"]\n[groups]\nvisual = ["synthetic-visual>=2"]\n')
    (original / 'inactive/provider-dependencies.toml').write_text(
        'version = 1\ndependencies = ["unrelated-client>=9"]\n')
    (original / 'src/factory.py').write_text(PROVIDER)
    (original / 'src/invalid.py').write_text(PROVIDER.replace("('invoke',)", "('unsupported',)"))
    (original / 'src/local.py').write_text('def answer(payload, value):\n    return payload + ":" + value\n')
    (original / 'inactive/factory.py').write_text(
        'from pathlib import Path\nPath(__file__).parents[1].joinpath("inactive-ran").touch()\n'
        'raise RuntimeError("inactive provider executed")\n')
    relocated = tmp_path / 'relocated-pack'
    shutil.copytree(original, relocated)
    shutil.rmtree(original)
    consumer_cwd = tmp_path / 'unrelated-consumer'
    consumer_cwd.mkdir()
    return relocated, original, consumer_cwd


@pytest.mark.parametrize('mode', ['success', 'invocation_failure', 'invalid_factory', 'invalid_capability'])
def test_installed_provider_lifecycle(installed_wheel, relocated_pack, mode):
    python, environment, repository, _ = installed_wheel
    pack, original, consumer_cwd = relocated_pack

    output = run([str(python), '-I', '-c', CONSUMER, str(environment), str(pack),
                  str(original), str(repository), mode], cwd=consumer_cwd)

    assert output.strip() == 'installed lifecycle verified:' + mode


def test_hostile_installer_environment_cannot_redirect_installation(installed_wheel, tmp_path, monkeypatch):
    _, _, repository, wheel = installed_wheel
    redirected = tmp_path / 'redirected-install'
    config = tmp_path / 'host-pip.conf'
    config.write_text('[global]\ntarget = ' + str(redirected) + '\n')
    monkeypatch.setenv('PIP_TARGET', str(redirected))
    monkeypatch.setenv('PIP_PREFIX', str(redirected))
    monkeypatch.setenv('PIP_CONFIG_FILE', str(config))
    monkeypatch.setenv('PYTHONPATH', str(repository / 'src'))
    monkeypatch.setenv('UV_CACHE_DIR', str(redirected))
    environment = tmp_path / 'isolated-env'
    run([sys.executable, '-I', '-m', 'venv', str(environment)], cwd=tmp_path)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')

    run([str(python), '-I', '-m', 'pip', 'install', '--no-index', '--no-deps',
         '--no-cache-dir', str(wheel)], cwd=tmp_path)
    output = run([str(python), '-I', '-c',
                  'import api_agent, importlib.metadata, os, sys; from pathlib import Path; '
                  'root = Path(sys.argv[1]); '
                  'assert Path(api_agent.__file__).is_relative_to(root); '
                  'assert Path(importlib.metadata.distribution("api-agent").locate_file("")).is_relative_to(root); '
                  'assert "PYTHONPATH" not in os.environ; '
                  'assert "PIP_TARGET" not in os.environ; '
                  'assert "UV_CACHE_DIR" not in os.environ; '
                  'print("isolated installation verified")', str(environment)], cwd=tmp_path)

    assert output.strip() == 'isolated installation verified'
    assert not redirected.exists()
