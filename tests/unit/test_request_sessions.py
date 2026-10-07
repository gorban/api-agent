"""Request ownership and origin validation without upstream credentials."""
import asyncio

import pytest

from api_agent.auth.session import SessionError, SessionStore


@pytest.mark.parametrize('origin', [
    None, 7, '', 'http://agent.example', 'https://',
    'https://user@agent.example', 'https://user:password@agent.example',
    'https://agent.example/path', 'https://agent.example?query',
    'https://agent.example#fragment', 'https://agent.example:0',
    'https://agent.example:invalid', 'https://AGENT.example',
    'https://agent.example:0443',
])
def test_invalid_callback_origins_are_rejected(origin):
    with pytest.raises(ValueError, match='^invalid_origin$'):
        SessionStore('demo', origin)


def test_explicit_port_and_trailing_slash_are_canonicalized():
    assert SessionStore('demo', 'https://agent.example:443/').callback_origin == 'https://agent.example:443'


@pytest.mark.parametrize('pack', [None, 7, '', ' '])
def test_pack_id_is_required(pack):
    with pytest.raises(ValueError, match='invalid_pack_id'):
        SessionStore(pack, 'https://agent.example')


@pytest.mark.parametrize('capacity', [True, 0, -1, 1.5, '1'])
def test_capacity_must_be_a_positive_integer(capacity):
    with pytest.raises(ValueError, match='invalid_capacity'):
        SessionStore('demo', 'https://agent.example', max_sessions=capacity)


@pytest.mark.parametrize('ttl', [True, 0, -1, float('nan'), float('inf'), '1'])
def test_lifetime_must_be_finite_and_positive(ttl):
    with pytest.raises(ValueError, match='invalid_session_ttl'):
        SessionStore('demo', 'https://agent.example', session_ttl=ttl)


@pytest.mark.parametrize('principal', ['', 7, False])
def test_invalid_principals_cannot_create_sessions(principal):
    store = SessionStore('demo', 'https://agent.example')
    with pytest.raises(SessionError, match='invalid_principal'):
        store.open(None, principal, create=True)


def test_anonymous_sessions_expire_at_boundary_without_sliding_extension():
    now = [0]
    store = SessionStore('demo', 'https://agent.example', clock=lambda: now[0], session_ttl=2, max_sessions=1)
    opened = store.open(None, None, create=True)
    assert opened.set_cookie and opened.cookie_value
    assert opened.cookie_value not in repr(opened) and opened.cookie_value not in repr(opened.handle)
    now[0] = 1
    reused = store.open(opened.cookie_value, None)
    assert reused.handle is opened.handle and not reused.set_cookie and reused.cookie_value is None
    now[0] = 2
    with pytest.raises(SessionError, match='invalid_session'):
        store.open(opened.cookie_value, None)
    replacement = store.open(None, None, create=True)
    assert replacement.cookie_value != opened.cookie_value
    asyncio.run(store.aclose())
    assert not store._sessions
