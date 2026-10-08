"""Optional vault identity retrieval stays scoped, lazy and owned until shutdown."""
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
    def fetch(source):
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


@pytest.mark.parametrize('value,index', [(None, 0), ({}, 0), ({'UNATTENDED_AUTH': {}}, 0),
    ({'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {'roles': {'LLM User': []}}}, 0)])
def test_absent_optional_descriptor_needs_no_helper(tmp_path, value, index):
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
        with pytest.raises(ContractError, match='descriptor is incomplete or invalid'):
            owner._credentials(index)
    finally:
        owner.close()


def test_cancelled_read_is_retained_and_close_waits_for_worker(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def fetch(source):
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
        with pytest.raises(asyncio.CancelledError):
            await read
        closing = asyncio.create_task(owner._close())
        await asyncio.sleep(.01)
        assert not closing.done()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert owner._credential_tasks and not next(iter(owner._credential_tasks)).done()
        release.set()
        await owner._close()
        assert not owner._credential_tasks
    try:
        owner.loop.run_until_complete(scenario())
    finally:
        release.set()
        owner.close()


def test_optional_role_metadata_must_be_boolean():
    with pytest.raises(ValueError, match='credential_role_optional must be boolean'):
        schema_from_dict({'version': 1, 'fields': {'ROLE': {
            'type': 'str', 'credential_role_optional': 'yes'}}})


@pytest.mark.parametrize('matches', [True, False])
def test_real_setup_uses_selected_vault_username_without_separate_profile(tmp_path, monkeypatch, matches):
    import time
    from pathlib import Path
    from api_agent.llm_providers.gpt.connections import ConnectionStore
    from api_agent.llm_providers.gpt.contracts import Connection
    from api_agent.llm_providers.gpt import factory as gpt
    from api_agent.providers.contracts import InteractionRequiredError
    monkeypatch.setattr(gpt, 'connect_account', lambda *args: pytest.fail('unexpected attended browser'))
    monkeypatch.setattr(credentials, 'get_username', lambda source: 'person@example.test')
    store = ConnectionStore(tmp_path / 'state')
    store.save(Connection('subject', 'client', store.host_id(),
                          'person@example.test' if matches else 'another@example.test',
                          'synthetic-token', 'refresh', 'identity', ('scope',), time.time() + 3600))
    data = config() | {'LLM_PROVIDER': str(Path(gpt.__file__)) + ':factory',
                      'GPT_STATE_DIR': store.root, 'UNATTENDED_USER_INDEX': 0}
    owner = IntegrationLLM(data, tmp_path)
    try:
        if matches:
            assert owner.ensure_ready() is None
            assert owner.owner._credentials is not None
        else:
            with pytest.raises(InteractionRequiredError, match='no unique cached OAuth'):
                owner.ensure_ready()
    finally:
        owner.close()
    assert owner.loop.is_closed()


@pytest.mark.parametrize('entries', [None, '', 0, False, {}])
def test_falsy_malformed_optional_role_never_bypasses_account_binding(tmp_path, entries):
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
        'type': 'str', 'default': 'LLM User', 'credential_role_optional': True}})
    app.session.environ['UNATTENDED_AUTH'] = json.dumps(data['UNATTENDED_AUTH'])
    assert app.auth_setup_needed()


def test_optional_role_metadata_requires_a_role_label_type():
    with pytest.raises(ValueError, match='credential_role_optional requires str type'):
        schema_from_dict({'version': 1, 'fields': {'ROLE': {
            'type': 'jsonobject', 'credential_role_optional': True}}})
