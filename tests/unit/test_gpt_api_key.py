"""Browser-free credentials and ownership for GPT API-key authentication."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from api_agent.llm_providers.gpt.api_key import APIKeySession
from api_agent.llm_providers.gpt import factory as module
from api_agent.providers.contracts import ContractError, SessionClosedError, SetupPrerequisiteError, InteractionRequiredError


@pytest.mark.parametrize('key', [None, 1, False, [], '', ' \t'])
def test_invalid_key_is_safe_contract_error(key):
    with pytest.raises(ContractError, match='nonempty API key'):
        APIKeySession(key)


def test_session_scope_and_close():
    async def scenario():
        session = APIKeySession('secret-test-key')
        assert 'secret-test-key' not in repr(session)
        async with session as scoped:
            assert scoped is session
            assert await session() == 'secret-test-key'
        assert session._key is None
        await session.aclose()
        with pytest.raises(SessionClosedError, match='closed'):
            await session()
        with pytest.raises(SessionClosedError, match='closed'):
            async with session:
                pytest.fail('closed session entered')
    asyncio.run(scenario())


@pytest.fixture
def no_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('API-key setup attempted external work')
    for name in ['ConnectionStore', 'subscription_browser', 'connect_account']:
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(module.httpx, 'AsyncClient', forbidden)


@pytest.mark.parametrize('mode', ['completions', 'responses'])
@pytest.mark.parametrize('interactive', [False, True])
def test_setup_only_reads_selected_password(mode, interactive, no_external_work):
    async def scenario():
        provider = module.factory({'GPT_AUTH_MODE': 'api_key', 'GPT_API_MODE': mode}, None)
        reader = AsyncMock(return_value='secret-test-key')
        session = await provider.asetup(interactive=interactive, user_index=0, credentials=reader)
        reader.assert_awaited_once_with(0, 'password')
        assert await session() == 'secret-test-key'
        assert provider.sessions == [session] and not provider.browsers
        await provider.aclose()
        await provider.aclose()
        assert not provider.sessions and session._key is None
        with pytest.raises(SessionClosedError):
            await session()
    asyncio.run(scenario())


@pytest.mark.parametrize('index,reader', [(None, AsyncMock()), (0, None)])
def test_missing_selection_or_reader_is_actionable(index, reader, no_external_work):
    provider = module.factory({'GPT_AUTH_MODE': 'api_key'}, None)
    with pytest.raises(SetupPrerequisiteError, match='Select an LLM User.*password'):
        asyncio.run(provider.asetup(interactive=True, user_index=index, credentials=reader))
    if reader is not None:
        reader.assert_not_awaited()


@pytest.mark.parametrize('index', [True, False, -1, 1.0, '0', []])
def test_invalid_index_rejected_before_credentials(index, no_external_work):
    reader = AsyncMock()
    provider = module.factory({'GPT_AUTH_MODE': 'api_key'}, None)
    with pytest.raises(ContractError, match='nonnegative integer'):
        asyncio.run(provider.asetup(interactive=False, user_index=index, credentials=reader))
    reader.assert_not_awaited()


@pytest.mark.parametrize('config', [{'GPT_API_MODE': 'wrong'}, {'GPT_AUTH_MODE': 'wrong'}, {'GPT_BASE_URL': 'http://remote.example/v1'}])
def test_configuration_rejected_before_secret_acquisition(config, no_external_work):
    reader = AsyncMock()
    provider = module.factory({'GPT_AUTH_MODE': 'api_key'} | config, None)
    with pytest.raises(ContractError):
        asyncio.run(provider.asetup(interactive=False, user_index=0, credentials=reader))
    reader.assert_not_awaited()


@pytest.mark.parametrize('key', [None, '', ' \t', 42])
def test_invalid_fetched_password_creates_no_session(key, no_external_work):
    provider = module.factory({'GPT_AUTH_MODE': 'api_key'}, None)
    with pytest.raises(ContractError, match='nonempty API key'):
        asyncio.run(provider.asetup(interactive=False, user_index=0, credentials=AsyncMock(return_value=key)))
    assert not provider.sessions


def test_identity_retrieval_failure_propagates(no_external_work):
    failure = InteractionRequiredError('Select the intended password-manager identity.')
    provider = module.factory({'GPT_AUTH_MODE': 'api_key'}, None)
    with pytest.raises(InteractionRequiredError) as error:
        asyncio.run(provider.asetup(interactive=False, user_index=0, credentials=AsyncMock(side_effect=failure)))
    assert error.value is failure and not provider.sessions


@pytest.mark.parametrize('cancel', [False, True])
def test_pending_credentials_cannot_attach_after_disposal(cancel, no_external_work):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def reader(index, kind):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return 'secret-test-key'
        provider = module.factory({'GPT_AUTH_MODE': 'api_key'}, None)
        task = asyncio.create_task(provider.asetup(interactive=False, user_index=0, credentials=reader))
        await entered.wait()
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
        await provider.aclose()
        release.set()
        with pytest.raises(ContractError, match='closed'):
            await task
        assert not provider.sessions
    asyncio.run(scenario())
