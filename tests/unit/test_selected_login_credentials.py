"""Synthetic selected-record fields only; no vault or real key access."""
import base64
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_private_key

from api_agent.auth import credentials as c
from api_agent.auth.login_credentials import PasskeyCredential, SelectedLoginCredentials, decode_id
from api_agent.auth.password_managers import keeper
from api_agent.auth.managers import ManagerError, PasswordManagerRegistry

USERNAME = 'selected@example.test'
RP = 'example.test'
UID = base64.urlsafe_b64encode(b'synthetic-record').decode().rstrip('=')


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


@pytest.fixture
def selected():
    material = ec.derive_private_key(123456789, ec.SECP256R1())
    numbers = material.private_numbers()
    key = dict(kty='EC', crv='P-256', d=encoded(numbers.private_value.to_bytes(32)),
               x=encoded(numbers.public_numbers.x.to_bytes(32)), y=encoded(numbers.public_numbers.y.to_bytes(32)))
    passkey = dict(privateKey=key, credentialId=encoded(b'synthetic-credential'), userId=encoded(b'synthetic-user'),
                   username=USERNAME, relyingParty=RP, signCount=4, createdDate=0,
                   prf={'unretained-secret': 'synthetic-prf-value'})
    fields = [dict(name='record_uid', value=UID), dict(name='title', value='Selected login'),
              dict(name='Login', value=[USERNAME]), dict(name='Password', value=[]),
              dict(name='Passkey', value=[passkey])]
    return SimpleNamespace(source=SimpleNamespace(vault='Selected vault', user='Selected login'),
        listing=[dict(type='record', uid=UID, name='Selected login')], fields=fields, key=key, passkey=passkey)


def read(selected):
    command = Mock(side_effect=[json.dumps(selected.listing), json.dumps(selected.fields)])
    result = keeper.retrieve_login(selected.source, '/cli', USERNAME, RP, command)
    return result, command


def test_selected_passkey_availability_and_private_key_conversion(selected):
    result, command = read(selected)
    assert result.available and result.passkey_available and not result.password_available
    assert result.username == USERNAME and result.passkey.sign_count == 4
    expected = [[ '/cli', 'ls', '-l', '--format', 'json', 'Selected vault/Selected login'],
                ['/cli', 'get', UID, '--format', 'fields', '--unmask']]
    assert [call.args[0] for call in command.call_args_list] == expected
    converted = load_der_private_key(base64.b64decode(result.passkey.pkcs8_base64()), None)
    assert converted.private_numbers().private_value == 123456789
    for value in (USERNAME, UID, *selected.key.values(), selected.passkey['credentialId'], selected.passkey['userId']):
        if value in {'EC', 'P-256'}: continue
        assert value in result.secret_values()
        assert value not in repr(result) and value not in repr(result.passkey)
    assert RP not in result.secret_values() and RP not in repr(result.passkey)
    assert 'synthetic-prf-value' not in result.secret_values() and 'prf' not in repr(result.passkey)


def test_password_only_and_both_methods_preserve_whitespace(selected):
    selected.fields[3]['value'] = ['  synthetic password  ']
    result, _ = read(selected)
    assert result.password_available and result.passkey_available and result.password == '  synthetic password  '
    assert result.password in result.secret_values()
    selected.fields.pop()
    result, _ = read(selected)
    assert result.password_available and not result.passkey_available


def test_missing_record_and_missing_credential_are_distinct(selected):
    selected.listing[0]['name'] = 'Other login'
    result, command = read(selected)
    assert not result.available and result.secret_values() == () and command.call_count == 1
    selected.listing[0]['name'] = 'Selected login'
    selected.fields.pop()
    result, _ = read(selected)
    assert result.available and not result.password_available and not result.passkey_available


@pytest.mark.parametrize('change', ['record-duplicate', 'uid', 'returned-uid', 'title', 'login', 'rp',
    'passkey-user', 'passkey-duplicate', 'counter', 'kty', 'curve', 'd', 'x', 'y', 'key-ops', 'ext', 'alg'])
def test_invalid_or_ambiguous_selected_material_is_rejected_without_values(selected, change):
    if change == 'record-duplicate': selected.listing.append(deepcopy(selected.listing[0]))
    if change == 'uid': selected.listing[0]['uid'] = 'bad!'
    if change == 'returned-uid': selected.fields[0]['value'] = encoded(b'different-record')
    if change == 'title': selected.fields[1]['value'] = 'Other login'
    if change == 'login': selected.fields[2]['value'] = ['wrong@example.test']
    if change == 'rp': selected.passkey['relyingParty'] = 'other.test'
    if change == 'passkey-user': selected.passkey['username'] = 'wrong@example.test'
    if change == 'passkey-duplicate': selected.fields[-1]['value'].append(deepcopy(selected.passkey))
    if change == 'counter': selected.passkey['signCount'] = True
    if change == 'kty': selected.key['kty'] = 'RSA'
    if change == 'curve': selected.key['crv'] = 'P-384'
    if change == 'd': selected.key['d'] = encoded(bytes(32))
    if change == 'x': selected.key['x'] = encoded(bytes(32))
    if change == 'y': selected.key['y'] = encoded(bytes(32))
    if change == 'key-ops': selected.key['key_ops'] = ['verify']
    if change == 'ext': selected.key['ext'] = False
    if change == 'alg': selected.key['alg'] = 'ES384'
    with pytest.raises(ManagerError) as caught: read(selected)
    assert USERNAME not in str(caught.value) and selected.key.get('d', '') not in str(caught.value)


@pytest.fixture
def retrieval(selected, monkeypatch):
    monkeypatch.setattr(c, 'resolve_cli_path', lambda value: '/cli')
    source = dict(manager='keeper', cli_path='synthetic-cli', vault=selected.source.vault, user=selected.source.user)
    runner = Mock(side_effect=lambda argv, *args, **kwargs: json.dumps(selected.listing if argv[1] == 'ls' else selected.fields))
    monkeypatch.setattr(c, '_run', runner)
    return source, runner


def get(source, **kwargs):
    return c.get_login_credentials(source, expected_username=USERNAME, relying_party_id=RP, **kwargs)


def test_public_retrieval_uses_owned_capture_and_old_adapters_are_unsupported(retrieval):
    source, runner = retrieval
    assert get(source).passkey_available and runner.call_count == 2
    source['manager'] = '1password'
    with pytest.raises(c.CredentialCapabilityUnsupportedError): get(source)
    assert runner.call_count == 2


def test_recovery_uses_only_safe_probe_then_retries_private_get(selected, retrieval, monkeypatch):
    import threading
    from api_agent.auth import recovery
    source, runner = retrieval
    runner.side_effect = [json.dumps(selected.listing), c.CredentialLoginRequiredError('safe'), json.dumps(selected.fields)]
    recover = Mock(return_value='ignored safe probe result')
    monkeypatch.setattr(recovery, 'recover', recover)
    interaction = SimpleNamespace(cancel_event=threading.Event(), lock=threading.RLock())
    assert get(source, interaction=interaction, recovery_timeout=10).passkey_available
    assert recover.call_args.args[0] == ['/cli', 'whoami']
    assert '--unmask' not in recover.call_args.args[0]
    assert runner.call_args_list[1].args[0] == runner.call_args_list[2].args[0]


def test_locked_cli_and_command_failure_never_mean_absence(retrieval):
    source, runner = retrieval
    for error in (c.CredentialLoginRequiredError('locked'), c.CredentialError('command failed')):
        runner.side_effect = error
        with pytest.raises(type(error)): get(source)


@pytest.mark.parametrize('username,rp', [('', RP), ('\nprivate', RP), ('x' * 1025, RP),
    (USERNAME, 'https://example.test'), (USERNAME, 'UPPER.test'), (USERNAME, 'x' * 254), (None, RP)])
def test_request_validation_precedes_cli(retrieval, username, rp):
    source, runner = retrieval
    with pytest.raises(c.CredentialError):
        c.get_login_credentials(source, expected_username=username, relying_party_id=rp)
    runner.assert_not_called()


@pytest.mark.parametrize('value,options', [('A', {}), ('AB', {}), ('AA', {'size': 2}),
    ('AAAA', {'maximum': 1}), ('A' * 10, {'maximum': 1}), ('bad=', {}), ('', {}), (None, {})])
def test_invalid_base64url_identifier_is_rejected(value, options):
    with pytest.raises(ManagerError): decode_id(value, **options)


@pytest.mark.parametrize('change', ['json', 'shape', 'row', 'folder', 'name-type', 'uid-type',
    'field-name', 'duplicate-field', 'password-type', 'passkey-type', 'missing-user', 'empty-user-valid-passkey'])
def test_selected_record_schema_and_absence_are_not_conflated(selected, change):
    outputs = [json.dumps(selected.listing), None]
    if change == 'json': outputs[0] = '{synthetic invalid json'
    if change == 'shape': outputs[0] = '{}'
    if change == 'row': outputs[0] = '[1]'
    if change == 'folder': selected.listing[0]['type'] = 'folder'
    if change == 'name-type': selected.listing[0]['name'] = None
    if change == 'uid-type': selected.listing[0]['uid'] = None
    if change == 'field-name': selected.fields.append({'name': None, 'value': 'private'})
    if change == 'duplicate-field': selected.fields.append(deepcopy(selected.fields[1]))
    if change == 'password-type': selected.fields[3]['value'] = [True]
    if change == 'passkey-type': selected.fields[-1]['value'] = ['not a passkey']
    if change == 'missing-user': selected.fields.pop(); selected.fields[2]['value'] = ''
    if change == 'empty-user-valid-passkey': selected.fields[2]['value'] = []
    if change not in {'json', 'shape', 'row'}: outputs[0] = json.dumps(selected.listing)
    outputs[1] = json.dumps(selected.fields)
    private = Mock(side_effect=outputs)
    if change == 'empty-user-valid-passkey':
        assert keeper.retrieve_login(selected.source, '/cli', USERNAME, RP, private).passkey_available
    else:
        with pytest.raises(ManagerError): keeper.retrieve_login(selected.source, '/cli', USERNAME, RP, private)


@pytest.mark.parametrize('target', ['counter', 'key'])
def test_public_passkey_type_rejects_unvalidated_constructor_values(selected, target):
    if target == 'counter':
        key = ec.derive_private_key(123456789, ec.SECP256R1()); count = True
    else:
        key = ec.derive_private_key(123456789, ec.SECP384R1()); count = 0
    with pytest.raises(ManagerError):
        PasskeyCredential(RP, USERNAME, selected.passkey['credentialId'], selected.passkey['userId'], count, key)


@pytest.mark.parametrize('kwargs', [dict(available=1), dict(available=False, password='private'),
    dict(available=True, username=USERNAME, password=''), dict(available=True, username=USERNAME, passkey=object())])
def test_public_availability_type_rejects_inconsistent_values(kwargs):
    with pytest.raises(ManagerError): SelectedLoginCredentials(**kwargs)


@pytest.mark.parametrize('timeout', [0, float('nan'), float('inf')])
def test_invalid_retrieval_timeout_is_an_error(retrieval, timeout):
    source, runner = retrieval
    with pytest.raises(c.CredentialError): get(source, timeout=timeout)
    runner.assert_not_called()


def test_adapter_parser_failure_is_secret_safe(selected, retrieval):
    source, runner = retrieval
    selected.key['d'] = encoded(bytes(32))
    with pytest.raises(c.CredentialError, match='validation failed') as caught: get(source)
    assert selected.key['d'] not in str(caught.value) and USERNAME not in str(caught.value)


def test_cli_budget_is_shared_across_listing_and_private_read(retrieval, monkeypatch):
    from api_agent.auth import selected_login
    source, runner = retrieval
    clock = [0.0]
    monkeypatch.setattr(selected_login.time, 'monotonic', lambda: clock[0])
    original = runner.side_effect
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] += 2
        return result
    runner.side_effect = delayed
    with pytest.raises(c.CredentialError, match='deadline expired'): get(source, timeout=1)
    assert runner.call_count == 1


def test_cancellation_before_capture_preserves_coordinator_policy(retrieval):
    import threading
    source, runner = retrieval
    cancel = threading.Event(); cancel.set()
    with pytest.raises(c.CredentialRecoveryError, match='cancelled'):
        get(source, interaction=SimpleNamespace(cancel_event=cancel, lock=threading.RLock()))
    runner.assert_not_called()

@pytest.fixture
def adapter_case(monkeypatch):
    from api_agent.auth import selected_login
    from api_agent.auth.login_credentials import LoginCredentialRequest
    adapter = SimpleNamespace(classify_login=lambda text: False, login_probe=lambda *args: ['/cli', 'whoami'])
    adapter.retrieve_login = lambda source, cli, user, rp, run: (run(['/cli', 'read']) and SelectedLoginCredentials(False))
    registry = SimpleNamespace(load=lambda name: adapter, get=lambda name: SimpleNamespace(label='Synthetic manager'))
    source = SimpleNamespace(manager='synthetic', cli_path='synthetic', _registry=registry)
    monkeypatch.setattr(c.CredentialSource, 'from_mapping', lambda *args, **kwargs: source)
    monkeypatch.setattr(c, 'resolve_cli_path', lambda path: '/cli')
    runner = Mock(return_value='private output')
    monkeypatch.setattr(c, '_run', runner)
    invoke = lambda **kwargs: selected_login.retrieve({}, LoginCredentialRequest(USERNAME, RP), 5, **kwargs)
    return adapter, registry, runner, invoke


@pytest.mark.parametrize('argv', [None, [], ['/other'], ['/cli', 1], ['/cli', 'bad\0value']])
def test_invalid_optional_adapter_commands_never_execute(adapter_case, argv):
    adapter, _, runner, invoke = adapter_case
    adapter.retrieve_login = lambda source, cli, user, rp, run: run(argv)
    with pytest.raises(c.CredentialError, match='invalid credential command'): invoke()
    runner.assert_not_called()


@pytest.mark.parametrize('mode', ['missing', 'valid', 'raises', 'nonbool'])
def test_optional_login_classifier_contract(adapter_case, mode):
    adapter, _, runner, invoke = adapter_case
    if mode == 'missing': del adapter.classify_login
    elif mode == 'raises': adapter.classify_login = Mock(side_effect=RuntimeError('private diagnostic'))
    elif mode == 'nonbool': adapter.classify_login = lambda text: 'invalid'
    def execute(*args, **kwargs):
        value = kwargs['classify_login']('private stderr')
        assert value is False
        return 'private output'
    runner.side_effect = execute
    if mode in ('missing', 'valid'): assert not invoke().available
    else:
        with pytest.raises(c.CredentialError, match='classification') as caught: invoke()
        assert 'private' not in str(caught.value)


def test_optional_implementation_load_failure_is_safe(adapter_case):
    _, registry, _, invoke = adapter_case
    registry.load = Mock(side_effect=ManagerError('private path'))
    with pytest.raises(c.CredentialError, match='implementation is unavailable') as caught: invoke()
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('result', [None, SelectedLoginCredentials(True, 'other@example.test')])
def test_optional_adapter_result_validation(adapter_case, result):
    adapter, _, _, invoke = adapter_case
    adapter.retrieve_login = lambda *args: result
    with pytest.raises(c.CredentialError): invoke()


def test_optional_adapter_capture_requires_text(adapter_case):
    _, _, runner, invoke = adapter_case
    runner.return_value = None
    with pytest.raises(c.CredentialError, match='invalid output'): invoke()


@pytest.mark.parametrize('mode', ['raises', 'unsafe', 'twice'])
def test_recovery_probe_contract_and_single_retry(adapter_case, monkeypatch, mode):
    from api_agent.auth import recovery
    adapter, _, runner, invoke = adapter_case
    runner.side_effect = c.CredentialLoginRequiredError('locked')
    recover = Mock()
    monkeypatch.setattr(recovery, 'recover', recover)
    if mode == 'raises': adapter.login_probe = Mock(side_effect=RuntimeError('private output'))
    if mode == 'unsafe': adapter.login_probe = lambda *args: ['/cli', 'get', '--unmask']
    error = c.CredentialLoginRequiredError if mode == 'twice' else c.CredentialRecoveryError
    with pytest.raises(error): invoke(interaction=SimpleNamespace(cancel_event=None))
    assert recover.call_count == (1 if mode == 'twice' else 0)
    assert runner.call_count == (2 if mode == 'twice' else 1)

@pytest.mark.parametrize('extra', [
    'retrieve_login = 7\n',
    'async def retrieve_login(*args): pass\n',
    'def retrieve_login(*args): yield None\n',
    'def retrieve_login(): pass\n',
    'def retrieve_login(*args): pass\n',
    'def login_probe(): pass\n',
    'async def login_probe(*args): yield None\n',
])
def test_optional_adapter_loader_rejects_unsafe_hook_contracts(tmp_path, extra):
    from test_password_manager_credentials import third_registry
    base = 'version=1\ndef command(*args): pass\ndef parse(*args): pass\n'
    registry, _ = third_registry(tmp_path, base + extra)
    with pytest.raises(ManagerError): registry.load('synthetic')


def test_optional_adapter_loader_accepts_synchronous_hooks(tmp_path):
    from test_password_manager_credentials import third_registry
    registry, _ = third_registry(tmp_path, 'version=1\ndef command(*args): pass\ndef parse(*args): pass\n'
        'def retrieve_login(*args): pass\ndef login_probe(*args): pass\n')
    assert callable(registry.load('synthetic').retrieve_login)


def test_optional_adapter_rejects_other_relying_party(adapter_case, selected):
    from dataclasses import replace
    adapter, _, _, invoke = adapter_case
    result, _ = read(selected)
    adapter.retrieve_login = lambda *args: replace(result, passkey=replace(result.passkey, relying_party_id='other.test'))
    with pytest.raises(c.CredentialError, match='requested account and relying party'): invoke()


def test_inherited_expiry_prevents_private_capture(adapter_case, monkeypatch):
    from contextlib import contextmanager
    from api_agent.auth import selected_login
    @contextmanager
    def expired(*args): yield SimpleNamespace(remaining=0)
    monkeypatch.setattr(selected_login, 'report_timeout_sync', expired)
    _, _, runner, invoke = adapter_case
    with pytest.raises(c.CredentialRecoveryError, match='deadline expired'): invoke()
    runner.assert_not_called()


def test_invalid_probe_command_is_never_executed(adapter_case):
    adapter, _, runner, invoke = adapter_case
    adapter.login_probe = lambda *args: ['/other', 'whoami']
    runner.side_effect = c.CredentialLoginRequiredError('locked')
    with pytest.raises(c.CredentialError, match='invalid credential command'):
        invoke(interaction=SimpleNamespace(cancel_event=None))
    assert runner.call_count == 1


def test_parent_budget_clips_recovery_without_restarting_private_budget(adapter_case, monkeypatch):
    from contextlib import contextmanager
    from api_agent.auth import selected_login, recovery
    _, _, runner, invoke = adapter_case
    runner.side_effect = [c.CredentialLoginRequiredError('locked'), 'private output']
    @contextmanager
    def budget(label, timeout): yield SimpleNamespace(remaining=2)
    monkeypatch.setattr(selected_login, 'report_timeout_sync', budget)
    recover = Mock()
    monkeypatch.setattr(recovery, 'recover', recover)
    assert not invoke(interaction=SimpleNamespace(cancel_event=None)).available
    assert recover.call_args.args[-1] == 2
