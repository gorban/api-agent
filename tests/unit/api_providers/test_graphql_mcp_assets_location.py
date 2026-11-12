"""Where the GraphQL MCP provider reads its prepared Node assets."""

import pytest

from api_agent.api_providers.graphql_mcp import GraphQLMcpError
from api_agent.api_providers.graphql_mcp import factory

SETTING = "GRAPHQL_MCP_ASSETS_DIR"
CONFIG = {"API_ENDPOINT": "http://127.0.0.1:1/graphql"}


def test_assets_default_to_the_owner_root(monkeypatch, tmp_path):
    monkeypatch.delenv(SETTING, raising=False)

    provider = factory.create(CONFIG, {"owner_root": tmp_path})

    assert provider._assets_dir == tmp_path / ".api-agent" / "graphql-mcp"


def test_configured_assets_directory_replaces_the_owner_root_location(monkeypatch, tmp_path):
    prepared = tmp_path / "prepared"
    monkeypatch.setenv(SETTING, str(prepared))

    provider = factory.create(CONFIG, {"owner_root": tmp_path / "plans"})

    assert provider._assets_dir == prepared
    assert provider._owner._assets_dir == prepared.resolve()


def test_configured_assets_directory_needs_no_owner_root(monkeypatch, tmp_path):
    monkeypatch.setenv(SETTING, str(tmp_path))

    provider = factory.create(CONFIG, {})

    assert provider._assets_dir == tmp_path


def test_configured_assets_directory_must_be_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv(SETTING, "relative/assets")

    with pytest.raises(GraphQLMcpError, match="assets_dir_invalid"):
        factory.create(CONFIG, {"owner_root": tmp_path})


@pytest.mark.asyncio
async def test_configured_assets_are_never_prepared_at_startup(monkeypatch, tmp_path):
    prepared = []

    async def prepare(*args, **kwargs):
        prepared.append(args)

    monkeypatch.setenv(SETTING, str(tmp_path / "missing"))
    monkeypatch.setattr(factory, "prepare_assets", prepare)
    provider = factory.create(CONFIG, {"owner_root": tmp_path})

    try:
        with pytest.raises(GraphQLMcpError, match="assets_missing"):
            await provider.astart()
    finally:
        await provider.aclose()
    assert prepared == []
