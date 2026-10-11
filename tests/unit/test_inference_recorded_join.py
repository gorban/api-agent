"""Actual loaded-factory recorded consumers retain usage and safe failures."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
import pytest


@pytest.mark.parametrize('jobs,debug', [(0,False),(2,False),(0,True)])
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
    monkeypatch.setenv('SYNTHETIC_TEST_PROMPT', 'Answer with exactly: Ok ' + key + ' /var/app/prompt.py')
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
    if debug:
        # Install controlled adapters inside the real supervised worker, rather
        # than assuming parent-process monkeypatches cross subprocess boundaries.
        data = ({'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':response_text}]}],
                 'usage':{'input_tokens':2,'output_tokens':1,'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}} if mode == 'responses' else
                {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':response_text}}],
                 'usage':{'prompt_tokens':2,'completion_tokens':1,'prompt_tokens_details':{'cached_tokens':0},'completion_tokens_details':{'reasoning_tokens':0}}})
        if status != 200:
            data = {'error':{'message':'Request permission denied '+key+' Bearer\nsecondary-synthetic-secret',
                             'code':'denied','type':'permission'}}
            if failure_usage:
                data['usage'] = ({'input_tokens':2,'output_tokens':1} if mode == 'responses' else
                                 {'prompt_tokens':2,'completion_tokens':1})
        (suite/'conftest.py').write_text(f'''
import json
import httpx
from api_agent.auth import credentials
key = {key!r}
credentials.get_password = lambda *args, **kwargs: key
def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected credential fallback")
credentials.get_username = forbidden
credentials.get_totp = forbidden
real_client = httpx.AsyncClient
def respond(request):
    if request.method == "GET":
        return httpx.Response(401) if "authorization" not in request.headers else httpx.Response(200, json={{"data": [{{"id": "gpt-4.1-nano"}}]}})
    assert request.method == "POST" and "/models" not in request.url.path
    assert request.headers["authorization"] == "Bearer " + key
    assert json.loads(request.content)["model"] == "gpt-4.1-nano"
    return httpx.Response({status!r}, json={data!r}, headers={{"x-request-id":"request-safe"}})
httpx.AsyncClient = lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)
''')
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
    if debug:
        # The supervised DEBUG driver runs in a separate process; request/auth
        # assertions execute there and failures return through actual reports.
        assert not reads and not requests and not loaded
    else:
        assert reads == [('synthetic-vault','selected-entry')] and sum(request.method == 'POST' for request in requests)==2 and len(loaded)==1
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
        assert '  input: 4' in console[-1] and '\n  output: 2' in console[-1]
        artifact = json.loads((result.directory/'accounting.json').read_text())
        assert [row['calls'][0]['usage']['input_tokens'] for row in artifact['tests']] == [2,2]
        assert [row['calls'][0]['usage']['output_tokens'] for row in artifact['tests']] == [1,1]
        if status == 200:
            assert all(row['list_cost_usd'] > 0 for row in artifact['tests'])
            for row in artifact['tests']:
                categories = row['stabilized_estimate']['category_costs_usd']
                assert categories['input_tokens'] == pytest.approx(.0000002)
                assert categories['output_tokens'] == pytest.approx(.0000004)
                assert categories['total_tokens'] == pytest.approx(row['stabilized_estimate']['estimated_cost_usd'])
                assert usage['test_costs'][row['test_id']]['category_costs_usd'] == categories
            assert usage['category_costs_usd']['total_tokens'] == pytest.approx(.0000012)
            assert 'input: 4 — $0.0000004\n  output: 2 — $0.0000008' in console[-1]
            assert 'total (input + output): 6 — $0.0000012' in console[-1]
        else:
            assert all(value is None for value in usage['category_costs_usd'].values())
            assert '  input: 4\n  output: 2\n' in console[-1]
            assert ' — unavailable' not in console[-1]
        if status == 200 and failure_usage:
            assert all(row['outcomes'] == ['failed'] for row in artifact['tests'])
            assert 'Synthetic failure after completed inference' in result.execution.output
        if status != 200:
            assert all(row['calls'][0]['outcome'] == 'failed' for row in artifact['tests'])
    else:
        assert len(csv.splitlines())==1
        assert len(usage['unmeasured_calls']) == 2
        assert 'total (input + output): unavailable' in console[-1]
        assert 'HTTP status: 403' in result.execution.output and 'Request permission denied' in result.execution.output
        assert 'code: denied' in result.execution.output and 'request ID: request-safe' in result.execution.output
    artifacts = '\n'.join(path.read_text() for path in result.directory.rglob('*') if path.is_file())
    for path in result.directory.glob('*-result.json'):
        outcome = json.loads(path.read_text())
        requests_saved = [row for row in outcome['attachments'] if row['name'].startswith('LLM request')]
        responses_saved = [row for row in outcome['attachments'] if row['name'].startswith('LLM response')]
        assert len(requests_saved) == int(log_content)
        assert len(responses_saved) == int(log_content and status == 200)
        if requests_saved:
            payload = json.loads((result.directory / requests_saved[0]['source']).read_text())
            assert payload['messages'][0]['text'] == 'Answer with exactly: Ok [REDACTED] /var/app/prompt.py'
        if responses_saved:
            payload = json.loads((result.directory / responses_saved[0]['source']).read_text())
            assert payload['text'] == response_text
    assert key not in artifacts and key not in result.execution.output
    assert secret_fragment not in artifacts and secret_fragment not in result.execution.output
    assert 'secondary-synthetic-secret' not in artifacts and 'secondary-synthetic-secret' not in result.execution.output
    forbidden.assert_not_called()
