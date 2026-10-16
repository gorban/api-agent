"""Optional structured reads keep account, operation and cancellation boundaries."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.providers.contracts import ContractError, SessionClosedError
from api_agent.providers.sessions import ScopedLLMCredentials, ScopedRoleCredentials
from api_agent.providers.shared_setup import RevocableCredentials


@pytest.mark.parametrize('role,index', [('other', 0), ('llm', 1), ('llm', True), ('llm', -1)])
def test_structured_reads_cannot_escape_binding(role, index):
    reader = ScopedRoleCredentials(('llm',), 0, AsyncMock(), retrieve_login=AsyncMock())
    with pytest.raises(ContractError, match='outside credential binding'):
        asyncio.run(reader.login_credentials(role, index, expected_username='person@example.test', relying_party_id='example.test'))
    reader._retrieve_login.assert_not_called()


def test_legacy_reader_has_no_optional_material():
    reader = ScopedRoleCredentials(('llm',), 0, AsyncMock())
    result = asyncio.run(reader.login_credentials('llm', 0, expected_username='person@example.test', relying_party_id='example.test'))
    assert result is None


def test_llm_binding_passes_only_its_own_role():
    retrieve = AsyncMock(return_value=object())
    reader = ScopedLLMCredentials('llm', ScopedRoleCredentials(('llm',), 0, AsyncMock(), retrieve_login=retrieve))
    result = asyncio.run(reader.login_credentials(0, expected_username='person@example.test', relying_party_id='example.test'))
    assert result is retrieve.return_value
    retrieve.assert_awaited_once_with('llm', 0, expected_username='person@example.test', relying_party_id='example.test')


def test_revocable_reader_keeps_legacy_adapter_usable():
    async def scenario():
        async def legacy(*_):
            return 'password'
        reader = RevocableCredentials('operation', legacy)
        assert await reader.login_credentials(0, expected_username='person@example.test', relying_party_id='example.test') is None
        await reader.aclose()
        with pytest.raises(SessionClosedError):
            await reader.login_credentials(0)
    asyncio.run(scenario())


def test_revocation_rejects_a_late_structured_result():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def retrieve(*_, **__):
            entered.set()
            await release.wait()
            return object()
        source = AsyncMock()
        source.login_credentials = retrieve
        reader = RevocableCredentials('operation', source)
        pending = asyncio.create_task(reader.login_credentials(0))
        await entered.wait()
        reader.revoke()
        release.set()
        with pytest.raises(SessionClosedError):
            await pending
        await reader.aclose()
        assert not reader._reads
    asyncio.run(scenario())


@pytest.fixture
def material_backend(tmp_path):
    from api_agent.testing.provider_setup import IntegrationLLM
    backend = IntegrationLLM({'LLM_CREDENTIAL_ROLE': 'LLM User', 'UNATTENDED_AUTH': {
        'password_manager': {'manager': 'keeper', 'cli_path': '/synthetic-cli'},
        'roles': {'LLM User': [{'vault': 'Synthetic vault', 'user': 'Synthetic login'}]}}}, tmp_path)
    yield backend
    backend.close()


def test_backend_registers_structured_secrets_before_return(material_backend, monkeypatch):
    from api_agent.auth import credentials as helpers
    from api_agent.auth.login_credentials import SelectedLoginCredentials
    material = SelectedLoginCredentials(True, 'person@example.test', 'synthetic-password')
    helper = Mock(return_value=material)
    monkeypatch.setattr(helpers, 'get_login_credentials', helper)
    reader = material_backend._credentials(0)
    result = material_backend.loop.run_until_complete(reader.login_credentials(0,
        expected_username='person@example.test', relying_party_id='example.test'))
    assert result is material
    assert material_backend.emissions._text('synthetic-password') == '[REDACTED]'
    assert helper.call_args.kwargs['expected_username'] == 'person@example.test'
    assert helper.call_args.kwargs['relying_party_id'] == 'example.test'


def test_backend_preserves_unsupported_optional_capability(material_backend, monkeypatch):
    from api_agent.auth import credentials as helpers
    def unsupported(*_, **__):
        raise helpers.CredentialCapabilityUnsupportedError('Unsupported optional capability.')
    monkeypatch.setattr(helpers, 'get_login_credentials', unsupported)
    reader = material_backend._credentials(0)
    result = material_backend.loop.run_until_complete(reader.login_credentials(0,
        expected_username='person@example.test', relying_party_id='example.test'))
    assert result is None


def test_backend_absent_login_returns_no_secret_material(material_backend, monkeypatch):
    from api_agent.auth import credentials as helpers
    from api_agent.auth.login_credentials import SelectedLoginCredentials
    material = SelectedLoginCredentials(False)
    registered_before = set(material_backend.emissions._secrets)
    monkeypatch.setattr(helpers, 'get_login_credentials', Mock(return_value=material))
    reader = material_backend._credentials(0)
    result = material_backend.loop.run_until_complete(reader.login_credentials(0,
        expected_username='person@example.test', relying_party_id='example.test'))
    assert not result.available
    assert material_backend.emissions._secrets == registered_before
