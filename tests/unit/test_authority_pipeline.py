import asyncio
import json
import subprocess
import sys
import hashlib
import hmac
import pytest
from unittest.mock import AsyncMock

from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupServer
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.inference_capture import bind_boundary, capture_inference


def test_real_process_projects_overlapping_authorities_once_and_isolates_callers():
    source = EmissionBoundary(ContentPolicy(True))
    coordinator = EmissionBoundary(ContentPolicy(True))
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            bind_boundary(source)
            capture_inference('input', {'text': 'abcdefg worker-one worker-two'}, 'cause')
            source.register_secret('abc')
            coordinator.register_secret('cde')
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    child = '''
import json, sys
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.setup_transport import SetupClient
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.authority_scope import TestAuthorityScope
descriptor = json.loads(sys.stdin.readline())
scope = TestAuthorityScope(('ui',))
scope.join('ui', EmissionBoundary(ContentPolicy(True), secrets=('efg', sys.argv[1])))
client = SetupClient(descriptor, timeout=5)
request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
try:
    client.complete(request, authority_scope=scope)
    print(json.dumps(client.evidence))
finally:
    client.close()
    scope.close()
'''
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=coordinator)
        descriptor = await server.start()
        def run(secret):
            completed = subprocess.run([sys.executable, '-c', child, secret], input=json.dumps(descriptor) + '\n',
                                       text=True, capture_output=True, timeout=10, check=True)
            assert not completed.stderr
            return json.loads(completed.stdout)
        try:
            first = await asyncio.to_thread(run, 'worker-one')
            second = await asyncio.to_thread(run, 'worker-two')
            assert first[0]['content']['text'] == '[REDACTED] [REDACTED] worker-two'
            assert second[0]['content']['text'] == '[REDACTED] worker-one [REDACTED]'
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_lost_release_retains_owner_until_authenticated_barrier():
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.setup_transport import SetupClient, _encode, _request_digest

    class Lease:
        released = False
        def release(self):
            self.released = True

    class Writer:
        closed = False
        def close(self):
            self.closed = True
        async def wait_closed(self):
            pass

    capability = 'synthetic-authority-capability-0000000'
    client = SetupClient(dict(version=1, host='127.0.0.1', port=1, capability=capability,
                              authority_path='/tmp/authority-test'), timeout=1, cleanup_timeout=.03)
    client._loop = asyncio.new_event_loop()
    client._reader = asyncio.StreamReader(loop=client._loop)
    client._writer = Writer()
    client._failed = True
    client._binding = dict(version=1, operation_id='op', selected_identity='selected', worker_id='worker', attempt=1)
    client._complete_count = 1
    client._authority_ticket = 'a' * 64
    client._authority_nonce = 'b' * 64
    lease = client._authority_lease = Lease()
    with pytest.raises(CleanupPendingError) as pending:
        client.close()
    assert client in pending.value.owners
    assert not lease.released and not client._writer.closed
    client._reader.feed_data(_encode(dict(version=1, status='authority_release', ticket=client._authority_ticket, proof='wrong')))
    with pytest.raises(CleanupPendingError):
        client.close()
    assert not lease.released and not client._writer.closed
    payload = [_request_digest(client._binding), client._authority_ticket, 1, client._authority_nonce]
    proof = hmac.new(capability.encode(), ('authority-release:' + _request_digest(payload)).encode(), hashlib.sha256).hexdigest()
    client._reader.feed_data(_encode(dict(version=1, status='authority_release', ticket=client._authority_ticket, proof=proof)))
    client.close()
    assert lease.released and client._writer.closed


def test_missing_caller_authority_withholds_raw_evidence():
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.setup_transport import SetupClient
    source = EmissionBoundary(ContentPolicy(True))
    coordinator = EmissionBoundary(ContentPolicy(True))
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            bind_boundary(source)
            capture_inference('input', {'text': 'unregistered-secret'}, 'cause')
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=coordinator)
        descriptor = await server.start()
        def run():
            client = SetupClient(descriptor, timeout=5)
            request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
            try:
                client.complete(request)
                assert client.evidence[0]['content'] == {}
                assert client.evidence[0]['reason'] == 'withheld'
                assert 'unregistered-secret' not in repr(client.evidence)
            finally:
                client.close()
        try:
            await asyncio.to_thread(run)
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_real_plain_process_explicitly_admits_coordinator_without_empty_authority():
    source = EmissionBoundary(ContentPolicy(True), secrets=('abc',))
    coordinator = EmissionBoundary(ContentPolicy(True), secrets=('cde',))
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            bind_boundary(source)
            capture_inference('input', {'text': 'abcde'}, 'cause')
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    child = '''
import json, sys
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.setup_transport import SetupClient
from api_agent.providers.contracts import ContractError
from api_agent.testing.authority_scope import TestAuthorityScope
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
rejected = TestAuthorityScope(('coordinator',))
try:
    rejected.join('ui', EmissionBoundary(ContentPolicy(True)))
except ContractError:
    pass
else:
    raise AssertionError('undeclared credential producer was admitted')
try:
    rejected.freeze()
except ContractError:
    pass
else:
    raise AssertionError('invalidated authority was admitted')
scope = TestAuthorityScope(('coordinator',))
client = SetupClient(json.loads(sys.stdin.readline()), timeout=10)
try:
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    client.complete(request, authority_scope=scope)
    print(json.dumps(client.evidence))
finally:
    client.close()
    scope.close()
'''
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=10, authority=coordinator)
        descriptor = await server.start()
        try:
            def run():
                return subprocess.run([sys.executable, '-c', child], input=json.dumps(descriptor) + '\n',
                                      text=True, capture_output=True, timeout=15, check=True)
            completed = await asyncio.to_thread(run)
            assert not completed.stderr
            rows = json.loads(completed.stdout)
            assert rows[0]['content']['text'] == '[REDACTED]'
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('trace_enabled', [False, True])
@pytest.mark.parametrize('overflow', [False, True])
def test_full_four_mib_capture_from_provider_through_real_worker_recording(tmp_path, trace_enabled, overflow):
    from api_agent.testing.inference_content import initialize_evidence, read_evidence
    from api_agent.providers.setup_transport import MAX_EVIDENCE_BYTES
    import uuid
    source = EmissionBoundary(ContentPolicy(True), secrets=('abcde',))
    coordinator = EmissionBoundary(ContentPolicy(True), secrets=('defghi',))
    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            bind_boundary(source)
            capture_inference('input', {'text': 'z' * (MAX_EVIDENCE_BYTES - len('{"text":""}') - 10 + int(overflow)) + 'abcdefghij'}, 'cause')
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
    child = '''
import json, sys, os
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.setup_transport import SetupClient
from api_agent.testing.authority_scope import TestAuthorityScope
from api_agent.testing.inference_content import publish_content
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
scope = TestAuthorityScope(('ui',))
scope.join('ui', EmissionBoundary(ContentPolicy(True), secrets=('hij',)))
client = SetupClient(json.loads(sys.stdin.readline()), timeout=40)
try:
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    client.complete(request, authority_scope=scope)
    for row in client.evidence:
        if row.get('reason') != sys.argv[3]:
            raise RuntimeError('transport evidence: ' + str(row.get('reason')))
    os.environ['API_AGENT_INTEGRATION_EVENTS'] = sys.argv[1] + '.events'
    os.environ['API_AGENT_INFERENCE_EVIDENCE_DB'] = sys.argv[1]
    os.environ['PYTEST_CURRENT_TEST'] = 'synthetic-node (call)'
    publish_content(client.evidence, sys.argv[2])
finally:
    client.close()
    scope.close()
'''
    async def scenario():
        sidecar = str(tmp_path / 'evidence.db')
        initialize_evidence(sidecar, ['synthetic-node'])
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=40, authority=coordinator)
        descriptor = await server.start()
        try:
            def run():
                return subprocess.run([sys.executable, '-c', child, sidecar, str(uuid.uuid4()),
                                       'truncated' if overflow else 'captured'],
                                      input=json.dumps(descriptor) + '\n', text=True, capture_output=True,
                                      timeout=30)
            completed = await asyncio.to_thread(run)
            assert completed.returncode == 0, completed.stderr
            records = read_evidence(sidecar, 0)
            assert len(records) == 1
            row = records[0][2]
            assert row['reason'] == ('truncated' if overflow else 'captured')
            if overflow:
                assert row['content'] == {}
            else:
                assert len(row['content']['text']) == MAX_EVIDENCE_BYTES - len('{"text":""}')
                assert row['content']['text'].endswith('[REDACTED]')
                assert 'abcdefghij' not in row['content']['text']
            return row
        finally:
            await server.aclose()
            await registry.aclose()
    row = asyncio.run(scenario())
    from api_agent.testing.contracts import RunnerEvent, TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.payloads import PayloadResolver, CHUNK_SIZE
    from api_agent.testing.recorded import run_recorded_integration
    from test_recorded_integration import fixture, Storage
    context, identity, config = fixture(tmp_path)
    config.update(LOG_CONTENT=True, TRACELOOP_TRACE_CONTENT=trace_enabled)
    def runner(_context, _tests, **hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('inference_content', identity=identity, text=json.dumps(row)))
        outcome = TestResult(identity, 'passed', 1)
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result
    result = run_recorded_integration(context, (identity,), config, accepted=True, storage=Storage(), runner=runner)
    report = json.loads(next(result.directory.glob('*-result.json')).read_text())
    resolver = PayloadResolver.local(result.directory, result.manifest)
    index = resolver.index(next(item['source'] for item in report['attachments'] if item['name'] == 'Payload index'))
    entry, = [item for item in index['entries'] if item['occurrence'].get('kind') == 'llm']
    assert all('abcde' not in path.read_text(errors='replace')
               and 'defghi' not in path.read_text(errors='replace')
               and 'hij' not in path.read_text(errors='replace')
               for path in result.directory.iterdir() if path.is_file())
    if overflow:
        assert entry['kind'] == 'omitted' and entry['reason'] == 'truncated'
        return
    assert entry['kind'] == 'chunked' and entry['length'] == MAX_EVIDENCE_BYTES
    ref = {key: entry[key] for key in ('kind', 'source', 'sha256', 'length')}
    assert resolver(ref) == row['content']
    descriptor = json.loads((result.directory / ref['source']).read_text())
    assert all(0 < chunk['length'] <= CHUNK_SIZE for chunk in descriptor['chunks'])
    assert sum(chunk['length'] for chunk in descriptor['chunks']) == MAX_EVIDENCE_BYTES


@pytest.mark.parametrize('failed', [False, True])
def test_background_result_precedes_projection_for_overlapping_real_processes(monkeypatch, failed):
    source = EmissionBoundary(ContentPolicy(True), secrets=('provider-secret',))
    coordinator = EmissionBoundary(ContentPolicy(True))

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            bind_boundary(source)
            capture_inference('input', {'text': 'provider-secret visible'}, 'cause')
            if failed:
                from api_agent.providers.contracts import ProviderAccessError
                raise ProviderAccessError('unknown', 'Synthetic failure')
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    child = '''
import json, sys
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.setup_transport import SetupClient
from api_agent.testing.authority_scope import TestAuthorityScope
scope = TestAuthorityScope(('coordinator',))
client = SetupClient(json.loads(sys.stdin.readline()), timeout=15)
request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
try:
    try:
        client.complete(request, authority_scope=scope,
            result_ready=lambda result: print('READY', flush=True),
            error_ready=lambda error: print('READY', flush=True))
    except Exception:
        pass
    print(json.dumps(client.evidence), flush=True)
finally:
    client.close()
    scope.close()
'''

    async def scenario():
        gate = asyncio.Event()
        original = SetupServer._prepare_isolated

        async def delayed(rows, projection):
            await gate.wait()
            return await original(rows, projection)

        monkeypatch.setattr(SetupServer, '_prepare_isolated', staticmethod(delayed))
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=15, authority=coordinator)
        descriptor = await server.start()
        children = []
        try:
            for _ in range(2):
                process = await asyncio.create_subprocess_exec(sys.executable, '-c', child,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                process.stdin.write((json.dumps(descriptor) + '\n').encode())
                await process.stdin.drain()
                children.append(process)
            for process in children:
                assert await asyncio.wait_for(process.stdout.readline(), 5) == b'READY\n'
                assert process.returncode is None
            gate.set()
            for process in children:
                rows = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
                assert rows[0]['content']['text'] == '[REDACTED] visible'
                assert await asyncio.wait_for(process.wait(), 5) == 0
                assert not await process.stderr.read()
        finally:
            gate.set()
            for process in children:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failed', [False, True])
def test_remote_provider_returns_before_owned_evidence_and_waits_on_close(monkeypatch, failed):
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.testing.authority_scope import authority_scope
    from api_agent.testing.provider_setup import RemoteIntegrationLLM

    source = EmissionBoundary(ContentPolicy(True))
    coordinator = EmissionBoundary(ContentPolicy(True))

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            bind_boundary(source)
            capture_inference('input', {'text': 'payload'}, 'cause')
            if failed:
                raise ProviderAccessError('unknown', 'Synthetic failure')
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    async def scenario():
        gate = asyncio.Event()
        original = SetupServer._prepare_isolated

        async def delayed(rows, projection):
            await gate.wait()
            return await original(rows, projection)

        monkeypatch.setattr(SetupServer, '_prepare_isolated', staticmethod(delayed))
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=15, authority=coordinator)
        provider = RemoteIntegrationLLM(await server.start())
        request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        try:
            with authority_scope(('coordinator',)):
                calls = [asyncio.create_task(asyncio.to_thread(provider.complete, request)) for _ in range(8)]
                outcomes = await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), 5)
                if failed:
                    assert all(isinstance(outcome, ProviderAccessError) for outcome in outcomes)
                else:
                    assert all(isinstance(outcome, LLMResult) for outcome in outcomes)
                closing = asyncio.create_task(asyncio.to_thread(provider.close))
                await asyncio.sleep(.05)
                assert not closing.done()
                gate.set()
                await asyncio.wait_for(closing, 20)
        finally:
            gate.set()
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_sequential_background_calls_bound_registry_replay_history(monkeypatch):
    from api_agent.providers import setup_registry
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.testing.authority_scope import authority_scope
    from api_agent.testing.provider_setup import RemoteIntegrationLLM

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    async def scenario():
        monkeypatch.setattr(setup_registry, '_MAX_REGISTRATIONS', 8)
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=15,
                             authority=EmissionBoundary(ContentPolicy(True)))
        provider = RemoteIntegrationLLM(await server.start())
        request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        try:
            with authority_scope(('coordinator',)):
                try:
                    for _ in range(32):
                        result = await asyncio.to_thread(provider.complete, request)
                        assert result.payload.content[0].text == 'done'
                    assert len(registry._history) == 8
                    original = next(iter(registry._history.values()))['binding']
                    with pytest.raises(ContractError, match='Duplicate or stale'):
                        registry.register(original['worker_id'], original['attempt'])
                    with pytest.raises(ContractError, match='registration limit'):
                        registry.register('another-worker')
                finally:
                    await asyncio.to_thread(provider.close)
        finally:
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_failed_background_cleanup_keeps_slot_until_retry(monkeypatch):
    import threading
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing.authority_scope import authority_scope
    from api_agent.testing.provider_setup import RemoteIntegrationLLM

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=15,
                             authority=EmissionBoundary(ContentPolicy(True)))
        provider = RemoteIntegrationLLM(await server.start())
        request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        original = SetupClient.close
        failed = threading.Event()

        def fail_once(client):
            if client is not provider.client and not failed.is_set():
                failed.set()
                raise CleanupPendingError((client,), None)
            return original(client)

        monkeypatch.setattr(SetupClient, 'close', fail_once)
        from api_agent.testing import inference_content
        failures = []
        monkeypatch.setattr(inference_content, 'notify_failure', lambda *args: failures.append(args))
        try:
            with authority_scope(('coordinator',)):
                try:
                    await asyncio.to_thread(provider.complete, request)
                    assert await asyncio.to_thread(failed.wait, 5)
                    # Wait for the owner to retain the failed cleanup, not just
                    # the injected failure to be raised.
                    for _ in range(1000):
                        with provider._evidence_stranded_lock:
                            if provider._evidence_stranded:
                                break
                        await asyncio.sleep(.001)
                    assert provider._evidence_slots._available.qsize() == 7
                    await asyncio.to_thread(provider.close)
                    assert provider._evidence_slots._available.qsize() == 8
                    assert not provider._evidence_stranded
                    assert failures
                finally:
                    await asyncio.to_thread(provider.close)
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_unconfirmed_revoke_retires_background_slot(monkeypatch):
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing import inference_content
    from api_agent.testing.authority_scope import authority_scope
    from api_agent.testing.provider_setup import RemoteIntegrationLLM

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=15,
                             authority=EmissionBoundary(ContentPolicy(True)))
        provider = RemoteIntegrationLLM(await server.start())
        request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        original = SetupClient.close
        failures = []
        monkeypatch.setattr(inference_content, 'notify_failure', lambda *args: failures.append(args))

        def lose_ack(client):
            original(client)
            if client is not provider.client:
                client._revoke_confirmed = False
                client._failed = True

        monkeypatch.setattr(SetupClient, 'close', lose_ack)
        try:
            with authority_scope(('coordinator',)):
                try:
                    await asyncio.to_thread(provider.complete, request)
                    await asyncio.to_thread(provider.close)
                    assert provider._evidence_slots._available.qsize() == 7
                    assert failures
                finally:
                    await asyncio.to_thread(provider.close)
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_real_caller_union_is_bound_before_provider_dispatch():
    from api_agent.providers.call_authority import current_call_authority
    source = EmissionBoundary(ContentPolicy(True), secrets=('abc',))
    coordinator = EmissionBoundary(ContentPolicy(True), secrets=('cde',))
    observed = []

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            # Acquired worker credentials must be admitted before the source
            # and coordinator leases are taken.
            await source.aregister_secret('worker-secret')
            values, lease = current_call_authority().acquire(source)
            try:
                observed.append(set(values))
                bind_boundary(source)
                capture_inference('input', {'text': 'abcdefg worker-secret'}, 'cause')
                return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
            finally:
                lease.release()

    child = '''
import json, sys
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.setup_transport import SetupClient
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.authority_scope import TestAuthorityScope
descriptor = json.loads(sys.stdin.readline())
scope = TestAuthorityScope(('ui',))
scope.join('ui', EmissionBoundary(ContentPolicy(True), secrets=('efg',)))
client = SetupClient(descriptor, timeout=5)
try:
    client.complete(LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),))), authority_scope=scope)
    print(json.dumps(client.evidence))
finally:
    client.close()
    scope.close()
'''
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=coordinator)
        descriptor = await server.start()
        try:
            completed = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', child],
                input=json.dumps(descriptor) + '\n', text=True, capture_output=True, timeout=10, check=True)
            assert not completed.stderr
            assert observed == [{'abc', 'cde', 'efg', 'worker-secret'}]
            assert json.loads(completed.stdout)[0]['content']['text'] == '[REDACTED] [REDACTED]'
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_incomplete_caller_scope_rejects_before_provider_dispatch():
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing.authority_scope import TestAuthorityScope
    coordinator = EmissionBoundary(ContentPolicy(True))
    dispatched = []

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            dispatched.append(True)
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    def call(descriptor):
        scope = TestAuthorityScope(('ui',))
        client = SetupClient(descriptor, timeout=5)
        try:
            with pytest.raises(ContractError):
                client.complete(LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),))),
                                authority_scope=scope)
        finally:
            client.close()
            scope.close()

    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=coordinator)
        descriptor = await server.start()
        try:
            await asyncio.to_thread(call, descriptor)
            assert dispatched == []
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('authority_channel', [False, True])
def test_real_caller_without_scope_cannot_reach_guarded_provider(authority_channel):
    from api_agent.providers.call_authority import require_remote_admission
    coordinator = EmissionBoundary(ContentPolicy(True)) if authority_channel else None
    source = EmissionBoundary(ContentPolicy(True))
    dispatched = []

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            require_remote_admission(source)
            dispatched.append(True)
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    child = '''
import json, sys
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.providers.setup_transport import SetupClient
descriptor = json.loads(sys.stdin.readline())
client = SetupClient(descriptor, timeout=5)
try:
    try:
        client.complete(LLMRequest('model', CompletionRequest((Message('user', (TextContent('caller-only-secret'),)),))))
    except Exception as error:
        print(type(error).__name__)
    else:
        raise AssertionError('unguarded dispatch')
finally:
    client.close()
'''
    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=coordinator)
        descriptor = await server.start()
        try:
            completed = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', child],
                input=json.dumps(descriptor) + '\n', text=True, capture_output=True, timeout=10, check=True)
            assert completed.stdout.strip() == 'ContractError'
            assert not completed.stderr
            assert dispatched == []
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_shared_source_credential_admission_waits_for_caller_lease():
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.call_authority import bind_call_authority
    from api_agent.testing.authority_scope import TestAuthorityScope

    async def scenario():
        source = EmissionBoundary(ContentPolicy(True), secrets=('existing-secret',))
        scope = TestAuthorityScope(('ui',))
        scope.join('ui', source)
        caller_lease = scope.freeze()
        provider = object.__new__(BedrockProvider)
        provider.emissions = source
        try:
            with bind_call_authority(source, caller_lease.secrets):
                pending = asyncio.create_task(provider._join_credentials(['new-worker-secret']))
                await asyncio.sleep(0)
                assert not pending.done()
                caller_lease.release()
                values, worker_lease = await asyncio.wait_for(pending, 2)
                try:
                    assert set(values) == {'existing-secret', 'new-worker-secret'}
                finally:
                    worker_lease.release()
        finally:
            caller_lease.release()
            scope.close()
    asyncio.run(scenario())


def test_shared_authority_retry_upgrades_serialize_across_servers():
    from api_agent.providers.call_authority import current_call_authority
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing.authority_scope import TestAuthorityScope
    source = EmissionBoundary(ContentPolicy(True), secrets=('caller-secret',))
    active = 0
    maximum = 0
    observed = []

    class Owner:
        emissions = source
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            nonlocal active, maximum
            authority = current_call_authority()
            active += 1
            maximum = max(maximum, active)
            try:
                for value in ('worker-first', 'worker-refresh'):
                    await authority.register(source, [value])
                    values, lease = authority.acquire(source)
                    try:
                        observed.append(set(values))
                        await asyncio.sleep(.01)
                    finally:
                        lease.release()
                bind_boundary(source)
                capture_inference('input', {'text': 'caller-secret worker-first worker-refresh'}, 'cause')
                return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
            finally:
                active -= 1

    def call(descriptor):
        scope = TestAuthorityScope(('ui',))
        scope.join('ui', source)
        client = SetupClient(descriptor, timeout=5)
        try:
            client.complete(LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),))),
                            authority_scope=scope)
            return client.evidence
        finally:
            client.close()
            scope.close()

    async def scenario():
        registries = [SetupRegistry('op-' + str(index), 'selected', Owner) for index in range(2)]
        servers = [SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=source)
                   for registry in registries]
        descriptors = [await server.start() for server in servers]
        try:
            rows = await asyncio.gather(*(asyncio.to_thread(call, descriptor) for descriptor in descriptors))
            assert maximum == 1
            assert len(observed) == 4
            assert all('caller-secret' in values for values in observed)
            assert 'worker-refresh' in observed[1]
            assert all(row[0]['content']['text'] == '[REDACTED] [REDACTED] [REDACTED]' for row in rows)
        finally:
            for server in servers:
                await server.aclose()
            for registry in registries:
                await registry.aclose()
    asyncio.run(scenario())


def test_new_scope_owner_during_gate_admission_releases_caller_lease():
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing.authority_scope import TestAuthorityScope
    first = EmissionBoundary(ContentPolicy(True))
    second = EmissionBoundary(ContentPolicy(True))
    scope = TestAuthorityScope(('ui',))
    scope.join('ui', first)
    dispatched = []

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            dispatched.append(True)
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    def call(descriptor):
        client = SetupClient(descriptor, timeout=5)
        try:
            with pytest.raises(ContractError):
                client.complete(LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),))),
                                authority_scope=scope)
        finally:
            client.close()

    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=first)
        original = server._caller_authority
        async def admit(peer, frame):
            scope.join('ui', second)
            return await original(peer, frame)
        server._caller_authority = admit
        descriptor = await server.start()
        try:
            await asyncio.to_thread(call, descriptor)
            assert not dispatched
            assert scope._leases == 0
        finally:
            await server.aclose()
            await registry.aclose()
            scope.close()
    asyncio.run(scenario())


def test_shared_authority_upgrades_across_server_event_loops():
    import threading
    from concurrent.futures import Future, ThreadPoolExecutor
    from api_agent.providers.call_authority import current_call_authority
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing.authority_scope import TestAuthorityScope

    source = EmissionBoundary(ContentPolicy(True), secrets=('caller-secret',))
    lock = threading.Lock()
    stop = threading.Event()
    loops = set()
    active = []
    observed = []
    barrier = threading.Barrier(2)

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()

        async def complete(self, request):
            authority = current_call_authority()
            with lock:
                loops.add(id(asyncio.get_running_loop()))
                active.append(1)
                observed.append(len(active))
            try:
                for value in ('first-worker-secret', 'refresh-worker-secret'):
                    await authority.register(source, [value])
                    values, lease = authority.acquire(source)
                    try:
                        assert value in values
                        await asyncio.sleep(.02)
                    finally:
                        lease.release()
                bind_boundary(source)
                capture_inference('input', {'text': 'caller-secret first-worker-secret refresh-worker-secret'}, 'cause')
                return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))
            finally:
                with lock:
                    active.pop()

    def serve(index, ready):
        async def run():
            registry = SetupRegistry('op-' + str(index), 'selected', Owner)
            server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=source)
            try:
                ready.set_result(await server.start())
                await asyncio.to_thread(stop.wait)
            finally:
                await server.aclose()
                await registry.aclose()
        asyncio.run(run())

    def call(descriptor):
        scope = TestAuthorityScope(('ui',))
        scope.join('ui', source)
        client = SetupClient(descriptor, timeout=5)
        try:
            barrier.wait(timeout=5)
            client.complete(LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),))),
                            authority_scope=scope)
            return client.evidence
        finally:
            client.close()
            scope.close()

    ready = [Future(), Future()]
    with ThreadPoolExecutor(max_workers=4) as executor:
        servers = [executor.submit(serve, index, ready[index]) for index in range(2)]
        try:
            descriptors = [future.result(timeout=5) for future in ready]
            calls = [executor.submit(call, descriptor) for descriptor in descriptors]
            rows = [future.result(timeout=10) for future in calls]
            assert len(loops) == 2
            assert observed == [1, 1]
            assert all(row[0]['content']['text'] == '[REDACTED] [REDACTED] [REDACTED]' for row in rows)
        finally:
            stop.set()
            for server in servers:
                server.result(timeout=10)


def test_scope_owner_change_while_server_waits_for_gate_releases_lease(monkeypatch):
    from contextlib import asynccontextmanager
    from api_agent.providers import authority_gate
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing.authority_scope import TestAuthorityScope
    first = EmissionBoundary(ContentPolicy(True))
    second = EmissionBoundary(ContentPolicy(True))
    scope = TestAuthorityScope(('ui',))
    scope.join('ui', first)
    dispatched = []

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            dispatched.append(True)
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    def call(descriptor):
        client = SetupClient(descriptor, timeout=5)
        try:
            with pytest.raises(ContractError):
                client.complete(LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),))),
                                authority_scope=scope)
        finally:
            client.close()

    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=first)
        descriptor = await server.start()
        original = authority_gate.acquire_authorities
        entered = asyncio.Event()
        @asynccontextmanager
        async def watched(authorities, timeout):
            entered.set()
            async with original(authorities, timeout):
                yield
        try:
            async with original([first._authority], 5):
                monkeypatch.setattr(authority_gate, 'acquire_authorities', watched)
                task = asyncio.create_task(asyncio.to_thread(call, descriptor))
                await asyncio.wait_for(entered.wait(), 5)
                scope.join('ui', second)
            await asyncio.wait_for(task, 5)
            assert not dispatched
            assert scope._leases == 0
        finally:
            await server.aclose()
            await registry.aclose()
            scope.close()
    asyncio.run(scenario())


def test_server_cancellation_while_waiting_for_gate_releases_admission(monkeypatch):
    from contextlib import asynccontextmanager
    from api_agent.providers import authority_gate
    from api_agent.providers.llm import CompletionRequest, LLMRequest
    from api_agent.providers.setup_transport import SetupClient
    from api_agent.testing.authority_scope import TestAuthorityScope
    boundary = EmissionBoundary(ContentPolicy(True))
    scope = TestAuthorityScope(('ui',))
    scope.join('ui', boundary)
    dispatched = []

    class Owner:
        ensure_ready = AsyncMock()
        aclose = AsyncMock()
        async def complete(self, request):
            dispatched.append(True)
            return LLMResult(Message('assistant', (TextContent('done'),)), Usage(1, 1))

    def call(descriptor):
        client = SetupClient(descriptor, timeout=5)
        try:
            with pytest.raises(Exception):
                client.complete(LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),))),
                                authority_scope=scope)
        finally:
            client.close()

    async def scenario():
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-authority-capability-0000000', timeout=5, authority=boundary)
        descriptor = await server.start()
        original = authority_gate.acquire_authorities
        entered = asyncio.Event()
        @asynccontextmanager
        async def watched(authorities, timeout):
            entered.set()
            async with original(authorities, timeout):
                yield
        try:
            async with original([boundary._authority], 5):
                monkeypatch.setattr(authority_gate, 'acquire_authorities', watched)
                task = asyncio.create_task(asyncio.to_thread(call, descriptor))
                await asyncio.wait_for(entered.wait(), 5)
                await asyncio.wait_for(server.aclose(), 5)
                await asyncio.wait_for(task, 5)
                assert not dispatched
                assert scope._leases == 0
            async with original([boundary._authority], 1):
                pass
        finally:
            await server.aclose()
            await registry.aclose()
            scope.close()
    asyncio.run(scenario())
