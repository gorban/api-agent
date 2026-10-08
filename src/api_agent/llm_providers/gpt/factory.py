"""Lazy personal subscription authentication through the provider boundary."""
from pathlib import Path

import httpx

from api_agent.providers.contracts import ContractError, InteractionRequiredError, ProviderCapabilities, SetupPrerequisiteError
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
        self.state_dir = Path(config.get("GPT_STATE_DIR") or Path.home() / ".config/api-agent/gpt")
        self.sessions = []
        self.closed = False

    async def asetup(self, *, interactive, user_index, credentials):
        if self.closed:
            raise ContractError("GPT provider is closed.")
        if not isinstance(self.auth_mode, str) or self.auth_mode not in {"subscription", "api_key"}:
            raise ContractError("GPT_AUTH_MODE must be subscription or api_key.")
        if not isinstance(self.api_mode, str) or self.api_mode not in {"responses", "completions"}:
            raise ContractError("GPT_API_MODE must be responses or completions.")
        if self.auth_mode != "subscription":
            raise SetupPrerequisiteError("GPT API-key authentication is pending implementation; select subscription authentication.")
        if self.api_mode != "responses":
            raise SetupPrerequisiteError("GPT subscription authentication requires responses mode; set GPT_API_MODE=responses.")
        store = ConnectionStore(self.state_dir)
        connections = store.list()
        if user_index is not None and credentials is not None:
            username = await credentials(user_index, "username")
            if not isinstance(username, str) or not username.strip():
                raise ContractError("The selected login must supply a nonempty username.")
            connections = tuple(connection for connection in connections
                                if connection.email.strip().casefold() == username.strip().casefold())
            if len(connections) != 1:
                raise InteractionRequiredError("The selected login has no unique cached OAuth connection; use attended sign-in first.")
        if len(connections) == 1:
            profile = store.profile_id(connections[0])
        elif interactive:
            # Let the verified OAuth result identify the account instead of a manual ID.
            profile = await connect_account(store)
        else:
            raise InteractionRequiredError("GPT requires attended sign-in; start attended integration through launch.py to connect or select your account.")
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
