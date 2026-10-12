"""Execution-only native quota preparation with synthetic client frames."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.gpt import quota_setup as setup
from api_agent.telemetry.usage import QuotaReadError


CONNECTION = SimpleNamespace(email='selected@example.test', access_token='application-secret')
MATCH = {'account': {'type': 'chatgpt', 'email': 'SELECTED@example.test'}}
EMPTY = {'account': None}
LOGIN = {'type': 'chatgpt', 'authUrl': 'https://auth.openai.com/authorize?state=fake', 'loginId': 'login-1'}
DONE = {'method': 'account/login/completed', 'params': {'loginId': 'login-1', 'success': True}}


class Process:
    def __init__(self, account=MATCH, login=LOGIN, completed=DONE, after=MATCH):
        self.frames = [dict(id=1, result={}), dict(id=2, result=account),
                       dict(id=3, result=login), completed, dict(id=5, result=after)]
        self.stdin = self.stdout = self
        self.written = []
        self.returncode = None
        self.signals = []

    def write(self, raw):
        self.written.append(json.loads(raw))

    async def drain(self):
        pass

    async def readline(self):
        return (json.dumps(self.frames.pop(0)).encode() + b'\n') if self.frames else b''

    def terminate(self):
        self.signals.append('terminate')
        self.returncode = -15

    async def wait(self):
        return self.returncode


@pytest.fixture
def client(monkeypatch):
    process = Process()
    spawned = []

    async def spawn(*args, **options):
        spawned.append((args, options))
        return process

    monkeypatch.setattr(setup.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/tools/codex')
    return process, spawned


def test_matching_existing_native_login_never_starts_login(client, monkeypatch):
    process, spawned = client
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'ACCESS_TOKEN'):
        monkeypatch.setenv(key, 'environment-secret')
    result = asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True))
    assert result == '/tools/codex'
    assert [frame['method'] for frame in process.written] == ['initialize', 'initialized', 'account/read']
    assert spawned[0][0] == ('/tools/codex', 'app-server', '--listen', 'stdio://')
    assert all(key not in spawned[0][1]['env'] for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'ACCESS_TOKEN'))
    assert 'application-secret' not in json.dumps(process.written)
    assert process.signals == ['terminate']


def test_missing_client_uses_launcher_installer_only_when_called(client, monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda name: None)
    calls = []

    async def install():
        calls.append('install')
        return '/managed/codex'

    result = asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=False, install_client=install))
    assert result == '/managed/codex'
    assert calls == ['install']
    assert client[1][0][0][0] == '/managed/codex'


def test_missing_client_without_installer_is_safe(client, monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda name: None)
    async def missing():
        raise setup.NativeClientSetupError('No supported runtime')
    monkeypatch.setattr(setup, 'install_native_client', missing)
    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=False)) == QuotaReadError('source')
    assert not client[1]


@pytest.mark.parametrize('account,expected', [({'account': {'type': 'chatgpt', 'email': 'other@example.test'}}, 'identity'),
                                               ({'account': {'type': 'apiKey'}}, 'identity'),
                                               (EMPTY, 'source')])
def test_unattended_never_starts_sign_in(client, account, expected):
    client[0].frames[1]['result'] = account
    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=False)) == QuotaReadError(expected)
    assert all(frame['method'] != 'account/login/start' for frame in client[0].written)


def test_attended_browser_sign_in_checks_selected_account_after_completion(client):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    shown = []

    async def present(url):
        shown.append(url)

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present)) == '/tools/codex'
    assert shown == [LOGIN['authUrl']]
    assert [frame['method'] for frame in process.written] == [
        'initialize', 'initialized', 'account/read', 'account/login/start', 'account/read']


def test_attended_sign_in_wrong_account_is_rejected(client):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    process.frames[4]['result'] = {'account': {'type': 'chatgpt', 'email': 'other@example.test'}}

    async def present(url):
        pass

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present)) == QuotaReadError('identity')


def test_attended_login_requires_presenter(client):
    client[0].frames[1]['result'] = EMPTY
    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True)) == QuotaReadError('source')
    assert all(frame['method'] != 'account/login/start' for frame in client[0].written)


def test_missing_verified_email_never_installs_or_spawns(client):
    assert asyncio.run(setup.prepare_quota_client(SimpleNamespace(email=' '), interactive=True)) == QuotaReadError('identity')
    assert not client[1]


@pytest.mark.parametrize('email', [None, 5])
def test_invalid_verified_email_never_spawns(client, email):
    assert asyncio.run(setup.prepare_quota_client(SimpleNamespace(email=email), interactive=True)) == QuotaReadError('identity')
    assert not client[1]


def test_failed_installer_is_safe(client, monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda name: None)

    async def install():
        raise OSError('private failure')

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=False, install_client=install)) == QuotaReadError('source')
    assert not client[1]


def test_installer_without_executable_is_safe(client, monkeypatch):
    monkeypatch.setattr(setup.shutil, 'which', lambda name: None)

    async def install():
        return None

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=False, install_client=install)) == QuotaReadError('source')
    assert not client[1]


def test_malformed_account_cannot_trigger_login(client):
    client[0].frames[1]['result'] = {}

    async def present(url):
        pytest.fail('unexpected sign-in')

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present)) == QuotaReadError('identity')


@pytest.mark.parametrize('url', ['http://auth.openai.com/', 'https://example.test/', 'https://user@chatgpt.com/',
                                  5, 'https://chatgpt.com/' + 'a' * 8192])
def test_invalid_login_url_is_not_presented(client, url):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    process.frames[2]['result'] = dict(LOGIN, authUrl=url)

    async def present(value):
        pytest.fail('unexpected URL presentation')

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present)) == QuotaReadError('request')


def test_declined_login_is_safe(client):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    process.frames[3] = dict(DONE, params=dict(DONE['params'], success=False))

    async def present(url):
        pass

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present)) == QuotaReadError('source')


def test_presenter_failure_cancels_login_and_returns_safe_error(client):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    process.frames[3] = dict(id=4, result={})

    async def present(url):
        raise OSError('browser failure')

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present)) == QuotaReadError('request')
    assert process.written[-1]['method'] == 'account/login/cancel'


def test_spawn_failure_is_safe(client, monkeypatch):
    async def spawn(*args, **kwargs):
        raise OSError('private failure')

    monkeypatch.setattr(setup.asyncio, 'create_subprocess_exec', spawn)
    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=False)) == QuotaReadError('request')


def test_completion_ignores_other_notifications(client):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    process.frames.insert(3, {'method': 'notice'})
    process.frames.insert(4, {'method': 'account/login/completed', 'params': {'loginId': 'other', 'success': True}})

    async def present(url):
        pass

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present)) == '/tools/codex'


@pytest.mark.parametrize('frame', [b'', b'x' * 65537, b'garbage', 'list'])
def test_malformed_completion_returns_safe_error(client, frame):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    process.frames[3] = frame if isinstance(frame, bytes) else []

    async def present(url):
        pass

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present)) == QuotaReadError('request')


def _installed_tree(root, package, *, primary_integrity=None, native_integrity=None):
    root.mkdir(parents=True, exist_ok=True)
    entries = {
        'node_modules/@openai/codex': {'resolved': setup._REGISTRY + '@openai/codex/-/codex.tgz',
                                      'integrity': primary_integrity or setup._CODEX_INTEGRITY,
                                      'version': setup._CODEX_VERSION},
        f'node_modules/{package}': {'resolved': setup._REGISTRY + '@openai/codex/-/codex-native.tgz',
                                     'integrity': native_integrity or setup._NATIVE_INTEGRITY[package],
                                     'version': setup._CODEX_VERSION + '-linux-x64'},
    }
    (root / 'package-lock.json').write_text(json.dumps({'packages': entries}))
    (root / 'node_modules' / package).mkdir(parents=True, exist_ok=True)
    (root / 'node_modules' / package / 'package.json').write_text('{}')
    script = root / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text('// fake')
    script.chmod(0o755)
    setup._seal_install(root, package)
    return script


def test_install_native_client_uses_pinned_registry_and_verifies_optional_package(monkeypatch, tmp_path):
    package = '@openai/codex-linux-x64'
    monkeypatch.setattr(setup, '_platform_package', lambda: package)
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/tools/' + name)
    calls = []

    async def command(*args, timeout, env, cwd=None):
        calls.append((args, timeout, env))
        if args[1] == 'install':
            _installed_tree(Path(args[args.index('--prefix') + 1]), package)
            return ''
        return 'codex-cli ' + setup._CODEX_VERSION

    monkeypatch.setattr(setup, '_command', command)
    result = asyncio.run(setup.install_native_client(cache_dir=tmp_path))
    assert result == str(tmp_path / setup._CODEX_VERSION / 'node_modules/@openai/codex/bin/codex.js')
    assert calls[0][0][:2] == ('/tools/npm', 'install')
    assert '--ignore-scripts' in calls[0][0] and '--no-audit' in calls[0][0]
    assert calls[0][0][-1] == '@openai/codex@' + setup._CODEX_VERSION
    assert calls[0][2]['npm_config_cache'] == str(tmp_path / 'npm-cache')
    assert calls[0][1] == 180
    assert calls[1][0] == ('/tools/node', Path(result.replace(str(tmp_path / setup._CODEX_VERSION), str(calls[0][0][calls[0][0].index('--prefix') + 1]))), '--version')


def test_valid_managed_client_is_reused_without_install(monkeypatch, tmp_path):
    package = '@openai/codex-linux-x64'
    monkeypatch.setattr(setup, '_platform_package', lambda: package)
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/tools/' + name)
    script = _installed_tree(tmp_path / setup._CODEX_VERSION, package)
    calls = []

    async def command(*args, timeout, env, cwd=None):
        calls.append(args)
        return 'codex-cli ' + setup._CODEX_VERSION

    monkeypatch.setattr(setup, '_command', command)
    assert asyncio.run(setup.install_native_client(cache_dir=tmp_path)) == str(script)
    assert calls == [('/tools/node', script, '--version')]


@pytest.mark.parametrize('system,machine,expected', [
    ('Linux', 'x86_64', '@openai/codex-linux-x64'),
    ('Darwin', 'arm64', '@openai/codex-darwin-arm64'),
    ('Windows', 'AMD64', None),
    ('Windows', 'arm64', None),
    ('Linux', 'aarch64', '@openai/codex-linux-arm64'),
    ('Solaris', 'x86_64', None),
    ('Linux', 'riscv64', None),
])
def test_supported_platform_mapping(monkeypatch, system, machine, expected):
    monkeypatch.setattr(setup.platform, 'system', lambda: system)
    monkeypatch.setattr(setup.platform, 'machine', lambda: machine)
    assert setup._platform_package() == expected


def test_missing_node_is_actionable_and_does_not_install(monkeypatch, tmp_path):
    monkeypatch.setattr(setup, '_platform_package', lambda: '@openai/codex-linux-x64')
    monkeypatch.setattr(setup.shutil, 'which', lambda name: None if name == 'node' else '/tools/npm')
    with pytest.raises(setup.NativeClientSetupError, match='Node.js/npm'):
        asyncio.run(setup.install_native_client(cache_dir=tmp_path))
    assert not list(tmp_path.iterdir())


def test_unsupported_windows_installer_fails_before_tool_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(setup.platform, 'machine', lambda: 'AMD64')
    monkeypatch.setattr(setup.shutil, 'which', lambda name: pytest.fail('unsupported installer must not probe tools'))
    with pytest.raises(setup.NativeClientSetupError, match='Linux and macOS'):
        asyncio.run(setup.install_native_client(cache_dir=tmp_path))
    assert not list(tmp_path.iterdir())


def test_existing_windows_client_can_still_prepare_quota(client, monkeypatch):
    monkeypatch.setattr(setup.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(setup.shutil, 'which', lambda name: pytest.fail('explicit native path must be used'))
    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=False,
                                                  executable='C:/tools/codex.exe')) == 'C:/tools/codex.exe'


def test_package_verification_rejects_wrong_integrity_and_missing_artifacts(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    root.mkdir()
    assert setup._verify_install(root, package) is None
    script = _installed_tree(root, package, primary_integrity='sha512-wrong')
    assert setup._verify_install(root, package) is None
    _installed_tree(root, package)
    assert setup._verify_install(root, package) == script
    script.unlink()
    assert setup._verify_install(root, package) is None


def test_package_verification_requires_executable_wrapper(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    script = _installed_tree(root, package)
    script.chmod(0o644)
    assert setup._verify_install(root, package) is None


def test_package_verification_rejects_nonregistry_and_native_integrity(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    lock = json.loads((root / 'package-lock.json').read_text())
    lock['packages'][f'node_modules/{package}']['resolved'] = 'https://other.example/codex.tgz'
    (root / 'package-lock.json').write_text(json.dumps(lock))
    assert setup._verify_install(root, package) is None
    lock['packages'][f'node_modules/{package}']['resolved'] = setup._REGISTRY + 'codex.tgz'
    lock['packages'][f'node_modules/{package}']['integrity'] = 'md5-weak'
    (root / 'package-lock.json').write_text(json.dumps(lock))
    assert setup._verify_install(root, package) is None


def test_native_install_rejects_bad_postinstall_version(monkeypatch, tmp_path):
    package = '@openai/codex-linux-x64'
    monkeypatch.setattr(setup, '_platform_package', lambda: package)
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/tools/' + name)

    async def command(*args, timeout, env, cwd=None):
        if args[1] == 'install':
            _installed_tree(Path(args[args.index('--prefix') + 1]), package)
            return ''
        return 'codex-cli older'

    monkeypatch.setattr(setup, '_command', command)
    with pytest.raises(setup.NativeClientSetupError, match='version verification'):
        asyncio.run(setup.install_native_client(cache_dir=tmp_path))
    assert not (tmp_path / setup._CODEX_VERSION).exists()


def test_native_install_rejects_missing_optional_package(monkeypatch, tmp_path):
    monkeypatch.setattr(setup, '_platform_package', lambda: '@openai/codex-linux-x64')
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/tools/' + name)

    async def command(*args, timeout, env, cwd=None):
        return ''

    monkeypatch.setattr(setup, '_command', command)
    with pytest.raises(setup.NativeClientSetupError, match='package verification'):
        asyncio.run(setup.install_native_client(cache_dir=tmp_path))


def test_invalid_cached_install_is_replaced_after_verification(monkeypatch, tmp_path):
    package = '@openai/codex-linux-x64'
    monkeypatch.setattr(setup, '_platform_package', lambda: package)
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/tools/' + name)
    target = tmp_path / setup._CODEX_VERSION
    _installed_tree(target, package, primary_integrity='sha512-wrong')

    async def command(*args, timeout, env, cwd=None):
        if args[1] == 'install':
            _installed_tree(Path(args[args.index('--prefix') + 1]), package)
            return ''
        return 'codex-cli ' + setup._CODEX_VERSION

    monkeypatch.setattr(setup, '_command', command)
    replacement = Path(asyncio.run(setup.install_native_client(cache_dir=tmp_path)))
    assert replacement.exists() and replacement.parent.parent.parent.parent != target
    assert setup._verify_install(target, package) is None
    assert setup._verify_install(replacement.parents[4], package) is not None


def test_subprocess_command_is_bounded_and_reaped(monkeypatch):
    class Output:
        reads = 0
        async def read(self, limit):
            assert limit == 4096
            self.reads += 1
            return b'codex-cli 0.160.1\n' if self.reads == 1 else b''

    class Child:
        stdout = Output()
        returncode = None
        terminated = False

        async def wait(self):
            self.returncode = 0
            return 0

        def terminate(self):
            self.terminated = True
            self.returncode = -15

    child = Child()
    async def spawn(*args, **options):
        assert args == ('/node', '--version')
        return child
    monkeypatch.setattr(setup.asyncio, 'create_subprocess_exec', spawn)
    assert asyncio.run(setup._command('/node', '--version', timeout=15, env={})) == 'codex-cli 0.160.1'
    assert not child.terminated


def test_subprocess_failure_does_not_report_install_success(monkeypatch):
    class Child:
        returncode = 1
        stdout = SimpleNamespace(read=lambda limit: asyncio.sleep(0, result=b''))

        async def wait(self):
            return 1

    async def spawn(*args, **options):
        return Child()
    monkeypatch.setattr(setup.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(setup.NativeClientSetupError, match='setup failed'):
        asyncio.run(setup._command('/npm', timeout=15, env={}))


def test_completion_frame_bound_is_safe():
    process = Process()
    process.frames = [{'method': 'notice'}] * 256
    with pytest.raises(ValueError, match='Too many'):
        asyncio.run(setup._completed(process, 'one'))


def test_cancellation_during_native_login_cleans_up_child(client):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    process.frames[3] = dict(id=4, result={})

    async def run():
        entered = asyncio.Event()
        async def present(url):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert process.signals == ['terminate']


def test_managed_cache_uses_absolute_xdg_location_only(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.platform, 'system', lambda: 'Linux')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    assert setup._managed_cache() == tmp_path / 'api-agent/native-codex'
    monkeypatch.setenv('XDG_CACHE_HOME', 'relative-cache')
    assert setup._managed_cache() == Path.home() / '.cache/api-agent/native-codex'


def test_managed_cache_uses_macos_cache_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(setup.platform, 'system', lambda: 'Darwin')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    assert setup._managed_cache() == Path.home() / 'Library/Caches/api-agent/native-codex'


def test_package_verification_rejects_wrong_native_version_and_missing_package(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    lock = json.loads((root / 'package-lock.json').read_text())
    lock['packages'][f'node_modules/{package}']['version'] = '0.159.0-linux-x64'
    (root / 'package-lock.json').write_text(json.dumps(lock))
    assert setup._verify_install(root, package) is None
    lock['packages'][f'node_modules/{package}']['version'] = setup._CODEX_VERSION + '-linux-x64'
    (root / 'package-lock.json').write_text(json.dumps(lock))
    (root / 'node_modules' / package / 'package.json').unlink()
    assert setup._verify_install(root, package) is None


def test_stale_cached_binary_version_triggers_reinstall(monkeypatch, tmp_path):
    package = '@openai/codex-linux-x64'
    monkeypatch.setattr(setup, '_platform_package', lambda: package)
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/tools/' + name)
    _installed_tree(tmp_path / setup._CODEX_VERSION, package)
    calls = []

    async def command(*args, timeout, env, cwd=None):
        calls.append(args)
        if args[1] == 'install':
            _installed_tree(Path(args[args.index('--prefix') + 1]), package)
            return ''
        return 'codex-cli older' if len(calls) == 1 else 'codex-cli ' + setup._CODEX_VERSION

    monkeypatch.setattr(setup, '_command', command)
    assert asyncio.run(setup.install_native_client(cache_dir=tmp_path)).endswith('bin/codex.js')
    assert len(calls) == 3


@pytest.mark.parametrize('raw', [b'', b'x' * 65537])
def test_completion_rejects_missing_or_oversized_frame(raw):
    async def readline():
        return raw
    process = SimpleNamespace(stdout=SimpleNamespace(readline=readline))
    with pytest.raises(ValueError, match='Invalid native login frame'):
        asyncio.run(setup._completed(process, 'one'))


@pytest.mark.parametrize('url', ['https://auth.openai.com:444/authorize',
                                  'https://user:pass@auth.openai.com/authorize',
                                  'https://auth.openai.com:bad/authorize',
                                  'https://auth.openai.com/authorize#fragment',
                                  'https://[bad/authorize'])
def test_login_url_rejects_authority_and_fragment_surprises(url):
    assert not setup._safe_auth_url(url)


def test_cached_client_rejects_corrupted_wrapper_even_with_original_lock(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    script = _installed_tree(root, package)
    assert setup._verify_install(root, package) == script
    script.write_text('// altered')
    assert setup._verify_install(root, package) is None


def test_cached_client_rejects_forged_lock_and_escaped_symlink(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    script = _installed_tree(root, package)
    lock = json.loads((root / 'package-lock.json').read_text())
    lock['packages'][f'node_modules/{package}']['integrity'] = 'sha512-forged'
    (root / 'package-lock.json').write_text(json.dumps(lock))
    assert setup._verify_install(root, package) is None
    _installed_tree(root, package)
    script.unlink()
    (tmp_path / 'external.js').write_text('// outside')
    (tmp_path / 'external.js').chmod(0o755)
    script.symlink_to(tmp_path / 'external.js')
    assert setup._verify_install(root, package) is None


@pytest.mark.parametrize('relative', ['node_modules', 'node_modules/@openai'])
def test_intermediate_directory_symlink_cannot_seal_or_reuse_cache(tmp_path, relative):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    internal = root / relative
    external = tmp_path / ('outside-' + relative.replace('/', '-'))
    internal.rename(external)
    internal.symlink_to(external, target_is_directory=True)
    assert setup._verify_install(root, package) is None
    with pytest.raises(ValueError, match='Untrusted native client'):
        setup._seal_install(root, package)


def test_symlinked_cache_parent_cannot_be_reused(tmp_path):
    package = '@openai/codex-linux-x64'
    owned = tmp_path / 'owned'
    root = owned / 'client'
    _installed_tree(root, package)
    alias = tmp_path / 'alias'
    alias.symlink_to(owned, target_is_directory=True)
    assert setup._verify_install(alias / 'client', package) is None


def test_cached_client_rejects_forged_validation_marker(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    marker = root / 'validation.json'
    marker.write_text(json.dumps({'version': setup._CODEX_VERSION, 'package': package,
                                  'fingerprint': '0' * 64}))
    assert setup._verify_install(root, package) is None
    marker.chmod(0o644)
    assert setup._verify_install(root, package) is None


def test_cached_client_rejects_hardlinked_package_file(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    script = _installed_tree(root, package)
    (root / 'node_modules' / package / 'copied.js').hardlink_to(script)
    assert setup._verify_install(root, package) is None


def test_cached_client_rejects_oversized_sparse_file(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    oversized = root / 'node_modules' / package / 'oversized.bin'
    with oversized.open('wb') as handle:
        handle.truncate(256 * 1024 * 1024 + 1)
    assert setup._verify_install(root, package) is None


def test_cached_client_rejects_excessive_file_count(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    extra = root / 'node_modules' / package
    for index in range(1001):
        (extra / f'{index:04}.txt').write_text('')
    assert setup._verify_install(root, package) is None


def test_cache_fingerprint_caps_total_reported_size(tmp_path, monkeypatch):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    first = root / 'node_modules' / package / 'first.bin'
    second = root / 'node_modules' / package / 'second.bin'
    first.write_bytes(b'x')
    second.write_bytes(b'y')
    original = Path.lstat

    def reported_size(path):
        value = original(path)
        if path in (first, second):
            values = list(value)
            values[6] = 256 * 1024 * 1024
            return __import__('os').stat_result(values)
        return value

    monkeypatch.setattr(Path, 'lstat', reported_size)
    with pytest.raises(ValueError, match='size limit'):
        setup._content_fingerprint(root, package)


def test_cache_fingerprint_observes_cancellation(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    with pytest.raises(ValueError, match='cancelled'):
        setup._content_fingerprint(root, package, cancelled=lambda: True)


def test_cache_fingerprint_observes_cancellation_during_file_read(tmp_path):
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks == 3

    with pytest.raises(ValueError, match='cancelled'):
        setup._content_fingerprint(root, package, cancelled=cancelled)


def test_npm_environment_uses_empty_config_and_public_registry(monkeypatch, tmp_path):
    package = '@openai/codex-linux-x64'
    monkeypatch.setattr(setup, '_platform_package', lambda: package)
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/tools/' + name)
    monkeypatch.setenv('API_AGENT_APP_TOKEN', 'secret')
    monkeypatch.setenv('npm_config_registry', 'https://private.example/')
    calls = []

    async def command(*args, timeout, env, cwd=None):
        calls.append((args, env.copy(), cwd))
        if args[1] == 'install':
            assert cwd == args[args.index('--prefix') + 1]
            assert env['npm_config_userconfig'] == env['npm_config_globalconfig']
            assert Path(env['npm_config_userconfig']).read_text() == ''
            assert '--registry' in args and args[args.index('--registry') + 1] == setup._REGISTRY
            assert 'API_AGENT_APP_TOKEN' not in env and 'npm_config_registry' not in env
            _installed_tree(Path(cwd), package)
            return ''
        return 'codex-cli ' + setup._CODEX_VERSION

    monkeypatch.setattr(setup, '_command', command)
    assert asyncio.run(setup.install_native_client(cache_dir=tmp_path)).endswith('bin/codex.js')
    assert len(calls) == 2


def test_subprocess_output_overflow_kills_child(monkeypatch):
    class Output:
        async def read(self, limit):
            return b'x' * limit

    class Child:
        stdout = Output()
        returncode = None
        terminated = False

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        async def wait(self):
            return self.returncode

    child = Child()
    async def spawn(*args, **options):
        return child
    monkeypatch.setattr(setup.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(setup.NativeClientSetupError, match='output exceeded'):
        asyncio.run(setup._command('/npm', timeout=15, env={}))
    assert child.terminated


def test_interrupt_during_login_preserves_interruption(client):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    process.frames[3] = dict(id=4, result={})

    async def present(url):
        raise SystemExit('stop')

    with pytest.raises(SystemExit, match='stop'):
        asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present))
    assert process.written[-1]['method'] == 'account/login/cancel'
    assert process.signals == ['terminate']


def test_cancel_rpc_has_own_deadline_after_presenter_failure(client, monkeypatch):
    process, _ = client
    process.frames[1]['result'] = EMPTY
    original_readline = process.readline

    async def readline():
        if len(process.frames) > 2:
            return await original_readline()
        await asyncio.Event().wait()

    process.readline = readline
    original_timeout = asyncio.timeout
    monkeypatch.setattr(setup.asyncio, 'timeout', lambda _: original_timeout(.01))

    async def present(url):
        raise OSError('browser unavailable')

    assert asyncio.run(setup.prepare_quota_client(CONNECTION, interactive=True, present_login=present)) == QuotaReadError('request')
    assert process.written[-1]['method'] == 'account/login/cancel'


@pytest.mark.parametrize('relative', ['package-lock.json', 'validation.json'])
def test_fifo_metadata_is_rejected_and_the_owned_verifier_exits(tmp_path, relative):
    import multiprocessing
    import os
    if not hasattr(os, 'mkfifo'):
        pytest.skip('FIFO control requires POSIX')
    package = '@openai/codex-linux-x64'
    root = tmp_path / 'client'
    _installed_tree(root, package)
    metadata = root / relative
    metadata.unlink()
    os.mkfifo(metadata, 0o600)
    context = multiprocessing.get_context('fork')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=lambda: child.send(setup._verify_install(root, package)))

    process.start()
    process.join(1)
    stopped = not process.is_alive()
    if not stopped:
        process.terminate()
        process.join(1)

    assert stopped, 'Quota metadata verifier blocked on a FIFO'
    assert process.exitcode == 0 and parent.poll() and parent.recv() is None
    parent.close()
    child.close()


@pytest.mark.parametrize('replacement', ['fifo', 'regular', 'symlink'])
def test_metadata_replacement_is_rejected_before_reading(tmp_path, monkeypatch, replacement):
    import os
    path = tmp_path / 'metadata.json'
    path.write_text('{}')
    original_open = os.open
    def swapped_open(selected, flags):
        selected.rename(tmp_path / 'original.json')
        if replacement == 'fifo':
            os.mkfifo(selected, 0o600)
        elif replacement == 'regular':
            selected.write_text('{}')
        else:
            selected.symlink_to(tmp_path / 'original.json')
        return original_open(selected, flags)
    monkeypatch.setattr(setup.os, 'open', swapped_open)

    with pytest.raises((ValueError, OSError)):
        setup._metadata(path, limit=4096)


@pytest.mark.parametrize(('relative', 'limit'), [('package-lock.json', 1024 * 1024), ('validation.json', 4096)])
def test_oversized_metadata_is_rejected_before_parsing(tmp_path, relative, limit):
    root = tmp_path / 'client'
    package = '@openai/codex-linux-x64'
    _installed_tree(root, package)
    with (root / relative).open('wb') as handle:
        handle.truncate(limit + 1)

    assert setup._verify_install(root, package) is None


def test_metadata_read_observes_cancellation(tmp_path):
    path = tmp_path / 'metadata.json'
    path.write_text('{}')
    with pytest.raises(ValueError, match='cancelled'):
        setup._metadata(path, limit=4096, cancelled=lambda: True)


def test_file_growth_cannot_exceed_read_limit(tmp_path, monkeypatch):
    path = tmp_path / 'growing.json'
    path.write_bytes(b'{}')
    original_fdopen = setup.os.fdopen
    def grown_file(descriptor, mode):
        stream = original_fdopen(descriptor, mode)
        original_read = stream.read
        def read(size):
            path.write_bytes(b'x' * 10)
            return original_read(size)
        stream.read = read
        return stream
    monkeypatch.setattr(setup.os, 'fdopen', grown_file)
    with pytest.raises(ValueError, match='size limit'):
        setup._metadata(path, limit=4)


def test_metadata_read_cancels_between_bounded_chunks(tmp_path):
    path = tmp_path / 'metadata.json'
    path.write_text('{}')
    checks = iter([False, False, True])
    with pytest.raises(ValueError, match='cancelled'):
        setup._metadata(path, limit=4096, cancelled=lambda: next(checks))
