"""Deterministic GPT setup checks using isolated synthetic saved state."""
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
        provider = factory({"GPT_AUTH_MODE": "subscription", "GPT_API_MODE": "responses",
                                "GPT_STATE_DIR": store.root}, None)
        try:
            session = await provider.asetup(interactive=False, user_index=None, credentials=None)
            assert session.profile_id == profile
            assert await session() == "synthetic-access"
            assert provider.capabilities.operations == ("describe",)
        finally:
            await provider.aclose()
    asyncio.run(scenario())


def test_invalid_subscription_completions_has_no_state_io(tmp_path):
    state = tmp_path / "not-created"
    provider = factory({"GPT_AUTH_MODE": "subscription", "GPT_API_MODE": "completions",
                        "GPT_STATE_DIR": state}, None)
    async def scenario():
        try:
            with pytest.raises(ContractError, match="requires responses"):
                await provider.asetup(interactive=False, user_index=None, credentials=None)
        finally:
            await provider.aclose()
    asyncio.run(scenario())
    assert not state.exists()
