"""Content opt-in keeps exact text and paths, never registered credentials."""
import asyncio
import csv
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from api_agent.providers.contracts import ContractError, SessionClosedError
from api_agent.providers.inference import request_to_wire
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupClient
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary, OwnedLogFilter
from api_agent.telemetry.usage import CSV_COLUMNS, SuiteUsage
from api_agent.testing.inference_content import (content_boundary, export_content,
                                               publish_content, content_attachment)
from test_inference_shared_join import REQUEST, RESULT
from test_setup_transport import descriptor


@pytest.mark.parametrize('value', [None, False, 'True', 1, True])
def test_content_requires_boolean_true(value):
    boundary = content_boundary({'LOG_CONTENT': value, 'CLIENT_SECRET': 'synthetic-secret'})
    rows = export_content(boundary, REQUEST, RESULT)
    assert bool(rows) is (value is True)
    if rows:
        assert rows[0]['content']['messages'] == request_to_wire(REQUEST)['messages']
        assert rows[1]['content']['text'] == 'Ok'


def test_publish_and_parent_scrub_preserve_json_whitespace(tmp_path, monkeypatch):
    path = tmp_path / 'events'
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    publish_content((), str(uuid4()))
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    publish_content((), str(uuid4()))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 't.py::test_one (call)')
    publish_content((), None)
    clean = ' { "text" : "Hello", "path" : "/var/app/file.py" } \n'
    publish_content(({'phase': 'request', 'content': {'text': clean,
        'tool_response': {'value': 'synthetic-secret', 'password': 'unregistered-secret'},
        'other': 'Bearer\nsecondary-secret'}},), str(uuid4()))
    row = json.loads(path.read_text())
    assert row['kind'] == 'inference_content' and row['node'] == 't.py::test_one'
    title, data, mime = content_attachment(SimpleNamespace(text=json.dumps(row['data'])),
        EmissionBoundary(secrets=('synthetic-secret',)))
    assert title.startswith('LLM request (') and mime == 'application/json'
    saved = json.loads(data)
    assert saved['text'] == clean
    assert saved['tool_response'] == {'value': '[REDACTED]'}
    assert saved['other'] == 'Bearer [REDACTED]'
    assert b'synthetic-secret' not in data and b'unregistered-secret' not in data
    assert b'secondary-secret' not in data


@pytest.mark.parametrize('row', [None, {}, {'call_id': 'bad', 'phase': 'request', 'content': {}},
    {'call_id': None, 'phase': 'request', 'content': {}},
    {'call_id': str(uuid4()), 'phase': 'bad', 'content': {}},
    {'call_id': str(uuid4()), 'phase': 'response', 'content': []}])
def test_bad_content_events_rejected(row):
    with pytest.raises(ValueError):
        content_attachment(SimpleNamespace(text=json.dumps(row)), EmissionBoundary())


@pytest.mark.parametrize('frame', [
    {'version': 1, 'status': 'evidence', 'phase': 'bad', 'content': {}},
    {'version': 1, 'status': 'evidence', 'phase': 'request', 'content': []},
    {'version': 1, 'status': 'evidence', 'phase': 'request', 'content': {}, 'extra': 1},
    {'version': 1, 'status': 'evidence', 'phase': 'request', 'content': {}},
])
def test_transport_rejects_invalid_or_duplicate_evidence(monkeypatch, frame):
    async def run():
        client = SetupClient(descriptor())
        client._binding = {}
        client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        client._reader = None
        valid = {'version': 1, 'status': 'evidence', 'phase': 'request', 'content': {}}
        monkeypatch.setattr('api_agent.providers.setup_transport._read', AsyncMock(side_effect=[valid, frame]))
        with pytest.raises(ContractError, match='Invalid inference evidence'):
            await client._exchange('complete', request_to_wire(REQUEST))
        assert client.evidence == [{'phase': 'request', 'content': {}}]
    asyncio.run(run())


def test_invalid_new_request_cannot_reuse_previous_content():
    client = SetupClient(descriptor())
    client.evidence = [{'phase': 'response', 'content': {'text': 'previous reply'}}]
    with pytest.raises(ContractError):
        client.complete(None)
    assert client.evidence == []


def test_registry_export_uses_live_lease_and_optional_owner():
    async def run():
        registry = SetupRegistry('operation', 'selection', lambda: None)
        binding = registry.register('worker')
        assert registry.export_content(binding, REQUEST) == ()
        registry._owner = SimpleNamespace(export_content=Mock(return_value=('saved',)))
        assert registry.export_content(binding, REQUEST, RESULT) == ('saved',)
        registry._owner.export_content.assert_called_once_with(REQUEST, RESULT)
        registry._lease(binding)['revoked'] = True
        with pytest.raises(SessionClosedError):
            registry.export_content(binding, REQUEST)
    asyncio.run(run())


@pytest.mark.parametrize('value', ['', None, 'synthetic-credential'])
def test_helper_values_register_only_nonempty_credentials(tmp_path, monkeypatch, value):
    from api_agent.testing.provider_setup import IntegrationLLM
    from test_minimal_subscription_credentials import config
    monkeypatch.setattr('api_agent.auth.credentials.get_password', lambda *args, **kwargs: value)
    owner = IntegrationLLM(dict(config(), LOG_CONTENT=True), tmp_path)
    try:
        assert owner.loop.run_until_complete(owner._credentials(0)(0, 'password')) == value
        assert owner.emissions.export({'text': 'synthetic-credential'}) == {
            'text': '[REDACTED]' if value else 'synthetic-credential'}
    finally:
        owner.close()


@pytest.mark.parametrize('auth', ['cached-server-token', '', object()])
def test_cached_session_token_registered_without_password_lookup(tmp_path, monkeypatch, auth):
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
    from api_agent.testing.provider_setup import IntegrationLLM
    session = AsyncMock(return_value=auth)
    implementation = SimpleNamespace(capabilities=ProviderCapabilities(kind='llm', operations=('complete',)),
        asetup=AsyncMock(return_value=session), acomplete=AsyncMock(return_value=RESULT),
        credential_protection_version=1, bind_credential_authority=Mock(return_value=None))
    loaded = SimpleNamespace(implementation=implementation, aclose=AsyncMock())
    monkeypatch.setattr('api_agent.testing.provider_setup.aload_provider', AsyncMock(return_value=loaded))
    provider = IntegrationLLM({'LOG_CONTENT': True, 'LLM_PROVIDER': 'synthetic',
                              'LLM_ALLOWED_MODELS': '.*'}, tmp_path)
    request = LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('cached-server-token'),)),)))
    try:
        assert provider.complete(request) == RESULT
        implementation.bind_credential_authority.assert_called_with(provider.emissions)
        rows = export_content(provider.emissions, request, RESULT)
        assert rows[0]['content']['messages'][0]['text'] == ('[REDACTED]' if auth == 'cached-server-token' else 'cached-server-token')
        implementation.acomplete.assert_awaited_once_with(request, auth=auth)
    finally:
        provider.close()


@pytest.mark.parametrize('allow', [False, True])
def test_log_paths_are_content_opt_in_but_secrets_never_survive(allow):
    boundary = EmissionBoundary(ContentPolicy(allow), secrets=('synthetic-secret',))
    record = logging.LogRecord('app', 20, '/var/synthetic-secret/source.py', 42,
        {'path': '/var/app/file.py', 'password': 'private', 'prompt': 'Hello'}, (), None,
        func='render')
    OwnedLogFilter(boundary).filter(record)
    assert record.pathname == ('/var/[REDACTED]/source.py' if allow else '')
    assert record.lineno == (42 if allow else 0)
    assert record.funcName == ('render' if allow else None)
    assert 'private' not in record.msg
    assert ('/var/app/file.py' in record.msg) is allow
    record = logging.LogRecord('app', 20, '', -1, {}, (), None)
    record.pathname = None
    OwnedLogFilter(boundary).filter(record)
    assert record.pathname == '' and record.lineno == 0


def test_csv_contract_matches_expected_order_and_default_ignores(tmp_path):
    assert CSV_COLUMNS == ('llm_provider', 'llm_model', 'llm_calls', 'session_id',
        'input_tokens', 'output_tokens', 'total_tokens', 'cache_read_tokens',
        'cache_creation_tokens', 'reasoning_tokens')
    usage = SuiteUsage('run')
    usage.add_usage(provider='provider', model='selection', session_id='test',
                    input_tokens=11, output_tokens=5, cache_read_tokens=3,
                    cache_creation_tokens=2, reasoning_tokens=1)
    usage.write_csv(tmp_path / 'run.csv', tmp_path / 'token_usage.csv')
    with (tmp_path / 'token_usage.csv').open(newline='') as stream:
        rows = list(csv.reader(stream))
    assert rows == [list(CSV_COLUMNS), ['provider', 'selection', '1', 'test', '11', '5', '16', '3', '2', '1']]
    ignored = (Path(__file__).resolve().parents[2] / '.gitignore').read_text().splitlines()
    assert all(pattern in ignored for pattern in ('/token_usage.csv', '/token_usage.csv.lock', '/token_usage.csv.legacy*'))


@pytest.mark.parametrize('enabled,policy', [(False, None), (True, None), (True, ContentPolicy(False))])
def test_parent_recording_scrubs_before_callbacks_and_keeps_usage_without_content(tmp_path, enabled, policy):
    from api_agent.testing.contracts import RunnerEvent, TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.recorded import run_recorded_integration
    from test_recorded_integration import fixture, Storage
    context, test, config = fixture(tmp_path)
    config['LOG_CONTENT'] = enabled
    events = []
    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('inference_content', identity=test, text=json.dumps({
            'call_id': str(uuid4()), 'phase': 'request', 'content': {'text': 'parent-only-secret /var/app/source.py'}})))
        outcome = TestResult(test, 'passed', 1)
        hooks['on_result'](outcome)
        result = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](result)
        return result
    result = run_recorded_integration(context, (test,), config, accepted=True,
        storage=Storage(), runner=runner, on_event=events.append,
        secret_values=('parent-only-secret',), content_policy=policy)
    content_events = [row for row in events if row.kind == 'inference_content']
    assert bool(content_events) is (enabled and policy is None)
    if content_events:
        assert 'parent-only-secret' not in content_events[0].text
        assert '[REDACTED] /var/app/source.py' in content_events[0].text
    outcome = json.loads(next(result.directory.glob('*-result.json')).read_text())
    assert len(outcome['attachments']) == 5
    if not (enabled and policy is None):
        diagnostic = next(row for row in outcome['attachments'] if 'LLM request diagnostics' in row['name'])
        notice = json.loads((result.directory / diagnostic['source']).read_text())
        assert 'LOG_CONTENT=True' in notice['content_notice']
        assert '/var/app/source.py' not in json.dumps(notice)
    if content_events:
        assert any(row['name'] == 'Payload index' for row in outcome['attachments'])
        assert any(row['name'].startswith('Unattached LLM request') for row in outcome['attachments'])
        assert all('parent-only-secret' not in path.read_text() for path in result.directory.iterdir())
    assert {'Test cost estimate', 'Suite usage', 'Immutable accounting'} <= {row['name'] for row in outcome['attachments']}
    assert (result.directory / 'token-usage.csv').read_text().splitlines()[0] == ','.join(CSV_COLUMNS)
