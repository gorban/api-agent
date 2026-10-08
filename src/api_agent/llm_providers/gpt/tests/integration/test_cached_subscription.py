"""Provider-owned setup checks using isolated synthetic saved state."""
import asyncio
import time

import pytest

from api_agent.llm_providers.gpt.connections import ConnectionStore
from api_agent.llm_providers.gpt.contracts import Connection
from api_agent.llm_providers.gpt.factory import factory
from api_agent.providers.contracts import ContractError


def test_saved_subscription_setup_without_transport(tmp_path, monkeypatch):
    import api_agent.llm_providers.gpt.factory as module
    store = ConnectionStore(tmp_path / "protected")
    profile = store.save(Connection(
        "synthetic-subject", "synthetic-client", store.host_id(), "",
        "synthetic-access", "synthetic-refresh", "synthetic-id",
        ("chatgpt.tokens.use.direct",), time.time() + 3600))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda: pytest.fail("cached setup started transport"))
    async def scenario():
        provider = factory({"GPT_STATE_DIR": store.root}, None)
        try:
            session = await provider.asetup(interactive=False, user_index=None, credentials=None)
            assert session.profile_id == profile
            assert await session() == "synthetic-access"
            assert provider.capabilities.operations == ()
        finally:
            await provider.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("config", [{"GPT_AUTH_MODE": "api_key"}, {"GPT_API_MODE": "completions"}])
def test_unsupported_auth_route_has_no_state_io(config, tmp_path):
    state = tmp_path / "not-created"
    provider = factory({**config, "GPT_STATE_DIR": state}, None)
    async def scenario():
        try:
            with pytest.raises(ContractError):
                await provider.asetup(interactive=False, user_index=None, credentials=None)
        finally:
            await provider.aclose()
    asyncio.run(scenario())
    assert not state.exists()
