from dataclasses import fields
from api_agent.providers import contracts as c


def test_carriers_hide_payloads_and_preserve_types():
    marker = object()
    values = [c.APIRequest(marker), c.APIResult(marker, (marker,)), c.LLMRequest("neutral", marker),
              c.LLMResult(marker, {"opaque": marker}), c.StreamEvent(marker), c.BrowserSession(marker, marker, marker)]
    for value in values:
        assert repr(marker) not in repr(value)
    assert values[2].selected_llm == "neutral"
    assert values[3].usage["opaque"] is marker
    assert values[4].final is False
    assert c.ProviderCapabilities("llm").version == 1
    assert set(c.OPERATION_METHODS) == {"api", "llm", "ui", "upstream"}
    assert {f.name for f in fields(c.BrowserSession)} == {"browser", "context", "page"}


def test_protocol_declarations_are_callable_without_payloads():
    # Execute stub bodies to cover the declaration-only protocol module.
    import asyncio
    async def scenario():
        await c.RoleCredentialReader.__call__(None, "neutral", 0, "password")
        await c.LLMCredentialReader.__call__(None, 0, "totp")
        await c.LLMAuthSession.__call__(None)
        await c.LLMAuthSession.aclose(None)
        await c.LLMAuthenticator.authenticate(None, session=None, user_index=0, credentials=None)
        await c.ResourcePackAuthenticator.authenticate(None, session=None, tui_mode="unit", user_index=None, credentials=None)
        await c.APIProvider.ainvoke(None, None)
        await c.LLMProvider.adescribe(None)
        await c.LLMProvider.acomplete(None, None)
        c.LLMProvider.astream(None, None)
        await c.LLMProvider.aread_usage(None)
        await c.UIProvider.astart(None)
        await c.UIProvider.aclose(None)
        await c.UpstreamAPIProvider.avalidate(None, None)
    asyncio.run(scenario())
