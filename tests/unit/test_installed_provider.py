"""Offline installed-wheel proof of the existing file-provider consumer.

The declared dev dependencies build the wheel without isolation or downloads.
API_AGENT_TEST_WHEEL optionally selects a freshly built local wheel. The consumer installs no runtime dependencies because this
loader/configuration surface uses only the standard library. This does not
exercise the application Runtime or dependency preparation.
"""
from hashlib import sha256
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile

import pytest

from api_agent.providers.progress import report_timeout_sync


PROVIDER = '''
from pathlib import Path
Path(__file__).parents[1].joinpath('active-import-ran').touch()
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
    Path(__file__).parents[1].joinpath('active-factory-ran').touch()
    return Adapter(config, services)
'''

CONSUMER = '''
import asyncio
import importlib.metadata
from pathlib import Path
import sys
import sysconfig
import api_agent
from api_agent.providers import loading
from api_agent.config.resolve import resolve_config
from api_agent.providers.contracts import APIRequest
from api_agent.providers.distribution import inspect_relocated_pack, selected_manifests

installed, pack, original, checkout, mode = sys.argv[1:]
installed, pack, original, checkout = map(Path, (installed, pack, original, checkout))
assert not original.exists()
assert not Path.cwd().is_relative_to(checkout)
stdlib = Path(sysconfig.get_path('stdlib')).resolve()
interpreter_paths = {stdlib, stdlib / 'lib-dynload',
                     stdlib.parent / ('python%d%d.zip' % sys.version_info[:2])}
assert all(not Path(p).resolve().is_relative_to(checkout) or Path(p).resolve() in interpreter_paths
           for p in sys.path if p), 'unsafe checkout path'
distribution = importlib.metadata.distribution('api-agent')
assert distribution.version == '0.1.0'
assert Path(distribution.locate_file('')).resolve().is_relative_to(installed)
def assert_installed_modules():
    for name, module in tuple(sys.modules.items()):
        if name == 'api_agent' or name.startswith('api_agent.'):
            assert module is not None and getattr(module, '__file__', None), name
            assert Path(module.__file__).resolve().is_relative_to(installed), name
assert_installed_modules()
base = Path(api_agent.__file__).parent
def assert_inert_discovery(stage):
    assert not (pack / 'active-factory-ran').exists(), stage + ': active factory executed during discovery'
    assert not (pack / 'active-import-ran').exists(), stage + ': active module executed during discovery'

context, schema = inspect_relocated_pack(base, pack)
assert_inert_discovery('inspect_relocated_pack')
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
assert_inert_discovery('selected_manifests')
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
assert_installed_modules()
assert (pack / 'active-import-ran').is_file()
assert (pack / 'active-factory-ran').is_file() == (mode != 'invalid_factory')
assert not (pack / 'inactive-ran').exists()
print('installed lifecycle verified:' + mode)
'''


PREMATURE_EXECUTION = '''
def discovery_with_early_execution(original_discovery, action, *args, **kwargs):
    result = original_discovery(*args, **kwargs)
    events = []
    # The missing factory imports its module but cannot invoke the factory.
    target = 'missing' if action == 'module' else 'factory'
    try:
        owner = loading.load_provider('src/factory.py:' + target, {},
                                      {'owner_root': pack, 'events': events}, kind='api')
    except loading.ProviderError as error:
        assert action == 'module'
        assert str(error) == 'provider_initialization_failed'
    else:
        assert action == 'factory'
        asyncio.run(owner.aclose())
    assert events == {'module': [], 'factory': ['factory', 'disposed']}[action]
    assert (pack / 'active-import-ran').is_file()
    assert (pack / 'active-factory-ran').is_file() == (action == 'factory')
    assert not any(name.startswith('_api_agent_provider_') for name in sys.modules)
    return result
'''


def eager_discovery_consumer(stage, action):
    """Simulate a completed eager load with no residual modules at either boundary."""
    anchor = {'inspect_relocated_pack': 'context, schema = inspect_relocated_pack(base, pack)',
              'selected_manifests': "manifests = selected_manifests(values, owner_root=pack, active_kinds={'api'})"}[stage]
    wrapper = (PREMATURE_EXECUTION + '\noriginal_discovery = ' + stage + '\n' + stage
               + ' = lambda *args, **kwargs: discovery_with_early_execution(original_discovery, '
               + repr(action) + ', *args, **kwargs)\n')
    assert CONSUMER.count(anchor) == 1
    return CONSUMER.replace(anchor, wrapper + anchor, 1)


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


def _synthetic_git_environment():
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_SYSTEM=os.devnull, GIT_AUTHOR_NAME='Test Author',
                       GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_NAME='Test Author',
                       GIT_COMMITTER_EMAIL='test@example.invalid')
    return environment


def _wheel_source_snapshot(repository, destination):
    """Capture finite current wheel inputs without unrelated repository drafts."""
    helper = runpy.run_path(str(repository / 'tools/frontend_assets.py'))
    build = runpy.run_path(str(repository / 'hatch_build.py'))
    build['_validate_frontend_assets'](repository)
    listed = subprocess.run(['git', '-C', str(repository), 'ls-files', '--stage', '-z'],
                            check=True, capture_output=True, timeout=20,
                            env=_synthetic_git_environment()).stdout
    assert len(listed) <= 4 * 1024 * 1024, 'Installed wheel index list exceeds limit'
    selected = set()
    observed = {}
    configuration = tomllib.loads(helper['_read'](repository, 'pyproject.toml').decode())
    wheel = configuration['tool']['hatch']['build']['targets']['wheel']
    roots = set(wheel['packages'])
    roots.update(path for path in wheel.get('force-include', {}) if (repository / path).is_dir())
    owners = helper['owner_roots'](repository)
    roots.update(owner for owner in owners if owner != '.')
    for root in roots:
        helper['_relative'](root)
    prefixes = tuple(path + '/' for path in sorted(roots))
    individual = {'pyproject.toml', 'hatch_build.py', 'package.json', 'package-lock.json',
                  '.npmrc', 'launch.sh', 'tools/frontend-assets.json',
                  'tools/frontend_assets.py', 'tools/prepare_frontend_assets.mjs',
                  'tools/checks.py', 'tools/pre_commit_dispatcher.py',
                  'tools/workflow_views.py', 'LICENSE', 'README.md', 'flow.md.html'}
    individual.update(path for target in ('wheel', 'sdist')
                      for path in configuration['tool']['hatch']['build']['targets'][target].get('force-include', {})
                      if (repository / path).is_file())

    def copy(relative, mode):
        helper['_relative'](relative)
        data = helper['_read'](repository, relative, 8 * 1024 * 1024)
        source_mode = (repository / relative).lstat().st_mode & 0o777
        assert bool(source_mode & 0o111) == bool(mode & 0o111), 'Installed wheel source mode changed'
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
        observed[relative] = (len(data), sha256(data).digest(), source_mode)

    for row in listed.split(b'\0'):
        if not row:
            continue
        metadata, raw_path = row.split(b'\t', 1)
        mode, _, stage = metadata.split()
        assert mode in (b'100644', b'100755') and stage == b'0'
        relative = raw_path.decode('utf-8')
        if relative not in individual and not relative.startswith(prefixes):
            continue
        if (relative in ('src/api_agent/runtime/viewer_assets/flow-modal.js',
                         'src/api_agent/runtime/viewer_assets/flow-modal.css')
                and not (repository / relative).exists()):
            # The shared asset move is represented as exact tracked deletions
            # until the feature commit; the synthetic snapshot models its tree.
            continue
        assert relative not in selected
        selected.add(relative)
        assert len(selected) <= 10000, 'Installed wheel source list exceeds limit'
        copy(relative, int(mode, 8) & 0o777)
    # A feature must pass before its implementation can be committed. Capture
    # new public inputs only inside the build's explicitly declared roots;
    # hidden/cache files and unrelated documentation remain outside this proof.
    current = subprocess.run(['git', '-C', str(repository), 'ls-files', '--others',
                              '--exclude-standard', '-z', '--', *sorted(roots | individual)],
                             check=True, capture_output=True, timeout=20,
                             env=_synthetic_git_environment()).stdout
    assert len(current) <= 4 * 1024 * 1024, 'Installed wheel new-input list exceeds limit'
    for raw in current.split(b'\0'):
        if not raw:
            continue
        relative = raw.decode('utf-8')
        helper['_relative'](relative)
        if relative in selected or (relative not in individual and not relative.startswith(prefixes)):
            continue
        if any(part in helper['_EXCLUDED_OWNERS'] for part in Path(relative).parts):
            continue
        if relative not in individual and any(part.startswith('.') for part in Path(relative).parts) and not build['_package_path'](
                relative, owners, helper):
            continue
        selected.add(relative)
        assert len(selected) <= 10000, 'Installed wheel source list exceeds limit'
        copy(relative, 0o755 if (repository / relative).lstat().st_mode & 0o111 else 0o644)
    assert {'pyproject.toml', 'hatch_build.py', 'tools/frontend-assets.json',
            'src/api_agent/__init__.py'} <= selected
    manifest = helper['_json'](helper['_read'](repository, 'tools/frontend-assets.json'))
    for relative, _ in helper['expanded_assets'](repository, manifest['assets']):
        if relative not in selected:
            copy(relative, 0o644)
    # These two first-party modal assets are fixed package inputs. During an
    # uncommitted feature checkout their owner copies have not reached the
    # index yet; include only these exact paths in the synthetic wheel source.
    for name in ('flow-modal.js', 'flow-modal.css'):
        canonical = helper['_read'](repository, '.private_resources/viewer/' + name)
        for owner in owners:
            relative = ('' if owner == '.' else owner + '/') + '.private_resources/viewer/' + name
            assert helper['_read'](repository, relative) == canonical
            if relative not in selected:
                selected.add(relative)
                copy(relative, 0o644)
    for relative, expected in observed.items():
        current = helper['_read'](repository, relative, 8 * 1024 * 1024)
        assert (len(current), sha256(current).digest(),
                (repository / relative).lstat().st_mode & 0o777) == expected, (
                    'Installed wheel source changed while copying')
    build['_validate_frontend_assets'](repository)

    for arguments in (('init', '-q', '-b', 'synthetic'),
                      ('add', '-f', '--', *sorted(selected)),
                      ('-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
                       'commit', '-qm', 'Synthetic installed-wheel test snapshot')):
        subprocess.run(['git', '-C', str(destination), *arguments], check=True,
                       capture_output=True, timeout=20, env=_synthetic_git_environment())


def _git_owned_checkout(repository):
    result = subprocess.run(['git', '-C', str(repository), 'rev-parse', '--show-toplevel'],
                            capture_output=True, text=True, timeout=20,
                            env=_synthetic_git_environment())
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == repository.resolve()


def test_wheel_snapshot_ignores_dirty_prd_and_packages_current_runtime_bytes(tmp_path, monkeypatch):
    repository = Path(__file__).resolve().parents[2]
    first = tmp_path / 'first'
    first.mkdir()
    foreign = tmp_path / 'foreign-git'
    foreign.mkdir()
    subprocess.run(['git', '-C', str(foreign), 'init', '-q'], check=True,
                   capture_output=True, timeout=20, env=_synthetic_git_environment())
    empty_index = tmp_path / 'empty-index'
    foreign_environment = _synthetic_git_environment()
    foreign_environment['GIT_INDEX_FILE'] = str(empty_index)
    subprocess.run(['git', '-C', str(foreign), 'read-tree', '--empty'], check=True,
                   capture_output=True, timeout=20, env=foreign_environment)
    with monkeypatch.context() as hostile:
        hostile.setenv('GIT_INDEX_FILE', str(empty_index))
        hostile.setenv('GIT_DIR', str(foreign / '.git'))
        hostile.setenv('GIT_WORK_TREE', str(foreign))
        assert _git_owned_checkout(repository) is True
        _wheel_source_snapshot(repository, first)
    unrelated = first / 'docs/prds/review-notes.md'
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('Committed review note.\n')
    for arguments in (('add', 'docs/prds/review-notes.md'),
                      ('-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
                       'commit', '-qm', 'Synthetic unrelated document')):
        subprocess.run(['git', '-C', str(first), *arguments], check=True,
                       capture_output=True, env=_synthetic_git_environment())
    unrelated.write_text('Unstaged review draft.\n')
    runtime = first / 'src/api_agent/__init__.py'
    updated = runtime.read_bytes() + b'\n# Current wheel input proof.\n'
    runtime.write_bytes(updated)

    second = tmp_path / 'second'
    second.mkdir()
    _wheel_source_snapshot(first, second)
    assert (second / 'src/api_agent/__init__.py').read_bytes() == updated
    assert not (second / 'docs/prds/review-notes.md').exists()
    output = tmp_path / 'wheels'
    run([sys.executable, '-I', '-m', 'build', '--no-isolation', '--wheel',
         '--outdir', str(output), str(second)], cwd=tmp_path)
    wheel, = output.glob('*.whl')
    with zipfile.ZipFile(wheel) as installed:
        assert installed.read('api_agent/__init__.py') == updated
    source_archives = tmp_path / 'source-archives'
    run([sys.executable, '-I', '-m', 'build', '--no-isolation', '--sdist',
         '--outdir', str(source_archives), str(second)], cwd=tmp_path)
    archive, = source_archives.glob('*.tar.gz')
    extracted = tmp_path / 'extracted'
    extracted.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(extracted, filter='data')
    unpacked, = extracted.iterdir()
    assert _git_owned_checkout(unpacked) is False
    rebuilt = tmp_path / 'rebuilt-wheels'
    run([sys.executable, '-I', '-m', 'build', '--no-isolation', '--wheel',
         '--outdir', str(rebuilt), str(unpacked)], cwd=tmp_path)
    rebuilt_wheel, = rebuilt.glob('*.whl')
    with zipfile.ZipFile(rebuilt_wheel) as installed:
        assert installed.read('api_agent/__init__.py') == updated

    runtime.unlink()
    missing = tmp_path / 'missing'
    missing.mkdir()
    with pytest.raises(FileNotFoundError):
        _wheel_source_snapshot(first, missing)
    outside = tmp_path / 'outside.py'
    outside.write_text('Private sentinel must not be copied.\n')
    runtime.symlink_to(outside)
    alias = tmp_path / 'alias'
    alias.mkdir()
    with pytest.raises(OSError):
        _wheel_source_snapshot(first, alias)


@pytest.fixture(scope='module')
def installed_wheel(tmp_path_factory):
    repository = Path(__file__).resolve().parents[2]
    temporary = tmp_path_factory.mktemp('installed-provider')
    wheel_setting = os.environ.get('API_AGENT_TEST_WHEEL')
    if wheel_setting:
        wheel = Path(wheel_setting).resolve()
        assert wheel.is_file(), 'API_AGENT_TEST_WHEEL must name a local wheel'
    else:
        source = repository
        if _git_owned_checkout(repository):
            source = temporary / 'source'
            source.mkdir()
            _wheel_source_snapshot(repository, source)
        output = temporary / 'wheels'
        run([sys.executable, '-I', '-m', 'build', '--no-isolation', '--wheel',
             '--outdir', str(output), str(source)], cwd=temporary)
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


@pytest.mark.parametrize('relative', ['src', 'tests', '.api-agent/bootstrap/unrelated'])
def test_installed_consumer_rejects_checkout_path_injection(installed_wheel, relocated_pack, relative):
    python, environment, repository, _ = installed_wheel
    pack, original, consumer_cwd = relocated_pack
    anchor = 'assert all(not Path(p).resolve().is_relative_to(checkout)'
    assert CONSUMER.count(anchor) == 1
    consumer = CONSUMER.replace(anchor, 'sys.path.append(str(checkout / ' + repr(relative) + '))\n' + anchor, 1)
    with pytest.raises(AssertionError, match='unsafe checkout path'):
        run([str(python), '-I', '-c', consumer, str(environment), str(pack),
             str(original), str(repository), 'success'], cwd=consumer_cwd)


def test_installed_consumer_rejects_checkout_module_origin(installed_wheel, relocated_pack):
    python, environment, repository, _ = installed_wheel
    pack, original, consumer_cwd = relocated_pack
    anchor = 'assert_installed_modules()\nbase ='
    assert CONSUMER.count(anchor) == 1
    consumer = CONSUMER.replace(anchor, "loading.__file__ = str(checkout / 'src/api_agent/providers/loading.py')\n" + anchor, 1)
    with pytest.raises(AssertionError, match='api_agent.providers.loading'):
        run([str(python), '-I', '-c', consumer, str(environment), str(pack),
             str(original), str(repository), 'success'], cwd=consumer_cwd)


@pytest.mark.parametrize('stage', ['inspect_relocated_pack', 'selected_manifests'])
@pytest.mark.parametrize('action', ['module', 'factory'])
@pytest.mark.parametrize('mode', ['success', 'invocation_failure', 'invalid_factory', 'invalid_capability'])
def test_installed_discovery_rejects_early_execution_after_module_cleanup(installed_wheel, relocated_pack, stage, action, mode):
    python, environment, repository, _ = installed_wheel
    pack, original, consumer_cwd = relocated_pack
    consumer = eager_discovery_consumer(stage, action)

    with pytest.raises(AssertionError, match=stage + ': active ' + action + ' executed during discovery'):
        run([str(python), '-I', '-c', consumer, str(environment), str(pack),
             str(original), str(repository), mode], cwd=consumer_cwd)

    assert (pack / 'active-import-ran').is_file()
    assert (pack / 'active-factory-ran').is_file() == (action == 'factory')


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


def test_installed_bundled_demo_declares_graphql_named_tool_default(installed_wheel, tmp_path):
    python, environment, repository, _ = installed_wheel
    consumer_cwd = tmp_path / 'unrelated-consumer'
    consumer_cwd.mkdir()
    consumer = '''
from pathlib import Path
import api_agent
from api_agent.providers.distribution import inspect_relocated_pack, selected_manifests
from api_agent.providers.loading import provider_location
from api_agent.config.declarations import UNSET
import sys
import tomllib

installed, checkout = map(Path, sys.argv[1:])
assert Path(api_agent.__file__).resolve().is_relative_to(installed)
assert not Path.cwd().is_relative_to(checkout)
base = Path(api_agent.__file__).resolve().parent
context, schema = inspect_relocated_pack(base, base / 'demo')
assert context.config_path == base / 'demo/simple/cfg/config.toml'
assert 'API_ENDPOINT' not in schema.fields and 'API_HEADERS' not in schema.fields
assert schema.fields['API_PROVIDER'].default is UNSET
assert context.workflow_entry == base / 'demo/simple/flow.md.html'
from api_agent.testing.collection import repository_label
assert repository_label(context, base) == 'api-agent'
assert 'cfg/tools/primary.toml' in context.workflow_entry.read_text()
instance = tomllib.loads((base / 'demo/simple/cfg/tools/primary.toml').read_text())
assert instance['id'] == 'demo/primary'
assert instance['implementation'] == 'package:api-agent:api_agent/api_providers/graphql_mcp/tool.toml'
assert instance['bindings'] == {'ENDPOINT': 'API_ENDPOINT', 'HEADERS': 'API_HEADERS'}
assert instance['defaults']['ENDPOINT'] == ''
implementation = tomllib.loads((base / 'api_providers/graphql_mcp/tool.toml').read_text())
assert implementation['factory'] == 'factory.py:create_tool'
assert implementation['inputs']['HEADERS']['default'] == {}
fixture = base / 'tests/integration/fixtures/tool_instances'
assert (fixture / 'shared_primary/flow.md.html').is_file()
assert (fixture / 'shared_primary/conversation.md.html').is_file()
assert (fixture / 'server.py').is_file()
assert (fixture / '.private_resources/viewer/marked-LICENSE.txt').is_file()
fixture_implementation = tomllib.loads((fixture / 'graphql.toml').read_text())
assert fixture_implementation['factory'] == 'package:api-agent:api_agent/api_providers/graphql_mcp/factory.py:create_tool'
assert not (base / 'tests/integration/test_tool_instances.py').exists()

path = base / 'api_providers/graphql_mcp/factory.py'
manifests = selected_manifests({'API_PROVIDER': str(path) + ':create'}, owner_root=context.owner_root,
                               active_kinds={'api'})
assert len(manifests) == 1
assert manifests[0].root == path.parent
assert manifests[0].requirements() == ('graphql-core==3.2.13',)
print('installed bundled GraphQL named tool default resolved')
'''
    output = run([str(python), '-I', '-c', consumer, str(environment), str(repository)],
                 cwd=consumer_cwd)
    assert output.strip() == 'installed bundled GraphQL named tool default resolved'
