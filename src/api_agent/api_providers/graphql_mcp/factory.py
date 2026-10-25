"""Local tool slots and the legacy API provider compatibility entrypoint."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from api_agent.api_providers.graphql_mcp import GraphQLMcpOwner, GraphQLMcpError, installed_script, prepare_assets
from api_agent.api_providers.graphql_mcp.owner import header_secret_values
from api_agent.providers.contracts import APIRequest, APIResult, ProviderCapabilities
from api_agent.providers.tools import PreparedCall


class GraphQLMcpAPIProvider:
    version = 1
    capabilities = ProviderCapabilities(kind="api", operations=("invoke",))

    def __init__(self, config: Mapping, services: Mapping):
        endpoint = config.get("ENDPOINT")
        headers = config.get("HEADERS")
        if headers is None:
            headers = {}
        if type(headers) is not dict:
            raise GraphQLMcpError("headers_invalid")
        emissions = services.get("emissions")
        if emissions is not None:
            for key, value in headers.items():
                for secret in header_secret_values(key, value):
                    emissions.register_secret(secret)
        owner_root = services.get("owner_root")
        if owner_root is None:
            raise GraphQLMcpError("owner_root_missing")
        root_fields = config.get("ROOT_FIELDS")
        if root_fields is not None:
            if (type(root_fields) not in (list, tuple, frozenset) or not root_fields or
                any(type(field) is not str or not field for field in root_fields)):
                raise GraphQLMcpError("root_fields_invalid")
            root_fields = frozenset(root_fields)
        self._assets_dir = Path(owner_root) / ".api-agent" / "graphql-mcp"
        self._owner = GraphQLMcpOwner(endpoint, headers=headers, assets_dir=self._assets_dir,
                                      root_fields=root_fields)
        self._ready = False

    async def astart(self) -> None:
        if self._ready:
            return
        try:
            installed_script(self._assets_dir)
        except GraphQLMcpError:
            await prepare_assets(self._assets_dir)
        self._ready = True

    def _require(self):
        if not self._ready:
            raise GraphQLMcpError("provider_not_started")
        return self._owner

    def for_invocation(self):
        return self._require().for_invocation()

    async def describe(self):
        return await self._require().describe()

    async def validate(self, prepared: PreparedCall) -> None:
        await self._require().validate(prepared)

    async def invoke(self, prepared: PreparedCall):
        return await self._require().invoke(prepared)

    async def ainvoke(self, request: APIRequest, *, auth: object = None) -> APIResult:
        if auth is not None or not isinstance(request, APIRequest) or request.operation != "invoke" or not isinstance(request.payload, PreparedCall):
            raise GraphQLMcpError("api_request_invalid")
        return APIResult(payload=await self.invoke(request.payload))

    async def aclose(self) -> None:
        await self._owner.aclose()


_LOCAL_SLOTS = ("ENDPOINT", "HEADERS", "ROOT_FIELDS")


def create_tool(local_config, services):
    """Create one owner from its selected instance's local configuration."""
    if any("API_" + slot in local_config for slot in _LOCAL_SLOTS):
        raise GraphQLMcpError("legacy_tool_slots_invalid")
    return GraphQLMcpAPIProvider(local_config, services)


def create(config, services):
    """Translate legacy provider slots at the compatibility boundary only."""
    if any(slot in config for slot in _LOCAL_SLOTS):
        raise GraphQLMcpError("tool_slots_conflict")
    local = {slot: config["API_" + slot] for slot in _LOCAL_SLOTS
             if "API_" + slot in config}
    return create_tool(local, services)
