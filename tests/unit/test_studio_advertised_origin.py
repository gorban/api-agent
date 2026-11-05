"""Trusted advertised origins preserve loopback transport and nonce admission."""
import asyncio

import pytest

from test_studio_bridge import setup_bridge, request


def test_named_origin_requires_explicit_owned_loopback_origin():
    with pytest.raises(ValueError, match='bridge configuration'):
        setup_bridge(origin='https://agent.example:8443')


@pytest.mark.parametrize('advertised', [
    'http://agent.example:8443', 'https://agent.example:8444',
    'https://user@agent.example:8443', 'https://agent.example:8443/path',
])
def test_advertised_origin_cannot_change_scheme_port_or_authority_shape(advertised):
    with pytest.raises(ValueError, match='readiness origin'):
        setup_bridge(origin='https://127.0.0.1:8443', advertised_origin=advertised)


@pytest.mark.parametrize('allowed', [True, False])
def test_named_browser_origin_still_requires_exact_origin_and_nonce(allowed):
    advertised = 'https://agent.example:8443'
    backend, calls = setup_bridge(origin='https://127.0.0.1:8443', advertised_origin=advertised)
    async def run():
        response = await backend.handle(request(Origin=advertised,
            **({} if allowed else {'X-API-Agent-Session': 'wrong'})), 'models')
        assert response.status_code == (200 if allowed else 403)
        assert calls == (['catalog'] if allowed else [])
        await backend.aclose()
    asyncio.run(run())


def test_bind_origin_cannot_substitute_for_advertised_browser_origin():
    backend, calls = setup_bridge(origin='https://127.0.0.1:8443',
        advertised_origin='https://agent.example:8443')
    async def run():
        response = await backend.handle(request(Origin='https://127.0.0.1:8443'), 'models')
        assert response.status_code == 403
        assert calls == []
        await backend.aclose()
    asyncio.run(run())
