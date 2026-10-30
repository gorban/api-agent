"""Wire-level failures do not publish unowned or malformed evidence."""
import asyncio
import json
from uuid import uuid4

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.setup_transport import SetupClient, _encode
from test_setup_transport import CAPABILITY


@pytest.mark.parametrize('frame, expected', [
    ({'version': 1, 'status': 'evidence_start', 'phase': 'request', 'reason': 'captured',
      'length': 4, 'sha256': '0' * 64, 'transport_attempt_id': str(uuid4())}, 'no transport attempt owner'),
    ({'version': 1, 'status': 'result_ready'}, 'Invalid early completion response'),
    ({'version': 1, 'status': 'error_ready', 'error': 'invalid'}, 'Invalid early failure response'),
    ({'version': 1, 'status': 'evidence', 'phase': 'request', 'content': {},
      'transport_attempt_id': str(uuid4())}, 'no transport attempt owner'),
])
def test_real_peer_malformed_evidence_and_early_frames_fail_closed(frame, expected):
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    async def scenario():
        finished = asyncio.Event()
        async def peer(reader, writer):
            try:
                registration = json.loads(await reader.readline())
                binding = dict(version=1, operation_id='synthetic-operation', selected_identity='selected',
                               worker_id=registration['worker_id'], attempt=registration['attempt'])
                writer.write(_encode(dict(version=1, status='registered', binding=binding)))
                await writer.drain()
                await reader.readline()
                writer.write(_encode(frame))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()
        server = await asyncio.start_server(peer, '127.0.0.1', 0)
        address = dict(version=1, host='127.0.0.1', port=server.sockets[0].getsockname()[1], capability=CAPABILITY)
        callbacks = []
        def consume():
            client = SetupClient(address, timeout=5)
            try:
                with pytest.raises(ContractError) as caught:
                    client.complete(request, result_ready=callbacks.append, error_ready=callbacks.append)
                assert expected in str(caught.value.__cause__)
                assert client.evidence == []
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
            await finished.wait()
            assert callbacks == []
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_background_quote_failure_preserves_result_without_private_diagnostic():
    from api_agent.providers.llm import LLMResult, Usage
    from api_agent.providers.setup_transport import SetupServer
    from test_setup_transport import Registry
    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    class Owner(Registry):
        async def complete(self, binding, request):
            return expected
        def quote_cost(self, *args):
            raise RuntimeError('synthetic-secret-quote-failure')
    async def scenario():
        server = SetupServer(Owner(), CAPABILITY, timeout=10)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=10)
            ready = []
            try:
                assert client.complete(request, result_ready=ready.append) == expected
                assert ready == [expected]
                assert client.cost_quote is None
                assert 'synthetic-secret-quote-failure' not in repr(client.evidence)
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
        finally:
            await server.aclose()
    asyncio.run(scenario())


def test_malformed_producer_evidence_is_omitted_without_losing_result():
    from api_agent.providers.llm import LLMResult, Usage
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import _COLLECTOR, bind_boundary
    from test_setup_transport import Registry
    expected = LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    class Owner(Registry):
        async def complete(self, binding, request):
            bind_boundary(EmissionBoundary(ContentPolicy(True)))
            _COLLECTOR.get()['rows'].append({'phase': 'request', 'content': 'untrusted content'})
            return expected
    async def scenario():
        server = SetupServer(Owner(), CAPABILITY, timeout=10)
        address = await server.start()
        def consume():
            client = SetupClient(address, timeout=10)
            try:
                assert client.complete(request) == expected
                assert client.evidence == [dict(phase=phase, content={}, reason='truncated')
                                           for phase in ('request', 'response')]
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
        finally:
            await server.aclose()
    asyncio.run(scenario())


def test_real_peer_interleaving_evidence_marks_partial_content_truncated():
    import hashlib
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    body = b'{"text":"secret"}'
    async def scenario():
        finished = asyncio.Event()
        async def peer(reader, writer):
            try:
                registration = json.loads(await reader.readline())
                binding = dict(version=1, operation_id='synthetic-operation', selected_identity='selected',
                               worker_id=registration['worker_id'], attempt=registration['attempt'])
                writer.write(_encode(dict(version=1, status='registered', binding=binding)))
                await writer.drain()
                await reader.readline()
                start = dict(version=1, status='evidence_start', phase='request', reason='captured',
                             length=len(body), sha256=hashlib.sha256(body).hexdigest())
                writer.write(_encode(start) + _encode(dict(version=1, status='cost', quote={})))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()
        server = await asyncio.start_server(peer, '127.0.0.1', 0)
        address = dict(version=1, host='127.0.0.1', port=server.sockets[0].getsockname()[1], capability=CAPABILITY)
        def consume():
            client = SetupClient(address, timeout=5)
            try:
                with pytest.raises(ContractError) as caught:
                    client.complete(request)
                assert 'Interleaved inference evidence response' in str(caught.value.__cause__)
                assert client.evidence == [dict(phase='request', content={}, reason='truncated')]
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
            await finished.wait()
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_aborted_evidence_cannot_be_replaced_by_later_frame():
    import hashlib
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    async def scenario():
        finished = asyncio.Event()
        async def peer(reader, writer):
            try:
                registration = json.loads(await reader.readline())
                binding = dict(version=1, operation_id='synthetic-operation', selected_identity='selected',
                               worker_id=registration['worker_id'], attempt=registration['attempt'])
                writer.write(_encode(dict(version=1, status='registered', binding=binding)))
                await writer.drain()
                await reader.readline()
                body = b'{"safe":true}'
                start = dict(version=1, status='evidence_start', phase='request', reason='captured',
                             length=len(body), sha256=hashlib.sha256(body).hexdigest())
                abort = dict(version=1, status='evidence_abort', phase='request')
                replacement = dict(version=1, status='evidence', phase='request', content={'safe': True})
                writer.write(_encode(start) + _encode(abort) + _encode(replacement))
                await writer.drain()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                finally:
                    finished.set()
        server = await asyncio.start_server(peer, '127.0.0.1', 0)
        address = dict(version=1, host='127.0.0.1', port=server.sockets[0].getsockname()[1], capability=CAPABILITY)
        def consume():
            client = SetupClient(address, timeout=5)
            try:
                with pytest.raises(ContractError) as caught:
                    client.complete(request)
                assert 'Conflicting inference evidence response' in str(caught.value.__cause__)
                assert client.evidence == [dict(phase='request', content={}, reason='truncated')]
            finally:
                client.close()
        try:
            await asyncio.to_thread(consume)
            await asyncio.wait_for(finished.wait(), 5)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_forged_authority_request_never_releases_caller_credential():
    from api_agent.testing.authority_scope import TestAuthorityScope
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    async def scenario():
        finished = asyncio.Event()
        frames = []
        async def peer(reader, writer):
            try:
                registration = json.loads(await reader.readline())
                binding = dict(version=1, operation_id='synthetic-operation', selected_identity='selected',
                               worker_id=registration['worker_id'], attempt=registration['attempt'])
                writer.write(_encode(dict(version=1, status='registered', binding=binding)))
                await writer.drain()
                frames.append(json.loads(await reader.readline()))
                writer.write(_encode(dict(version=1, status='authority_request', ticket='a' * 64,
                                          sequence=1, proof='0' * 64)))
                await writer.drain()
                frames.append(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()
        server = await asyncio.start_server(peer, '127.0.0.1', 0)
        address = dict(version=1, host='127.0.0.1', port=server.sockets[0].getsockname()[1],
                       capability=CAPABILITY, authority_path='/tmp/synthetic-authority-test')
        def consume():
            scope = TestAuthorityScope(('ui',))
            scope.join('ui', EmissionBoundary(ContentPolicy(True), secrets=('synthetic-caller-secret',)))
            client = SetupClient(address, timeout=5)
            try:
                with pytest.raises(ContractError) as caught:
                    client.complete(request, authority_scope=scope)
                assert 'Authority server authentication failed' in str(caught.value.__cause__)
                assert client.evidence == []
            finally:
                client.close()
                scope.close()
        try:
            await asyncio.to_thread(consume)
            await finished.wait()
            assert frames[1] == b''
            assert 'synthetic-caller-secret' not in repr(frames)
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())
