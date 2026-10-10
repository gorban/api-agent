"""Required unattended vault identity retrieval stays scoped, lazy and owned until shutdown."""
import asyncio
import threading

import pytest

from api_agent.auth import credentials
from api_agent.config import schema_from_dict
from api_agent.providers.contracts import ContractError
from api_agent.testing.provider_setup import IntegrationLLM


def config():
    return {'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {
        'password_manager': {'manager': '1password', 'cli_path': 'op'},
        'roles': {'LLM User': [{'vault': 'fixture', 'user': 'login'}],
                  'Unrelated': [{'vault': 'elsewhere', 'user': 'other'}]}}}


@pytest.mark.parametrize('kind', ['username', 'password', 'totp'])
def test_selected_descriptor_retrieval_is_lazy_and_scoped(tmp_path, monkeypatch, kind):
    calls = []
    def fetch(source, *, timeout, interaction=None, recovery_timeout=None):
        assert 0 < timeout <= credentials.DEFAULT_TIMEOUT
        calls.append(source)
        return 'synthetic-value'
    monkeypatch.setattr(credentials, 'get_' + kind, fetch)
    owner = IntegrationLLM(config(), tmp_path)
    reader = owner._credentials(0)
    assert calls == []
    try:
        assert owner.loop.run_until_complete(reader(0, kind)) == 'synthetic-value'
        assert calls[0].manager == '1password' and calls[0].user == 'login'
        with pytest.raises(ContractError, match='outside binding'):
            owner.loop.run_until_complete(reader(1, kind))
        with pytest.raises(ContractError, match='Unsupported credential kind'):
            owner.loop.run_until_complete(reader(0, 'invalid'))
    finally:
        owner.close()
    assert owner.loop.is_closed() and not owner._credential_tasks


@pytest.mark.parametrize('value,index', [(None, 0), ({}, 0), ({'UNATTENDED_AUTH': {}}, 0)])
def test_provider_without_a_credential_role_needs_no_helper(tmp_path, value, index):
    owner = IntegrationLLM(value or {}, tmp_path)
    try:
        assert owner._credentials(index) is None
    finally:
        owner.close()


@pytest.mark.parametrize('problem', ['malformed', 'manager', 'negative', 'bool', 'range'])
def test_configured_invalid_descriptor_fails_without_retrieval(tmp_path, problem):
    data = config()
    index = 0
    if problem == 'malformed':
        data['UNATTENDED_AUTH'] = None
    elif problem == 'manager':
        data['UNATTENDED_AUTH']['password_manager']['manager'] = ''
    elif problem == 'negative':
        index = -1
    elif problem == 'bool':
        index = False
    else:
        index = 2
    owner = IntegrationLLM(data, tmp_path)
    try:
        with pytest.raises(ContractError, match='shared password manager' if problem == 'manager' else 'descriptor is incomplete or invalid'):
            owner._credentials(index)
    finally:
        owner.close()


def test_cancelled_read_is_retained_and_close_waits_for_worker(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def fetch(source, *, timeout, interaction=None, recovery_timeout=None):
        assert 0 < timeout <= credentials.DEFAULT_TIMEOUT
        entered.set()
        assert release.wait(5)
        return 'synthetic-user'
    monkeypatch.setattr(credentials, 'get_username', fetch)
    owner = IntegrationLLM(config(), tmp_path)
    async def scenario():
        read = asyncio.create_task(owner._credentials(0)(0, 'username'))
        while not entered.is_set():
            await asyncio.sleep(.001)
        read.cancel()
        await asyncio.sleep(.01)
        assert not read.done()
        closing = asyncio.create_task(owner._close())
        await asyncio.sleep(.01)
        assert not closing.done()
        closing.cancel()
        await asyncio.sleep(.01)
        assert not closing.done()
        assert owner._credential_tasks and not next(iter(owner._credential_tasks)).done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await read
        with pytest.raises(asyncio.CancelledError):
            await closing
        await owner._close()
        assert not owner._credential_tasks
    try:
        owner.loop.run_until_complete(scenario())
    finally:
        release.set()
        owner.close()


def test_helper_receives_ancestor_remaining_budget(tmp_path, monkeypatch):
    from api_agent.providers import progress
    now, timeouts, rows = [0.0], [], []
    monkeypatch.setattr(progress, '_clock', lambda: now[0])
    original_to_thread = asyncio.to_thread
    async def delayed_to_thread(function, *args, **kwargs):
        now[0] = 4  # Time spent waiting for the worker counts against the budget.
        return await original_to_thread(function, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', delayed_to_thread)
    def fetch(source, *, timeout, interaction=None, recovery_timeout=None):
        timeouts.append(timeout)
        return 'synthetic-value'
    monkeypatch.setattr(credentials, 'get_password', fetch)
    owner = IntegrationLLM(config(), tmp_path)
    async def run():
        with progress.progress_sink(rows.append):
            async with progress.report_timeout('Login', 5):
                now[0] = 3
                assert await owner._credentials(0)(0, 'password') == 'synthetic-value'
        assert timeouts == [1]
        child = [row for row in rows if row.name == 'Password-manager password']
        assert len(child) == 2 and all(row.timeout is None for row in child)
        assert all(row.parent_id == rows[0].id for row in child)
    try:
        owner.loop.run_until_complete(run())
    finally:
        owner.close()
    assert not owner._credential_tasks and owner.loop.is_closed()


def test_real_helper_stops_on_parent_budget_and_remains_owned_until_reaped(tmp_path):
    from api_agent.providers.progress import report_timeout
    from api_agent.providers.contracts import InteractionRequiredError
    from test_credentials import fake_cli
    cli = fake_cli(tmp_path, 'time.sleep(60)')
    data = config()
    data['UNATTENDED_AUTH']['password_manager'] = {'manager': 'keeper', 'cli_path': str(cli)}
    owner = IntegrationLLM(data, tmp_path)
    async def run():
        async with report_timeout('Login', .05):
            with pytest.raises(InteractionRequiredError, match='Sign in to the configured CLI'):
                await asyncio.wait_for(owner._credentials(0)(0, 'password'), .8)
        await owner._close()
        assert not owner._credential_tasks
    try:
        owner.loop.run_until_complete(run())
    finally:
        owner.close()


@pytest.mark.parametrize('matches', [True, False])
def test_real_setup_uses_selected_vault_username_without_separate_profile(tmp_path, monkeypatch, matches):
    import time
    from pathlib import Path
    from api_agent.llm_providers.gpt.connections import ConnectionStore
    from api_agent.llm_providers.gpt.contracts import Connection
    from api_agent.llm_providers.gpt import factory as gpt
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    async def connect(selected_store, **kwargs):
        from dataclasses import replace
        assert kwargs['login_username'] == 'person@example.test'
        return await selected_store.save_serialized(replace(selected_store.list()[0],
            subject='new-subject', email='person@example.test', access_token='new-token'))
    connection = AsyncMock(side_effect=connect)
    monkeypatch.setattr(gpt, 'connect_account', connection)
    monkeypatch.setattr(gpt, 'subscription_browser', lambda **kwargs: SimpleNamespace(aclose=AsyncMock()))
    monkeypatch.setattr(credentials, 'get_username', lambda source, **kwargs: 'person@example.test')
    store = ConnectionStore(tmp_path / 'state')
    store.save(Connection('subject', 'client', store.host_id(),
                          'person@example.test' if matches else 'another@example.test',
                          'synthetic-token', 'refresh', 'identity', ('scope',), time.time() + 3600))
    data = config() | {'LLM_PROVIDER': str(Path(gpt.__file__)) + ':factory',
                      'GPT_STATE_DIR': store.root, 'UNATTENDED_USER_INDEX': 0}
    owner = IntegrationLLM(data, tmp_path)
    try:
        from api_agent.providers.loading import aload_provider
        owner.provider = owner.loop.run_until_complete(aload_provider(
            data['LLM_PROVIDER'], data, {'owner_root': tmp_path}, kind='llm'))
        loaded_globals = owner.provider.implementation.asetup.__func__.__globals__
        monkeypatch.setitem(loaded_globals, 'connect_account', connection)
        monkeypatch.setitem(loaded_globals, 'subscription_browser', lambda **kwargs: SimpleNamespace(aclose=AsyncMock()))
        assert owner.ensure_ready() is None
        assert owner.owner._credentials is not None
        assert connection.await_count == int(not matches)
    finally:
        owner.close()
    assert owner.loop.is_closed()


@pytest.mark.parametrize('source', [None, 'dotenv', '../base/.env', '../pack/.env', 'environment', 'cli'])
@pytest.mark.parametrize('kind', ['username', 'password', 'totp'])
def test_cli_retrieval_failure_is_actionable_and_private_output_is_sanitized(tmp_path, monkeypatch, source, kind):
    from api_agent.providers.contracts import UnattendedCredentialError
    def failed(source, *, timeout, interaction=None, recovery_timeout=None):
        assert 0 < timeout <= credentials.DEFAULT_TIMEOUT
        raise credentials.CredentialError('private CLI identity/output')
    monkeypatch.setattr(credentials, 'get_' + kind, failed)
    owner = IntegrationLLM(config(), tmp_path, unattended_index_source=source)
    try:
        reader = owner._credentials(0)
        with pytest.raises(UnattendedCredentialError, match='Sign in to the configured CLI') as error:
            owner.loop.run_until_complete(reader(0, kind))
        assert 'private CLI identity/output' not in str(error.value)
        message = str(error.value)
        guidance, internal = message.split('\n\n')
        assert guidance.startswith('Failed to get credentials for unattended access.')
        assert '[ and ]' in guidance and '"No unattended user set"' in guidance
        dotenv = source == 'dotenv' or isinstance(source, str) and source.endswith('/.env')
        assert ('Clear .env configuration' in guidance) is dotenv
        assert ('stop passing the override' in guidance) is not dotenv
        assert 'UNATTENDED_USER_INDEX' in guidance
        assert internal.startswith('Password-manager ' + kind + ' retrieval is unavailable.')
    finally:
        owner.close()
    assert not owner._credential_tasks and owner.loop.is_closed()


@pytest.mark.parametrize('entries', [None, '', 0, False, {}])
def test_falsy_malformed_role_never_bypasses_account_binding(tmp_path, entries):
    data = config()
    data['UNATTENDED_AUTH']['roles']['LLM User'] = entries
    owner = IntegrationLLM(data, tmp_path)
    try:
        with pytest.raises(ContractError, match='descriptor is incomplete or invalid'):
            owner._credentials(0)
    finally:
        owner.close()
    from test_tui import fixture
    import json
    app = fixture(tmp_path, fields={'LLM_CREDENTIAL_ROLE': {
        'type': 'str', 'default': 'LLM User'}})
    app.session.environ['UNATTENDED_AUTH'] = json.dumps(data['UNATTENDED_AUTH'])
    assert app.auth_setup_needed()


@pytest.mark.parametrize('field_type,value', [('str', True), ('str', False), ('str', 'yes'), ('jsonobject', True)])
def test_removed_optional_role_metadata_is_rejected(field_type, value):
    with pytest.raises(ValueError, match='unknown metadata'):
        schema_from_dict({'version': 1, 'fields': {'ROLE': {
            'type': field_type, 'credential_role_optional': value}}})


@pytest.mark.parametrize('profile', [{}, {'roles': {'LLM User': []}}])
def test_missing_declared_unattended_account_is_a_prerequisite(tmp_path, monkeypatch, profile):
    from api_agent.providers.contracts import SetupPrerequisiteError
    monkeypatch.setattr(credentials, 'get_username', lambda *args: pytest.fail('missing account retrieved'))
    owner = IntegrationLLM({'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': profile}, tmp_path)
    try:
        with pytest.raises(SetupPrerequisiteError, match='LLM User.*account'):
            owner._credentials(0)
    finally:
        owner.close()


def test_missing_shared_manager_is_an_unattended_prerequisite(tmp_path, monkeypatch):
    from api_agent.providers.contracts import SetupPrerequisiteError
    monkeypatch.setattr(credentials, 'get_username', lambda *args: pytest.fail('unconfigured manager used'))
    owner = IntegrationLLM({'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {
        'roles': {'LLM User': [{'vault': 'fixture', 'user': 'login'}]}}}, tmp_path)
    try:
        with pytest.raises(SetupPrerequisiteError, match='shared password manager'):
            owner._credentials(0)
    finally:
        owner.close()
