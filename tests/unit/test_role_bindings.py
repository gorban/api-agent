"""Shared role binders validate descriptors and retain helper ownership."""
import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from api_agent.auth import credentials as helpers
from api_agent.auth.bindings import bind_role_credentials
from api_agent.auth.login_credentials import SelectedLoginCredentials
from api_agent.providers.contracts import ContractError, SetupPrerequisiteError, UnattendedCredentialError
from api_agent.testing.inference_content import content_boundary


@pytest.fixture
def binding():
    config = {'UNATTENDED_AUTH': {'password_manager': {'manager':'keeper', 'cli_path':'/synthetic-cli'},
        'roles': {'UI User':[{'vault':'Synthetic vault', 'user':'Saved item'}]}}}
    boundary, pending = content_boundary({}), set()
    def create(**kwargs):
        return bind_role_credentials(config, ('UI User',), 0, registry=None,
            emissions=boundary, pending_tasks=pending, **kwargs)
    return SimpleNamespace(config=config, boundary=boundary, pending=pending, create=create)


@pytest.mark.parametrize('kind', ['username','password','totp'])
def test_secret_reads_are_lazy_scoped_and_registered(binding, monkeypatch, kind):
    helper = Mock(return_value='synthetic-secret')
    monkeypatch.setattr(helpers, 'get_' + kind, helper)
    reader = binding.create()
    helper.assert_not_called()
    async def exercise():
        assert await reader('UI User', 0, kind) == 'synthetic-secret'
        with pytest.raises(ContractError): await reader('Other User', 0, kind)
        with pytest.raises(ContractError): await reader('UI User', 1, kind)
    asyncio.run(exercise())
    helper.assert_called_once()
    assert helper.call_args.args[0].user == 'Saved item'
    assert binding.boundary._text('synthetic-secret') == '[REDACTED]'
    assert not binding.pending


@pytest.mark.parametrize('value', [SelectedLoginCredentials(False), SelectedLoginCredentials(True, 'user@example.test', 'synthetic-password')])
def test_optional_login_material_registered_before_return(binding, monkeypatch, value):
    helper = Mock(return_value=value)
    monkeypatch.setattr(helpers, 'get_login_credentials', helper)
    reader = binding.create()
    result = asyncio.run(reader.login_credentials('UI User',0, expected_username='user@example.test', relying_party_id='example.test'))
    assert result is value
    assert helper.call_args.kwargs['relying_party_id'] == 'example.test'
    for secret in value.secret_values(): assert binding.boundary._text(secret) == '[REDACTED]'


@pytest.mark.parametrize('error', [helpers.CredentialCapabilityUnsupportedError('unsupported'),
    helpers.CredentialLoginRequiredError('login required'), helpers.CredentialRecoveryError('recovery'),
    helpers.CredentialError('private helper diagnostic')])
def test_helper_errors_preserve_actionable_safe_account_failure(binding, monkeypatch, error):
    monkeypatch.setattr(helpers, 'get_password', Mock(side_effect=error))
    reader = binding.create(index_source='dotenv')
    if isinstance(error, helpers.CredentialCapabilityUnsupportedError):
        assert asyncio.run(reader('UI User',0,'password')) is None
    else:
        with pytest.raises(UnattendedCredentialError) as caught:
            asyncio.run(reader('UI User',0,'password'))
        assert caught.value.index_source == 'dotenv'
        if type(error) is helpers.CredentialError:
            assert 'private helper diagnostic' not in str(caught.value)


@pytest.mark.parametrize('change,expected', [('missing_role', SetupPrerequisiteError), ('missing_manager', SetupPrerequisiteError),
    ('bad_index', ContractError), ('invalid', ContractError)])
def test_invalid_account_refused_without_cli(binding, monkeypatch, change, expected):
    helper = Mock()
    monkeypatch.setattr(helpers,'get_password',helper)
    if change == 'missing_role': binding.config['UNATTENDED_AUTH']['roles'] = {}
    if change == 'missing_manager': binding.config['UNATTENDED_AUTH']['password_manager']['manager'] = ''
    if change == 'invalid': binding.config['UNATTENDED_AUTH'] = None
    with pytest.raises(expected):
        if change == 'bad_index':
            bind_role_credentials(binding.config, ('UI User',), 5, registry=None,
                emissions=binding.boundary,pending_tasks=binding.pending)
        else: binding.create()
    helper.assert_not_called()


def test_cancel_waits_for_helper_and_closes_its_interaction(binding, monkeypatch):
    started, release = Event(), Event()
    interaction = SimpleNamespace(cancel=Mock(side_effect=release.set), close=Mock())
    parent = SimpleNamespace(fork=Mock(return_value=interaction))
    def helper(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return 'late secret'
    monkeypatch.setattr(helpers,'get_password',helper)
    async def exercise():
        reader = binding.create(interaction_owner=parent)
        task = asyncio.create_task(reader('UI User',0,'password'))
        while not started.is_set(): await asyncio.sleep(.001)
        assert len(binding.pending) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not binding.pending
    asyncio.run(exercise())
    interaction.cancel.assert_called_once()
    interaction.close.assert_called_once()


def test_empty_optional_scalar_does_not_register_secret(binding,monkeypatch):
    helper=Mock(return_value='')
    monkeypatch.setattr(helpers,'get_totp',helper)
    reader=binding.create()
    assert asyncio.run(reader('UI User',0,'totp')) == ''
    assert not binding.boundary._secrets


def test_unattended_cancellation_retains_helper_without_interaction(binding,monkeypatch):
    entered,release=Event(),Event()
    def helper(*args,**kwargs):
        entered.set()
        assert release.wait(2)
        return 'late result'
    monkeypatch.setattr(helpers,'get_password',helper)
    async def exercise():
        reader=binding.create()
        task=asyncio.create_task(reader('UI User',0,'password'))
        while not entered.is_set(): await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert not binding.pending
    asyncio.run(exercise())
