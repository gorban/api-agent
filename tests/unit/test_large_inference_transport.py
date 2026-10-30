import json

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.setup_transport import (
    EVIDENCE_CHUNK, MAX_CONTENT_FRAME, MAX_EVIDENCE_BYTES,
    _EvidenceReceiver, _evidence_frames,
)


def frames_for(content, remaining=MAX_EVIDENCE_BYTES):
    row = {'phase': 'request', 'content': content, 'reason': 'captured'}
    return [json.loads(frame) for frame in _evidence_frames(row, remaining)]


def test_large_evidence_round_trip_uses_bounded_frames_and_logical_chunks():
    content = {'messages': [{'text': 'é"' * 180000}]}
    frames = frames_for(content)
    receiver = _EvidenceReceiver()
    result = None
    for frame in frames:
        assert len(json.dumps(frame, separators=(',', ':')).encode()) + 1 <= MAX_CONTENT_FRAME
        result = receiver.accept(frame)
    assert result == {'phase': 'request', 'content': content, 'reason': 'captured'}
    offsets = [frame['offset'] for frame in frames if frame['status'] == 'evidence_part']
    assert EVIDENCE_CHUNK in offsets


@pytest.mark.parametrize('damage', ['offset', 'digest', 'missing', 'phase'])
def test_corrupt_chunk_sequence_never_admits_content(damage):
    frames = frames_for({'text': 'x' * 150000})
    if damage == 'offset':
        frames[1]['offset'] += 1
    elif damage == 'digest':
        frames[0]['sha256'] = '0' * 64
    elif damage == 'missing':
        frames.pop(1)
    else:
        frames[1]['phase'] = 'response'
    receiver = _EvidenceReceiver()
    with pytest.raises(ContractError):
        for frame in frames:
            receiver.accept(frame)


def test_overflow_emits_only_explicit_omission():
    frames = frames_for({'text': 'x' * 200000}, remaining=1000)
    assert frames == [{'version': 1, 'status': 'evidence', 'phase': 'request',
                       'content': {}, 'reason': 'truncated'}]


def test_oversized_compatibility_carrier_is_rejected_before_serialization(monkeypatch):
    import api_agent.providers.setup_transport as transport
    original_dumps = transport.json.dumps
    oversized = 'x' * (MAX_EVIDENCE_BYTES + 1)

    def reject_oversized(value, *args, **kwargs):
        assert value is not oversized
        assert not (type(value) is dict and value.get('text') is oversized)
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(transport.json, 'dumps', reject_oversized)
    assert frames_for({'text': oversized})[0]['reason'] == 'truncated'


def test_tiny_fragment_is_rejected_before_assembly():
    import base64
    frames = frames_for({'text': 'x' * 150000})
    frames[1]['data'] = base64.b64encode(b'x').decode('ascii')
    receiver = _EvidenceReceiver()
    receiver.accept(frames[0])
    with pytest.raises(ContractError):
        receiver.accept(frames[1])


def test_incomplete_or_concurrent_start_is_never_admitted():
    frames = frames_for({'text': 'x' * 150000})
    receiver = _EvidenceReceiver()
    assert receiver.accept(frames[0]) is None
    with pytest.raises(ContractError):
        receiver.accept(frames[0])
    assert receiver.pending is not None


@pytest.mark.parametrize('ending', ['eof', 'cancel', 'interleave'])
def test_interrupted_exchange_keeps_an_explicit_omission(ending):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from api_agent.providers.setup_transport import SetupClient, _encode

    async def scenario():
        client = SetupClient(dict(version=1, host='127.0.0.1', port=1,
                                  capability='public-synthetic-capability-value-000000'))
        client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'selected',
                           'worker_id': 'worker', 'attempt': 1}
        client._reader = asyncio.StreamReader()
        client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        start = frames_for({'text': 'x' * 150000})[0]
        client._reader.feed_data(_encode(start))
        if ending == 'eof':
            client._reader.feed_eof()
        if ending == 'interleave':
            client._reader.feed_data(_encode(dict(version=1, status='evidence', phase='request', content={})))
        task = asyncio.create_task(client._exchange('complete', {}))
        if ending == 'cancel':
            await asyncio.sleep(0)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if ending == 'cancel' else ContractError):
            await task
        assert client.evidence == [{'phase': 'request', 'content': {}, 'reason': 'truncated'}]

    asyncio.run(scenario())


def test_setup_server_closes_connections_above_peer_limit():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from api_agent.providers.setup_transport import SetupServer, _MAX_SETUP_PEERS
    server = SetupServer(object(), 'public-synthetic-capability-value-000000')
    server._peers = set(range(_MAX_SETUP_PEERS))
    writer = SimpleNamespace(close=Mock())
    server._accept(None, writer)
    writer.close.assert_called_once_with()
    assert len(server._peers) == _MAX_SETUP_PEERS


def test_aborted_transfer_preserves_completion_and_records_omission():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_transport import SetupClient, _encode

    async def scenario():
        client = SetupClient(dict(version=1, host='127.0.0.1', port=1,
                                  capability='public-synthetic-capability-value-000000'))
        client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'selected',
                           'worker_id': 'worker', 'attempt': 1}
        client._reader = asyncio.StreamReader()
        client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
        for frame in (frames_for({'text': 'x' * 150000})[0],
                      dict(version=1, status='evidence_abort', phase='request'),
                      dict(version=1, status='completed', result=result_to_wire(expected))):
            client._reader.feed_data(_encode(frame))
        assert await client._exchange('complete', {}) == expected
        assert client.evidence == [{'phase': 'request', 'content': {}, 'reason': 'truncated'}]

    asyncio.run(scenario())


def test_busy_evidence_slots_omit_content_and_keep_provider_result():
    import asyncio
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_transport import SetupClient, SetupServer, _MAX_EVIDENCE_TRANSFERS
    from test_setup_transport import CAPABILITY, Registry

    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))

    class Owner(Registry):
        async def complete(self, binding, request):
            from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
            from api_agent.workflows.inference_capture import bind_boundary, _COLLECTOR
            bind_boundary(EmissionBoundary(ContentPolicy(True)))
            _COLLECTOR.get()['rows'].append(dict(phase='request', content={'text': 'visible'}, reason='captured'))
            return expected

    async def scenario():
        server = SetupServer(Owner(), CAPABILITY, timeout=2)
        address = await server.start()
        for _ in range(_MAX_EVIDENCE_TRANSFERS):
            await server._evidence_slots.acquire()
        def consume():
            client = SetupClient(address, timeout=2)
            try:
                assert client.complete(request) == expected
                assert client.evidence == [{'phase': 'request', 'content': {}, 'reason': 'truncated'}]
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
        finally:
            for _ in range(_MAX_EVIDENCE_TRANSFERS):
                server._evidence_slots.release()
            await server.aclose()

    asyncio.run(scenario())


def test_unproven_exporter_is_never_invoked_and_evidence_is_withheld():
    import asyncio
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from test_setup_transport import CAPABILITY, Registry

    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))

    class Owner(Registry):
        async def complete(self, binding, request):
            return expected

        def export_content(self, *args):
            pytest.fail('Unproven exporter must not be invoked')

    async def scenario():
        server = SetupServer(Owner(), CAPABILITY, timeout=2)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=2)
            try:
                assert client.complete(request) == expected
                assert client.evidence == [dict(phase=phase, content={}, reason='withheld')
                                           for phase in ('request', 'response')]
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
        finally:
            await server.aclose()

    asyncio.run(scenario())


def test_large_background_preparation_preserves_event_loop_and_completed_result(monkeypatch):
    import asyncio
    import threading
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from test_setup_transport import CAPABILITY, Registry

    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    class Owner(Registry):
        async def complete(self, binding, request):
            from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
            from api_agent.workflows.inference_capture import bind_boundary, _COLLECTOR
            bind_boundary(EmissionBoundary(ContentPolicy(True)))
            _COLLECTOR.get()['rows'].append(dict(phase='request', content={'text': 'x' * 4_000_000}, reason='captured'))
            return expected
    async def scenario():
        server = SetupServer(Owner(), CAPABILITY, timeout=30)
        address = await server.start()
        entered, release = threading.Event(), threading.Event()
        original = SetupServer._prepare_cancellable
        def controlled(*args):
            entered.set()
            if not release.wait(30):
                raise AssertionError('Evidence worker was not released')
            return original(*args)
        monkeypatch.setattr(SetupServer, '_prepare_cancellable', staticmethod(controlled))
        def consume():
            client = SetupClient(address, timeout=30)
            try:
                ready = []
                assert client.complete(request, result_ready=ready.append) == expected
                assert ready == [expected]
                assert client.evidence == [{'phase': 'request', 'content': {'text': 'x' * 4_000_000}, 'reason': 'captured'}]
            finally:
                client.close()
        task = asyncio.create_task(asyncio.to_thread(consume))
        try:
            assert await asyncio.to_thread(entered.wait, 15)
            # Reaching this point requires the event loop to run while the
            # worker is held inside evidence preparation.
            assert not task.done()
            assert not release.is_set()
            release.set()
            await task
        finally:
            release.set()
            await server.aclose()
    asyncio.run(scenario())


def test_invalid_abort_records_interrupted_phase():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from api_agent.providers.setup_transport import SetupClient, _encode
    async def scenario():
        client = SetupClient(dict(version=1, host='127.0.0.1', port=1,
                                  capability='public-synthetic-capability-value-000000'))
        client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'selected',
                           'worker_id': 'worker', 'attempt': 1}
        client._reader = asyncio.StreamReader()
        client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        client._reader.feed_data(_encode(frames_for({'text': 'x' * 150000})[0]))
        client._reader.feed_data(_encode(dict(version=1, status='evidence_abort', phase='response')))
        with pytest.raises(ContractError):
            await client._exchange('complete', {})
        assert client.evidence == [{'phase': 'request', 'content': {}, 'reason': 'truncated'}]
    asyncio.run(scenario())


def test_authoritative_large_evidence_supersedes_large_fallback_before_budget():
    from api_agent.providers.setup_transport import SetupServer
    rows = [dict(phase='request', content={'text': 'f' * 3_000_000}, _source='fallback'),
            dict(phase='request', content={'text': 'a' * 3_000_000})]
    frames = SetupServer._prepare_evidence(rows)
    receiver = _EvidenceReceiver()
    result = None
    for _, status, body in frames:
        assert status.startswith('evidence_')
        result = receiver.accept(json.loads(body))
    assert result['content'] == {'text': 'a' * 3_000_000}


def test_transfer_slot_is_held_until_writer_finishes():
    import asyncio
    from types import SimpleNamespace
    from api_agent.providers.setup_transport import SetupServer, _Peer, _MAX_EVIDENCE_TRANSFERS
    from test_setup_transport import CAPABILITY
    async def scenario():
        server = SetupServer(object(), CAPABILITY)
        entered, release = asyncio.Event(), asyncio.Event()
        async def drain():
            entered.set()
            await release.wait()
        peer = _Peer(None, SimpleNamespace(write=lambda data: None, drain=drain))
        task = asyncio.create_task(server._publish_evidence(peer, [dict(phase='request', content={'text': 'x'})]))
        await entered.wait()
        assert server._evidence_slots._value == _MAX_EVIDENCE_TRANSFERS - 1
        release.set()
        await task
        assert server._evidence_slots._value == _MAX_EVIDENCE_TRANSFERS
    asyncio.run(scenario())


def test_duplicate_start_records_interrupted_other_phase():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from api_agent.providers.setup_transport import SetupClient, _encode

    async def scenario():
        client = SetupClient(dict(version=1, host='127.0.0.1', port=1,
                                  capability='public-synthetic-capability-value-000000'))
        client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'selected',
                           'worker_id': 'worker', 'attempt': 1}
        client._reader = asyncio.StreamReader()
        client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        request = dict(version=1, status='evidence', phase='request', content={'text': 'ok'})
        response_start = dict(frames_for({'text': 'x' * 150000})[0], phase='response')
        duplicate_start = dict(response_start, phase='request')
        for frame in (request, response_start, duplicate_start):
            client._reader.feed_data(_encode(frame))
        with pytest.raises(ContractError, match='Conflicting inference evidence'):
            await client._exchange('complete', {})
        assert client.evidence == [dict(phase='request', content={'text': 'ok'}),
                                   dict(phase='response', content={}, reason='truncated')]

    asyncio.run(scenario())


def test_unhashable_start_phase_records_interrupted_response():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from api_agent.providers.setup_transport import SetupClient, _encode

    async def scenario():
        client = SetupClient(dict(version=1, host='127.0.0.1', port=1,
                                  capability='public-synthetic-capability-value-000000'))
        client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'selected',
                           'worker_id': 'worker', 'attempt': 1}
        client._reader = asyncio.StreamReader()
        client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        start = dict(frames_for({'text': 'x' * 150000})[0], phase='response')
        malformed = dict(start, phase=[])
        for frame in (start, malformed):
            client._reader.feed_data(_encode(frame))
        with pytest.raises(ContractError, match='Invalid inference evidence'):
            await client._exchange('complete', {})
        assert client.evidence == [dict(phase='response', content={}, reason='truncated')]

    asyncio.run(scenario())


@pytest.mark.parametrize('racing', [False, True])
def test_late_registered_credential_is_rescrubbed_before_each_ipc_call(racing):
    import asyncio
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference
    from test_setup_transport import CAPABILITY

    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    boundary = EmissionBoundary(ContentPolicy(True))
    if racing:
        original_commit = boundary.commit_if_current
        def register_at_first_commit(generation, commit):
            boundary.register_secret('race-credential')
            return original_commit(generation, commit)
        boundary.commit_if_current = register_at_first_commit
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            bind_boundary(boundary)
            secret = 'late-credential-' + str(self.calls)
            capture_inference('input', {'text': 'visible ' + secret + (' race-credential' if racing else '')}, 'cause')
            boundary.register_secret(secret)
            self.calls += 1
            return expected
        calls = 0
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, CAPABILITY, timeout=3)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=3)
            try:
                for index in range(2):
                    assert client.complete(request) == expected
                    assert client.evidence[0]['content']['text'] == 'visible [REDACTED]' + (' [REDACTED]' if racing else '')
                    assert ('late-credential-' + str(index)) not in repr(client.evidence)
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_repeated_credential_generation_changes_omit_evidence_keep_result():
    import asyncio
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference
    from test_setup_transport import CAPABILITY

    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    boundary = EmissionBoundary(ContentPolicy(True))
    boundary.commit_if_current = lambda generation, commit: False
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            bind_boundary(boundary)
            capture_inference('input', {'text': 'visible'}, 'cause')
            return expected
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, CAPABILITY, timeout=3)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=3)
            try:
                assert client.complete(request) == expected
                assert client.evidence == [dict(phase='request', content={}, reason='truncated')]
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_builtin_authority_transfers_near_limit_scrubbed_evidence():
    import asyncio
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference
    from test_setup_transport import CAPABILITY

    visible = 'x' * 3_900_000
    secret = 'credential-known-before-capture'
    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,))
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            bind_boundary(boundary)
            capture_inference('input', {'text': visible + secret}, 'cause')
            return expected
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, CAPABILITY, timeout=30)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=30)
            try:
                ready = []
                assert client.complete(request, result_ready=ready.append) == expected
                assert ready == [expected]
                assert client.evidence == [dict(phase='request', content={'text': visible + '[REDACTED]'}, reason='captured')]
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [False, True])
def test_unproven_custom_exporter_never_sends_credentials(failure):
    import asyncio
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from test_setup_transport import CAPABILITY, Registry
    secret = 'custom-private-credential'
    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    class CustomOwner(Registry):
        exports = 0
        async def complete(self, binding, request):
            if failure:
                raise ValueError('provider failed')
            return expected
        def export_content(self, *args):
            self.exports += 1
            return [dict(phase='request', content={'text': secret})]
    async def scenario():
        owner = CustomOwner()
        server = SetupServer(owner, CAPABILITY, timeout=2)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=2)
            try:
                if failure:
                    with pytest.raises(ContractError):
                        client.complete(request)
                else:
                    assert client.complete(request) == expected
                phases = ('request',) if failure else ('request', 'response')
                assert client.evidence == [dict(phase=phase, content={}, reason='withheld') for phase in phases]
                assert secret not in repr(client.evidence)
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
            assert owner.exports == 0
        finally:
            await server.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('authority', ['absent', 'disabled'])
def test_unadmitted_collector_rows_are_withheld_before_ipc(authority):
    import asyncio
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import bind_boundary, _COLLECTOR
    from test_setup_transport import CAPABILITY, Registry
    secret = 'unadmitted-synthetic-secret'
    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    class Owner(Registry):
        async def complete(self, binding, request):
            if authority == 'disabled':
                bind_boundary(EmissionBoundary(ContentPolicy(False)))
            _COLLECTOR.get()['rows'].append(dict(phase='request', content={'text': secret}, reason='captured'))
            return expected
    async def scenario():
        server = SetupServer(Owner(), CAPABILITY, timeout=2)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=2)
            try:
                assert client.complete(request) == expected
                assert client.evidence == [dict(phase=phase, content={}, reason='withheld')
                                           for phase in ('request', 'response')]
                assert secret not in repr(client.evidence)
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
        finally:
            await server.aclose()
    asyncio.run(scenario())


def test_foreground_expired_evidence_allowance_keeps_result_and_omits_content(monkeypatch):
    import asyncio
    import threading
    from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
    from api_agent.providers.setup_transport import SetupClient, SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference
    from test_setup_transport import CAPABILITY, Registry

    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = SetupServer._prepare_cancellable

    def controlled(*args):
        entered.set()
        try:
            if not release.wait(15):
                raise AssertionError('Evidence worker was not released')
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(SetupServer, '_prepare_cancellable', staticmethod(controlled))

    class Owner(Registry):
        async def complete(self, binding, request):
            bind_boundary(EmissionBoundary(ContentPolicy(True)))
            capture_inference('input', {'text': 'visible'}, request.request_id)
            return expected

    async def scenario():
        server = SetupServer(Owner(), CAPABILITY, timeout=4)
        address = await server.start()

        def consume():
            client = SetupClient(address, timeout=4)
            try:
                result = client.complete(request)
                return result, client.evidence
            finally:
                client.close()

        task = asyncio.create_task(asyncio.to_thread(consume))
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            assert not task.done() and not finished.is_set()
            result, evidence = await asyncio.wait_for(task, 10)
            assert not release.is_set() and not finished.is_set()
            assert result == expected
            assert evidence == [dict(phase='request', content={}, reason='truncated')]
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 10)
            await server.aclose()
    asyncio.run(scenario())
