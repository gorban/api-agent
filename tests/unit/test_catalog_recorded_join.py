"""Real recorded workers verify the selected credential and retain safe failures."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest


@pytest.mark.parametrize('jobs, failure_request_counts', [
    pytest.param(0, (4,), id='sequential'),
    pytest.param(2, (2, 4), id='parallel'),
])
@pytest.mark.parametrize('outcome', ['accepted', 'wrong-password', 'restricted', 'public-wrong-password', 'expired',
    'control-newline', 'control-nul', 'control-escape', 'bearer-space', 'bearer-tab', 'bearer-newline', 'bearer-crlf'])
def test_recorded_discovery_rejects_mismatches_and_records_safe_error(tmp_path, monkeypatch, jobs, failure_request_counts, outcome):
    from api_agent.auth import credentials
    from api_agent.llm_providers.gpt import factory
    from api_agent.testing import provider_setup
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.recorded import run_recorded_integration
    key = 'synthetic-subscription-password-or-key-never-exported'
    secondary_key = 'synthetic-secondary-credential-not-selected'
    credential_reads, requests, loaded = [], [], []
    def password(profile, **kwargs):
        credential_reads.append((profile.vault, profile.user))
        return key
    monkeypatch.setattr(credentials, 'get_password', password)
    forbidden = Mock(side_effect=AssertionError('Unexpected account fallback'))
    monkeypatch.setattr(credentials, 'get_username', forbidden)
    monkeypatch.setattr(credentials, 'get_totp', forbidden)
    real_client = httpx.AsyncClient
    def respond(request):
        requests.append(request)
        if 'Authorization' not in request.headers:
            return httpx.Response(200, json={'data': [{'id': 'gpt-example'}]}) if outcome in ('public-wrong-password', 'expired') else httpx.Response(401)
        assert request.headers['Authorization'] == 'Bearer ' + key
        if outcome == 'accepted':
            return httpx.Response(200, json={'data': [{'id': 'gpt-example', 'name': 'Example', 'max_output_tokens': 2048}]})
        if outcome == 'expired':
            challenge = 'Bearer error="invalid_token", error_description="API key expired"'
            return httpx.Response(401, json={'error': {'message': 'API key expired.', 'code': 401,
                'metadata': {'headers': {'WWW-Authenticate': challenge, 'secret': key}}}},
                headers={'www-authenticate': challenge})
        controls = {'control-newline': '\n', 'control-nul': '\0', 'control-escape': '\x1b'}
        if outcome in controls:
            reflected = key[:10] + controls[outcome] + key[10:]
            return httpx.Response(401, json={'error': {'message': 'Selected credential rejected ' + reflected,
                'code': reflected, 'type': reflected}}, headers={'x-request-id': reflected})
        separators = {'bearer-space': ' ', 'bearer-tab': '\t', 'bearer-newline': '\n', 'bearer-crlf': '\r\n'}
        if outcome in separators:
            reflected = 'Bearer' + separators[outcome] + secondary_key
            return httpx.Response(401, json={'error': {'message': 'Selected credential rejected; ' + reflected,
                'code': reflected, 'type': reflected}}, headers={'x-request-id': reflected})
        status = 403 if outcome == 'restricted' else 401
        return httpx.Response(status, json={'error': {'message': 'Selected credential rejected ' + key,
            'code': 'restricted' if status == 403 else 'invalid_key', 'type': 'access_error'}}, headers={'x-request-id': 'req-safe'})
    def make_client(**kwargs):
        return real_client(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', make_client)
    original = provider_setup.aload_provider
    async def capture(*args, **kwargs):
        provider = await original(*args, **kwargs)
        loaded.append(provider)
        for name in ('ConnectionStore', 'subscription_browser', 'connect_account'):
            provider.implementation.asetup.__globals__[name] = forbidden
        return provider
    monkeypatch.setattr(provider_setup, 'aload_provider', capture)
    base = tmp_path / 'base'
    suite = base / 'tests/integration'
    suite.mkdir(parents=True)
    # Run the actual public integration assertion, twice, through its real worker protocol.
    body = (Path(__file__).parents[1] / 'integration/test_llm_discovery.py').read_text()
    (suite / 'test_discovery.py').write_text(body + '\ndef test_second(llm_provider):\n    test_llm_discovery(llm_provider)\n')
    context = SimpleNamespace(base_root=base, owner_root=base, suite_paths=lambda mode: [suite])
    config = {'LLM_PROVIDER': str(Path(factory.__file__).resolve()) + ':factory', 'LLM_CREDENTIAL_ROLE': 'LLM User',
        'GPT_AUTH_MODE': 'api_key', 'GPT_API_MODE': 'responses', 'UNATTENDED_USER_INDEX': 0,
        'UNATTENDED_AUTH': {'password_manager': {'manager': 'keeper', 'cli_path': 'synthetic-cli'},
            'roles': {'LLM User': [{'vault': 'synthetic-vault', 'user': 'selected-entry'}, {'vault': 'other', 'user': 'must-not-read'}]}},
        'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic', 'ALLURE_RESULTS_DIR': tmp_path / 'allure', 'TEST_PARALLEL_JOBS': jobs}
    selected = [test for test in collect_tests(context, 'integration', config=config) if test.repo == 'base']
    saved = []
    result = run_recorded_integration(context, selected, config, accepted=True,
        storage=SimpleNamespace(persist=lambda directory, manifest: saved.append(manifest)))
    assert result.persisted and len(saved) == 1
    expected = 'passed' if outcome == 'accepted' else 'failed'
    assert [test.status for test in result.execution.tests] == [expected, expected]
    assert result.exit_code == (0 if outcome == 'accepted' else 1)
    assert credential_reads == [('synthetic-vault', 'selected-entry')]
    expected_request_counts = (2,) if outcome == 'accepted' else failure_request_counts
    assert len(requests) in expected_request_counts
    assert len(loaded) == 1
    assert all('Authorization' not in request.headers for request in requests[::2])
    assert all(request.headers.get('Authorization') == 'Bearer ' + key for request in requests[1::2])
    assert loaded[0].closed and not loaded[0].implementation.sessions
    forbidden.assert_not_called()
    artifacts = [json.loads(path.read_text()) for path in result.directory.glob('*-result.json')]
    assert [item['status'] for item in artifacts] == [expected, expected]
    assert key not in result.execution.output
    assert secondary_key not in result.execution.output
    if outcome == 'expired':
        assert 'API key expired.' in result.execution.output
        assert 'code: 401' in result.execution.output and 'type: invalid_token' in result.execution.output
    all_text = '\n'.join(path.read_text() for path in result.directory.rglob('*') if path.is_file())
    assert key not in all_text
    assert secondary_key not in all_text
    if outcome != 'accepted':
        for item in artifacts:
            details = item['statusDetails']
            if outcome == 'expired':
                assert 'API key expired.' in details['trace']
                assert 'HTTP status: 401' in details['trace'] and 'code: 401' in details['trace']
                assert 'type: invalid_token' in details['trace']
                assert 'WWW-Authenticate' not in details['trace']
                continue
            assert 'Selected credential rejected' in details['message'] + details['trace']
            assert f'HTTP status: {403 if outcome == "restricted" else 401}' in details['trace']
            if outcome.startswith('bearer-'):
                assert 'code: Bearer [redacted]' in details['trace']
                assert 'type: Bearer [redacted]' in details['trace'] and 'request ID: Bearer [redacted]' in details['trace']
                continue
            if outcome.startswith('control-'):
                assert 'code: [redacted]' in details['trace']
                assert 'type: [redacted]' in details['trace'] and 'request ID: [redacted]' in details['trace']
                continue
            assert 'type: access_error' in details['trace'] and 'request ID: req-safe' in details['trace']
