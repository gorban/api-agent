"""GPT proposal admission and capture regressions."""
import httpx
import pytest

from api_agent.llm_providers.gpt import inference
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import (
    CompletionRequest, LLMRequest, Message, StructuredFunctionProfile,
    StructuredOutput, TextContent, Tool,
)

SCHEMA = '{"type":"object","properties":{},"additionalProperties":false}'


def proposal(model='gpt-6-luna'):
    structured = StructuredOutput('Shape', SCHEMA, False, encoding='function_calling',
        provider_tool_schema=SCHEMA, function_profile=StructuredFunctionProfile(None, 'converter'))
    return LLMRequest(model, CompletionRequest((Message('user', (TextContent('Describe'),)),),
        structured=structured))


@pytest.mark.parametrize('base,model,subscription', [
    ('https://openrouter.ai/api/v1', 'gpt-6-luna', False),
    ('https://api.openai.com/v1', 'gpt-unknown', False),
    ('https://api.openai.com/v1', 'gpt-6-sol', True),
])
def test_unqualified_proposal_is_rejected_before_transport(base, model, subscription):
    with pytest.raises(ProviderAccessError, match='named function proposal is unsupported'):
        inference.validate_text_request(base_url=base, access_token='synthetic-token',
            subscription=subscription, api_mode='responses', request=proposal(model))


def test_proposal_cannot_mix_with_executable_tools():
    req = proposal()
    with pytest.raises(ValueError, match='invalid LLM description'):
        CompletionRequest(req.payload.messages, tools=(Tool('lookup', 'Lookup', SCHEMA),),
            structured=req.payload.structured)


@pytest.mark.asyncio
async def test_proposal_rejection_does_not_export_provider_error():
    from api_agent.workflows.inference_capture import collect_inference
    def send(outbound):
        return httpx.Response(400, json={'error': {'message': 'private-response', 'code': 'private-code'}})
    with collect_inference(retain_raw=True) as collected:
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
            with pytest.raises(ProviderAccessError) as error:
                await inference.complete_text(client=client, base_url='https://api.openai.com/v1',
                    access_token='synthetic-token', subscription=False, api_mode='responses', request=proposal())
    assert error.value.message == 'GPT inference was rejected.'
    assert error.value.code is None
    assert 'private-response' not in repr(collected['rows'])


@pytest.mark.asyncio
async def test_failed_projection_omits_capture(monkeypatch):
    from api_agent.providers import structured_proposals
    from api_agent.workflows.inference_capture import collect_inference
    def fail(value):
        raise ValueError('unsafe projection')
    monkeypatch.setattr(structured_proposals, 'proposal_capture_projection', fail)
    with collect_inference(retain_raw=True) as collected:
        await inference._capture_safe_response({'output': 'private'}, 'request', 'synthetic-token', None, proposal=True)
    assert collected['rows'] == []


@pytest.mark.asyncio
async def test_proposal_capture_projects_response():
    from api_agent.workflows.inference_capture import collect_inference
    value = {'output': [{'type': 'reasoning', 'encrypted_content': 'opaque'},
        {'type': 'function_call', 'name': 'Shape', 'arguments': '{}', 'call_id': 'id'}]}
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    boundary = EmissionBoundary(ContentPolicy(True))
    with collect_inference(boundary, retain_raw=True) as collected:
        await inference._capture_safe_response(value, 'request', 'synthetic-token', None, proposal=True)
    assert 'opaque' not in repr(collected['rows'])
    assert 'Shape' in repr(collected['rows'])
