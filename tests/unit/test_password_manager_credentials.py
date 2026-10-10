"""Common field retrieval conforms across bundled and independently authored adapters."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.auth import credentials, profiles
from api_agent.auth.managers import (ManagerDefinition, ManagerError, PasswordManagerRegistry,
                                     bundled_registry, load_manager_registry)
from api_agent.auth.password_managers import keeper, onepassword
from test_credentials import fake_cli, source


@pytest.mark.parametrize('manager,kind,args,output,expected', [
    ('keeper', 'username', ['clipboard-copy', "vault with spaces/user ' name", '--field', 'login', '--output', 'stdout'], ' identity \n', ' identity '),
    ('keeper', 'password', ['find-password', "vault with spaces/user ' name"], ' secret \n\n', ' secret \n'),
    ('keeper', 'totp', ['clipboard-copy', "vault with spaces/user ' name", '--totp', '--output', 'stdout', '-t'], '123456\n', '123456'),
    ('1password', 'username', ['item', 'get', "user ' name", '--vault', 'vault with spaces', '--fields', 'username'], '"last,first""alias"\n', 'last,first"alias'),
    ('1password', 'password', ['item', 'get', "user ' name", '--vault', 'vault with spaces', '--fields', 'password', '--reveal'], '" secret,with""quote\nline "\n', ' secret,with"quote\nline '),
    ('1password', 'totp', ['item', 'get', "user ' name", '--vault', 'vault with spaces', '--otp'], '123456\n', '123456'),
])
def test_bundled_fields_use_exact_argv_and_preserve_independent_values(tmp_path, manager, kind, args, output, expected):
    cli = fake_cli(tmp_path, f"assert sys.argv[1:] == {args!r}\nassert sys.stdin.read() == ''\nsys.stdout.write({output!r})")
    descriptor = credentials.CredentialSource.from_mapping(source(cli, manager))
    assert getattr(credentials, 'get_' + kind)(descriptor) == expected
    assert descriptor.user == "user ' name"
    assert '_registry' not in repr(descriptor)


def third_registry(tmp_path, implementation=None):
    pack = tmp_path / 'pack'
    (pack / 'cfg').mkdir(parents=True)
    module = pack / 'synthetic.py'
    module.write_text(implementation or '''version = 1

def command(source, kind, cli):
    return [cli, 'fetch', kind, source.vault, source.user]

def parse(output, kind):
    prefix, value = output.split('=', 1)
    if prefix != kind:
        raise ValueError('wrong field')
    return value.removesuffix('\\n')
''')
    (pack / 'cfg/password_managers.toml').write_text('''version=1
[[managers]]
id="synthetic"
label="Synthetic manager"
implementation="../synthetic.py"
cli_default="synthetic-cli"
''')
    return load_manager_registry(pack_root=pack), pack


@pytest.mark.parametrize('kind,value', [('username', 'actual identity'), ('password', ' secret with spaces '), ('totp', '987654')])
def test_pack_adapter_uses_unchanged_consumer_and_bound_registry(tmp_path, kind, value):
    registry, _ = third_registry(tmp_path)
    cli = fake_cli(tmp_path, f"assert sys.argv[1:] == ['fetch', {kind!r}, 'vault with spaces', \"user ' name\"]\nassert sys.stdin.read() == ''\nprint({(kind + '=' + value)!r})")
    descriptor = credentials.CredentialSource.from_mapping(source(cli, 'synthetic'), registry=registry)
    assert getattr(credentials, 'get_' + kind)(descriptor) == value
    assert descriptor._registry is registry
    assert registry.choices == (('1Password', '1password'), ('Keeper', 'keeper'), ('Synthetic manager', 'synthetic'))
    assert descriptor == credentials.CredentialSource('synthetic', cli, 'vault with spaces', "user ' name")
    assert '_registry' not in repr(descriptor)


def test_password_only_request_cannot_fetch_username_or_totp(tmp_path):
    registry, _ = third_registry(tmp_path)
    cli = fake_cli(tmp_path, "assert sys.argv[1:3] == ['fetch', 'password']\nprint('password=api-key-value')")
    canonical = {'password_manager': {'manager': 'synthetic', 'cli_path': cli},
                 'roles': {'Selected role': [{'vault': 'vault', 'user': 'entry'}]}}
    parsed = profiles.parse_profiles(canonical, ('Selected role',), registry=registry)
    selected = profiles.select_profiles(parsed, ('Selected role',)).for_role('Selected role')
    assert credentials.get_password(selected) == 'api-key-value'
    assert selected._registry is registry
    assert canonical['roles']['Selected role'] == [{'vault': 'vault', 'user': 'entry'}]
    assert set(profiles.normalize_auth(canonical, registry=registry)['password_manager']) == {'manager', 'cli_path'}


@pytest.mark.parametrize('legacy', [False, True])
def test_pack_defaults_and_explicit_executable_are_declared_without_helper_execution(tmp_path, legacy):
    registry, _ = third_registry(tmp_path)
    account = {'vault': 'vault', 'user': 'entry'}
    data = {'Selected role': [dict(manager='synthetic', cli_path='', **account)]} if legacy else {
        'password_manager': {'manager': 'synthetic', 'cli_path': ''}, 'roles': {'Selected role': [account]}}
    normalized = profiles.normalize_auth(data, registry=registry)
    assert normalized['password_manager'] == {'manager': 'synthetic', 'cli_path': 'synthetic-cli'}
    assert profiles.parse_profiles(data, ('Selected role',), registry=registry).for_role('Selected role')[0]._registry is registry
    normalized['password_manager']['cli_path'] = '/explicit executable'
    assert profiles.normalize_auth(normalized, registry=registry)['password_manager']['cli_path'] == '/explicit executable'


def test_removed_pack_manager_fails_before_cli_and_does_not_fallback(tmp_path, monkeypatch):
    registry, _ = third_registry(tmp_path)
    descriptor = source('synthetic-cli', 'synthetic')
    run = Mock(return_value='must not run')
    resolve = Mock(return_value='/resolved-cli')
    monkeypatch.setattr(credentials, '_run', run)
    monkeypatch.setattr(credentials, 'resolve_cli_path', resolve)
    with pytest.raises(credentials.CredentialError, match='unavailable'):
        credentials.CredentialSource.from_mapping(descriptor, registry=bundled_registry())
    with pytest.raises(ManagerError, match='restore its resource pack'):
        profiles.normalize_auth({'password_manager': {'manager': 'synthetic', 'cli_path': 'synthetic-cli'}, 'roles': {}}, registry=bundled_registry())
    run.assert_not_called()
    resolve.assert_not_called()
    assert credentials.CredentialSource.from_mapping(descriptor, registry=registry).manager == 'synthetic'


@pytest.mark.parametrize('implementation', [
    'version=True\ndef command(source, kind, cli): return [cli]\ndef parse(output, kind): return output\n',
    'version=1\ndef command(source): return []\ndef parse(output, kind): return output\n',
    'version=1\ndef command(source, kind, cli): return [cli]\nasync def parse(output, kind): return output\n',
    'version=1\ndef command(source, kind, cli): return [cli]\ndef parse(output, kind): yield output\n',
])
def test_invalid_adapter_interface_is_rejected_before_cli_resolution(tmp_path, monkeypatch, implementation):
    registry, _ = third_registry(tmp_path, implementation)
    descriptor = credentials.CredentialSource.from_mapping(source('synthetic-cli', 'synthetic'), registry=registry)
    run, resolve = Mock(), Mock()
    monkeypatch.setattr(credentials, '_run', run)
    monkeypatch.setattr(credentials, 'resolve_cli_path', resolve)
    with pytest.raises(credentials.CredentialError):
        credentials.get_password(descriptor)
    run.assert_not_called()
    resolve.assert_not_called()


@pytest.mark.parametrize('command', [(), [], ['/wrong-cli', 'valid'], ['/resolved-cli', 1], ['/resolved-cli', 'bad\0argument']])
def test_invalid_command_never_reaches_owned_process(tmp_path, monkeypatch, command):
    registry, _ = third_registry(tmp_path)
    monkeypatch.setattr(PasswordManagerRegistry, 'load', lambda *args: SimpleNamespace(
        command=lambda *args: command, parse=lambda *args: 'valid'))
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda *args: '/resolved-cli')
    run = Mock(return_value='valid')
    monkeypatch.setattr(credentials, '_run', run)
    descriptor = credentials.CredentialSource.from_mapping(source('synthetic-cli', 'synthetic'), registry=registry)
    with pytest.raises(credentials.CredentialError, match='invalid credential command'):
        credentials.get_password(descriptor)
    run.assert_not_called()


@pytest.mark.parametrize('stage,error', [('command', ManagerError('safe command rejection')),
    ('command', RuntimeError('private command details')), ('parse', ManagerError('safe parse rejection')),
    ('parse', RuntimeError('private parser details'))])
def test_adapter_failures_are_typed_and_unknown_details_withheld(tmp_path, monkeypatch, stage, error):
    registry, _ = third_registry(tmp_path)
    def failed(*args):
        raise error
    adapter = SimpleNamespace(command=failed if stage == 'command' else lambda *args: ['/resolved-cli'],
                              parse=failed if stage == 'parse' else lambda *args: 'valid')
    monkeypatch.setattr(PasswordManagerRegistry, 'load', lambda *args: adapter)
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda *args: '/resolved-cli')
    run = Mock(return_value='valid')
    monkeypatch.setattr(credentials, '_run', run)
    descriptor = credentials.CredentialSource.from_mapping(source('synthetic-cli', 'synthetic'), registry=registry)
    with pytest.raises(credentials.CredentialError) as caught:
        credentials.get_password(descriptor)
    assert ('safe' in str(caught.value)) is isinstance(error, ManagerError)
    assert 'private' not in str(caught.value)
    assert run.call_count == int(stage == 'parse')


@pytest.mark.parametrize('raw,parsed', [(None, 'valid'), (b'output', 'valid'), ('valid', None), ('valid', {'secret': 'value'})])
def test_output_types_are_validated_without_exposing_values(tmp_path, monkeypatch, raw, parsed):
    registry, _ = third_registry(tmp_path)
    parse = Mock(return_value=parsed)
    monkeypatch.setattr(PasswordManagerRegistry, 'load', lambda *args: SimpleNamespace(
        command=lambda *args: ['/resolved-cli'], parse=parse))
    monkeypatch.setattr(credentials, 'resolve_cli_path', lambda *args: '/resolved-cli')
    monkeypatch.setattr(credentials, '_run', lambda *args: raw)
    descriptor = credentials.CredentialSource.from_mapping(source('synthetic-cli', 'synthetic'), registry=registry)
    with pytest.raises(credentials.CredentialError, match='invalid') as caught:
        credentials.get_password(descriptor)
    assert 'secret' not in str(caught.value)
    assert parse.call_count == int(type(raw) is str)


@pytest.mark.parametrize('module', [keeper, onepassword])
def test_bundled_adapter_rejects_unsupported_direct_kind(module):
    with pytest.raises(ManagerError, match='Unsupported'):
        module.command(SimpleNamespace(vault='v', user='u'), 'unknown', '/cli')
    with pytest.raises(ManagerError, match='Unsupported'):
        module.parse('valid', 'unknown')


@pytest.mark.parametrize('module,platform,output,expected', [(keeper, 'nt', ' password \r\n', ' password '),
    (keeper, 'posix', ' password ', ' password '), (onepassword, 'nt', '123456\r\n', '123456'),
    (onepassword, 'posix', '123456', '123456')])
def test_raw_terminator_policy_is_platform_specific_and_preserves_content(monkeypatch, module, platform, output, expected):
    monkeypatch.setattr(module, 'os', SimpleNamespace(name=platform))
    assert module.parse(output, 'password' if module is keeper else 'totp') == expected


@pytest.mark.parametrize('manager', [None, 1, ['keeper']])
def test_manager_identifier_requires_a_string_before_registry_access(manager, monkeypatch):
    lookup = Mock(side_effect=AssertionError('must not load an invalid identifier'))
    registry = SimpleNamespace(get=lookup)
    with pytest.raises(ValueError, match='manager must be a string'):
        profiles.normalize_auth({'password_manager': {'manager': manager, 'cli_path': 'cli'},
                                 'roles': {}}, registry=registry)
    lookup.assert_not_called()


@pytest.mark.parametrize('marker,message', [('manager_conflict', 'different password managers'),
                                          ('invalid', 'malformed or invalid')])
def test_saved_auth_error_prevents_adapter_selection(marker, message):
    lookup = Mock(side_effect=AssertionError('saved invalid configuration must not load a manager'))
    with pytest.raises(ValueError, match=message):
        profiles.normalize_auth({'_auth_error': marker}, registry=SimpleNamespace(get=lookup))
    lookup.assert_not_called()
