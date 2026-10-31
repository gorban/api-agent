"""The isolated SDK process joins the caller before it can dispatch."""
import asyncio
import os
from pathlib import Path

import pytest

from api_agent.llm_providers.bedrock.auth import BedrockAuthSession
from api_agent.llm_providers.bedrock.sdk import SDKTransport
from api_agent.providers.contracts import ProviderAccessError
from api_agent.telemetry.privacy import EmissionBoundary


@pytest.fixture
def synthetic_sdk(tmp_path, monkeypatch):
    # sitecustomize loads in the actual worker interpreter, replacing only the
    # remote SDK and credential chain; the worker and socket transport are real.
    (tmp_path / 'sitecustomize.py').write_text('''
import json, os, sys, time, types
boto = types.ModuleType("boto3")
class Session:
    def __init__(self, **kw): self.region_name = "us-east-1"
    def get_credentials(self): return self
    def get_frozen_credentials(self):
        token = os.environ.get("SYNTHETIC_TOKEN", "synthetic-token")
        counter = os.environ.get("SYNTHETIC_RETRY_COUNTER")
        if counter:
            try:
                with open(counter) as source: count = int(source.read()) + 1
            except FileNotFoundError:
                count = 1
            with open(counter, "w") as target: target.write(str(count))
            if count > 1: token = "synthetic-refreshed-token"
        return types.SimpleNamespace(access_key="synthetic-access", secret_key="synthetic-secret", token=token)
boto.Session = Session
sys.modules["boto3"] = boto
http = types.ModuleType("httpx2")
class Client:
    def __init__(self, **kw): self.timeout = kw["timeout"]; self.event_hooks = {"response": []}
    def close(self): pass
http.Client = Client
http.Timeout = lambda *a, **kw: None
sys.modules["httpx2"] = http
openai = types.ModuleType("openai"); openai.__path__ = []
providers = types.ModuleType("openai.providers"); providers.__path__ = []
bedrock = types.ModuleType("openai.providers.bedrock")
bedrock.bedrock = lambda **kw: None
class Result:
    def model_dump(self, **kw):
        if os.environ.get("SYNTHETIC_ERROR"):
            return {"error": {"message": os.environ["SYNTHETIC_ERROR"]}}
        if os.environ.get("SYNTHETIC_GRAPH"):
            value = {"model":"zai.glm-4.7-flash", "provider_extra":"observed", "choices":[{"index":0,"message":{"role":"assistant","content":os.environ.get("SYNTHETIC_CROSS_SECRET", os.environ.get("SYNTHETIC_RESPONSE", "Ok")), **({"reasoning_content":os.environ["SYNTHETIC_REASONING"]} if os.environ.get("SYNTHETIC_REASONING") else {})},"finish_reason":"stop"}], "usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}
            if os.environ.get("SYNTHETIC_CONTRACT_REJECTION"):
                value["choices"][0]["message"]["unsupported"] = "contract-response-canary"
                value["encrypted_content"] = "excluded-encrypted-canary"
                value["checkpoint_state"] = "excluded-state-canary"
                split = os.environ.get("SYNTHETIC_SPLIT_FIELD")
                if split:
                    value["choices"][0]["message"]["content"] = "safe" if split == "synthetic-" else "synthetic-"
                    value["choices"][0]["message"][split] = "caller-only"
            return value
        return {"model":"zai.glm-4.7-flash", "choices":[{"index":0,"message":{"role":"assistant","content":os.environ.get("SYNTHETIC_RESPONSE", "safe answer")},"finish_reason":"stop"}], "usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}
class ClientSDK:
    def __init__(self, **kw):
        def create(**kw):
            if os.environ.get("SYNTHETIC_REQUEST_FILE"):
                with open(os.environ["SYNTHETIC_REQUEST_FILE"], "w") as out: json.dump(kw, out)
            if os.environ.get("SYNTHETIC_BLOCK"):
                time.sleep(60)
            counter = os.environ.get("SYNTHETIC_RETRY_COUNTER")
            if counter:
                with open(counter) as source: count = int(source.read())
                if count == 1:
                    class RetryError(Exception):
                        status_code = 503
                        body = {"error":{"message":os.environ.get("SYNTHETIC_RETRY_ERROR", "retry synthetic-refreshed-token")}}
                    raise RetryError()
            if kw.get("stream"):
                class Chunk:
                    def __init__(self, value): self.value = value
                    def model_dump(self, **kwargs): return self.value
                class Stream:
                    def __iter__(self):
                        model = "zai.glm-4.7-flash"
                        yield Chunk({"id":"r", "object":"chat.completion.chunk", "model":model, "choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":None}]})
                        yield Chunk({"id":"r", "object":"chat.completion.chunk", "model":model, "choices":[{"index":0,"delta":{"content":"Ok"},"finish_reason":None}]})
                        yield Chunk({"id":"r", "object":"chat.completion.chunk", "model":model, "choices":[{"index":0,"delta":{},"finish_reason":"stop"}]})
                        yield Chunk({"id":"r", "object":"chat.completion.chunk", "model":model, "choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}})
                    def close(self): pass
                return Stream()
            return Result()
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
        def list_models():
            marker = os.environ.get("SYNTHETIC_CATALOG_MARKER")
            if marker:
                with open(marker, "w") as out: out.write("dispatched")
            return types.SimpleNamespace(model_dump=lambda **kw: {"data":[{"id":"zai.glm-4.7-flash", "extra":os.environ.get("SYNTHETIC_CATALOG_EXTRA", "safe")} ]})
        self.models = types.SimpleNamespace(list=list_models)
    def close(self): pass
openai.OpenAI = ClientSDK
sys.modules.update({"openai":openai, "openai.providers":providers, "openai.providers.bedrock":bedrock})
''')
    site = tmp_path / 'sitecustomize.py'
    site.write_text('import sys\nif sys.argv[0].endswith("bedrock/worker.py"):\n' + ''.join('    ' + line for line in site.read_text().splitlines(keepends=True)))
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join((str(tmp_path), str(Path(__file__).parents[2] / 'src'))))


def test_real_catalog_worker_loads_source_without_inherited_pythonpath(synthetic_sdk, monkeypatch, tmp_path):
    # Keep only synthetic third-party SDKs in the worker's environment, not the
    # source path supplied by the pytest parent. No account or network is used.
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    source = Path(__file__).resolve().parents[2] / 'src/api_agent/__init__.py'
    site = tmp_path / 'sitecustomize.py'
    site.write_text(site.read_text() + f'''
if sys.argv[0].endswith("bedrock/worker.py"):
    import importlib.machinery
    from pathlib import Path
    class SourceOnly:
        def find_spec(self, name, path=None, target=None):
            if name == "api_agent":
                spec = importlib.machinery.PathFinder.find_spec(name, path)
                if spec is None or Path(spec.origin).resolve() != Path({str(source)!r}):
                    raise ModuleNotFoundError("Source package must not fall back to an installed wheel")
    sys.meta_path.insert(0, SourceOnly())
''')
    async def run():
        transport = SDKTransport(timeout=10)
        session = BedrockAuthSession({'mode': 'api_key', 'region': 'us-east-1',
                                     'api_key': 'synthetic-selected'})
        try:
            result = await transport.call('models', {}, auth=session)
            assert result == {'data': [{'id': 'zai.glm-4.7-flash', 'extra': 'safe'}]}
        finally:
            await transport.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_catalog_joins_authority_before_dispatch_and_rejects_echo(synthetic_sdk, monkeypatch, log, trace):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.telemetry.privacy import ContentPolicy

    monkeypatch.setenv('LOG_CONTENT', str(log).lower())
    monkeypatch.setenv('TRACELOOP_TRACE_CONTENT', str(trace).lower())
    monkeypatch.setenv('SYNTHETIC_CATALOG_EXTRA', 'synthetic-caller-only')
    async def run():
        boundary = EmissionBoundary(ContentPolicy(log), secrets=('synthetic-caller-only',))
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {'emissions': boundary})
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        try:
            with pytest.raises(ProviderAccessError) as failure:
                await provider.adescribe(auth=session)
            assert 'synthetic-caller-only' not in str(failure.value)
            assert {'synthetic-access', 'synthetic-secret', 'synthetic-token'} <= boundary._secrets
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_catalog_registration_is_causal_for_caller_credential(synthetic_sdk, monkeypatch):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider

    monkeypatch.setenv('SYNTHETIC_CATALOG_EXTRA', 'synthetic-caller-only')
    async def run():
        boundary = EmissionBoundary(secrets=('synthetic-caller-only',))
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {'emissions': boundary})
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        async def disabled_registration(values):
            return values, None
        try:
            with pytest.raises(ProviderAccessError):
                await provider.transport.call('models', {}, auth=session, join=provider._join_credentials)
            exposed = await provider.transport.call('models', {}, auth=session, join=disabled_registration)
            assert exposed['data'][0]['extra'] == 'synthetic-caller-only'
        finally:
            await provider.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('secret_source', ['caller', 'coordinator'])
def test_catalog_generated_metadata_uses_complete_call_authority(synthetic_sdk, secret_source):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.call_authority import bind_call_authority
    from api_agent.providers.contracts import ContractError

    async def run():
        secret = 'GLM 4.7 Flash'
        coordinator = EmissionBoundary(secrets=(secret,) if secret_source == 'coordinator' else ())
        caller = (secret,) if secret_source == 'caller' else ()
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {'emissions': EmissionBoundary()})
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        try:
            with bind_call_authority(coordinator, caller):
                with pytest.raises(ContractError, match='safely released'):
                    await provider.adescribe(auth=session)
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_catalog_close_drains_validation_and_releases_coordinator(synthetic_sdk, monkeypatch):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.call_authority import bind_call_authority
    from api_agent.providers import credential_guard

    async def run():
        coordinator = EmissionBoundary()
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {'emissions': EmissionBoundary()})
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        entered = asyncio.Event()
        original = credential_guard.async_guard_json
        async def gated_guard(value, secrets):
            entered.set()
            await asyncio.Event().wait()
            return await original(value, secrets)
        monkeypatch.setattr(credential_guard, 'async_guard_json', gated_guard)
        with bind_call_authority(coordinator, ()):
            task = asyncio.create_task(provider.adescribe(auth=session))
        await asyncio.wait_for(entered.wait(), 5)
        registration = asyncio.create_task(coordinator.aregister_secret('later-secret'))
        await asyncio.sleep(0)
        assert not registration.done()
        await asyncio.wait_for(provider.aclose(), 5)
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(registration, 1)
        assert not provider._describes and not provider.transport._processes
    asyncio.run(run())


def test_catalog_late_matching_registration_waits_for_caller_handoff(synthetic_sdk, monkeypatch):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers import credential_guard

    async def run():
        boundary = EmissionBoundary()
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {'emissions': boundary})
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        entered, release = asyncio.Event(), asyncio.Event()
        original = credential_guard.async_guard_json
        async def gated_guard(value, secrets):
            entered.set()
            await release.wait()
            return await original(value, secrets)
        monkeypatch.setattr(credential_guard, 'async_guard_json', gated_guard)
        async def consume():
            rows = await provider.adescribe(auth=session)
            assert not registration.done()
            return rows
        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            registration = asyncio.create_task(boundary.aregister_secret('GLM 4.7 Flash'))
            await asyncio.sleep(0)
            assert not registration.done()
            release.set()
            assert (await task)[0].display_name == 'GLM 4.7 Flash'
            await asyncio.wait_for(registration, 1)
        finally:
            await provider.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('cancel', [False, True])
def test_catalog_registration_failure_or_cancellation_prevents_dispatch(synthetic_sdk, monkeypatch, tmp_path, cancel):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider

    marker = tmp_path / 'catalog-dispatch'
    monkeypatch.setenv('SYNTHETIC_CATALOG_MARKER', str(marker))
    async def run():
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {})
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        entered = asyncio.Event()
        async def reject(values):
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError('registration failed')
        provider._join_credentials = reject
        task = asyncio.create_task(provider.adescribe(auth=session))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                await task
            assert not marker.exists()
            assert not provider.transport._processes
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_real_worker_joins_before_dispatch_and_blocks_credential_input(synthetic_sdk):
    async def run():
        boundary = EmissionBoundary()
        transport = SDKTransport(timeout=10)
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        events = []
        async def join(values):
            for value in values:
                await boundary.aregister_secret(value)
            events.append('joined')
        def dispatch():
            assert {'synthetic-access', 'synthetic-secret', 'synthetic-token'} <= boundary._secrets
            events.append('dispatch')
        payload = {'prefix': '/v1', 'body': {'model': 'zai.glm-4.7-flash', 'messages': [{'role': 'user', 'content': 'hello'}]}}
        try:
            result = await transport.call('completions', payload, auth=session, join=join, on_dispatch=dispatch)
            assert result == {'model': 'zai.glm-4.7-flash', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': 'safe answer'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 7, 'completion_tokens': 2, 'total_tokens': 9}}
            assert events == ['joined', 'dispatch']
            payload['body']['messages'][0]['content'] = 'synthetic-secret'
            with pytest.raises(ProviderAccessError):
                await transport.call('completions', payload, auth=session, join=join, on_dispatch=dispatch)
        finally:
            await transport.aclose()
    asyncio.run(run())


def test_real_worker_rejects_credential_output(synthetic_sdk, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_RESPONSE', 'synthetic-secret')
    async def run():
        transport = SDKTransport(timeout=10)
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        boundary = EmissionBoundary()
        async def join(values):
            for value in values:
                await boundary.aregister_secret(value)
        payload = {'prefix': '/v1', 'body': {'model': 'zai.glm-4.7-flash', 'messages': [{'role': 'user', 'content': 'hello'}]}}
        try:
            with pytest.raises(ProviderAccessError) as failure:
                await transport.call('completions', payload, auth=session, join=join, on_dispatch=lambda: None)
            assert 'synthetic-secret' not in str(failure.value)
        finally:
            await transport.aclose()
    asyncio.run(run())


@pytest.mark.parametrize(('message', 'expected'), [('service error', {'error': {'message': 'service error'}}), ('synthetic-secret', None)])
def test_real_worker_transfers_only_safe_observed_errors(synthetic_sdk, monkeypatch, message, expected):
    monkeypatch.setenv('SYNTHETIC_ERROR', message)
    async def run():
        transport = SDKTransport(timeout=10)
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        boundary = EmissionBoundary()
        async def join(values):
            for value in values:
                await boundary.aregister_secret(value)
        payload = {'prefix': '/v1', 'body': {'model': 'zai.glm-4.7-flash', 'messages': [{'role': 'user', 'content': 'hello'}]}}
        try:
            with pytest.raises(ProviderAccessError) as failure:
                await transport.call('completions', payload, auth=session, join=join, on_dispatch=lambda: None)
            assert getattr(failure.value, 'provider_response', None) == expected
            assert 'synthetic-secret' not in str(failure.value)
        finally:
            await transport.aclose()
    asyncio.run(run())


def test_factory_records_real_worker_error_response(synthetic_sdk, monkeypatch):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.telemetry.privacy import ContentPolicy
    from api_agent.workflows.inference_capture import collect_inference

    monkeypatch.setenv('SYNTHETIC_ERROR', 'observed failure')
    async def run():
        boundary = EmissionBoundary(ContentPolicy(True))
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {})
        provider.bind_credential_authority(boundary)
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        request = LLMRequest('zai.glm-4.7-flash', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        try:
            with collect_inference(boundary, retain_raw=True) as collector:
                with pytest.raises(ProviderAccessError):
                    await provider.acomplete(request, auth=session)
            responses = [row for row in collector['rows'] if row['phase'] == 'response']
            assert responses[0]['content'] == {'provider_response': {'error': {'message': 'observed failure'}}}
            assert {'synthetic-access', 'synthetic-secret', 'synthetic-token'} <= boundary._secrets
        finally:
            await provider.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('log', [False, True])
@pytest.mark.parametrize('metadata', [False, True])
def test_factory_keeps_rejected_response_diagnostics_without_releasing_payload(monkeypatch, log, metadata):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent, Usage
    from api_agent.telemetry.privacy import ContentPolicy
    from api_agent.workflows.inference_capture import collect_inference, capture_transport_diagnostics
    async def run():
        boundary = EmissionBoundary(ContentPolicy(log))
        provider = BedrockProvider({'BEDROCK_API_MODE':'completions'}, {})
        provider.bind_credential_authority(boundary)
        diagnostics = {'version':1, 'response_validation':'rejected', 'finish_reasons':['stop']}
        error = ProviderAccessError('unknown', 'Safe contract failure', status=200,
                                    code='tool_finish_mismatch', usage=Usage(375,25))
        if metadata:
            error.provider_diagnostics = diagnostics
        async def call(*args, **kwargs):
            if metadata:
                capture_transport_diagnostics(diagnostics)
            raise error
        monkeypatch.setattr(provider.transport, 'call', call)
        request = LLMRequest('zai.glm-4.7-flash', CompletionRequest((Message('user',(TextContent('hello'),)),)))
        try:
            with collect_inference(boundary, retain_raw=True) as captured:
                with pytest.raises(ProviderAccessError) as caught:
                    await provider.acomplete(request, auth=None)
            assert caught.value is error and caught.value.usage == Usage(375,25)
            assert captured['rows'] == [dict(phase='response', content={}, reason='unavailable',
                **({'diagnostics':diagnostics} if metadata else {}))]
        finally:
            await provider.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('failure', [False, True])
def test_bedrock_worker_through_recorded_allure(synthetic_sdk, tmp_path, monkeypatch, log, trace, failure):
    import json
    from types import SimpleNamespace
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.payloads import PayloadResolver
    from api_agent.testing.recorded import run_recorded_integration
    root = Path(__file__).resolve().parents[2]
    cross = os.environ.get('SYNTHETIC_CROSS_SECRET') == 'synthetic-caller-only'
    no_scope = os.environ.get('SYNTHETIC_NO_SCOPE') == '1'
    retry = os.environ.get('SYNTHETIC_RETRY_COUNTER') is not None
    if retry:
        from api_agent.providers import inference_retry
        monkeypatch.setattr(inference_retry, 'retry_delay', lambda *args: 0)
    monkeypatch.setenv('SYNTHETIC_GRAPH', '1')
    if failure:
        monkeypatch.setenv('SYNTHETIC_ERROR', 'observed failure')
    monkeypatch.setenv('SYNTHETIC_REQUEST_FILE', str(tmp_path / 'wire.json'))
    pack = tmp_path / 'pack'
    suite = pack / 'tests/integration'
    suite.mkdir(parents=True)
    (pack / 'acquirer.py').write_text('''
from api_agent.llm_providers.bedrock.acquisition import SessionMaterial
class Acquirer:
    version = 1
    async def acquire(self, request, *, previous=None): return SessionMaterial(kind='profile')
    async def aclose(self): pass
def factory(config, services): return Acquirer()
''')
    (suite / 'test_graph.py').write_text('''
import asyncio
import os
import pytest
from api_agent.runtime.tasks import offload
from api_agent.workflows.application import build
from api_agent.providers.control import OperationalError
from api_agent.providers.contracts import ContractError
from api_agent.testing.authority_scope import join_current
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

@pytest.mark.credential_authorities('workflow')
def test_graph(llm_provider, integration_config):
    async def complete(request): return await offload(llm_provider.complete, request)
    if os.environ.get('SYNTHETIC_CROSS_SECRET'):
        join_current('workflow', EmissionBoundary(ContentPolicy(True), secrets=('synthetic-caller-only',)))
    with open(integration_config.values['SYNTHETIC_WORKER_PID'], 'w') as out: out.write(str(os.getpid()))
    graph = build(ROOT, llm=complete, selection='zai.glm-4.7-flash', config=integration_config.values)
    try:
        result = asyncio.run(graph.arun({'messages': [{'role':'user', 'content':'Say Ok'}]}))
    except (OperationalError, ContractError):
        assert os.environ.get('SYNTHETIC_ERROR') == 'observed failure' or os.environ.get('SYNTHETIC_CROSS_SECRET') or os.environ.get('SYNTHETIC_NO_SCOPE') or os.environ.get('SYNTHETIC_CONTRACT_REJECTION')
    else:
        assert result['llm_call']['response_text'] == 'Ok'
'''.replace('ROOT,', repr(str(root)) + ',').replace("@pytest.mark.credential_authorities('workflow')", '' if no_scope else "@pytest.mark.credential_authorities('workflow')"))
    context = SimpleNamespace(base_root=root, owner_root=pack, workflow_entry=None,
        workflow_replacements=(), workflow_root='base', pack_root=pack, suite_paths=lambda mode: (suite,))
    identity = TestIdentity('pack::tests/integration/test_graph.py::test_graph', 'pack',
        'tests/integration/test_graph.py', 'tests/integration/test_graph.py::test_graph',
        tags=frozenset() if no_scope else frozenset({'credential_authorities'}))
    config = {'TUI_MODE':'integration', 'ALLURE_S3_BUCKET':'synthetic', 'ALLURE_RESULTS_DIR':tmp_path / 'results',
        'TEST_PARALLEL_JOBS':0, 'LLM_PROVIDER':str(root / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory',
        'LLM_DEFAULT_MODEL':'zai.glm-4.7-flash', 'LLM_ALLOWED_MODELS':'zai.*', 'LLM_MAX_ATTEMPTS':2 if retry else 1,
        'BEDROCK_AUTH_MODE':'session', 'BEDROCK_API_MODE':'completions', 'BEDROCK_REGION':'us-east-1',
        'BEDROCK_SESSION_PROVIDER':str(pack / 'acquirer.py') + ':factory',
        'LOG_CONTENT':log, 'TRACELOOP_TRACE_CONTENT':trace, 'SYNTHETIC_WORKER_PID':str(tmp_path / 'worker-pid')}
    result = run_recorded_integration(context, [identity], config, accepted=True,
        storage=SimpleNamespace(persist=lambda directory, manifest: None))
    assert result.execution.exit_code == 0, (result.execution.output, [test.local_diagnostic for test in result.execution.tests])
    assert result.persisted and int((tmp_path / 'worker-pid').read_text()) != os.getpid()
    if no_scope:
        assert not (tmp_path / 'wire.json').exists()
        contents = ''.join(path.read_text(errors='replace') for path in result.directory.rglob('*') if path.is_file())
        assert 'synthetic-caller-only' not in contents
        return
    actual = json.loads((tmp_path / 'wire.json').read_text())
    report = json.loads(next(result.directory.glob('*-result.json')).read_text())
    graph, = report['steps']
    component, = graph['steps']
    node, = component['steps']
    call, = [step for step in node['steps'] if step['name'].startswith('call ')]
    attempts = call['steps']
    assert len(attempts) == (2 if retry else 1)
    ids = [next(item['value'] for item in attempt['parameters'] if item['name'] == 'transport_id') for attempt in attempts]
    resolver = PayloadResolver.local(result.directory, result.manifest)
    index = resolver.index(next(row['source'] for row in report['attachments'] if row['name'] == 'Payload index'))
    entries = [entry for entry in index['entries'] if entry['occurrence'].get('kind') == 'llm'
               and entry['kind'] in ('payload', 'chunked')]
    if cross and log:
        omitted = [entry for entry in index['entries'] if entry['occurrence'].get('kind') == 'llm'
                   and entry['occurrence']['phase'] == 'response' and entry['kind'] == 'omitted']
        assert len(omitted) == 1 and omitted[0]['reason'] == 'unavailable'
    assert len(entries) == (4 if retry and log else 2 if log and not cross else 1 if log and cross else 0)
    assert {entry['occurrence']['phase'] for entry in entries} == ({'request'} if cross and log else {'request','response'} if log else set())
    for entry in entries:
        occurrence = entry['occurrence']
        assert occurrence['transport_attempt_id'] in ids
        assert occurrence['capture_source'] == 'provider'
        body = resolver({'kind':'payload', 'source':entry['source'], 'sha256':entry['sha256'], 'length':entry['length']})
        if occurrence['phase'] == 'request':
            assert body == {'provider_payload':actual, 'adapter_instructions':[]}
        else:
            if retry and occurrence['transport_attempt_id'] == ids[0]:
                assert body['provider_response'] == {'error': {'message': 'retry [REDACTED]'}}
            elif failure:
                assert body['provider_response'] == {'error': {'message': 'observed failure'}}
            else:
                assert body['provider_response']['provider_extra'] == 'observed'
                assert body['provider_response']['choices'][0]['message']['content'] == 'Ok'
    for attempt, attempt_id in zip(attempts, ids, strict=True):
        assert sorted(item['source'] for item in attempt.get('attachments', [])) == sorted(entry['source'] for entry in entries if entry['occurrence']['transport_attempt_id'] == attempt_id)
    contents = ''.join(path.read_text(errors='replace') for path in result.directory.rglob('*') if path.is_file())
    for secret in ('synthetic-access', 'synthetic-secret', 'synthetic-token', 'synthetic-caller-only', 'synthetic-refreshed-token'):
        assert secret not in contents


def test_direct_provider_joins_active_collector_before_capture(synthetic_sdk):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.telemetry.privacy import ContentPolicy
    from api_agent.workflows.inference_capture import collect_inference

    async def run():
        boundary = EmissionBoundary(ContentPolicy(True))
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {})
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        request = LLMRequest('zai.glm-4.7-flash', CompletionRequest((Message('user', (TextContent('synthetic-secret'),)),)))
        try:
            with collect_inference(boundary, retain_raw=True) as collector:
                with pytest.raises(ProviderAccessError):
                    await provider.acomplete(request, auth=session)
            assert all('synthetic-secret' not in str(row) for row in collector['rows'])
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_worker_protects_existing_operation_authority(synthetic_sdk, monkeypatch):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.telemetry.privacy import ContentPolicy
    from api_agent.workflows.inference_capture import collect_inference

    async def run():
        boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-caller-secret',))
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {})
        provider.bind_credential_authority(boundary)
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        def request(text):
            return LLMRequest('zai.glm-4.7-flash', CompletionRequest((Message('user', (TextContent(text),)),)))
        try:
            with collect_inference(boundary, retain_raw=True) as collector:
                with pytest.raises(ContractError):
                    await provider.acomplete(request('synthetic-caller-secret'), auth=session)
            assert all('synthetic-caller-secret' not in str(row) for row in collector['rows'])
            monkeypatch.setenv('SYNTHETIC_RESPONSE', 'synthetic-caller-secret')
            with collect_inference(boundary, retain_raw=True) as collector:
                with pytest.raises(ProviderAccessError):
                    await provider.acomplete(request('hello'), auth=session)
            assert all('synthetic-caller-secret' not in str(row) for row in collector['rows'])
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_real_worker_rejects_caller_only_secret_through_recorder(synthetic_sdk, tmp_path, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_CROSS_SECRET', 'synthetic-caller-only')
    test_bedrock_worker_through_recorded_allure(synthetic_sdk, tmp_path, monkeypatch, True, False, False)


@pytest.mark.parametrize('stream', [False, True])
def test_direct_provider_without_boundary_joins_worker_before_dispatch(synthetic_sdk, monkeypatch, stream):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    monkeypatch.setenv('SYNTHETIC_GRAPH', '1')
    async def run():
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {})
        session = BedrockAuthSession({'mode': 'session', 'region': 'us-east-1', 'profile': None})
        request = LLMRequest('zai.glm-4.7-flash', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        from api_agent.llm_providers import response_policy
        original_capture = response_policy.capture_payload
        captured = []
        def capture(*args, **kwargs):
            assert {'synthetic-access', 'synthetic-secret', 'synthetic-token'} <= provider.emissions._secrets
            captured.append(True)
            return original_capture(*args, **kwargs)
        monkeypatch.setattr(response_policy, 'capture_payload', capture)
        try:
            if stream:
                events = [event async for event in provider.astream(request, auth=session)]
                assert events[-1].kind == 'completed'
            else:
                assert (await provider.acomplete(request, auth=session)).payload.content[0].text == 'Ok'
            assert captured == [True]
            assert {'synthetic-access', 'synthetic-secret', 'synthetic-token'} <= provider.emissions._secrets
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_remote_without_caller_authority_does_not_dispatch(synthetic_sdk, tmp_path, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_NO_SCOPE', '1')
    monkeypatch.setenv('SYNTHETIC_RESPONSE', 'synthetic-caller-only')
    test_bedrock_worker_through_recorded_allure(synthetic_sdk, tmp_path, monkeypatch, True, False, False)


def test_real_worker_refresh_upgrades_overlapping_caller_authority(synthetic_sdk, monkeypatch):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.call_authority import bind_call_authority
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.telemetry.privacy import ContentPolicy
    from api_agent.testing.authority_scope import TestAuthorityScope
    monkeypatch.setenv('SYNTHETIC_GRAPH', '1')
    async def run():
        boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-caller',))
        scope = TestAuthorityScope(('workflow',))
        scope.join('workflow', boundary)
        lease = scope.freeze()
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {})
        provider.bind_credential_authority(boundary)
        session = BedrockAuthSession({'mode':'session', 'region':'us-east-1', 'profile':None})
        request = LLMRequest('zai.glm-4.7-flash', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        try:
            with bind_call_authority(boundary, lease.secrets, lease):
                assert (await asyncio.wait_for(provider.acomplete(request, auth=session), 5)).payload.content[0].text == 'Ok'
                monkeypatch.setenv('SYNTHETIC_TOKEN', 'synthetic-refreshed-token')
                assert (await asyncio.wait_for(provider.acomplete(request, auth=session), 5)).payload.content[0].text == 'Ok'
                assert {'synthetic-token', 'synthetic-refreshed-token'} <= set(lease.secrets)
        finally:
            lease.release()
            await provider.aclose()
    asyncio.run(run())


def test_cancelled_real_worker_releases_authority_and_process(synthetic_sdk, tmp_path, monkeypatch):
    from api_agent.llm_providers.bedrock.factory import BedrockProvider
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    monkeypatch.setenv('SYNTHETIC_BLOCK', '1')
    marker = tmp_path / 'wire.json'
    monkeypatch.setenv('SYNTHETIC_REQUEST_FILE', str(marker))
    async def run():
        provider = BedrockProvider({'BEDROCK_API_MODE': 'completions'}, {})
        session = BedrockAuthSession({'mode':'session', 'region':'us-east-1', 'profile':None})
        request = LLMRequest('zai.glm-4.7-flash', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        task = asyncio.create_task(provider.acomplete(request, auth=session))
        try:
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not provider.transport._processes
            await asyncio.wait_for(provider.emissions.aregister_secret('synthetic-after-cancel'), 1)
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_real_worker_retry_scrubs_later_refreshed_credential_from_earlier_response(synthetic_sdk, tmp_path, monkeypatch):
    monkeypatch.setenv('SYNTHETIC_RETRY_COUNTER', str(tmp_path / 'retry-count'))
    test_bedrock_worker_through_recorded_allure(synthetic_sdk, tmp_path, monkeypatch, True, False, False)
