"""A real pytest worker carries GPT retry evidence through recorded integration."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from api_agent.testing.collection import TestIdentity
from api_agent.testing.payloads import PayloadResolver
from api_agent.testing.recorded import run_recorded_integration

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_gpt_retry_through_real_worker_and_recorder(tmp_path, monkeypatch, log, trace):
    from api_agent.auth import credentials
    from api_agent.llm_providers.gpt import factory
    from api_agent.llm_providers import response_policy
    from api_agent.providers import inference_retry, rates

    secret = 'synthetic-api-key-subprocess-test'
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(filter(None, (str(ROOT / 'src'), os.environ.get('PYTHONPATH')))))
    monkeypatch.setattr(credentials, 'get_password', lambda *args, **kwargs: secret)
    monkeypatch.setattr(inference_retry, 'retry_delay', lambda *args: 0)
    monkeypatch.setattr(rates, 'freeze_pricing', lambda *args: rates.bundled_rate_card())
    requests = []
    original_capture = response_policy.capture_payload
    injected = []

    def distinctive_capture(request, body, evidence, *, auth):
        # Capture and send a distinct synthetic field for each physical attempt.
        body['metadata'] = {'synthetic_attempt': len(injected) + 1}
        injected.append(body['metadata']['synthetic_attempt'])
        return original_capture(request, body, evidence, auth=auth)

    monkeypatch.setattr(response_policy, 'capture_payload', distinctive_capture)
    real_client = httpx.AsyncClient

    def respond(request):
        if request.method == 'GET' and request.url.path.endswith('/key'):
            return httpx.Response(200, json={'data': {'label': 'synthetic-selected-key'}})
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'gpt-4.1-nano'}]})
        assert request.method == 'POST' and request.url.path.endswith('/responses')
        assert request.headers['authorization'] == 'Bearer ' + secret
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(503, json={'error': {'message': 'retry ' + secret},
                                             'usage': {'input_tokens': 2, 'output_tokens': 1}})
        return httpx.Response(200, json={'status': 'completed', 'provider_extra': 'observed success',
            'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Ok'}]}],
            'usage': {'input_tokens': 3, 'output_tokens': 1}, 'hidden_reasoning': 'hidden-sentinel'})

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    pack = tmp_path / 'pack'
    suite = pack / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_graph.py').write_text('''
import asyncio
import os
import pytest
from api_agent.runtime.tasks import offload
from api_agent.workflows.application import build

@pytest.mark.credential_authorities('workflow')
def test_graph(llm_provider, integration_config):
    async def complete(request):
        return await offload(llm_provider.complete, request)
    with open(integration_config.values['SYNTHETIC_WORKER_PID'], 'w') as stream:
        stream.write(str(os.getpid()))
    graph = build(ROOT, llm=complete, selection='gpt-4.1-nano', config=integration_config.values)
    result = asyncio.run(graph.arun({'messages': [{'role': 'user', 'content': 'Say Ok'}]}))
    assert result['llm_call']['response_text'] == 'Ok'
'''.replace('ROOT,', repr(str(ROOT)) + ','))
    context = SimpleNamespace(base_root=ROOT, owner_root=pack, workflow_entry=None,
        workflow_replacements=(), workflow_root='base', pack_root=pack, suite_paths=lambda mode: (suite,))
    identity = TestIdentity('pack::tests/integration/test_graph.py::test_graph', 'pack',
        'tests/integration/test_graph.py', 'tests/integration/test_graph.py::test_graph',
        tags=frozenset({'credential_authorities'}))
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
        'ALLURE_RESULTS_DIR': tmp_path / 'results', 'TEST_PARALLEL_JOBS': 0,
        'LLM_PROVIDER': str(Path(factory.__file__).resolve()) + ':factory',
        'LLM_DEFAULT_MODEL': 'gpt-4.1-nano', 'LLM_ALLOWED_MODELS': 'gpt-4.*', 'LLM_MAX_ATTEMPTS': 2,
        'LLM_CREDENTIAL_ROLE': 'LLM User', 'GPT_AUTH_MODE': 'api_key', 'GPT_API_MODE': 'responses',
        'UNATTENDED_USER_INDEX': 0, 'UNATTENDED_AUTH': {'password_manager': {'manager': 'keeper', 'cli_path': 'synthetic'},
        'roles': {'LLM User': [{'vault': 'synthetic', 'user': 'synthetic'}]}},
        'LOG_CONTENT': log, 'TRACELOOP_TRACE_CONTENT': trace, 'SYNTHETIC_WORKER_PID': str(tmp_path / 'worker-pid')}
    result = run_recorded_integration(context, [identity], config, accepted=True,
        storage=SimpleNamespace(persist=lambda directory, manifest: None))
    assert result.execution.exit_code == 0, result.execution.output
    assert result.persisted and int((tmp_path / 'worker-pid').read_text()) != os.getpid()
    assert len(requests) == 2 and [row['metadata'] for row in requests] == [
        {'synthetic_attempt': 1}, {'synthetic_attempt': 2}]
    assert requests[0]['input'][0]['content'] == 'Say Ok'
    report = json.loads(next(result.directory.glob('*-result.json')).read_text())
    graph, = report['steps']
    component, = graph['steps']
    node, = component['steps']
    call, = [step for step in node['steps'] if step['name'].startswith('call ')]
    attempts = call['steps']
    assert [step['status'] for step in attempts] == ['broken', 'passed']
    ids = [next(p['value'] for p in step['parameters'] if p['name'] == 'transport_id') for step in attempts]
    assert len(set(ids)) == 2
    source = next(item['source'] for item in graph['attachments'] if item['name'] == 'Execution evidence')
    journal = json.loads((result.directory / source).read_text())
    call_event, = [event for event in journal['events'] if event['event'] == 'call']
    ledger = call_event['transport_attempts']['attempts']
    assert [attempt['id'] for attempt in ledger] == ids
    assert [attempt['outcome'] for attempt in ledger] == ['failed', 'completed']
    assert ledger[0]['status'] == 503
    resolver = PayloadResolver.local(result.directory, result.manifest)
    index = resolver.index(next(row['source'] for row in report['attachments'] if row['name'] == 'Payload index'))
    entries = [entry for entry in index['entries'] if entry['occurrence'].get('kind') == 'llm']
    assert len(entries) == (4 if log else 0)
    for step, attempt in zip(attempts, ids, strict=True):
        owned = [entry for entry in entries if entry['occurrence']['transport_attempt_id'] == attempt]
        assert {entry['occurrence']['phase'] for entry in owned} == ({'request', 'response'} if log else set())
        assert all(entry['occurrence']['capture_source'] == 'provider' for entry in owned)
        assert sorted(item['source'] for item in step.get('attachments', [])) == sorted(entry['source'] for entry in owned)
        for entry in owned:
            body = resolver({'kind': 'payload', 'source': entry['source'], 'sha256': entry['sha256'], 'length': entry['length']})
            if entry['occurrence']['phase'] == 'request':
                assert body == {'provider_payload': requests[ids.index(attempt)], 'adapter_instructions': []}
            if entry['occurrence']['phase'] == 'response':
                expected = ({'error': {'message': 'retry [REDACTED]'},
                             'usage': {'input_tokens': 2, 'output_tokens': 1}} if attempt == ids[0] else
                    {'status': 'completed', 'provider_extra': 'observed success',
                     'output': [{'type': 'message', 'role': 'assistant',
                                 'content': [{'type': 'output_text', 'text': 'Ok'}]}],
                     'usage': {'input_tokens': 3, 'output_tokens': 1}})
                assert body == expected
    contents = ''.join(path.read_text(errors='replace') for path in result.directory.rglob('*') if path.is_file())
    assert secret not in contents and 'hidden-sentinel' not in contents


def test_slow_projection_finishes_before_allure_is_finalized(tmp_path, monkeypatch):
    from api_agent.providers.setup_transport import SetupServer
    import asyncio

    original = SetupServer._prepare_isolated

    async def slow(rows, projection):
        await asyncio.sleep(1.2)
        return await original(rows, projection)

    monkeypatch.setattr(SetupServer, '_prepare_isolated', staticmethod(slow))
    test_gpt_retry_through_real_worker_and_recorder(tmp_path, monkeypatch, True, False)
