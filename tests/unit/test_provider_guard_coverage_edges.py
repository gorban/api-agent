"""Fail closed on concealed authentication and invalid provider commitments."""
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.bedrock.normalized_stream import guard_semantic
from api_agent.providers.credential_guard import CredentialProtectionError, guard_json, require_joined_provider
from api_agent.providers.contracts import ContractError
from api_agent.telemetry.privacy import EmissionBoundary


@pytest.mark.parametrize('content', [r'Be\u0061rer unregistered-token', 'Be\tarer other-token', '\x01Bearer other-token'])
def test_bedrock_semantic_guard_rejects_concealed_bearer_even_without_registered_token(content):
    assert guard_json(content, {'independent-secret'}) == content
    with pytest.raises(ContractError, match='cannot be safely released'):
        guard_semantic(content, {'independent-secret'})


def test_provider_cannot_claim_authority_commitment_by_returning_a_value():
    authority = EmissionBoundary()
    calls = []
    def bind(value):
        calls.append(value)
        return 'not-an-acknowledgement'
    provider = SimpleNamespace(credential_protection_version=1, bind_credential_authority=bind)
    with pytest.raises(CredentialProtectionError, match='could not join'):
        require_joined_provider(provider, authority)
    assert calls == [authority]


@pytest.mark.parametrize('content', [r'Be\u0061rer unregistered-token', 'Be\tarer other-token', '\x01Bearer other-token'])
def test_bedrock_worker_rejects_concealed_bearer_before_http_and_sdk_construction(content):
    import json
    from unittest.mock import Mock
    from api_agent.llm_providers.bedrock import worker
    http_factory = Mock(side_effect=AssertionError('HTTP must not be constructed'))
    client_factory = Mock(side_effect=AssertionError('SDK must not be constructed'))
    frames = []
    request = {'timeout': 30, 'operation': 'completions',
        'auth': {'mode': 'api_key', 'api_key': 'independent-secret', 'region': 'us-east-1'},
        'payload': {'prefix': '/v1', 'body': {'model': 'openai.gpt-oss-20b',
            'messages': [{'role': 'user', 'content': content}]}}}
    worker.execute(request, frames.append, http_factory=http_factory, client_factory=client_factory)
    assert frames == [{'error': 'unknown', 'execution': 'not_started'}]
    http_factory.assert_not_called()
    client_factory.assert_not_called()
    assert 'independent-secret' not in json.dumps(frames)
    assert 'unregistered-token' not in json.dumps(frames) and 'other-token' not in json.dumps(frames)


def test_bedrock_worker_clean_control_reaches_sdk_dispatch_and_closes_owners():
    from unittest.mock import Mock
    from api_agent.llm_providers.bedrock import worker
    http = SimpleNamespace(close=Mock())
    from test_bedrock_mantle import completion
    result = completion()
    create = Mock(return_value=SimpleNamespace(model_dump=lambda **kw: result))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=Mock())
    http_factory = Mock(return_value=http)
    client_factory = Mock(return_value=client)
    frames = []
    request = {'timeout': 30, 'operation': 'completions',
        'auth': {'mode': 'api_key', 'api_key': 'independent-secret', 'region': 'us-east-1'},
        'payload': {'prefix': '/v1', 'body': {'model': 'openai.gpt-oss-20b',
            'messages': [{'role': 'user', 'content': 'ordinary text'}]}}}
    worker.execute(request, frames.append, http_factory=http_factory, client_factory=client_factory)
    assert len(frames) == 1 and frames[0]['result'] == result
    assert set(frames[0]) == {'result', 'diagnostics'}
    http_factory.assert_called_once()
    client_factory.assert_called_once()
    create.assert_called_once()
    client.close.assert_called_once()
    http.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_registration_after_lease_release_does_not_wake_cancelled_future():
    import asyncio
    boundary = EmissionBoundary()
    lease = boundary.begin_evidence_lease()
    task = asyncio.create_task(boundary.aregister_secret('synthetic-credential'))
    await asyncio.sleep(0)
    assert len(boundary._authority.registration_waiters) == 1
    errors = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        lease.release()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        await asyncio.sleep(0)
        assert not errors and not boundary._authority.registration_waiters
        await boundary.aregister_secret('other-credential')
        assert boundary._text('other-credential') == '[REDACTED]'
    finally:
        loop.set_exception_handler(previous)


def test_escaped_json_member_cannot_recreate_registered_object():
    secret = '{"b":2,"a":"abc"}'
    payload = {'a': r'a\u0062c', 'b': 2}
    with pytest.raises(CredentialProtectionError, match='cannot be safely released') as caught:
        guard_json(payload, {secret})
    assert secret not in str(caught.value)
    assert guard_json({'a': r'a\u0062d', 'b': 2}, {secret}) == {'a': r'a\u0062d', 'b': 2}


def test_stream_closed_during_synchronous_authority_binding_never_starts_provider():
    import asyncio
    import threading
    from unittest.mock import Mock
    from api_agent.providers.loading import _StreamView
    from api_agent.providers.llm import LLMRequest, CompletionRequest, Message, TextContent
    stream = Mock(side_effect=AssertionError('closed stream must not start'))
    holder = {}
    def bind(authority):
        thread = threading.Thread(target=lambda: asyncio.run(holder['iterator'].aclose()))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    implementation = SimpleNamespace(credential_protection_version=1,
        bind_credential_authority=bind, astream=stream)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('ordinary'),)),)))
    holder['iterator'] = _StreamView(implementation).astream(request)
    async def run():
        with pytest.raises(StopAsyncIteration): await anext(holder['iterator'])
    asyncio.run(run())
    assert holder['iterator'].closed and holder['iterator'].request is None
    stream.assert_not_called()


@pytest.mark.parametrize('openings, accepted', [(13, True), (14, False)])
def test_nested_carrier_recovery_is_bounded_for_adversarial_long_suffix(openings, accepted):
    text = 'prefix' + '[' * openings + '"' + 'a' * 1048576 + '"'
    secrets = {'{"b":2,"a":"abc"}'}
    if accepted:
        assert guard_json(text, secrets) == text
    else:
        with pytest.raises(CredentialProtectionError, match='cannot be safely released'):
            guard_json(text, secrets)


def test_empty_embedded_array_is_preserved_when_unrelated_to_registered_structure():
    text = 'untrusted prefix [] suffix'
    assert guard_json(text, {'{"a":1}'}) == text


def test_duplicate_json_keys_across_text_fragments_are_rejected_before_release():
    value = ['{"x":1,', '"x":2}']
    with pytest.raises(CredentialProtectionError, match='cannot be safely released'):
        guard_json(value, {'{"a":1}'})
    assert guard_json(['{"x":1,', '"y":2}'], {'{"a":1}'}) == ['{"x":1,', '"y":2}']


def test_large_fragment_set_respects_total_carrier_inspection_budget():
    secrets = {'{"a":1}'}
    safe = ['[]'] + ['q'] * 99994
    assert guard_json(safe, secrets) == safe
    excessive = ['[]'] + ['q'] * 99998
    with pytest.raises(CredentialProtectionError, match='cannot be safely released'):
        guard_json(excessive, secrets)
