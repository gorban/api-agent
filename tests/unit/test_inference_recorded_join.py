"""Actual loaded-factory recorded consumers retain usage and safe failures."""
import csv as csv_module
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
import pytest


@pytest.mark.parametrize('jobs,debug', [(0,False),(2,False),(0,True),(2,True)])
@pytest.mark.parametrize('mode', ['responses','completions'])
@pytest.mark.parametrize('status', [200,403])
@pytest.mark.parametrize('log_content', [False, True])
@pytest.mark.parametrize('failure_usage', [False, True])
def test_recorded_default_inference_counts_usage_and_keeps_selected_auth(tmp_path, monkeypatch, jobs, debug, mode, status, log_content, failure_usage):
    from api_agent.auth import credentials
    from api_agent.llm_providers.gpt import factory
    from api_agent.testing import provider_setup
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.recorded import run_recorded_integration
    from api_agent.providers import rates
    monkeypatch.setattr(rates, 'freeze_pricing', lambda *args: rates.bundled_rate_card())
    secret_fragment = 'synthetic-inference-secret-not-exported'
    key = secret_fragment * 40
    response_text = 'Ok /var/app/renderer.py'
    monkeypatch.setenv('SYNTHETIC_TEST_PROMPT', 'Answer with exactly: Ok /var/app/prompt.py')
    reads, requests, loaded = [], [], []
    def password(profile, **kwargs):
        reads.append((profile.vault,profile.user))
        return key
    monkeypatch.setattr(credentials,'get_password',password)
    forbidden = Mock(side_effect=AssertionError('Unexpected credential fallback'))
    monkeypatch.setattr(credentials,'get_username',forbidden)
    monkeypatch.setattr(credentials,'get_totp',forbidden)
    real_client = httpx.AsyncClient
    def respond(request):
        requests.append(request)
        if request.method == 'GET':
            return httpx.Response(401) if 'authorization' not in request.headers else httpx.Response(200, json={'data': [{'id': 'gpt-4.1-nano'}]})
        assert request.method == 'POST' and '/models' not in request.url.path
        assert request.headers['authorization'] == 'Bearer '+key
        assert json.loads(request.content)['model'] == 'gpt-4.1-nano'
        assert request.url.path.endswith('/responses' if mode == 'responses' else '/chat/completions')
        if status != 200:
            data = {'error': {'message':'Request permission denied '+key+' Bearer\nsecondary-synthetic-secret', 'code':'denied','type':'permission'}}
            if failure_usage:
                data['usage'] = ({'input_tokens':2,'output_tokens':1} if mode == 'responses' else
                                 {'prompt_tokens':2,'completion_tokens':1})
            return httpx.Response(status, json=data, headers={'x-request-id':'request-safe'})
        if mode == 'responses':
            data = dict(status='completed',output=[dict(type='message',role='assistant',content=[dict(type='output_text',text=response_text)])],usage=dict(input_tokens=2,output_tokens=1,input_tokens_details={'cached_tokens':0},output_tokens_details={'reasoning_tokens':0}))
        else:
            data = dict(choices=[dict(finish_reason='stop',message=dict(role='assistant',content=response_text))],usage=dict(prompt_tokens=2,completion_tokens=1,prompt_tokens_details={'cached_tokens':0},completion_tokens_details={'reasoning_tokens':0}))
        return httpx.Response(status,json=data)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: real_client(transport=httpx.MockTransport(respond),**kwargs))
    original = provider_setup.aload_provider
    async def capture(*args, **kwargs):
        provider = await original(*args,**kwargs)
        loaded.append(provider)
        return provider
    monkeypatch.setattr(provider_setup,'aload_provider',capture)
    base = tmp_path/'base'
    suite = base/'tests/integration'
    suite.mkdir(parents=True)
    body = 'from os import environ\n' + (Path(__file__).parents[1]/'integration/test_llm_request.py').read_text().replace("'Answer with exactly: Ok'", "environ['SYNTHETIC_TEST_PROMPT']")
    if status == 200 and failure_usage:
        body += '    raise AssertionError("Synthetic failure after completed inference")\n'
    (suite/'test_request.py').write_text(body+'\ndef test_second(llm_selection,llm_provider):\n    test_llm_request(llm_selection,llm_provider)\n')
    context = SimpleNamespace(base_root=base,owner_root=base,suite_paths=lambda mode: [suite])
    config = dict(LLM_DEFAULT_MODEL='gpt-4.1-nano', LLM_ALLOWED_MODELS='gpt-4.*', LLM_PROVIDER=str(Path(factory.__file__).resolve())+':factory',LLM_CREDENTIAL_ROLE='LLM User',
        LOG_CONTENT=log_content,GPT_AUTH_MODE='api_key',GPT_API_MODE=mode,UNATTENDED_USER_INDEX=0,
        UNATTENDED_AUTH={'password_manager':{'manager':'keeper','cli_path':'synthetic-cli'},
        'roles':{'LLM User':[{'vault':'synthetic-vault','user':'selected-entry'}]}},TUI_MODE='integration',DEBUG=debug,ALLURE_S3_BUCKET='synthetic',ALLURE_RESULTS_DIR=tmp_path/'allure',TEST_PARALLEL_JOBS=jobs)
    selected = [test for test in collect_tests(context,'integration',config=config) if test.repo == 'base']
    events, console = [], []
    result = run_recorded_integration(context,selected,config,accepted=True,storage=SimpleNamespace(persist=lambda d,m:None),on_event=events.append, usage_output=console.append)
    expected = 'passed' if status == 200 and not failure_usage else 'failed'
    assert result.persisted and [test.status for test in result.execution.tests] == [expected,expected]
    assert result.exit_code == (0 if expected == 'passed' else 1)
    assert reads == [('synthetic-vault','selected-entry')]
    assert sum(request.method == 'POST' for request in requests)==2
    assert len(loaded)==1
    assert loaded[0].closed and not loaded[0].implementation.sessions
    assert result.usage['prompt_count'] == 2
    usage = json.loads((result.directory/'usage-summary.json').read_text())
    assert usage['prompt_count'] == 2
    csv = (result.directory/'token-usage.csv').read_text()
    if status == 200 or failure_usage:
        assert 'selected-llm,gpt-4.1-nano,1,' in csv
        assert len(csv.splitlines()) == 3
        assert sum(row.kind=='inference_usage' for row in events)==4
        assert sum(row['total_tokens'] for row in usage['usage_observations']) == 6
        assert sum(row['input_tokens'] for row in usage['usage_observations']) == 4
        artifact = json.loads((result.directory/'accounting.json').read_text())
        assert [row['calls'][0]['usage']['input_tokens'] for row in artifact['tests']] == [2,2]
        assert [row['calls'][0]['usage']['output_tokens'] for row in artifact['tests']] == [1,1]
        if status == 200:
            assert '  non-cached input (writes unreported): 4' in console[-1]
            assert '\n  non-reasoning output: 2' in console[-1]
            assert all(row['list_cost_usd'] > 0 for row in artifact['tests'])
            for row in artifact['tests']:
                categories = row['stabilized_estimate']['category_costs_usd']
                assert categories['input_tokens'] == pytest.approx(.0000002)
                assert categories['output_tokens'] == pytest.approx(.0000004)
                assert categories['total_tokens'] == pytest.approx(row['stabilized_estimate']['estimated_cost_usd'])
                assert usage['test_costs'][row['test_id']]['category_costs_usd'] == categories
            assert usage['category_costs_usd']['total_tokens'] == pytest.approx(.0000012)
            assert 'non-cached input (writes unreported): 4 — $0.0000004\n  non-reasoning output: 2 — $0.0000008' in console[-1]
            assert 'total: 6 — $0.0000012' in console[-1]
        else:
            assert '  non-cached input (writes unreported): unavailable' in console[-1]
            assert '\n  output (reasoning unreported): 2' in console[-1]
            assert all(value is None for value in usage['category_costs_usd'].values())
            assert '  non-cached input (writes unreported): unavailable\n  output (reasoning unreported): 2\n' in console[-1]
            assert ' — unavailable' not in console[-1]
        if status == 200 and failure_usage:
            assert all(row['outcomes'] == ['failed'] for row in artifact['tests'])
            assert 'Synthetic failure after completed inference' in result.execution.output
        if status != 200:
            assert all(row['calls'][0]['outcome'] == 'failed' for row in artifact['tests'])
    else:
        rows = list(csv_module.DictReader(io.StringIO(csv)))
        assert len(rows) == 2 and len({row['session_id'] for row in rows}) == 2
        assert all(row['llm_calls'] == '1' for row in rows)
        assert all(row[name] == '' for row in rows for name in
                   ('input_tokens', 'output_tokens', 'total_tokens', 'cache_read_tokens', 'cache_creation_tokens', 'reasoning_tokens'))
        assert len(usage['unmeasured_calls']) == 2
        assert 'total: unavailable' in console[-1]
        assert 'HTTP status: 403' in result.execution.output and 'Request permission denied' in result.execution.output
        assert 'code: denied' in result.execution.output and 'request ID: request-safe' in result.execution.output
    artifacts = '\n'.join(path.read_text() for path in result.directory.rglob('*') if path.is_file())
    accounting = json.loads((result.directory / 'accounting.json').read_text())
    attempt_ids = [row['calls'][0]['transport_attempts']['attempts'][0]['id'] for row in accounting['tests']]
    assert len(attempt_ids) == len(set(attempt_ids)) == 2
    for path in result.directory.glob('*-result.json'):
        outcome = json.loads(path.read_text())
        pending, attachments = [outcome], []
        while pending:
            step = pending.pop()
            attachments.extend(step.get('attachments', []))
            pending.extend(step.get('steps', []))
        requests_saved = [row for row in attachments if row['name'].startswith(('LLM request (', 'Unattached LLM request ('))]
        responses_saved = [row for row in attachments if row['name'].startswith(('LLM response (', 'Unattached LLM response ('))]
        assert len(requests_saved) == int(log_content)
        assert len(responses_saved) == int(log_content)
        if requests_saved:
            recorded_attempt = requests_saved[0]['name'].split('attempt ', 1)[1].rstrip(')')
            assert recorded_attempt == responses_saved[0]['name'].split('attempt ', 1)[1].rstrip(')')
            owner = next(row for row in accounting['tests'] if row['node_id'] == outcome['fullName'])
            assert recorded_attempt == owner['calls'][0]['transport_attempts']['attempts'][0]['id']
            payload = json.loads((result.directory / requests_saved[0]['source']).read_text())
            container = 'input' if mode == 'responses' else 'messages'
            expected_body = {'model': 'gpt-4.1-nano', container: [
                {'role': 'user', 'content': 'Answer with exactly: Ok /var/app/prompt.py'}],
                'stream': False}
            if mode == 'responses':
                expected_body.update(store=False, max_output_tokens=128)
            else:
                expected_body['max_completion_tokens'] = 128
            assert payload == {'provider_payload': expected_body, 'adapter_instructions': []}
            dispatched_body = {**expected_body, container: [
                {'role': 'user', 'content': 'Answer with exactly: Ok /var/app/prompt.py'}]}
            assert [json.loads(request.content) for request in requests if request.method == 'POST'] == [
                dispatched_body, dispatched_body]
        if responses_saved:
            payload = json.loads((result.directory / responses_saved[0]['source']).read_text())
            if status != 200:
                assert payload['error'] == {'code': 'denied', 'type': 'permission',
                    'message': 'Request permission denied [REDACTED] Bearer [REDACTED]'}
            elif mode == 'responses':
                assert payload['output'][0]['content'][0]['text'] == response_text
                assert payload['usage']['input_tokens'] == 2
            else:
                assert payload['choices'][0]['message']['content'] == response_text
                assert payload['usage'] == {}
    assert key not in artifacts and key not in result.execution.output
    assert secret_fragment not in artifacts and secret_fragment not in result.execution.output
    assert 'secondary-synthetic-secret' not in artifacts and 'secondary-synthetic-secret' not in result.execution.output
    forbidden.assert_not_called()


@pytest.mark.parametrize('mode', ['responses', 'completions'])
def test_recorded_worker_rejects_selected_credential_in_prompt_before_dispatch(tmp_path, monkeypatch, mode):
    from api_agent.auth import credentials
    from api_agent.llm_providers.gpt import factory
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.recorded import run_recorded_integration
    from api_agent.providers import rates

    secret = 'synthetic-selected-credential-must-not-be-dispatched'
    monkeypatch.setattr(credentials, 'get_password', lambda *args, **kwargs: secret)
    monkeypatch.setattr(rates, 'freeze_pricing', lambda *args: rates.bundled_rate_card())
    monkeypatch.setenv('SYNTHETIC_TEST_PROMPT', 'Do not send ' + secret)
    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        if request.method == 'GET':
            if 'authorization' not in request.headers:
                return httpx.Response(401)
            return httpx.Response(200, json={'data': [{'id': 'gpt-4.1-nano'}]})
        raise AssertionError('Credential-bearing prompt must not be dispatched')

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    base = tmp_path / 'base'
    suite = base / 'tests/integration'
    suite.mkdir(parents=True)
    source = (Path(__file__).parents[1] / 'integration/test_llm_request.py').read_text()
    (suite / 'test_request.py').write_text('from os import environ\n' + source.replace(
        "'Answer with exactly: Ok'", "environ['SYNTHETIC_TEST_PROMPT']"))
    context = SimpleNamespace(base_root=base, owner_root=base, suite_paths=lambda mode: [suite])
    config = dict(LLM_DEFAULT_MODEL='gpt-4.1-nano', LLM_ALLOWED_MODELS='gpt-4.*',
        LLM_PROVIDER=str(Path(factory.__file__).resolve()) + ':factory', LLM_CREDENTIAL_ROLE='LLM User',
        LOG_CONTENT=True, GPT_AUTH_MODE='api_key', GPT_API_MODE=mode, UNATTENDED_USER_INDEX=0,
        UNATTENDED_AUTH={'password_manager': {'manager': 'keeper', 'cli_path': 'synthetic-cli'},
            'roles': {'LLM User': [{'vault': 'synthetic-vault', 'user': 'selected-entry'}]}},
        TUI_MODE='integration', ALLURE_S3_BUCKET='synthetic', ALLURE_RESULTS_DIR=tmp_path / 'allure',
        TEST_PARALLEL_JOBS=0)
    selected = [test for test in collect_tests(context, 'integration', config=config) if test.repo == 'base']
    events = []
    result = run_recorded_integration(context, selected, config, accepted=True,
        storage=SimpleNamespace(persist=lambda directory, manifest: None), on_event=events.append)

    assert result.persisted and result.exit_code == 1
    assert [test.status for test in result.execution.tests] == ['failed']
    assert requests and all(request.method == 'GET' for request in requests)
    assert 'protected authentication content' in result.execution.output
    assert secret not in result.execution.output
    assert secret not in repr(events)
    for path in result.directory.rglob('*'):
        if path.is_file():
            assert secret not in path.read_text()
