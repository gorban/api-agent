"""Provider credential recovery stays bound to the original operation."""
import asyncio
import hashlib
from dataclasses import replace

import pytest

from api_agent.llm_providers.gpt.connections import AuthError, ConnectionStore
from api_agent.llm_providers.gpt.factory import OwnedSubscriptionSession
from api_agent.llm_providers.gpt.oauth import SCOPES
from test_gpt_oauth import run


def test_saved_rotation_mismatch_preserves_pending_material(tmp_path):
    store = ConnectionStore(tmp_path / 'store')
    previous = replace(run()[0], host_id=store.host_id())
    profile = store.save(previous)
    updated = replace(previous, access_token='new-access', refresh_token='new-refresh')
    session = OwnedSubscriptionSession(store, profile)
    session._pending_refresh.update(identity=hashlib.sha256(previous.refresh_token.encode()).digest(),
        data={'access_token': updated.access_token, 'refresh_token': 'other-refresh'})
    with pytest.raises(AuthError, match='pending_refresh_mismatch'):
        session._recover_saved(previous, updated)
    assert session._pending_refresh['data']['refresh_token'] == 'other-refresh'
    session._pending_refresh['data']['refresh_token'] = updated.refresh_token
    session._recover_saved(previous, updated)
    assert not session._pending_refresh


def test_disposal_admits_pending_rotation_even_without_active_refresh_task(tmp_path):
    store = ConnectionStore(tmp_path / 'store')
    previous = replace(run()[0], host_id=store.host_id())
    profile = store.save(previous)
    session = OwnedSubscriptionSession(store, profile)
    session.clock = lambda: 1050
    session._pending_refresh.update(identity=hashlib.sha256(previous.refresh_token.encode()).digest(),
        data={'access_token': 'new-access', 'refresh_token': 'new-refresh', 'token_type': 'Bearer',
              'expires_in': 100, 'scope': SCOPES}, received_at=1050)
    asyncio.run(session.aclose())
    assert store.load(profile).refresh_token == 'new-refresh'
    assert not session._pending_refresh


def test_failed_ack_without_recovery_callback_keeps_saved_rotation_and_fails_closed(tmp_path):
    from api_agent.llm_providers.gpt.connections import SubscriptionSession
    from api_agent.llm_providers.gpt.contracts import Connection
    store = ConnectionStore(tmp_path / 'private')
    original = Connection('subject', 'client', store.host_id(), '', 'access', 'refresh', 'id', ('one',), 100)
    profile = store.save(original)
    pending, issued = {'active': True}, []
    async def refresh(value, now):
        issued.append(value.refresh_token)
        return replace(value, access_token='new-access', refresh_token='new-refresh', expires_at=200)
    def fail_ack(value): raise RuntimeError('sensitive detail')
    async def scenario():
        session = SubscriptionSession(store, profile, refresh, clock=lambda: 50,
            has_pending=lambda: bool(pending), on_saved=fail_ack)
        try:
            with pytest.raises(AuthError, match='refresh_admission_failed'): await session()
            with pytest.raises(AuthError, match='refresh_admission_failed') as caught: await session()
            assert 'sensitive detail' not in str(caught.value)
            assert store.load(profile).refresh_token == 'new-refresh'
            assert issued == ['refresh']
        finally:
            pending.clear()
            session._retry.set()
            await session.aclose()
    asyncio.run(scenario())
