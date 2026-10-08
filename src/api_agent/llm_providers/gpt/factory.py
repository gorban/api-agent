"""Lazy personal subscription authentication through the provider boundary."""
from pathlib import Path

import httpx

from api_agent.providers.contracts import ContractError, InteractionRequiredError, ProviderCapabilities
from .connect import _dispose, connect_account
from .connections import ConnectionStore, SubscriptionSession
from .oauth import refresh_connection


class OwnedSubscriptionSession(SubscriptionSession):
    """Own refresh transport on the same loop as the subscription session."""

    def __init__(self, store, profile):
        self.client = None
        super().__init__(store, profile, self._refresh)

    async def _refresh(self, connection, now):
        if self.client is None:
            self.client = httpx.AsyncClient()
        return await refresh_connection(connection, client=self.client, now=now)

    async def _dispose(self):
        await super()._dispose()
        if self.client is not None:
            await _dispose(self.client.aclose())
            self.client = None


class GPTProvider:
    version = 1
    capabilities = ProviderCapabilities(kind="llm", operations=())

    def __init__(self, config):
        self.auth_mode = config.get("GPT_AUTH_MODE", "subscription")
        self.api_mode = config.get("GPT_API_MODE", "responses" if self.auth_mode == "subscription" else "completions")
        self.profile = config.get("GPT_PROFILE", "")
        self.state_dir = Path(config.get("GPT_STATE_DIR") or Path.home() / ".config/api-agent/gpt")
        self.sessions = []
        self.closed = False

    async def asetup(self, *, interactive, user_index, credentials):
        if self.closed:
            raise ContractError("GPT provider is closed.")
        if self.auth_mode != "subscription":
            raise ContractError("GPT API-key authentication is pending implementation; select subscription authentication.")
        if self.api_mode != "responses":
            raise ContractError("GPT subscription authentication requires responses mode; set GPT_API_MODE=responses.")
        store = ConnectionStore(self.state_dir)
        profile = self.profile
        if profile:
            store.load(profile)
        else:
            connections = store.list()
            if len(connections) > 1:
                raise InteractionRequiredError("Multiple saved GPT connections exist; set GPT_PROFILE to the selected saved profile.")
            if connections:
                profile = store.profile_id(connections[0])
            elif not interactive:
                raise InteractionRequiredError("GPT requires attended sign-in; start attended integration through launch.py to connect your account.")
            else:
                profile = await connect_account(store)
        session = OwnedSubscriptionSession(store, profile)
        self.sessions.append(session)
        return session

    async def aclose(self):
        self.closed = True
        for session in self.sessions:
            await _dispose(session.aclose())
        self.sessions.clear()


def factory(config, services):
    """Create without reading saved state or starting transport/browser work."""
    return GPTProvider(config)
