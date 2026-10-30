"""An authenticated peer cannot reacquire a completion's credential snapshot."""
import asyncio
import hashlib
import hmac
import json

import pytest

from api_agent.providers import setup_transport as transport
from api_agent.providers.authority_channel import AuthorityChannel
from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import result_to_wire
from api_agent.providers.llm import LLMResult, Message, TextContent
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.authority_scope import TestAuthorityScope


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['before-release', 'after-release', 'distinct-completions'])
async def test_authority_request_is_one_shot_for_each_completion(mode):
    key = 'a' * 32
    secret = 'synthetic-authority-replay-secret'
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    scope = TestAuthorityScope(('ui',))
    scope.join('ui', boundary)
    expected = LLMResult(Message('assistant', (TextContent('safe answer'),)))
    receipts, wire_requests, holders = [], [], {}

    def signed_request(ticket):
        client = holders['client']
        payload = [transport._request_digest(client._binding), ticket, client._complete_count, holders['nonce']]
        proof = hmac.new(key.encode(), ('authority-request:' + transport._request_digest(payload)).encode(), hashlib.sha256).hexdigest()
        return {'version': 1, 'status': 'authority_request', 'ticket': ticket,
                'sequence': client._complete_count, 'proof': proof}

    def signed_release(binding, ticket, sequence):
        payload = [binding, ticket, sequence, holders['nonce']]
        proof = hmac.new(key.encode(), ('authority-release:' + transport._request_digest(payload)).encode(), hashlib.sha256).hexdigest()
        return {'version': 1, 'status': 'authority_release', 'ticket': ticket, 'proof': proof}

    def admit(binding, ticket, sequence):
        def receive(values):
            receipts.append((sequence, ticket, list(values)))
            reader = holders['reader']
            if mode == 'before-release':
                reader.feed_data(transport._encode(signed_request('b' * 64)))
                reader.feed_eof()
            elif mode == 'after-release':
                reader.feed_data(transport._encode(signed_release(binding, ticket, sequence)) +
                                 transport._encode(signed_request('b' * 64)))
                reader.feed_eof()
            else:
                reader.feed_data(transport._encode(signed_release(binding, ticket, sequence)) +
                                 transport._encode({'version': 1, 'status': 'completed', 'result': result_to_wire(expected)}))
        return receive

    channel = AuthorityChannel(key.encode(), admit)
    path = await channel.start()
    client = transport.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1,
                                    'capability': key, 'authority_path': path})
    holders['client'] = client
    client._authority_scope = scope
    client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'selected',
                       'worker_id': 'worker', 'attempt': 1}
    class Writer:
        def write(self, data):
            frame = json.loads(data)
            wire_requests.append(frame)
            if frame.get('action') == 'complete':
                holders['nonce'] = frame['authority_nonce']
                ticket = ('1' if client._complete_count == 1 else '2') * 64
                holders['reader'].feed_data(transport._encode(signed_request(ticket)))
        async def drain(self): pass
    client._writer = Writer()
    try:
        if mode == 'distinct-completions':
            for _ in range(2):
                holders['reader'] = client._reader = asyncio.StreamReader()
                assert await asyncio.wait_for(client._exchange('complete', {}), 5) == expected
                assert client._authority_lease is None and client._local_call is None
            assert [row[0] for row in receipts] == [1, 2]
            assert receipts[0][1] != receipts[1][1]
            assert wire_requests[0]['authority_nonce'] != wire_requests[1]['authority_nonce']
        else:
            holders['reader'] = client._reader = asyncio.StreamReader()
            with pytest.raises(ContractError, match='Duplicate authority request'):
                await asyncio.wait_for(client._exchange('complete', {}), 5)
            assert len(receipts) == 1 and receipts[0][2] == [secret]
            if mode == 'before-release':
                assert client._authority_lease is not None and scope._leases == 1
                await client._settle_authority()
            assert client._authority_lease is None and client._local_call is None and scope._leases == 0
        assert secret not in repr(wire_requests)
    finally:
        client._release_authority()
        await channel.close()
        scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('retain_tuple', [False, True])
async def test_missing_local_handoff_entry_declines_without_credential_transfer(retain_tuple):
    """Fault injection: ownership disappears after registration but before handoff."""
    from api_agent.providers.local_call_registry import remove
    key = 'a' * 32
    secret = 'synthetic-private-handoff-secret'
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    scope = TestAuthorityScope(('ui',))
    scope.join('ui', boundary)
    admissions = []
    channel = AuthorityChannel(key.encode(), lambda *args: admissions.append(args))
    path = await channel.start()
    client = transport.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1,
                                    'capability': key, 'authority_path': path})
    client._authority_scope = scope
    client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'selected',
                       'worker_id': 'worker', 'attempt': 1}
    client._reader = asyncio.StreamReader()
    observed = []
    class Writer:
        def write(self, data):
            frame = json.loads(data)
            observed.append(frame)
            if frame.get('action') == 'complete':
                assert client._local_call is not None
                remove(*client._local_call)
                if not retain_tuple:
                    client._local_call = None
                ticket = 'b' * 64
                payload = [transport._request_digest(client._binding), ticket, 1, frame['authority_nonce']]
                proof = hmac.new(key.encode(), ('authority-request:' + transport._request_digest(payload)).encode(), hashlib.sha256).hexdigest()
                client._reader.feed_data(transport._encode({'version': 1, 'status': 'authority_request',
                    'ticket': ticket, 'sequence': 1, 'proof': proof}))
            elif frame.get('status') == 'authority_declined':
                client._reader.feed_eof()
        async def drain(self): pass
    client._writer = Writer()
    try:
        with pytest.raises(ContractError, match='Setup connection unavailable'):
            await asyncio.wait_for(client._exchange('complete', {}), 5)
        assert [frame.get('status') for frame in observed if 'status' in frame] == ['authority_declined']
        assert admissions == [] and secret not in repr(observed)
        assert client._authority_lease is not None and scope._leases == 1
        await client._settle_authority()
        assert client._authority_lease is None and scope._leases == 0
    finally:
        client._release_authority()
        await channel.close()
        scope.close()
