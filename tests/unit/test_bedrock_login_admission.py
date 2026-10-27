"""Offline native admission uses positive private interaction capabilities."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.auth.interaction_transport import InteractionClient, InteractionServer, OwnedInteraction, _CancelledChannel
from api_agent.llm_providers.bedrock.acquisition import SessionRequest
from api_agent.llm_providers.bedrock.aws_login import AwsLoginAcquirer
from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
from api_agent.llm_providers.bedrock.login_transport import LoginTransport
from api_agent.providers.contracts import InteractionRequiredError


class Channel:
    def __init__(self):
        self.closed = False
    def write(self, text):
        pass
    def read(self, timeout):
        return None
    def close(self):
        self.closed = True


class Interaction:
    def __init__(self):
        self.channels = []
    def open(self, label):
        channel = Channel()
        self.channels.append(channel)
        return channel


def request(owner):
    return SessionRequest('chosen', 'us-east-1', None, True, interaction=owner)


def adapter():
    return AwsLoginAcquirer({'BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID': '123456789012',
        'BEDROCK_LOGIN_EXPECTED_PRINCIPAL': 'arn:aws:iam::123456789012:user/example'})


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['cancelled', 'unreachable', 'live'])
async def test_acquirer_rejects_unavailable_private_owner_before_transport(monkeypatch, unavailable):
    raw = Interaction()
    server = InteractionServer(raw)
    root = OwnedInteraction(InteractionClient(server.descriptor) if unavailable == 'unreachable' else raw)
    if unavailable == 'cancelled':
        root.cancel()
    if unavailable == 'unreachable':
        server.close()
    owner = adapter()
    transport = AsyncMock(return_value=dict(AccessKeyId='synthetic', SecretAccessKey='synthetic',
        SessionToken='synthetic', expires_at=time.time() + 120, identity=dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example')))
    monkeypatch.setattr(owner._transport, 'acquire', transport)
    try:
        if unavailable == 'live':
            await owner.acquire(request(root))
            transport.assert_awaited_once()
        else:
            with pytest.raises(InteractionRequiredError, match='^AWS sign-in interaction is unavailable[.]$'):
                await owner.acquire(request(root))
            transport.assert_not_awaited()
        await owner.aclose()
        assert not root.requests
    finally:
        await owner.aclose()
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('moment', ['fork', 'open'])
async def test_cancel_during_private_open_is_rejected_and_channel_disposed(moment):
    raw = Interaction()
    root = OwnedInteraction(raw)
    if moment == 'fork':
        original = root.fork
        def cancelled_fork():
            child = original()
            root.cancel()
            return child
        root.fork = cancelled_fork
    else:
        original = raw.open
        def cancelled_open(label):
            channel = original(label)
            root.cancel()
            return channel
        raw.open = cancelled_open
    bridge = LoginInteraction(request(root))
    with pytest.raises(InteractionRequiredError):
        await bridge.open()
    await bridge.aclose()
    assert not root.requests
    assert all(channel.closed for channel in raw.channels)


@pytest.mark.asyncio
async def test_actual_native_transport_rechecks_cancellation_before_launch(monkeypatch):
    root = OwnedInteraction(Interaction())
    bridge = LoginInteraction(request(root))
    await bridge.open()
    root.cancel()
    launch = AsyncMock(side_effect=AssertionError('native process must not start'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    transport = LoginTransport()
    with pytest.raises(InteractionRequiredError):
        await transport.acquire({'login': True}, {}, bridge)
    launch.assert_not_awaited()
    await bridge.aclose()
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('bridge', [None, object(), SimpleNamespace(check_ready=False)])
async def test_native_login_requires_readiness_contract_before_launch(monkeypatch, bridge):
    launch = AsyncMock(side_effect=AssertionError('native process must not start'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    transport = LoginTransport()
    with pytest.raises(InteractionRequiredError):
        await transport.acquire({'login': True}, {}, bridge)
    launch.assert_not_awaited()
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('remote_cancel', ['before', 'after', 'raw_closed', 'raw_unusable', 'live'])
async def test_remote_readiness_checks_server_owner_and_raw_channel(remote_cancel):
    raw = Interaction()
    server_root = OwnedInteraction(raw)
    server = InteractionServer(server_root)
    bridge = LoginInteraction(request(OwnedInteraction(InteractionClient(server.descriptor))))
    try:
        if remote_cancel == 'before':
            server_root.cancel()
            with pytest.raises(InteractionRequiredError):
                await bridge.open()
        else:
            await bridge.open()
            if remote_cancel == 'after':
                server_root.cancel()
            if remote_cancel == 'raw_closed':
                raw.channels[0].close()
            if remote_cancel == 'raw_unusable':
                raw.channels[0].write = None
            if remote_cancel == 'live':
                await bridge.check_ready()
            else:
                with pytest.raises(InteractionRequiredError):
                    await bridge.check_ready()
        await bridge.aclose()
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt', [None, {}, {'not_started': True}, {'channel': None}, {'channel': '0' * 32}, {'action': 'cancel', 'value': ''}])
async def test_lost_or_invalid_open_receipt_retains_server_channel_until_disposal(monkeypatch, receipt):
    raw = Interaction()
    server = InteractionServer(raw)
    client = InteractionClient(server.descriptor)
    original = client._call
    def lose_receipt(**message):
        result = original(**message)
        return receipt if message['action'] == 'open' else result
    monkeypatch.setattr(client, '_call', lose_receipt)
    bridge = LoginInteraction(request(OwnedInteraction(client)))
    try:
        with pytest.raises(InteractionRequiredError):
            await bridge.open()
        assert server.channels and not raw.channels[0].closed
        await bridge.aclose()
        assert not server.channels and raw.channels[0].closed
    finally:
        server.close()


def test_close_before_delayed_open_reserves_identity_and_prevents_admission():
    raw = Interaction()
    server = InteractionServer(raw)
    identifier = 'a' * 32
    try:
        assert server._dispatch({'action': 'close', 'channel': identifier}) == {'closed': True}
        result = server._dispatch({'action': 'open', 'channel': identifier, 'label': 'private'})
        assert result['action'] == 'cancel'
        assert not raw.channels and not server.channels
    finally:
        server.close()


def test_repeated_open_receipt_keeps_one_allocated_channel():
    raw = Interaction()
    server = InteractionServer(raw)
    message = {'action': 'open', 'channel': 'b' * 32, 'label': 'private'}
    try:
        assert server._dispatch(message) == server._dispatch(message)
        assert len(raw.channels) == 1
    finally:
        server.close()


@pytest.mark.parametrize('action', ['open', 'close'])
@pytest.mark.parametrize('identifier', [None, 1, 'z' * 32, 'short'])
def test_server_rejects_invalid_correlation_identifier(action, identifier):
    server = InteractionServer(Interaction())
    try:
        with pytest.raises(ValueError):
            server._dispatch({'action': action, 'channel': identifier, 'label': 'private'})
    finally:
        server.close()


def test_unknown_close_does_not_overflow_reserved_identity_bound():
    server = InteractionServer(Interaction())
    try:
        server.closed_channels.update(str(index) for index in range(4096))
        with pytest.raises(ValueError):
            server._dispatch({'action': 'close', 'channel': 'c' * 32})
    finally:
        server.close()


def test_generic_cancelled_channel_still_supports_legacy_read_contract():
    from api_agent.auth.interaction import Reply
    channel = _CancelledChannel()
    channel.write('private')
    assert channel.read(0) == Reply('cancel')
    with pytest.raises(RuntimeError, match='^Interaction unavailable$'):
        channel.check_open()
    channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('state', ['closed', 'missing_write', 'none'])
async def test_unusable_local_channel_rejected_before_native_admission(wrapped, state):
    raw = Interaction()
    channel = Channel()
    if state == 'closed':
        channel.closed = True
    if state == 'missing_write':
        channel.write = None
    raw.open = lambda label: None if state == 'none' else channel
    root = OwnedInteraction(raw) if wrapped else raw
    bridge = LoginInteraction(request(root))
    with pytest.raises(InteractionRequiredError):
        await bridge.open()
    if state == 'none' and wrapped:
        bridge._channel.channel = channel
    await bridge.aclose()


@pytest.mark.asyncio
async def test_unopened_or_closed_bridge_cannot_pass_readiness():
    bridge = LoginInteraction(request(Interaction()))
    with pytest.raises(InteractionRequiredError):
        await bridge.check_ready()
    await bridge.open()
    await bridge.aclose()
    with pytest.raises(InteractionRequiredError):
        await bridge.check_ready()


def test_unconfirmed_remote_open_preserves_cancelled_legacy_io(monkeypatch):
    from api_agent.auth.interaction import Reply
    client = InteractionClient({'port': 1, 'token': 'a' * 64})
    monkeypatch.setattr(client, '_call', lambda **message: None)
    channel = client.open('private')
    channel.write('private')
    assert channel.read(0) == Reply('cancel')
    with pytest.raises(RuntimeError):
        channel.check_open()


@pytest.mark.parametrize('reply', [None, {}, {'open': False}, {'open': 1}, {'open': True, 'extra': True}])
def test_remote_readiness_requires_strict_positive_ack(reply):
    from api_agent.auth.interaction_transport import _Channel
    channel = _Channel(SimpleNamespace(_call=lambda **message: reply), 'a' * 32)
    with pytest.raises(RuntimeError):
        channel.check_open()


def test_owned_wrapper_checks_closed_and_cancelled_status():
    root = OwnedInteraction(Interaction())
    channel = root.open('private')
    channel.check_open()
    channel.close()
    with pytest.raises(RuntimeError):
        channel.check_open()


@pytest.mark.asyncio
@pytest.mark.parametrize('remote', [False, True])
async def test_readiness_preserves_pending_private_authorization_reply(remote):
    from api_agent.auth.interaction import Reply
    raw = Interaction()
    pending = [Reply('input', 'c3ludGhldGlj')]
    channel = Channel()
    channel.read = lambda timeout: pending.pop(0)
    raw.open = lambda label: channel
    server = InteractionServer(raw)
    owner = OwnedInteraction(InteractionClient(server.descriptor) if remote else raw)
    bridge = LoginInteraction(request(owner))
    try:
        await bridge.open()
        await bridge.check_ready()
        await bridge.check_ready()
        assert len(pending) == 1
        assert await bridge.read() == 'c3ludGhldGlj'
        assert not pending
        await bridge.aclose()
    finally:
        server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('checker', [lambda: None, AsyncMock(side_effect=RuntimeError('synthetic private failure'))])
async def test_native_readiness_contract_failure_is_fixed_before_launch(monkeypatch, checker):
    launch = AsyncMock(side_effect=AssertionError('native process must not start'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    transport = LoginTransport()
    with pytest.raises(InteractionRequiredError, match='^AWS sign-in interaction is unavailable[.]$'):
        await transport.acquire({'login': True}, {}, SimpleNamespace(check_ready=checker))
    launch.assert_not_awaited()
    await transport.aclose()


@pytest.mark.asyncio
async def test_remote_raw_channel_contract_is_checked_before_admission():
    raw = Interaction()
    channel = Channel()
    channel.write = None
    raw.open = lambda label: channel
    server = InteractionServer(raw)
    bridge = LoginInteraction(request(OwnedInteraction(InteractionClient(server.descriptor))))
    try:
        with pytest.raises(InteractionRequiredError):
            await bridge.open()
        await bridge.aclose()
        assert channel.closed and not server.channels
    finally:
        server.close()


@pytest.mark.asyncio
async def test_caller_cancelled_during_readiness_cannot_start_native_helper(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def ready():
        entered.set()
        await release.wait()
    launch = AsyncMock(side_effect=AssertionError('native process must not start'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch)
    transport = LoginTransport()
    operation = asyncio.create_task(transport.acquire({'login': True}, {}, SimpleNamespace(check_ready=ready)))
    await entered.wait()
    operation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    launch.assert_not_awaited()
    assert not transport._launches and not transport._processes
    await transport.aclose()


@pytest.fixture(autouse=True)
def unavailable_ambient(monkeypatch):
    from api_agent.llm_providers.bedrock.ambient import AmbientSession
    async def acquire(self, request, **kwargs):
        return None, {}
    monkeypatch.setattr(AmbientSession, 'acquire', acquire)
