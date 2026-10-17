"""One small inference request through the selected, owned provider session."""
import pytest
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent


pytestmark = pytest.mark.feature('LLM inference')

def test_llm_request(llm_selection, llm_provider):
    request = LLMRequest(llm_selection, CompletionRequest(
        (Message('user', (TextContent('Answer with exactly: Ok'),)),)))
    result = llm_provider.complete(request)
    assert result.outcome == 'completed', 'The selected provider did not complete the request.'
    assert result.payload.content and any(part.text.strip() for part in result.payload.content)
