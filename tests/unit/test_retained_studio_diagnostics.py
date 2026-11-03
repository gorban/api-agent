"""Retained execution errors are actionable without exposing credentials."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from api_agent.testing.diagnostics import write_exception_diagnostic
from api_agent.testing.studio_http import _execution_error, agent_endpoint
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


def test_owned_manual_error_uses_live_credentials_even_with_content_disabled(capsys):
    boundary = EmissionBoundary(ContentPolicy(False), secrets=('selected-api-secret',))
    provider = SimpleNamespace(manual=SimpleNamespace(emissions=boundary))
    try:
        raise ValueError('upstream rejected selected-api-secret and browser-nonce\x1b')
    except ValueError as error:
        _execution_error(provider, 'browser-nonce', error)
    diagnostic = capsys.readouterr().err
    assert 'Studio execution failed' in diagnostic
    assert 'ValueError' in diagnostic
    assert 'upstream rejected' in diagnostic
    assert 'selected-api-secret' not in diagnostic
    assert 'browser-nonce' not in diagnostic
    assert '\x1b' not in diagnostic
    assert '\\u001b' in diagnostic


def test_missing_authority_withholds_arbitrary_messages(capsys):
    try:
        raise ValueError('unregistered-private-value')
    except ValueError as error:
        _execution_error(object(), 'nonce', error)
    diagnostic = capsys.readouterr().err
    assert 'Stack locations' in diagnostic
    assert 'unregistered-private-value' not in diagnostic


def test_exception_publication_retries_a_changed_credential_generation(monkeypatch):
    authority = EmissionBoundary(ContentPolicy(False))
    commit = authority.commit_if_current
    calls = []
    def changed(generation, callback):
        calls.append(generation)
        if len(calls) == 1:
            authority.register_secret('newly-enrolled-secret')
            return False
        return commit(generation, callback)
    monkeypatch.setattr(authority, 'commit_if_current', changed)
    output = []
    write_exception_diagnostic(ValueError('newly-enrolled-secret'), output.append, authority=authority)
    assert len(calls) == 2
    assert len(output) == 1
    assert '[REDACTED]' in output[0]
    assert 'newly-enrolled-secret' not in output[0]


def test_unstable_credentials_withhold_details_after_bounded_retries(monkeypatch):
    authority = EmissionBoundary(ContentPolicy(False))
    monkeypatch.setattr(authority, 'commit_if_current', lambda generation, callback: False)
    output = []
    write_exception_diagnostic(ValueError('private-value'), output.append, authority=authority)
    assert output == ['Execution failed: diagnostic withheld because credential registration changed.\n']


@pytest.mark.parametrize('failure', ['short', 'flush'])
def test_stderr_delivery_errors_are_not_reported_as_success(monkeypatch, failure):
    from api_agent.testing import studio_http
    def flush():
        if failure == 'flush':
            raise OSError('flush failed')
    monkeypatch.setattr(studio_http.sys, 'stderr', SimpleNamespace(
        write=lambda text: 0 if failure == 'short' else len(text), flush=flush))
    with pytest.raises(OSError):
        _execution_error(object(), 'nonce', ValueError('private-value'))


def test_real_bridge_reports_execution_error_to_stderr_and_keeps_public_response_small(capsys):
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.llm import AvailableSelection
    class Provider:
        emissions = EmissionBoundary(ContentPolicy(False), secrets=('owned-api-secret',))
        def describe(self):
            return (AvailableSelection('sample'),)
        def complete(self, request):
            raise ProviderAccessError('unknown', 'upstream rejected owned-api-secret')
    async def scenario():
        origin = 'http://localhost:9443'
        nonce = 'browser-nonce' * 3
        async with agent_endpoint(Provider(), 'sample', origin, nonce) as endpoint:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(endpoint + '/app/agent/chat',
                    headers={'Origin': origin, 'X-API-Agent-Session': nonce},
                    json={'session_id': str(uuid4()), 'model': 'sample',
                          'messages': [{'role': 'user', 'content': 'Hello'}]})
                assert response.status_code == 502
                assert response.json()['message'] == 'Selected provider rejected the request.'
    asyncio.run(scenario())
    diagnostic = capsys.readouterr().err
    assert 'Studio execution failed' in diagnostic
    assert 'ProviderAccessError' in diagnostic
    assert 'upstream rejected [REDACTED]' in diagnostic
    assert 'owned-api-secret' not in diagnostic
