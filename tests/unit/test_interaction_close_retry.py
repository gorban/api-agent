"""Owned channel failures retain retry without closing unrelated operations."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from api_agent.auth.interaction_transport import OwnedInteraction


class Channel:
    def __init__(self):
        self.fail = False
        self.closes = 0

    def close(self):
        self.closes += 1
        if self.fail:
            raise ValueError('private failure')


class Transport:
    def __init__(self):
        self.channels = []

    def open(self, label):
        channel = Channel()
        self.channels.append(channel)
        return channel


def test_channel_failure_stays_owned_until_successful_retry():
    transport = Transport()
    root = OwnedInteraction(transport)
    child = root.fork()
    sibling = root.fork()
    channel = child.open('private')
    sibling_channel = sibling.open('other')
    transport.channels[0].fail = True
    with pytest.raises(RuntimeError, match='^Interaction cleanup failed$'):
        child.close()
    assert child in root.requests
    assert channel in child.channels and not channel.closed
    assert not sibling.cancelled and not sibling_channel.closed
    transport.channels[0].fail = False
    child.close()
    child.close()
    assert channel.closed and not child.channels and child not in root.requests
    assert transport.channels[0].closes == 2
    assert not root.cancelled and not sibling.cancelled and not sibling_channel.closed


def test_failed_child_does_not_prevent_successful_siblings_cleanup():
    transport = Transport()
    root = OwnedInteraction(transport)
    failing = root.fork()
    successful = root.fork()
    bad = failing.open('bad')
    good = successful.open('good')
    direct = root.open('direct')
    transport.channels[0].fail = True
    with pytest.raises(RuntimeError):
        root.close()
    assert root.requests == {failing}
    assert good.closed and direct.closed and not bad.closed
    transport.channels[0].fail = False
    root.close()
    assert not root.channels and not root.requests and bad.closed
    assert [channel.closes for channel in transport.channels] == [2, 1, 1]


def test_simultaneous_owner_and_channel_close_acknowledge_once():
    transport = Transport()
    root = OwnedInteraction(transport)
    channel = root.open('private')
    entered, release = threading.Event(), threading.Event()
    original = transport.channels[0].close
    def delayed():
        entered.set()
        assert release.wait(2)
        original()
    transport.channels[0].close = delayed
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(root.close)
        assert entered.wait(2)
        second = pool.submit(root.close)
        third = pool.submit(channel.close)
        release.set()
        first.result(2)
        second.result(2)
        third.result(2)
    assert transport.channels[0].closes == 1
    assert not root.channels and channel.closed


def test_remote_channel_failure_requires_acknowledgment_and_retry():
    from api_agent.auth.interaction_transport import InteractionClient, InteractionServer
    transport = Transport()
    server = InteractionServer(transport)
    remote = InteractionClient(server.descriptor).open('private')
    raw = transport.channels[0]
    try:
        raw.fail = True
        with pytest.raises(RuntimeError, match='^Interaction cleanup failed$'):
            remote.close()
        assert remote.identifier in server.channels
        assert raw.closes == 1
        raw.fail = False
        remote.close()
        remote.close()
        assert not server.channels and raw.closes == 2
    finally:
        raw.fail = False
        server.close()


def test_remote_lost_ack_retries_success_without_duplicate_raw_close(monkeypatch):
    from api_agent.auth.interaction_transport import InteractionClient, InteractionServer
    transport = Transport()
    server = InteractionServer(transport)
    client = InteractionClient(server.descriptor)
    remote = client.open('private')
    original = client._call
    lost = []
    def interrupted(**message):
        reply = original(**message)
        if message['action'] == 'close' and not lost:
            lost.append(True)
            return {'action': 'cancel', 'value': ''}
        return reply
    monkeypatch.setattr(client, '_call', interrupted)
    try:
        with pytest.raises(RuntimeError):
            remote.close()
        assert not server.channels and transport.channels[0].closes == 1
        remote.close()
        assert transport.channels[0].closes == 1
    finally:
        server.close()


def test_server_cleanup_retains_failed_raw_and_retires_other_channels():
    from api_agent.auth.interaction_transport import InteractionClient, InteractionServer
    transport = Transport()
    server = InteractionServer(transport)
    client = InteractionClient(server.descriptor)
    first, second = client.open('first'), client.open('second')
    transport.channels[0].fail = True
    try:
        with pytest.raises(RuntimeError, match='^Interaction cleanup failed$'):
            server.close()
        assert set(server.channels) == {first.identifier}
        assert second.identifier in server.closed_channels
        assert [raw.closes for raw in transport.channels] == [1, 1]
        transport.channels[0].fail = False
        server.close()
        assert not server.channels
        assert [raw.closes for raw in transport.channels] == [2, 1]
    finally:
        transport.channels[0].fail = False
        server.close()


@pytest.mark.parametrize('reply', [None, {}, {'closed': False}, {'closed': 1}, {'closed': True, 'other': True}])
def test_remote_close_rejects_missing_or_malformed_ack(reply):
    from types import SimpleNamespace
    from api_agent.auth.interaction_transport import _Channel
    remote = _Channel(SimpleNamespace(_call=lambda **message: reply), 'opaque')
    with pytest.raises(RuntimeError, match='^Interaction cleanup failed$'):
        remote.close()


def test_remote_close_ack_history_is_bounded():
    from api_agent.auth.interaction_transport import InteractionServer
    server = InteractionServer(Transport())
    try:
        server.closed_channels.update(str(index) for index in range(4096))
        with pytest.raises(ValueError):
            server._dispatch({'action': 'open', 'label': 'private'})
        assert not server.channels
    finally:
        server.close()
