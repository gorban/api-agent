"""Protocol rejects malformed evidence and preserves cleanup ownership."""
import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.providers import authority_channel as authority
from api_agent.providers import setup_transport as setup
from api_agent.providers.contracts import ContractError, SetupRegistrationLimitError


@pytest.mark.parametrize('path', ['', 'relative', '/tmp/a\x00b', '/' + 'x' * 101, None])
def test_descriptor_rejects_invalid_authority_path(path):
    with pytest.raises(ContractError, match='authority endpoint'):
        setup.validate_descriptor({'version': 1, 'host': '127.0.0.1', 'port': 1,
            'capability': 'x' * 32, 'authority_path': path})


@pytest.mark.parametrize('value,budget', [('', 1), ('\ud800', 100), ({1: 'x'}, 100),
    (object(), 100), ([], 1), ([[[[['x']]]]], 1), (float('nan'), 100)])
def test_invalid_or_over_budget_evidence_is_not_admitted(value, budget):
    assert setup._bounded_evidence(value, budget) is False


def test_evidence_depth_and_cycles_are_rejected_without_mutating_input():
    value = []
    value.append(value)
    assert setup._bounded_evidence(value, 100) is False
    assert value[0] is value
    value = 'leaf'
    for _ in range(22): value = [value]
    assert setup._bounded_evidence(value, 1000) is False


def evidence_start(payload):
    return {'version': 1, 'status': 'evidence_start', 'phase': 'request', 'reason': 'captured',
            'length': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}


@pytest.mark.parametrize('payload', [b'[]', b'{ "key":1}', b'{"key":NaN}', b'{"x":1,"x":2}', b'\xff'])
def test_receiver_rejects_noncanonical_or_invalid_json_even_with_matching_digest(payload):
    receiver = setup._EvidenceReceiver()
    receiver.accept(evidence_start(payload))
    receiver.accept({'version': 1, 'status': 'evidence_part', 'phase': 'request',
                     'offset': 0, 'data': base64.b64encode(payload).decode()})
    with pytest.raises(ContractError, match='Invalid inference evidence'):
        receiver.accept({'version': 1, 'status': 'evidence_end', 'phase': 'request'})
    assert receiver.total == 0


def test_receiver_rejects_unstarted_oversized_invalid_and_corrupt_parts():
    receiver = setup._EvidenceReceiver()
    with pytest.raises(ContractError): receiver.accept({'status': 'evidence_end'})
    with pytest.raises(ContractError): receiver.accept({'status': 'evidence_part', 'data': 'x' * setup.MAX_CONTENT_FRAME})
    receiver.accept(evidence_start(b'{}'))
    with pytest.raises(ContractError):
        receiver.accept({'version': 1, 'status': 'evidence_part', 'phase': 'request', 'offset': 0, 'data': '%%%'})
    with pytest.raises(ContractError):
        receiver.accept({'version': 1, 'status': 'unexpected', 'phase': 'request'})
    assert receiver.total == 0


def test_registration_limit_frame_requires_exact_contract():
    frame = dict(version=1, status='error', category='registration_limit', index_source=None,
                 message='Setup registration limit reached for this operation')
    with pytest.raises(SetupRegistrationLimitError): setup._raise_error(frame)
    with pytest.raises(ContractError): setup._raise_error(frame | {'message': 'untrusted'})


@pytest.mark.parametrize('raw', [b'[]\n', b'{"x":1,"x":2}\n', b'\xff\n', b'{}'])
@pytest.mark.asyncio
async def test_authority_read_rejects_invalid_frame(raw):
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    with pytest.raises(ContractError, match='Invalid authority handoff'):
        await authority._read(reader)


def test_authority_bounds_and_peer_identity_fail_closed():
    with pytest.raises(ContractError, match='limit'):
        authority._encode({'text': 'x' * authority._LIMIT})
    with pytest.raises(ContractError, match='configuration'):
        authority.AuthorityChannel(b'short', lambda *_: None)
    with pytest.raises(ContractError, match='peer identity'):
        authority._check_peer(SimpleNamespace(get_extra_info=lambda _: None))


@pytest.mark.asyncio
async def test_authority_close_cancels_owned_handler_and_releases_writer(monkeypatch):
    channel = authority.AuthorityChannel(b'x' * 32, lambda *_: None)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def serve(reader, writer):
        entered.set()
        try: await asyncio.Future()
        finally:
            cancelled.set()
            writer.close()
    monkeypatch.setattr(channel, '_serve', serve)
    writer = SimpleNamespace(close=Mock())
    channel._accept(None, writer)
    await entered.wait()
    await channel.close()
    assert cancelled.is_set()
    writer.close.assert_called_once()
    assert not channel._tasks


def test_authority_capacity_closes_new_unadmitted_writer():
    channel = authority.AuthorityChannel(b'x' * 32, lambda *_: None)
    channel._tasks = set(range(128))
    writer = SimpleNamespace(close=Mock())
    channel._accept(None, writer)
    writer.close.assert_called_once()
    assert len(channel._tasks) == 128


@pytest.mark.parametrize('field,value,error', [
    ('authority_nonce', 'bad', 'authority challenge'),
    ('authority_required', False, 'authority requirement'),
    ('background', False, 'background evidence'),
])
def test_server_rejects_invalid_authority_and_background_admission(field, value, error):
    server = setup.SetupServer(SimpleNamespace(), 'a' * 32)
    server._authority_channel = object()
    frame = {'action': 'complete', 'authority_nonce': 'a' * 64, field: value}
    with pytest.raises(ContractError, match=error): server._request(frame)


def test_server_rejects_invalid_operation_authority():
    with pytest.raises(ContractError, match='operation authority'):
        setup.SetupServer(SimpleNamespace(), 'a' * 32, authority=object())


@pytest.mark.asyncio
async def test_invalid_transfer_shape_never_commits_and_closes_even_when_close_raises(monkeypatch):
    key = b'x' * 32
    binding, ticket, nonce = 'a' * 64, 'b' * 64, 'c' * 64
    received = []
    channel = authority.AuthorityChannel(key, lambda *_: received.append)
    hello = {'version': 1, 'binding': binding, 'ticket': ticket, 'sequence': 1, 'nonce': nonce,
             'proof': authority._mac(key, 'client-hello', binding, ticket, 1, nonce)}
    reader = asyncio.StreamReader()
    reader.feed_data(authority._encode(hello) + authority._encode({'version': 1, 'secrets': []}))
    reader.feed_eof()
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(),
                             wait_closed=AsyncMock(side_effect=OSError('already closed')))
    monkeypatch.setattr(authority, '_check_peer', lambda writer: None)
    await channel._serve(reader, writer)
    assert received == []
    assert writer.write.call_count == 1  # challenge only; no acknowledgement
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_forged_challenge_is_never_sent_credentials_and_writer_is_closed(monkeypatch):
    reader = asyncio.StreamReader()
    reader.feed_data(authority._encode({'version': 1, 'nonce': 'a' * 64, 'proof': '0' * 64}))
    reader.feed_eof()
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(),
                             wait_closed=AsyncMock(side_effect=OSError('already closed')))
    monkeypatch.setattr(authority, '_check_peer', lambda writer: None)
    monkeypatch.setattr(authority.asyncio, 'open_unix_connection', AsyncMock(return_value=(reader, writer)))
    with pytest.raises(ContractError, match='server authentication'):
        await authority.transfer('/synthetic', b'x' * 32, 'a' * 64, 'b' * 64, 1, ['synthetic-secret'])
    assert writer.write.call_count == 1 and b'synthetic-secret' not in writer.write.call_args.args[0]
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


def test_invalid_capture_set_returns_bounded_truncated_phases():
    rows = setup.SetupServer._prepare_evidence([None] * 129)
    frames = [json.loads(frame) for _, _, frame in rows]
    assert [(frame['phase'], frame['reason'], frame['content']) for frame in frames] == [
        ('request', 'truncated', {}), ('response', 'truncated', {})]


def test_oversized_capture_is_omitted_without_losing_safe_other_phase():
    rows = [{'phase': 'request', 'content': {'text': 'x' * (setup.MAX_EVIDENCE_BYTES + 1)}},
            {'phase': 'response', 'content': {'text': 'ordinary'}, 'reason': 'captured'}]
    frames = [json.loads(frame) for _, _, frame in setup.SetupServer._prepare_evidence(rows)]
    assert frames[0]['reason'] == 'truncated' and frames[0]['content'] == {}
    assert frames[1]['content'] == {'text': 'ordinary'} and frames[1]['reason'] == 'captured'


def test_projected_partial_capture_stays_partial_after_secret_redaction():
    import threading
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-secret',))
    rows = [{'phase': 'request', 'content': {'text': 'ordinary synthetic-secret'}, 'reason': 'partial'}]
    frames = [json.loads(frame) for _, _, frame in setup.SetupServer._prepare_cancellable(rows, threading.Event(), boundary, True)]
    assert frames[0]['reason'] == 'partial'
    assert frames[0]['content']['text'] == 'ordinary [REDACTED]'


@pytest.mark.asyncio
async def test_corrupt_chunk_keeps_only_truncated_omission_and_never_releases_payload():
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client._reader = asyncio.StreamReader()
    start = evidence_start(b'{}')
    invalid = {'version': 1, 'status': 'evidence_part', 'phase': 'request', 'offset': 0, 'data': '%%'}
    client._reader.feed_data(setup._encode(start) + setup._encode(invalid))
    client._reader.feed_eof()
    with pytest.raises(ContractError, match='Invalid inference evidence'):
        await client._exchange('complete', {})
    assert client.evidence == [{'phase': 'request', 'content': {}, 'reason': 'truncated'}]


@pytest.mark.asyncio
async def test_authority_admission_capacity_rejects_before_writing_any_ticket(monkeypatch):
    server = setup.SetupServer(SimpleNamespace(), 'a' * 32)
    server._authority_pending = {str(i): None for i in range(setup._MAX_SETUP_PEERS)}
    peer = SimpleNamespace(writer=SimpleNamespace(write=Mock()))
    assert await server._caller_authority(peer, {'authority_nonce': 'a' * 64}) is None
    peer.writer.write.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('next_frame,match', [
    (None, 'Setup connection unavailable'),
    ({'version': 2, 'status': 'evidence_part'}, 'Invalid setup response'),
    ({'version': 1, 'status': 'evidence_abort', 'phase': 'response'}, 'Invalid inference evidence'),
    ({'version': 1, 'status': 'evidence_start', 'phase': None}, 'Invalid inference evidence'),
    ({'version': 1, 'status': 'evidence', 'phase': 'response', 'content': {'text': 'untrusted'}}, 'Interleaved inference evidence'),
])
async def test_interrupted_evidence_is_omitted_before_protocol_failure(next_frame, match):
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client._reader = asyncio.StreamReader()
    data = setup._encode(evidence_start(b'{}'))
    if next_frame is not None: data += setup._encode(next_frame)
    client._reader.feed_data(data)
    client._reader.feed_eof()
    with pytest.raises(ContractError, match=match):
        await client._exchange('complete', {})
    assert client.evidence == [{'phase': 'request', 'content': {}, 'reason': 'truncated'}]


@pytest.mark.asyncio
async def test_completed_result_survives_interrupted_optional_evidence():
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
    expected = LLMResult(Message('assistant', (TextContent('answer'),)), Usage(3, 1))
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client._reader = asyncio.StreamReader()
    client._reader.feed_data(setup._encode(evidence_start(b'{}')) + setup._encode(
        {'version': 1, 'status': 'completed', 'result': result_to_wire(expected)}))
    client._reader.feed_eof()
    assert await client._exchange('complete', {}) == expected
    assert client.evidence == [{'phase': 'request', 'content': {}, 'reason': 'truncated'}]


@pytest.mark.asyncio
async def test_invalid_authority_decline_drops_reservation_without_publishing_credentials():
    server = setup.SetupServer(SimpleNamespace(), 'a' * 32)
    reader = asyncio.StreamReader()
    reader.feed_data(setup._encode({'version': 1, 'status': 'authority_declined', 'ticket': 'wrong'}))
    reader.feed_eof()
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    peer = SimpleNamespace(writer=writer, reader=reader, binding={'worker_id': 'worker'}, complete_count=1)
    with pytest.raises(ContractError, match='Invalid authority response'):
        await server._caller_authority(peer, {'authority_nonce': 'a' * 64})
    assert server._authority_pending == {}
    assert writer.write.call_count == 1
    assert b'secrets' not in writer.write.call_args.args[0]


def test_unsupported_peer_platform_is_rejected_without_sending_credentials(monkeypatch):
    monkeypatch.delattr(authority.socket, 'SO_PEERCRED', raising=False)
    monkeypatch.setattr(authority.sys, 'platform', 'unsupported')
    peer = SimpleNamespace(fileno=lambda: 5)
    with pytest.raises(ContractError, match='peer identity unavailable'):
        authority._check_peer(SimpleNamespace(get_extra_info=lambda name: peer))


@pytest.mark.asyncio
@pytest.mark.parametrize('corrupt', [b'{}', b'[["bad"]]', b'not-json'])
async def test_corrupt_projection_child_result_rejects_and_reaps_real_process(monkeypatch, corrupt):
    from api_agent.providers import evidence_watchdog
    from api_agent.telemetry.privacy import EmissionBoundary
    processes = []
    original_spawn = asyncio.create_subprocess_exec
    original_communicate = evidence_watchdog.communicate
    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process
    async def corrupt_output(process, payload):
        output, stderr = await original_communicate(process, payload)
        assert process.returncode == 0 and json.loads(output) == []
        return corrupt, stderr
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(evidence_watchdog, 'communicate', corrupt_output)
    with pytest.raises((ContractError, ValueError)):
        await setup.SetupServer._prepare_isolated([], EmissionBoundary())
    assert len(processes) == 1 and processes[0].returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('eof', [False, True])
async def test_settlement_releases_authority_only_after_real_eof(eof):
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    lease = SimpleNamespace(release=Mock())
    client._authority_lease = lease
    client._reader = asyncio.StreamReader()
    client._reader.feed_data(b'invalid\n')
    if eof: client._reader.feed_eof()
    if eof:
        await client._settle_authority()
        lease.release.assert_called_once()
        assert client._authority_lease is None
    else:
        with pytest.raises(ContractError): await client._settle_authority()
        lease.release.assert_not_called()
        assert client._authority_lease is lease


@pytest.mark.asyncio
async def test_projection_rejects_oversized_input_before_spawning(monkeypatch):
    from api_agent.telemetry.privacy import EmissionBoundary
    spawn = AsyncMock(side_effect=AssertionError('oversized input spawned a child'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    rows = [{'phase': 'request', 'content': {'text': 'x' * (setup.MAX_EVIDENCE_BYTES + 1024 * 1024)}}]
    with pytest.raises(ContractError, match='input limit'):
        await setup.SetupServer._prepare_isolated(rows, EmissionBoundary())
    spawn.assert_not_awaited()


def test_invalid_evidence_never_produces_wire_frames():
    assert list(setup._evidence_frames({'phase': 'request', 'content': []}, 100)) == []


def test_registration_limit_error_is_serialized_without_private_message():
    frame = setup._error(SetupRegistrationLimitError('private synthetic detail'))
    assert frame == dict(version=1, status='error', category='registration_limit', index_source=None,
                         message='Setup registration limit reached for this operation')
    assert 'private synthetic detail' not in repr(frame)


@pytest.mark.asyncio
async def test_invalid_authority_request_is_rejected_before_lease_or_credential_transfer():
    scope = SimpleNamespace(freeze=Mock(side_effect=AssertionError('must not freeze')))
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1,
                                'capability': 'a' * 32, 'authority_path': '/tmp/authority'})
    client._authority_scope = scope
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client._reader = asyncio.StreamReader()
    client._reader.feed_data(setup._encode({'version': 1, 'status': 'authority_request',
                                            'ticket': 'malformed', 'sequence': 1, 'proof': 'bad'}))
    client._reader.feed_eof()
    try:
        with pytest.raises(ContractError, match='Invalid authority request'):
            await client._exchange('complete', {})
        scope.freeze.assert_not_called()
        assert client._authority_lease is None
        assert client._writer.write.call_count == 1
    finally:
        client._release_authority()


@pytest.mark.asyncio
async def test_unhanded_authority_lease_released_on_successful_completion():
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers.llm import LLMResult, Message, TextContent
    expected = LLMResult(Message('assistant', (TextContent('safe'),)))
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    lease = SimpleNamespace(release=Mock())
    client._authority_lease = lease
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client._reader = asyncio.StreamReader()
    client._reader.feed_data(setup._encode({'version': 1, 'status': 'completed', 'result': result_to_wire(expected)}))
    client._reader.feed_eof()
    assert await client._exchange('complete', {}) == expected
    lease.release.assert_called_once()
    assert client._authority_lease is None


@pytest.mark.asyncio
async def test_optional_early_error_without_observer_does_not_replace_final_result():
    from api_agent.providers.control import OperationalError
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers.llm import LLMResult, Message, TextContent
    expected = LLMResult(Message('assistant', (TextContent('safe'),)))
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    client._result_ready = Mock()
    client._error_ready = None
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client._reader = asyncio.StreamReader()
    client._reader.feed_data(setup._encode({'version': 1, 'status': 'error_ready',
                                            'error': setup._error(OperationalError('unavailable'))}) +
                             setup._encode({'version': 1, 'status': 'completed', 'result': result_to_wire(expected)}))
    client._reader.feed_eof()
    assert await client._exchange('complete', {}) == expected
    assert client._result_ready is None and client._error_ready is None


@pytest.mark.asyncio
async def test_inline_evidence_after_full_chunked_budget_is_omitted_but_result_survives():
    from api_agent.providers.inference import result_to_wire
    from api_agent.providers.llm import LLMResult, Message, TextContent
    content = {'text': 'x' * (setup.MAX_EVIDENCE_BYTES - len(b'{"text":""}'))}
    assert len(json.dumps(content, separators=(',', ':')).encode()) == setup.MAX_EVIDENCE_BYTES
    frames = list(setup._evidence_frames({'phase': 'request', 'content': content, 'reason': 'captured'}, setup.MAX_EVIDENCE_BYTES))
    frames.append(setup._encode({'version': 1, 'status': 'evidence', 'phase': 'response',
                                 'content': {'text': 'extra'}, 'reason': 'captured'}))
    expected = LLMResult(Message('assistant', (TextContent('safe'),)))
    frames.append(setup._encode({'version': 1, 'status': 'completed', 'result': result_to_wire(expected)}))
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client._reader = asyncio.StreamReader(limit=setup.MAX_FRAME)
    for frame in frames: client._reader.feed_data(frame)
    client._reader.feed_eof()
    assert await client._exchange('complete', {}) == expected
    assert len(client.evidence[0]['content']['text']) == len(content['text'])
    assert client.evidence[1] == {'phase': 'response', 'content': {}, 'reason': 'truncated'}


@pytest.mark.asyncio
async def test_oversized_registered_credential_rejects_projection_before_child_creation(monkeypatch):
    from api_agent.telemetry.privacy import EmissionBoundary
    spawn = AsyncMock(side_effect=AssertionError('must not spawn on oversized credential'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    boundary = EmissionBoundary(secrets=('x' * (6 * 1024 * 1024),))
    with pytest.raises(ContractError, match='input limit'):
        await setup.SetupServer._prepare_isolated([], boundary)
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_projection_unsupported_platform_fails_before_starting_child(monkeypatch):
    from api_agent.telemetry.privacy import EmissionBoundary
    spawn = AsyncMock(side_effect=AssertionError('unsupported platform must not start a worker'))
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(setup.sys, 'platform', 'unsupported')
    with pytest.raises(ContractError, match='platform unavailable'):
        await setup.SetupServer._prepare_isolated([], EmissionBoundary())
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_projection_child_nonzero_exit_is_rejected_and_reaped(monkeypatch):
    import sys
    from api_agent.telemetry.privacy import EmissionBoundary
    original = asyncio.create_subprocess_exec
    processes = []
    async def failing(_executable, *_args, **kwargs):
        process = await original(sys.executable, '-c', 'import sys; sys.exit(17)', **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', failing)
    with pytest.raises(ContractError, match='projection unavailable'):
        await setup.SetupServer._prepare_isolated([], EmissionBoundary())
    assert len(processes) == 1 and processes[0].returncode == 17


def test_malformed_authority_release_cannot_release_existing_lease():
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    lease = SimpleNamespace(release=Mock())
    client._authority_lease = lease
    client._authority_ticket = 'a' * 64
    client._authority_nonce = 'b' * 64
    with pytest.raises(ContractError, match='Invalid authority release'):
        client._accept_authority_release({'version': 1, 'status': 'authority_release', 'ticket': 'wrong', 'proof': '0' * 64})
    lease.release.assert_not_called()
    assert client._authority_lease is lease
    client._release_authority()
    lease.release.assert_called_once()


@pytest.mark.asyncio
async def test_failed_publication_aborts_started_transfer_without_losing_result(monkeypatch):
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.llm import LLMResult, LLMRequest, CompletionRequest, Message, TextContent, Usage
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import bind_boundary, capture_inference
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-secret',))
    expected = LLMResult(Message('assistant', (TextContent('safe answer'),)), Usage(3, 2))
    closed = []
    class Owner:
        ensure_ready = AsyncMock()
        async def complete(self, request):
            bind_boundary(boundary)
            capture_inference('input', {'text': 'synthetic-secret'}, 'cause')
            return expected
        async def aclose(self): closed.append(True)
    registry = SetupRegistry('operation', 'selected', Owner)
    server = setup.SetupServer(registry, 'a' * 32)
    async def interrupted(peer, rows, boundary=None, *, raw=False):
        peer.writer.write(setup._encode(evidence_start(b'{"text":"unpublished"}')))
        peer.evidence_phase = ('request', None)
        raise ContractError('synthetic projection interruption')
    monkeypatch.setattr(server, '_publish_evidence', interrupted)
    descriptor = await server.start()
    client = setup.SetupClient(descriptor)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('ordinary'),)),)))
    try:
        result = await asyncio.to_thread(client.complete, request)
        assert result == expected and result.usage == Usage(3, 2)
        assert {'phase': 'request', 'content': {}, 'reason': 'truncated'} in client.evidence
        assert 'synthetic-secret' not in repr(client.evidence)
    finally:
        await asyncio.to_thread(client.close)
        await server.aclose()
        await registry.aclose()
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize('frames,match,omissions', [
    ([{'version': 1, 'status': 'evidence_abort', 'phase': 'request'}], 'Invalid inference evidence', []),
    ([{'version': 1, 'status': 'evidence_start', 'phase': None}], 'Invalid inference evidence', []),
    ([{'version': 1, 'status': 'evidence_start', 'phase': 'request'}], 'Invalid inference evidence', []),
    ([evidence_start(b'{}'), {'version': 1, 'status': 'evidence_abort', 'phase': 'request'}, evidence_start(b'{}')],
     'Conflicting inference evidence', [{'phase': 'request', 'content': {}, 'reason': 'truncated'}]),
])
async def test_orphan_or_conflicting_evidence_frames_never_publish_content(frames, match, omissions):
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client._reader = asyncio.StreamReader()
    client._reader.feed_data(b''.join(setup._encode(frame) for frame in frames))
    client._reader.feed_eof()
    with pytest.raises(ContractError, match=match):
        await client._exchange('complete', {})
    assert client.evidence == omissions


@pytest.mark.asyncio
async def test_authority_settlement_ignores_nonrelease_frames_and_waits_for_eof():
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    lease = SimpleNamespace(release=Mock())
    client._authority_lease = lease
    client._reader = asyncio.StreamReader()
    client._reader.feed_data(setup._encode({'version': 1, 'status': 'progress'}))
    task = asyncio.create_task(client._settle_authority())
    await asyncio.sleep(0)
    assert not task.done()
    lease.release.assert_not_called()
    client._reader.feed_eof()
    await task
    lease.release.assert_called_once()
    assert client._authority_lease is None


@pytest.mark.asyncio
@pytest.mark.parametrize('rows,allow,raw', [
    (None, False, False),
    ([None, {'phase': 'unexpected'}, {'phase': 'request', 'content': {'text': 'synthetic-secret'}}], False, False),
    ([None, {'phase': 'unexpected'}, {'phase': 'request', 'content': {'text': 'synthetic-secret'}}], True, False),
    ([None] * 129, True, False),
    ([], True, True),
    ('remove-rows', False, False),
])
async def test_malformed_producer_evidence_is_withheld_or_omitted_without_losing_result(rows, allow, raw):
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.llm import LLMResult, LLMRequest, CompletionRequest, Message, TextContent, Usage
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import _COLLECTOR, bind_boundary
    boundary = EmissionBoundary(ContentPolicy(True))
    expected = LLMResult(Message('assistant', (TextContent('safe answer'),)), Usage(2, 1))
    closed = []
    class Owner:
        ensure_ready = AsyncMock()
        async def complete(self, request):
            if allow: bind_boundary(boundary)
            if rows == 'remove-rows':
                del _COLLECTOR.get()['rows']
            else:
                _COLLECTOR.get()['rows'] = rows
            _COLLECTOR.get()['retain_raw'] = raw
            return expected
        async def aclose(self): closed.append(True)
    registry = SetupRegistry('operation', 'selected', Owner)
    server = setup.SetupServer(registry, 'a' * 32)
    descriptor = await server.start()
    client = setup.SetupClient(descriptor)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('ordinary'),)),)))
    try:
        assert await asyncio.to_thread(client.complete, request) == expected
        if rows == 'remove-rows':
            assert client.evidence == []
        else:
            assert {row['phase'] for row in client.evidence} == {'request', 'response'}
            assert all(row['content'] == {} and row['reason'] == ('truncated' if allow and not raw else 'withheld') for row in client.evidence)
        assert 'synthetic-secret' not in repr(client.evidence)
    finally:
        await asyncio.to_thread(client.close)
        await server.aclose()
        await registry.aclose()
    assert closed == [True]


@pytest.mark.asyncio
async def test_cancelled_projection_spawn_reaps_child_that_already_exited(monkeypatch):
    import sys
    from api_agent.telemetry.privacy import EmissionBoundary
    original = asyncio.create_subprocess_exec
    exited, release = asyncio.Event(), asyncio.Event()
    processes = []
    async def delayed(_executable, *_args, **kwargs):
        process = await original(sys.executable, '-c', 'pass', **kwargs)
        processes.append(process)
        assert await process.wait() == 0
        exited.set()
        await release.wait()
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed)
    task = asyncio.create_task(setup.SetupServer._prepare_isolated([], EmissionBoundary()))
    await exited.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert len(processes) == 1 and processes[0].returncode == 0


@pytest.mark.parametrize('mutation, reason', [(object(), 'unavailable'), ('x' * 1000, 'truncated')])
def test_invalid_mutation_after_initial_evidence_measurement_is_omitted(monkeypatch, mutation, reason):
    row = {'phase': 'request', 'content': {'text': 'original'}, 'reason': 'captured'}
    original = setup._content_frame
    def race(value):
        frame = original(value)
        value['content']['text'] = mutation  # Competing producer changes the shared evidence after validation.
        return frame
    monkeypatch.setattr(setup, '_content_frame', race)
    frames = [json.loads(frame) for frame in setup._evidence_frames(row, 100)]
    assert frames == [{'version': 1, 'status': 'evidence', 'phase': 'request', 'content': {}, 'reason': reason}]


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['producer-mutation', 'credential-registration'])
async def test_real_publication_owns_projected_rows_and_retries_new_credentials(monkeypatch, race):
    import threading
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    secret = 'synthetic-publication-secret'
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(secret,) if race == 'producer-mutation' else ())
    content = 'ordinary content' if race == 'producer-mutation' else 'prefix ' + secret
    rows = [{'phase': 'request', 'content': {'text': content}, 'reason': 'captured'}]
    prepared, resume = threading.Event(), threading.Event()
    original = setup.SetupServer._prepare_cancellable
    attempts = []
    def controlled(rows_arg, stopped, projection=None, raw=False):
        frames = original(rows_arg, stopped, projection, raw)
        attempts.append(frames)
        if len(attempts) == 1:
            prepared.set()
            if not resume.wait(timeout=5): raise AssertionError('projection test did not resume')
        return frames
    monkeypatch.setattr(setup.SetupServer, '_prepare_cancellable', staticmethod(controlled))
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    peer = SimpleNamespace(writer=writer, evidence_phase=None, evidence_sent=set())
    server = setup.SetupServer(SimpleNamespace(), 'a' * 32)
    task = asyncio.create_task(server._publish_evidence(peer, rows, boundary))
    try:
        assert await asyncio.to_thread(prepared.wait, 5)
        if race == 'producer-mutation':
            rows[0]['content']['text'] = secret
        else:
            boundary.register_secret(secret)
        assert writer.write.call_count == 0
        resume.set()
        await task
        published = [json.loads(call.args[0]) for call in writer.write.call_args_list]
        assert len(published) == 1 and published[0]['status'] == 'evidence'
        assert secret not in repr(published)
        assert published[0]['content']['text'] == ('ordinary content' if race == 'producer-mutation' else 'prefix [REDACTED]')
        assert len(attempts) == (1 if race == 'producer-mutation' else 2)
        assert peer.evidence_sent == {('request', None)}
        writer.drain.assert_awaited_once()
    finally:
        resume.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_prepared_frame_keeps_owned_content_if_producer_changes_before_accounting(monkeypatch):
    rows = [{'phase': 'request', 'content': {'text': 'original'}, 'reason': 'captured'},
            {'phase': 'response', 'content': {'text': 'other safe'}, 'reason': 'captured'}]
    original = setup._evidence_frames
    def mutate_after_frames(evidence, remaining):
        yield from original(evidence, remaining)
        if evidence['phase'] == 'request':
            evidence['content']['text'] = 'x' * (setup.MAX_EVIDENCE_BYTES + 1)
    monkeypatch.setattr(setup, '_evidence_frames', mutate_after_frames)
    frames = [json.loads(frame) for _, _, frame in setup.SetupServer._prepare_evidence(rows)]
    assert frames[0]['content'] == {'text': 'original'}
    assert frames[1]['content'] == {'text': 'other safe'}


@pytest.mark.asyncio
async def test_concurrent_evidence_state_change_cannot_commit_duplicate_phase():
    waiting = asyncio.Event()
    class Reader(asyncio.StreamReader):
        calls = 0
        async def readline(self):
            self.calls += 1
            if self.calls == 2: waiting.set()
            return await super().readline()
    client = setup.SetupClient({'version': 1, 'host': '127.0.0.1', 'port': 1, 'capability': 'a' * 32})
    client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client._reader = Reader()
    body = b'{"text":"owned response"}'
    client._reader.feed_data(setup._encode(evidence_start(body)))
    task = asyncio.create_task(client._exchange('complete', {}))
    await waiting.wait()
    prior = {'phase': 'request', 'content': {}, 'reason': 'withheld'}
    client.evidence.append(prior)
    client._reader.feed_data(setup._encode({'version': 1, 'status': 'evidence_part', 'phase': 'request',
        'offset': 0, 'data': base64.b64encode(body).decode()}) + setup._encode(
        {'version': 1, 'status': 'evidence_end', 'phase': 'request'}))
    client._reader.feed_eof()
    with pytest.raises(ContractError, match='Invalid inference evidence'):
        await task
    assert client.evidence == [prior]
    assert 'owned response' not in repr(client.evidence)


def test_maximum_evidence_fragment_fits_wire_frame_at_largest_offset():
    content = {'text': 'x' * (setup.MAX_EVIDENCE_BYTES - len(b'{"text":""}'))}
    wire = list(setup._evidence_frames({'phase': 'response', 'content': content, 'reason': 'captured'}, setup.MAX_EVIDENCE_BYTES))
    parts = [(frame, json.loads(frame)) for frame in wire if json.loads(frame)['status'] == 'evidence_part']
    full = [(frame, item) for frame, item in parts if len(base64.b64decode(item['data'])) == setup._EVIDENCE_FRAGMENT]
    largest_offset, item = max(full, key=lambda pair: pair[1]['offset'])
    assert len(item['data']) == 65536
    assert item['offset'] > 3 * 1024 * 1024
    assert max(len(frame) for frame, _ in parts) <= setup.MAX_CONTENT_FRAME
    assert len(largest_offset) < setup.MAX_CONTENT_FRAME
    receiver = setup._EvidenceReceiver()
    value = None
    for frame in wire:
        value = receiver.accept(json.loads(frame))
    assert value['phase'] == 'response' and value['content'] == content


def test_fragment_limit_drift_fails_closed_before_releasing_oversized_part(monkeypatch):
    """Model a future frame-limit change after a valid part has been encoded."""
    original_encode = setup._encode
    original_cap = setup.MAX_CONTENT_FRAME
    inspected = []
    def encode_with_limit_drift(value):
        frame = original_encode(value)
        if value.get('status') == 'evidence_part':
            assert len(frame) < original_cap
            inspected.append(frame)
            monkeypatch.setattr(setup, 'MAX_CONTENT_FRAME', len(frame) - 1)
        return frame
    monkeypatch.setattr(setup, '_encode', encode_with_limit_drift)
    frames = setup._evidence_frames({'phase': 'request', 'content': {'text': 'x' * 150000},
                                     'reason': 'captured'}, setup.MAX_EVIDENCE_BYTES)
    assert json.loads(next(frames))['status'] == 'evidence_start'
    with pytest.raises(ContractError, match='Evidence frame exceeds limit'):
        next(frames)
    assert len(inspected) == 1
