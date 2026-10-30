"""Startup behavior for the locked GraphQL MCP installation."""

import pytest

from api_agent.api_providers.graphql_mcp import GraphQLMcpError, installed_script
from api_agent.api_providers.graphql_mcp import factory


def _provider(owner_root):
    return factory.create(
        {"API_ENDPOINT": "http://127.0.0.1:1/graphql"},
        {"owner_root": owner_root},
    )


def test_factory_registers_normalized_header_secrets_before_owner_validation(tmp_path):
    class Emissions:
        def __init__(self):
            self.secrets = set()

        def register_secret(self, value):
            self.secrets.add(value)

    emissions = Emissions()
    with pytest.raises(GraphQLMcpError, match="endpoint_invalid"):
        factory.create(
            {"API_ENDPOINT": "invalid", "API_HEADERS": {
                "Authorization": " Bearer  SYNTHETIC_TEST_ONLY ",
                "X-Private": " padded-secret ",
            }},
            {"owner_root": tmp_path, "emissions": emissions},
        )
    assert {"Bearer SYNTHETIC_TEST_ONLY", "SYNTHETIC_TEST_ONLY",
            "padded-secret"} <= emissions.secrets


@pytest.mark.asyncio
async def test_second_provider_reuses_verified_install_without_npm(monkeypatch, tmp_path):
    first = _provider(tmp_path)
    await first.astart()
    await first.aclose()
    script = installed_script(first._assets_dir)

    async def unavailable_npm(_):
        raise AssertionError("verified installation must not be replaced")

    monkeypatch.setattr(factory, "prepare_assets", unavailable_npm)
    second = _provider(tmp_path)
    await second.astart()
    assert second._ready
    assert installed_script(second._assets_dir) == script
    await second.aclose()


@pytest.mark.asyncio
async def test_frozen_authority_defers_validation_and_registers_before_use(monkeypatch, tmp_path):
    import asyncio
    from api_agent.telemetry.privacy import EmissionBoundary
    authority = EmissionBoundary()
    lease = authority.begin_evidence_lease()
    provider = factory.create({'API_ENDPOINT': 'invalid', 'API_HEADERS': {'Authorization': 'Bearer synthetic'}},
                              {'owner_root': tmp_path, 'emissions': authority})
    task = asyncio.create_task(provider.astart())
    await asyncio.sleep(0)
    assert not task.done()
    assert 'synthetic' not in authority._secrets
    lease.release()
    with pytest.raises(GraphQLMcpError, match='endpoint_invalid'):
        await task
    assert 'synthetic' in authority._secrets
    await provider.aclose()


@pytest.mark.asyncio
async def test_deferred_owner_is_created_once_after_authority_unfreezes(monkeypatch, tmp_path):
    from api_agent.telemetry.privacy import EmissionBoundary
    authority = EmissionBoundary()
    lease = authority.begin_evidence_lease()
    provider = factory.create({'API_ENDPOINT': 'http://127.0.0.1:1/graphql',
                               'API_HEADERS': {'Authorization': 'Bearer synthetic'}},
                              {'owner_root': tmp_path, 'emissions': authority})
    try:
        assert provider._owner is None and provider._deferred is not None
        original_owner = factory.GraphQLMcpOwner
        calls = []
        def construct(*args, **kwargs):
            assert 'synthetic' in authority._secrets
            calls.append(True)
            return original_owner(*args, **kwargs)
        monkeypatch.setattr(factory, 'GraphQLMcpOwner', construct)
        lease.release()
        monkeypatch.setattr(factory, 'installed_script', lambda _: tmp_path / 'installed.js')
        await provider.astart()
        owner = provider._owner
        await provider.astart()
        assert owner is not None and provider._owner is owner and calls == [True]
        assert provider._deferred is None
        assert 'synthetic' in authority._secrets
    finally:
        lease.release()
        await provider.aclose()


@pytest.mark.asyncio
async def test_tampered_install_requires_explicit_preparation(monkeypatch, tmp_path):
    first = _provider(tmp_path)
    await first.astart()
    await first.aclose()
    script = installed_script(first._assets_dir)
    original = script.read_bytes()
    script.write_bytes(original + b"\nsynthetic-tamper")

    async def unavailable_npm(_):
        raise GraphQLMcpError("npm_missing")

    monkeypatch.setattr(factory, "prepare_assets", unavailable_npm)
    second = _provider(tmp_path)
    try:
        with pytest.raises(GraphQLMcpError, match="npm_missing"):
            await second.astart()
        assert not second._ready
    finally:
        script.write_bytes(original)
        await second.aclose()
    assert installed_script(first._assets_dir) == script
