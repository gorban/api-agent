"""Synthetic filesystem checks for selected native profile persistence."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from api_agent.llm_providers.bedrock import persistence as p


@pytest.fixture
def store(tmp_path):
    home = tmp_path / 'home'
    aws = home / '.aws'
    aws.mkdir(parents=True)
    return p.ProfileStore(dict(home=str(home), config=str(aws / 'config'),
        credentials=str(aws / 'credentials'), login=str(aws / 'login/cache')), 'chosen')


@pytest.fixture
def native(tmp_path):
    root = tmp_path / 'owned'
    aws = root / '.aws'
    aws.mkdir(parents=True)
    (aws / 'config').write_bytes(b'[profile chosen]\nregion = us-east-1\nlogin_session = example\n')
    cache = aws / 'login/cache' / (hashlib.sha256(b'example').hexdigest() + '.json')
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'{"native":"refresh-token"}')
    return root


def publish(store, native):
    store.publish(native, dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token'),
        9999999999, dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example'))


def test_publish_restart_seed_preserves_other_profiles_and_caches(store, native, tmp_path):
    unrelated = b'# keep exact\r\n[other]\r\naws_access_key_id=other\r\n'
    store.credentials.write_bytes(unrelated)
    store.config.write_bytes(b'# settings\n[profile other]\nregion = us-west-2\n')
    unrelated_cache = store.login / 'unrelated.json'
    unrelated_cache.parent.mkdir(parents=True)
    unrelated_cache.write_bytes(b'untouched')
    publish(store, native)
    assert store.credentials.read_bytes().startswith(unrelated)
    assert store.config.read_bytes().startswith(b'# settings\n[profile other]\nregion = us-west-2\n')
    assert store.snapshot()['expires_at'] == 9999999999
    assert store.unmasked_credentials() == unrelated
    assert unrelated_cache.read_bytes() == b'untouched'
    assert store.credentials.stat().st_mode & 0o777 == 0o600
    assert store.config.stat().st_mode & 0o777 == 0o600
    restored = tmp_path / 'restart'
    (restored / '.aws').mkdir(parents=True)
    store.seed(restored)
    assert (restored / '.aws/config').read_bytes() == (native / '.aws/config').read_bytes()
    assert list((restored / '.aws/login/cache').iterdir())[0].read_bytes() == b'{"native":"refresh-token"}'


def test_user_changed_tuple_is_not_masked_or_overwritten(store, native):
    publish(store, native)
    changed = store.credentials.read_bytes().replace(b'= key\n', b'= changed\n')
    store.credentials.write_bytes(changed)
    assert store.snapshot() is None
    assert store.unmasked_credentials() == changed
    with pytest.raises(ValueError, match='Selected'):
        publish(store, native)
    assert store.credentials.read_bytes() == changed


def test_snapshot_requires_owned_metadata(store):
    store.credentials.write_bytes(b'[chosen]\naws_access_key_id = user\n')
    assert store.snapshot() is None
    assert store.unmasked_credentials() == store.credentials.read_bytes()
    store.credentials.write_bytes(b'[chosen]\napi_agent_login_snapshot = {}\n')
    with pytest.raises(ValueError, match='metadata'):
        store.snapshot()


@pytest.mark.parametrize('selected', [b'[default]\nregion = a\n', b''])
def test_section_missing_and_default_bytes(selected):
    assert p.section(selected, 'missing') == (len(selected), len(selected), b'')
    assert p.replace_section(b'# no newline', 'chosen', b'[chosen]\n') == b'# no newline\n[chosen]\n'
    assert p.section(b'[a]\nx=1\n[b]\nx=2', 'a')[2] == b'[a]\nx=1\n'


def test_static_selected_profile_cannot_be_overwritten(store, native):
    store.credentials.write_bytes(b'[chosen]\naws_access_key_id=external\n')
    with pytest.raises(ValueError, match='Selected'):
        publish(store, native)


def test_missing_native_route_or_cache_rejects_before_publication(store, native):
    config = native / '.aws/config'
    original = config.read_bytes()
    for text in (b'', b'[profile chosen]\nregion=us-east-1\n'):
        config.write_bytes(text)
        with pytest.raises(ValueError, match='refreshable'):
            publish(store, native)
    config.write_bytes(original)
    list((native / '.aws/login/cache').iterdir())[0].unlink()
    with pytest.raises(ValueError, match='refresh cache'):
        publish(store, native)
    assert not store.credentials.exists()


def test_sso_cache_selection_and_shared_route_conflict(store, native):
    config = b'[profile chosen]\nsso_session = shared\n[sso-session shared]\nsso_start_url = https://example.com/start\n'
    (native / '.aws/config').write_bytes(config)
    source = native / '.aws/sso/cache' / (hashlib.sha1(b'shared').hexdigest() + '.json')
    source.parent.mkdir(parents=True)
    source.write_bytes(b'sso')
    publish(store, native)
    assert store.selected_config(store.config.read_bytes()) == config
    store.config.write_bytes(config.replace(b'example.com', b'changed.example'))
    with pytest.raises(ValueError, match='SSO routing'):
        publish(store, native)
    legacy = b'[profile chosen]\nsso_start_url = https://example.com/start\n'
    assert store.cache_paths(legacy)[0][0].name == hashlib.sha1(b'https://example.com/start').hexdigest() + '.json'
    assert store.cache_paths(b'') == []
    assert store.selected_config(b'') == b''


def test_seed_empty_store_preserves_generated_config(store, native):
    before = (native / '.aws/config').read_bytes()
    store.seed(native)
    assert (native / '.aws/config').read_bytes() == before


def test_atomic_rejects_concurrent_edit_and_cleans_staging(tmp_path, monkeypatch):
    target = tmp_path / 'file'
    target.write_bytes(b'before')
    with pytest.raises(ValueError, match='concurrently'):
        p.atomic(target, b'after', b'wrong')
    original = p.read
    calls = []
    def mutate(path):
        calls.append(path)
        if len(calls) == 2:
            target.write_bytes(b'other')
        return original(path)
    monkeypatch.setattr(p, 'read', mutate)
    with pytest.raises(ValueError, match='concurrently'):
        p.atomic(target, b'after', b'before')
    assert target.read_bytes() == b'other'
    assert list(tmp_path.iterdir()) == [target]


def test_failed_replace_removes_private_staging(tmp_path, monkeypatch):
    target = tmp_path / 'file'
    def fail(*args, **kwargs):
        raise OSError
    monkeypatch.setattr(p.os, 'replace', fail)
    with pytest.raises(OSError):
        p.atomic(target, b'after', b'')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('kind', ['link', 'parent', 'directory', 'large', 'fifo'])
def test_unsafe_or_unbounded_read_rejected(tmp_path, kind):
    target = tmp_path / 'file'
    if kind == 'link':
        target.symlink_to(tmp_path / 'missing')
    elif kind == 'parent':
        (tmp_path / 'real').mkdir()
        target.symlink_to(tmp_path / 'real', target_is_directory=True)
        target = target / 'child'
    elif kind == 'directory':
        target.mkdir()
    elif kind == 'large':
        target.write_bytes(b'x' * (p.LIMIT + 1))
    else:
        os.mkfifo(target)
    with pytest.raises((ValueError, OSError)):
        p.read(target)


def test_unsafe_directory_and_lock_rejected(store, tmp_path):
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        with p.directory_fd(link, create=True):
            pass
    with pytest.raises((ValueError, OSError)):
        with p.directory_fd(link / 'home', create=True):
            pass
    ordinary = tmp_path / 'file'
    ordinary.touch()
    with pytest.raises((ValueError, OSError)):
        with p.directory_fd(ordinary, create=True):
            pass
    os.mkfifo(store.credentials.parent / '.api-agent-login.lock')
    with pytest.raises(ValueError):
        with store.locked():
            pass


def test_publication_rolls_back_on_write_failure(store, native, monkeypatch):
    before = b'[other]\nx=y\n'
    store.config.write_bytes(before)
    original = p.atomic
    def fail_credentials(path, data, expected):
        if Path(path) == store.credentials:
            raise OSError
        original(path, data, expected)
    monkeypatch.setattr(p, 'atomic', fail_credentials)
    with pytest.raises(OSError):
        publish(store, native)
    assert store.config.read_bytes() == before
    assert not store.credentials.exists()


def test_publication_checks_complete_read_set(store, native, monkeypatch):
    original = p.read
    counts = {}
    def mutate(path):
        path = Path(path)
        counts[path] = counts.get(path, 0) + 1
        if path == store.config and counts[path] == 2:
            path.write_bytes(b'# another writer\n')
        return original(path)
    monkeypatch.setattr(p, 'read', mutate)
    with pytest.raises(ValueError, match='concurrently'):
        publish(store, native)
    assert not store.credentials.exists()


def test_selected_custom_settings_and_comments_survive(store, native):
    store.config.write_bytes(b'[profile chosen]\n# keep me\noutput = json\nregion = us-west-2\n')
    store.credentials.write_bytes(b'[chosen]\n# owner comment\ncustom = value\n')
    publish(store, native)
    assert b'# keep me\noutput = json\n' in store.config.read_bytes()
    assert b'# owner comment\ncustom = value\n' in store.credentials.read_bytes()
    assert p.merge_fields(b'[x]\ncustom=value', 'x', {'region':'us-east-1'}) == b'[x]\ncustom=value\nregion = us-east-1\n'


def test_seed_missing_exact_cache_is_skipped(store, native, tmp_path):
    store.config.write_bytes((native / '.aws/config').read_bytes())
    restored = tmp_path / 'restart'
    (restored / '.aws').mkdir(parents=True)
    store.seed(restored)
    assert not (restored / '.aws/login').exists()


@pytest.fixture
def selected_environment(store, monkeypatch):
    monkeypatch.setenv('HOME', str(store.home))
    monkeypatch.setenv('AWS_CONFIG_FILE', str(store.config))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(store.credentials))
    monkeypatch.setenv('AWS_LOGIN_CACHE_DIRECTORY', str(store.login))


@pytest.mark.parametrize('expiry', [1, 1030])
def test_next_launch_refresh_masks_owned_snapshot_at_refresh_deadline(store, native, selected_environment, monkeypatch, expiry):
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    monkeypatch.setattr(worker.time, 'time', lambda: 1000)
    store.credentials.write_bytes(store.credentials.read_bytes().replace(b'9999999999', str(expiry).encode()))
    def resolve(config):
        assert p.parsed(Path(os.environ['AWS_SHARED_CREDENTIALS_FILE']).read_bytes()).sections() == []
        assert config['account'] == '123456789012'
        assert config['principal'] == 'arn:aws:iam::123456789012:user/example'
        return dict(AccessKeyId='newkey', SecretAccessKey='newsecret', SessionToken='newtoken',
                    expires_at=9999999999, identity=dict(Account=config['account'], Arn=config['principal']), region='us-east-1')
    monkeypatch.setattr(worker, '_ambient', resolve)
    result = worker.ambient(dict(profile='chosen', region='us-east-1'))
    assert result['AccessKeyId'] == 'newkey'
    assert 'newkey' in store.credentials.read_text()
    assert os.environ['AWS_SHARED_CREDENTIALS_FILE'] == str(store.credentials)


@pytest.mark.parametrize('changes', [dict(account='999999999999'), dict(principal='arn:aws:iam::123456789012:user/other')])
def test_snapshot_identity_does_not_override_explicit_assertions(store, native, selected_environment, changes):
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    with pytest.raises(ValueError):
        worker.ambient(dict(profile='chosen', region='us-east-1', **changes))


def test_unavailable_native_snapshot_is_not_published_and_environment_restored(store, native, selected_environment, monkeypatch):
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    monkeypatch.setattr(worker.time, 'time', lambda: 9999999990)
    before = store.credentials.read_bytes()
    monkeypatch.delenv('AWS_SHARED_CREDENTIALS_FILE')
    monkeypatch.setattr(worker, '_ambient', lambda config: {'unavailable':True, 'sso':{}})
    result = worker.ambient(dict(profile='chosen'))
    assert result == {'unavailable':True, 'sso':{}, 'identity': store.snapshot()['identity']}
    assert 'AWS_SHARED_CREDENTIALS_FILE' not in os.environ
    assert store.credentials.read_bytes() == before


def test_native_worker_persists_only_after_exact_tuple_verification(store, native, selected_environment, monkeypatch):
    from api_agent.llm_providers.bedrock import login_worker as worker
    original_home = str(store.home)
    monkeypatch.setenv('HOME', str(native))
    monkeypatch.setattr(worker.shutil, 'which', lambda name: '/synthetic/aws')
    calls = []
    def cli(command, environment, **kwargs):
        calls.append(command)
        if command[-1] == '--version':
            return b'aws-cli/2.32.0'
        if 'export-credentials' in command:
            return json.dumps(dict(Version=1, AccessKeyId='key', SecretAccessKey='secret', SessionToken='token', Expiration='2286-11-20T17:46:39Z')).encode()
        return b''
    monkeypatch.setattr(worker, 'cli', cli)
    def verify(material, config):
        assert material == dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token')
        assert not store.credentials.exists()
        return dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example')
    monkeypatch.setattr(worker, 'verify_identity', verify)
    frames = []
    monkeypatch.setattr(worker, 'emit', frames.append)
    worker.run(dict(profile='chosen', region='us-east-1', method='direct', login=True,
        persistence=dict(home=original_home, config=str(store.config), credentials=str(store.credentials), login=str(store.login))))
    assert frames[-1]['result']['AccessKeyId'] == 'key'
    assert store.snapshot() is not None


@pytest.mark.parametrize('expiry', [None, True, float('inf'), 1])
def test_unknown_invalid_or_expired_snapshot_is_not_published(store, native, expiry):
    with pytest.raises(ValueError, match='material'):
        store.publish(native, dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token'), expiry, {})
    assert not store.credentials.exists()


@pytest.mark.parametrize('key', ['AccessKeyId', 'SecretAccessKey', 'SessionToken'])
@pytest.mark.parametrize('value', ['', 'injected\n[other]', None])
def test_snapshot_rejects_unserializable_or_injected_tuple(store, native, key, value):
    material = dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token')
    material[key] = value
    with pytest.raises(ValueError, match='material'):
        store.publish(native, material, 9999999999, {})
    assert not store.credentials.exists()


def test_seed_respects_explicit_native_route(store, native):
    store.config.write_bytes(b'[profile chosen]\nlogin_session = previous\n')
    before = (native / '.aws/config').read_bytes()
    store.seed(native, configured=True)
    assert (native / '.aws/config').read_bytes() == before


def test_rollback_refuses_to_erase_concurrent_cache_change(store, native, monkeypatch):
    original = p.atomic
    cache = store.cache_paths((native / '.aws/config').read_bytes())[0][0]
    def fail_and_change(path, data, expected):
        if Path(path) == store.credentials:
            cache.write_bytes(b'new concurrent writer')
            raise OSError
        original(path, data, expected)
    monkeypatch.setattr(p, 'atomic', fail_and_change)
    with pytest.raises(ValueError, match='rollback'):
        publish(store, native)
    assert cache.read_bytes() == b'new concurrent writer'


def test_new_cache_is_removed_after_failed_publication(store, native, monkeypatch):
    original = p.atomic
    cache = store.cache_paths((native / '.aws/config').read_bytes())[0][0]
    def fail(path, data, expected):
        if Path(path) == store.credentials:
            raise OSError
        original(path, data, expected)
    monkeypatch.setattr(p, 'atomic', fail)
    with pytest.raises(OSError):
        publish(store, native)
    assert not store.config.exists()
    assert not cache.exists()


def test_selected_profile_mutation_during_handoff_is_rejected(store, native):
    store.seed(native)
    store.config.write_bytes(b'[profile chosen]\nregion = us-west-2\n')
    with pytest.raises(ValueError, match='concurrently'):
        publish(store, native)
    assert store.config.read_bytes() == b'[profile chosen]\nregion = us-west-2\n'
    assert not store.credentials.exists()


def test_unrelated_profile_added_during_handoff_survives(store, native):
    store.seed(native)
    store.config.write_bytes(b'[profile unrelated]\noutput = text\n')
    publish(store, native)
    assert store.config.read_bytes().startswith(b'[profile unrelated]\noutput = text\n')


def test_atomic_checks_anchored_file_even_if_path_read_is_stale(tmp_path, monkeypatch):
    target = tmp_path / 'file'
    target.write_bytes(b'concurrent')
    monkeypatch.setattr(p, 'read', lambda path: b'before')
    with pytest.raises(ValueError, match='concurrently'):
        p.atomic(target, b'after', b'before')
    assert target.read_bytes() == b'concurrent'
    assert list(tmp_path.iterdir()) == [target]


def test_parent_link_swap_cannot_redirect_atomic_publication(tmp_path, monkeypatch):
    parent = tmp_path / 'parent'
    parent.mkdir()
    target = parent / 'file'
    target.write_bytes(b'before')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'file').write_bytes(b'unrelated')
    original = p.read
    count = 0
    def swap(path):
        nonlocal count
        count += 1
        if count == 2:
            parent.rename(tmp_path / 'held')
            parent.symlink_to(outside, target_is_directory=True)
        return original(path)
    monkeypatch.setattr(p, 'read', swap)
    with pytest.raises(OSError):
        p.atomic(target, b'after', b'before')
    assert (outside / 'file').read_bytes() == b'unrelated'
    assert list((tmp_path / 'held').iterdir()) == [tmp_path / 'held/file']
    assert (tmp_path / 'held/file').read_bytes() == b'before'


def test_failed_identity_verification_cannot_publish_snapshot(store, native, selected_environment, monkeypatch):
    from api_agent.llm_providers.bedrock import login_worker as worker
    monkeypatch.setenv('HOME', str(native))
    monkeypatch.setattr(worker.shutil, 'which', lambda name: '/synthetic/aws')
    exported = json.dumps(dict(Version=1, AccessKeyId='key', SecretAccessKey='secret', SessionToken='token', Expiration='2286-11-20T17:46:39Z')).encode()
    outputs = iter((b'aws-cli/2.32.0', exported))
    monkeypatch.setattr(worker, 'cli', lambda *args, **kwargs: next(outputs))
    def reject(*args):
        raise ValueError
    monkeypatch.setattr(worker, 'verify_identity', reject)
    with pytest.raises(worker.AcquisitionFailure) as failure:
        worker.run(dict(profile='chosen', region='us-east-1', method='direct', login=False,
            persistence=dict(home=str(store.home), config=str(store.config), credentials=str(store.credentials), login=str(store.login))))
    assert failure.value.stage == 'identity_verification'
    assert not store.config.exists()
    assert not store.credentials.exists()


@pytest.mark.asyncio
async def test_snapshot_identity_survives_native_cache_expiry_into_attended_recovery(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import sys
    from api_agent.llm_providers.bedrock import aws_login
    from api_agent.llm_providers.bedrock.acquisition import SessionRequest
    from api_agent.providers.contracts import ContractError
    monkeypatch.setenv('HOME', str(tmp_path))
    identity = dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/original')
    owner = aws_login.AwsLoginAcquirer({})
    async def unavailable(config, environment):
        return dict(unavailable=True, sso={}, identity=identity)
    monkeypatch.setattr(owner._ambient.transport, 'acquire', unavailable)
    class Channel:
        async def open(self):
            pass
        async def aclose(self):
            pass
    monkeypatch.setitem(sys.modules, 'api_agent.llm_providers.bedrock.login_interaction', SimpleNamespace(LoginInteraction=lambda request: Channel()))
    async def different_identity(config, environment, interaction):
        assert config['account'] == identity['Account']
        assert config['principal'] == identity['Arn']
        return dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token', expires_at=9999999999,
            identity=dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/different'))
    monkeypatch.setattr(owner._transport, 'acquire', different_identity)
    try:
        with pytest.raises(ContractError, match='identity changed'):
            await owner.acquire(SessionRequest('chosen', 'us-east-1', None, True, interaction=object()))
        assert owner._logged_in is False
        assert owner._verified_identity == (identity['Account'], identity['Arn'])
    finally:
        await owner.aclose()


def test_native_direct_route_does_not_retain_old_sso_source(store, native):
    store.config.write_bytes(b'[profile chosen]\nsso_session = old\noutput = text\n')
    publish(store, native)
    route = p.parsed(store.config.read_bytes())['profile chosen']
    assert 'sso_session' not in route
    assert route['login_session'] == 'example'
    assert route['output'] == 'text'


@pytest.mark.parametrize('syntax', ['tilde', 'variable', 'relative'])
def test_sdk_paths_are_frozen_in_original_environment_without_workspace_exports(tmp_path, monkeypatch, native, syntax):
    from api_agent.llm_providers.bedrock import aws_login
    home = tmp_path / 'user-home'
    home.mkdir()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('SYNTHETIC_AWS_HOME', str(home))
    prefix = {'tilde':'~', 'variable':'$SYNTHETIC_AWS_HOME', 'relative':'../user-home'}[syntax]
    monkeypatch.setenv('AWS_CONFIG_FILE', prefix + '/.aws/config')
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', prefix + '/.aws/credentials')
    monkeypatch.setenv('AWS_LOGIN_CACHE_DIRECTORY', prefix + '/.aws/login/cache')
    owner = aws_login.AwsLoginAcquirer({})
    assert owner._persistence == dict(home=str(home), config=str(home / '.aws/config'),
        credentials=str(home / '.aws/credentials'), login=str(home / '.aws/login/cache'))
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv('HOME', str(elsewhere))
    monkeypatch.setenv('SYNTHETIC_AWS_HOME', str(elsewhere))
    publish(p.ProfileStore(owner._persistence, 'chosen'), native)
    assert (home / '.aws/credentials').is_file()
    assert (home / '.aws/config').is_file()
    assert list(workspace.iterdir()) == []
    assert list(elsewhere.iterdir()) == []


def test_default_sdk_paths_expand_against_original_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for key in ('AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_LOGIN_CACHE_DIRECTORY'):
        monkeypatch.delenv(key, raising=False)
    assert p.capture_paths() == dict(home=str(tmp_path), config=str(tmp_path / '.aws/config'),
        credentials=str(tmp_path / '.aws/credentials'), login=str(tmp_path / '.aws/login/cache'))


def test_persistence_protocol_rejects_unfrozen_relative_paths(store):
    with pytest.raises(ValueError, match='absolute'):
        p.ProfileStore(dict(home=str(store.home), config='relative/config', credentials=str(store.credentials), login=str(store.login)), 'chosen')


@pytest.mark.asyncio
@pytest.mark.parametrize('syntax', ['tilde', 'variable', 'relative'])
async def test_ordinary_ambient_acquisition_uses_frozen_native_locations(tmp_path, monkeypatch, syntax):
    from api_agent.llm_providers.bedrock import aws_login
    from api_agent.llm_providers.bedrock.acquisition import SessionRequest
    original = tmp_path / 'original'
    original.mkdir()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv('HOME', str(original))
    monkeypatch.setenv('SYNTHETIC_AWS_HOME', str(original))
    prefix = {'tilde':'~', 'variable':'$SYNTHETIC_AWS_HOME', 'relative':'../original'}[syntax]
    monkeypatch.setenv('AWS_CONFIG_FILE', prefix + '/.aws/config')
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', prefix + '/.aws/credentials')
    monkeypatch.setenv('AWS_LOGIN_CACHE_DIRECTORY', prefix + '/.aws/login/cache')
    owner = aws_login.AwsLoginAcquirer({})
    changed = tmp_path / 'changed'
    changed.mkdir()
    monkeypatch.chdir(changed)
    monkeypatch.setenv('HOME', str(changed))
    monkeypatch.setenv('SYNTHETIC_AWS_HOME', str(changed))
    monkeypatch.setenv('AWS_CONFIG_FILE', 'changed-config')
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', 'changed-credentials')
    monkeypatch.setenv('AWS_LOGIN_CACHE_DIRECTORY', 'changed-cache')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'deliberately-current')
    monkeypatch.setenv('AWS_PROFILE', 'unselected-ambient')
    async def resolved(config, environment):
        assert config['profile'] == 'chosen'
        assert environment['HOME'] == str(original)
        assert environment['AWS_CONFIG_FILE'] == str(original / '.aws/config')
        assert environment['AWS_SHARED_CREDENTIALS_FILE'] == str(original / '.aws/credentials')
        assert environment['AWS_LOGIN_CACHE_DIRECTORY'] == str(original / '.aws/login/cache')
        assert environment['AWS_ACCESS_KEY_ID'] == 'deliberately-current'
        assert environment['AWS_PROFILE'] == 'unselected-ambient'
        return dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken=None,
            expires_at=None, region='us-east-1', identity=dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example'))
    monkeypatch.setattr(owner._ambient.transport, 'acquire', resolved)
    try:
        material = await owner.acquire(SessionRequest('chosen', 'us-east-1', None, False))
        assert material.access_key == 'key'
        assert owner._root is None
        assert list(workspace.iterdir()) == []
        assert list(changed.iterdir()) == []
    finally:
        await owner.aclose()


@pytest.mark.parametrize('final_newline', [False, True])
def test_sso_session_before_last_profile_survives_seed_publish_restart(store, tmp_path, final_newline):
    session = b'[sso-session demo]\nsso_start_url = https://example.invalid/start\nsso_region = us-east-1\n'
    unrelated = b'[profile other]\n# retain exact unrelated settings\noutput=text\n'
    profile = b'[profile chosen]\nsso_session = demo' + (b'\n' if final_newline else b'')
    original = session + unrelated + profile
    store.config.write_bytes(original)
    cache_name = hashlib.sha1(b'demo').hexdigest() + '.json'
    source = store.home / '.aws/sso/cache' / cache_name
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"synthetic":"refresh-token"}')
    unrelated_cache = source.parent / 'unrelated.json'
    unrelated_cache.write_bytes(b'preserve unrelated cache')

    seeded = tmp_path / 'seeded'
    store.seed(seeded)
    seeded_config = p.parsed((seeded / '.aws/config').read_bytes())
    assert seeded_config['profile chosen']['sso_session'] == 'demo'
    assert seeded_config['sso-session demo']['sso_start_url'] == 'https://example.invalid/start'
    assert (seeded / '.aws/sso/cache' / cache_name).read_bytes() == source.read_bytes()
    assert store.config.read_bytes() == original

    publish(store, seeded)
    restarted = p.ProfileStore(dict(home=store.home, config=store.config,
        credentials=store.credentials, login=store.login), 'chosen')
    restored = tmp_path / 'restart'
    restarted.seed(restored)
    restored_config = p.parsed((restored / '.aws/config').read_bytes())
    assert restored_config['profile chosen']['sso_session'] == 'demo'
    assert restored_config['sso-session demo']['sso_region'] == 'us-east-1'
    assert (restored / '.aws/sso/cache' / cache_name).read_bytes() == b'{"synthetic":"refresh-token"}'
    assert restarted.snapshot()['identity']['Account'] == '123456789012'
    assert p.section(store.config.read_bytes(), 'profile other')[2] == unrelated
    assert p.section(store.config.read_bytes(), 'sso-session demo')[2] == session
    assert unrelated_cache.read_bytes() == b'preserve unrelated cache'


@pytest.mark.parametrize('final_newline', [False, True])
def test_missing_referenced_sso_section_keeps_selected_profile_bytes(store, final_newline):
    profile = b'[profile chosen]\nsso_session = missing' + (b'\n' if final_newline else b'')
    assert store.selected_config(profile) == profile


@pytest.fixture
def sdk_login_restart(store, native, selected_environment, monkeypatch):
    """Publish the public SDK token schema, then resolve in a fresh SDK session."""
    import botocore.credentials
    from api_agent.llm_providers.bedrock import login_worker as worker
    cache = next((native / '.aws/login/cache').iterdir())
    cache.write_text(json.dumps(dict(accessToken=dict(accessKeyId='cached-key',
        secretAccessKey='cached-secret', sessionToken='cached-token',
        expiresAt='2286-11-20T17:46:39Z', accountId='123456789012'),
        refreshToken='synthetic-refresh', dpopKey='synthetic-key', clientId='synthetic-client')))
    publish(store, native)
    verified = []
    def verify(material, config, **kwargs):
        verified.append(material)
        return dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example')
    monkeypatch.setattr(worker, 'verify_identity', verify)
    return worker, botocore.credentials, verified


def test_sdk_restart_reads_published_login_cache_after_masking_snapshot(sdk_login_restart, store, monkeypatch):
    worker, sdk, verified = sdk_login_restart
    monkeypatch.setattr(worker.time, 'time', lambda: 9999999990)
    # The SDK checks dependency presence before reading an unexpired token; no
    # crypto operation is needed for this synthetic, network-free cache read.
    monkeypatch.setattr(sdk, 'EC', object())
    result = worker.ambient(dict(profile='chosen', region='us-east-1'))
    assert result['AccessKeyId'] == 'cached-key'
    assert result['expires_at'] == 9999999999
    assert verified == [dict(AccessKeyId='cached-key', SecretAccessKey='cached-secret', SessionToken='cached-token')]
    assert p.parsed(store.credentials.read_bytes())['chosen']['aws_access_key_id'] == 'cached-key'


def test_sdk_restart_missing_login_crypto_reports_setup(sdk_login_restart, monkeypatch):
    worker, sdk, verified = sdk_login_restart
    monkeypatch.setattr(worker.time, 'time', lambda: 9999999990)
    monkeypatch.setattr(sdk, 'EC', None)
    with pytest.raises(ImportError, match='dependency unavailable'):
        worker.ambient(dict(profile='chosen', region='us-east-1'))
    assert verified == []


def test_bedrock_install_routes_include_native_login_crypto():
    import tomllib
    from packaging.requirements import Requirement
    root = Path(__file__).resolve().parents[2]
    package = tomllib.loads((root / 'pyproject.toml').read_text())
    provider = tomllib.loads((root / 'src/api_agent/llm_providers/bedrock/provider-dependencies.toml').read_text())
    for dependencies in (package['project']['optional-dependencies']['bedrock'], provider['dependencies']):
        boto = next(Requirement(value) for value in dependencies if Requirement(value).name == 'boto3')
        assert 'crt' in boto.extras
        assert boto.specifier.contains('1.41.0')
        assert not boto.specifier.contains('2.0.0')


def test_valid_snapshot_reuses_exact_tuple_without_native_cache(store, native, selected_environment, monkeypatch):
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    next(store.login.iterdir()).unlink()
    before = store.credentials.read_bytes()
    verified = []
    def verify(material, config):
        verified.append((material, config))
        assert config['account'] == '123456789012'
        assert config['principal'] == 'arn:aws:iam::123456789012:user/example'
        return store.snapshot()['identity']
    monkeypatch.setattr(worker, 'verify_identity', verify)
    monkeypatch.setattr(worker, '_ambient', lambda config: pytest.fail('native resolution'))
    result = worker.ambient(dict(profile='chosen'))
    assert result == dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token',
                         expires_at=9999999999, region='us-east-1', identity=store.snapshot()['identity'])
    assert verified[0][0] == dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token')
    assert store.credentials.read_bytes() == before


@pytest.mark.parametrize('expiry', [None, True, '9999999999', float('nan'), float('inf')])
def test_owned_snapshot_rejects_invalid_expiry(store, native, selected_environment, expiry):
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    metadata = store.snapshot()
    metadata['expires_at'] = expiry
    store.credentials.write_bytes(p.merge_fields(store.credentials.read_bytes(), 'chosen', {p.MARKER:json.dumps(metadata)}))
    with pytest.raises(ValueError, match='expiry'):
        worker.ambient(dict(profile='chosen', region='us-east-1'))


@pytest.mark.parametrize('region', [None, 'bad'])
def test_snapshot_requires_valid_region(store, native, selected_environment, region):
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    store.config.write_bytes(b'[profile chosen]\n')
    with pytest.raises(ValueError, match='region'):
        worker.ambient(dict(profile='chosen', region=region))


def test_snapshot_verification_failure_never_tries_native(store, native, selected_environment, monkeypatch):
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    def reject(*args):
        raise ValueError('synthetic identity mismatch')
    monkeypatch.setattr(worker, 'verify_identity', reject)
    monkeypatch.setattr(worker, '_ambient', lambda config: pytest.fail('native fallback'))
    with pytest.raises(worker.AcquisitionFailure) as failure:
        worker.ambient(dict(profile='chosen', region='us-east-1'))
    assert failure.value.stage == 'identity_verification'


def test_snapshot_reread_rejects_concurrent_tuple_edit(store, native, selected_environment, monkeypatch):
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    original = worker.ProfileStore.snapshot
    def changed(self):
        marker = original(self)
        self.credentials.write_bytes(self.credentials.read_bytes().replace(b'= key\n', b'= changed\n'))
        return marker
    monkeypatch.setattr(worker.ProfileStore, 'snapshot', changed)
    with pytest.raises(ValueError, match='changed'):
        worker.ambient(dict(profile='chosen', region='us-east-1'))


@pytest.mark.parametrize('identity', [
    dict(Account='999999999999', Arn='arn:aws:iam::123456789012:user/example'),
    dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/other'),
])
def test_snapshot_sts_must_match_stored_identity(store, native, selected_environment, monkeypatch, identity):
    from types import SimpleNamespace
    import boto3
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    monkeypatch.setattr(boto3, 'client', lambda *args, **kwargs:
        SimpleNamespace(get_caller_identity=lambda:identity, close=lambda:None))
    with pytest.raises(worker.AcquisitionFailure) as failure:
        worker.ambient(dict(profile='chosen', region='us-east-1'))
    assert failure.value.stage == 'identity_verification'


def test_expired_snapshot_missing_native_cache_returns_pinned_unavailable(sdk_login_restart, store, monkeypatch):
    worker, sdk, verified = sdk_login_restart
    monkeypatch.setattr(sdk, 'EC', object())
    store.credentials.write_bytes(store.credentials.read_bytes().replace(b'9999999999', b'1'))
    next(store.login.iterdir()).unlink()
    before = store.credentials.read_bytes()
    result = worker.ambient(dict(profile='chosen', region='us-east-1'))
    assert result['unavailable'] is True
    assert result['identity'] == store.snapshot()['identity']
    assert verified == []
    assert store.credentials.read_bytes() == before


def test_expired_snapshot_malformed_native_cache_does_not_prompt(sdk_login_restart, store, monkeypatch):
    from botocore.exceptions import LoginTokenLoadError
    worker, sdk, verified = sdk_login_restart
    monkeypatch.setattr(sdk, 'EC', object())
    store.credentials.write_bytes(store.credentials.read_bytes().replace(b'9999999999', b'1'))
    next(store.login.iterdir()).write_text('{}')
    with pytest.raises(LoginTokenLoadError):
        worker.ambient(dict(profile='chosen', region='us-east-1'))
    assert verified == []


@pytest.mark.parametrize('selection,expected', [
    ({'AWS_PROFILE':'other'}, 'other-key'),
    ({'AWS_DEFAULT_PROFILE':'other'}, 'other-key'),
    ({'AWS_PROFILE':'chosen', 'AWS_DEFAULT_PROFILE':'other'}, 'other-key'),
    ({'AWS_PROFILE':'other', 'AWS_ACCESS_KEY_ID':'env-key', 'AWS_SECRET_ACCESS_KEY':'env-secret'}, 'env-key'),
])
def test_implicit_profile_preserves_sdk_profile_and_environment_precedence(store, native, selected_environment, monkeypatch, selection, expected):
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    default = p.section(store.credentials.read_bytes(), 'chosen')[2].replace(b'[chosen]', b'[default]')
    store.credentials.write_bytes(default + b'[other]\naws_access_key_id=other-key\naws_secret_access_key=other-secret\n')
    for name in ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    for name, value in selection.items():
        monkeypatch.setenv(name, value)
    checked = []
    def verify(material, config, **kwargs):
        checked.append(material['AccessKeyId'])
        return dict(Account='999999999999', Arn='arn:aws:iam::999999999999:user/other')
    monkeypatch.setattr(worker, 'verify_identity', verify)
    result = worker.ambient(dict(profile=None, region='us-east-1'))
    assert result['AccessKeyId'] == expected
    assert result['identity']['Account'] == '999999999999'
    assert checked == [expected]


@pytest.fixture(params=['default', 'chosen'])
def implicit_owned_snapshot(request, store, native, selected_environment, monkeypatch):
    from api_agent.llm_providers.bedrock import login_worker as worker
    for name in ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'):
        monkeypatch.delenv(name, raising=False)
    profile = request.param
    if profile == 'default':
        store.profile, store.name = 'default', 'default'
        path = native / '.aws/config'
        path.write_bytes(path.read_bytes().replace(b'[profile chosen]', b'[default]'))
    else:
        monkeypatch.setenv('AWS_PROFILE', profile)
    publish(store, native)
    monkeypatch.setattr(worker.time, 'time', lambda:1000)
    store.credentials.write_bytes(store.credentials.read_bytes().replace(b'9999999999', b'1040'))
    return store, worker


def test_implicit_owned_snapshot_preserves_known_expiry_and_identity(implicit_owned_snapshot, monkeypatch):
    store, worker = implicit_owned_snapshot
    next(store.login.iterdir()).unlink()
    checked = []
    def verify(material, config):
        checked.append((material, config))
        return store.snapshot()['identity']
    monkeypatch.setattr(worker, 'verify_identity', verify)
    result = worker.ambient(dict(profile=None, region='us-east-1'))
    assert result['expires_at'] == 1040
    assert checked[0][0] == dict(AccessKeyId='key', SecretAccessKey='secret', SessionToken='token')
    assert checked[0][1]['account'] == '123456789012'
    assert checked[0][1]['principal'] == 'arn:aws:iam::123456789012:user/example'


def test_implicit_owned_snapshot_applies_stored_sts_binding(implicit_owned_snapshot, monkeypatch):
    from types import SimpleNamespace
    import boto3
    _, worker = implicit_owned_snapshot
    monkeypatch.setattr(boto3, 'client', lambda *args, **kwargs:SimpleNamespace(
        get_caller_identity=lambda:dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/other'), close=lambda:None))
    with pytest.raises(worker.AcquisitionFailure) as failure:
        worker.ambient(dict(profile=None, region='us-east-1'))
    assert failure.value.stage == 'identity_verification'


@pytest.mark.parametrize('expiry', [1030, 999])
def test_implicit_owned_snapshot_masks_at_native_refresh_deadline(implicit_owned_snapshot, monkeypatch, expiry):
    store, worker = implicit_owned_snapshot
    monkeypatch.setattr(worker, 'verify_identity', lambda *args:store.snapshot()['identity'])
    store.credentials.write_bytes(store.credentials.read_bytes().replace(b'1040', str(expiry).encode()))
    def resolve(config):
        assert config['profile'] is None
        assert store.profile not in p.parsed(Path(os.environ['AWS_SHARED_CREDENTIALS_FILE']).read_bytes()).sections()
        assert config['account'] == '123456789012'
        return dict(unavailable=True, sso={})
    monkeypatch.setattr(worker, '_ambient', resolve)
    result = worker.ambient(dict(profile=None, region='us-east-1'))
    assert result == dict(unavailable=True, sso={}, identity=store.snapshot()['identity'])


def test_implicit_environment_credentials_shadow_owned_snapshot(implicit_owned_snapshot, monkeypatch):
    store, worker = implicit_owned_snapshot
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'env-key')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'env-secret')
    # Unrelated owned metadata must not restrict deliberate environment selection.
    store.credentials.write_bytes(store.credentials.read_bytes().replace(b'1040', b'null'))
    checked = []
    def verify(material, config, **kwargs):
        checked.append(material['AccessKeyId'])
        assert not config.get('account') and not config.get('principal')
        return dict(Account='999999999999', Arn='arn:aws:iam::999999999999:user/other')
    monkeypatch.setattr(worker, 'verify_identity', verify)
    result = worker.ambient(dict(profile=None, region='us-east-1'))
    assert result['AccessKeyId'] == 'env-key'
    assert result['expires_at'] is None
    assert checked == ['env-key']


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('request_region,environment_region,profile_region,expected', [
    (None, 'us-west-2', 'us-east-1', 'us-west-2'),
    ('eu-west-1', 'us-west-2', 'us-east-1', 'eu-west-1'),
    (None, None, 'us-east-1', 'us-east-1'),
    (None, None, None, None),
])
def test_snapshot_uses_sdk_region_precedence(implicit_owned_snapshot, monkeypatch, explicit, request_region, environment_region, profile_region, expected):
    store, worker = implicit_owned_snapshot
    monkeypatch.delenv('AWS_DEFAULT_REGION', raising=False)
    if environment_region:
        monkeypatch.setenv('AWS_DEFAULT_REGION', environment_region)
    if profile_region is None:
        store.config.write_bytes(store.config.read_bytes().replace(b'region = us-east-1\n', b''))
    regions = []
    def verify(material, config):
        regions.append(config['region'])
        return store.snapshot()['identity']
    monkeypatch.setattr(worker, 'verify_identity', verify)
    config = dict(profile=store.profile if explicit else None, region=request_region)
    if expected is None:
        with pytest.raises(ValueError, match='region'):
            worker.ambient(config)
        assert regions == []
    else:
        result = worker.ambient(config)
        assert result['region'] == expected
        assert regions == [expected]


def test_explicit_snapshot_region_discovery_does_not_resolve_credentials(store, native, selected_environment, monkeypatch):
    from types import SimpleNamespace
    import boto3
    from api_agent.llm_providers.bedrock import login_worker as worker
    publish(store, native)
    def session(**kwargs):
        assert kwargs == dict(profile_name='chosen')
        return SimpleNamespace(region_name='us-west-2', get_credentials=lambda:pytest.fail('credential resolution'))
    monkeypatch.setattr(boto3, 'Session', session)
    monkeypatch.setattr(worker, 'verify_identity', lambda material, config:store.snapshot()['identity'])
    assert worker.ambient(dict(profile='chosen', region=None))['region'] == 'us-west-2'
