"""An explicit admitted adapter may enrich display data, never usage or identity."""
import pytest
from api_agent.api.application_result import project_application_result
from api_agent.providers.contracts import ContractError


def base():
    return {'session_id': 'turn', 'selected_model': 'model', 'response_text': 'encoded',
            'usage': {'input_tokens': 7}}


def test_absent_projection_preserves_original_result():
    value = base()
    assert project_application_result(value, None) is value


def test_explicit_projection_preserves_metadata_and_enriches_display():
    value = base()
    def project(row):
        return {**row, 'response_text': 'answer', 'response_html': '<p>answer</p>'}
    assert project_application_result(value, project) == {
        **value, 'response_text': 'answer', 'response_html': '<p>answer</p>'}
    assert value == base()


@pytest.mark.parametrize('change', [
    {'session_id': 'other'}, {'selected_model': 'other'}, {'usage': {'input_tokens': 0}},
    {'response_text': None}, {'response_html': {1, 2}}, {'response_html': float('nan')},
    {'response_html': 'x' * 1048577},
])
def test_projection_cannot_change_admitted_metadata_or_publish_invalid_json(change):
    with pytest.raises(ContractError, match='projection'):
        project_application_result(base(), lambda row: {**row, **change})


@pytest.mark.parametrize('value', [None, [], {'response_text': 'only'}])
def test_projection_cannot_drop_the_original_contract(value):
    with pytest.raises(ContractError, match='projection'):
        project_application_result(base(), lambda row: value)


def test_projection_failure_has_safe_fixed_classification():
    def broken(row):
        raise ValueError('untrusted application body')
    with pytest.raises(ContractError, match='projection') as caught:
        project_application_result(base(), broken)
    assert 'untrusted application body' not in str(caught.value)


def test_projector_cannot_mutate_original_nested_accounting():
    value = base()
    def mutate(row):
        row['usage']['input_tokens'] = 0
        return row
    with pytest.raises(ContractError, match='projection'):
        project_application_result(value, mutate)
    assert value['usage']['input_tokens'] == 7


@pytest.mark.parametrize('capability', ['chat', 'chat_stream'])
def test_actual_http_result_projects_before_display_tokens(capability):
    import asyncio
    import json
    from test_studio_bridge import setup_bridge, request, payload
    def projection(row):
        return {**row, 'response_text': 'rendered answer', 'response_html': '<p>rendered answer</p>'}
    async def scenario():
        backend, calls = setup_bridge(result_projector=projection)
        try:
            response = await backend.handle(request('POST', payload()), capability)
            assert response.status_code == 200
            if capability == 'chat_stream':
                chunks = [part.decode() async for part in response.streaming_content]
                frames = [(part.splitlines()[0][7:], json.loads(part.splitlines()[1][6:])) for part in chunks]
                token = next(value for event, value in frames if event == 'token')
                result = next(value for event, value in frames if event == 'result')
                assert token['content'] == 'rendered answer'
            else:
                result = json.loads(response.content)
            assert result['response_text'] == 'rendered answer'
            assert result['response_html'] == '<p>rendered answer</p>'
        finally:
            await backend.aclose()
    asyncio.run(scenario())


def test_noncallable_projector_is_rejected_before_requests():
    from test_studio_bridge import setup_bridge
    with pytest.raises(ValueError, match='projector'):
        setup_bridge(result_projector='not callable')


@pytest.mark.parametrize('reject', [False, True])
def test_native_stream_never_emits_unprojected_application_tokens(reject):
    import asyncio
    import json
    from api_agent.providers.llm import LLMResult, Message, StreamEvent, TextContent
    from test_studio_bridge import setup_bridge, request, payload

    async def stream(value, *, auth, context):
        yield StreamEvent(TextContent('encoded-application-'))
        yield StreamEvent(TextContent('payload'))
        yield StreamEvent(LLMResult(Message('assistant', (
            TextContent('encoded-application-payload'),))), 'completed')

    def projection(row):
        if reject:
            raise ValueError('invalid application result')
        return {**row, 'response_text': 'rendered answer'}

    async def scenario():
        backend, _ = setup_bridge(stream=stream, result_projector=projection)
        try:
            response = await backend.handle(request('POST', payload()), 'chat_stream')
            chunks = [part.decode() async for part in response.streaming_content]
            frames = [(part.splitlines()[0][7:], json.loads(part.splitlines()[1][6:]))
                      for part in chunks]
            tokens = [value['content'] for event, value in frames if event == 'token']
            assert tokens == ([] if reject else ['rendered answer'])
            assert 'encoded-application-payload' not in ''.join(chunks)
            if reject:
                assert frames[-1][0] == 'error'
            else:
                result = next(value for event, value in frames if event == 'result')
                assert result['response_text'] == ''.join(tokens)
        finally:
            await backend.aclose()

    asyncio.run(scenario())
