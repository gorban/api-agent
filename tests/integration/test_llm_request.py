"""One small inference request through the selected, owned provider session."""
import pytest
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent


pytestmark = pytest.mark.feature('LLM inference')

def test_llm_request(llm_selection, llm_provider):
    selection = next(item for item in llm_provider.describe() if item.selected_llm == llm_selection)
    maximum = (min(256, selection.max_output_tokens or 256)
               if 'max_output_tokens' in selection.features else None)
    request = LLMRequest(llm_selection, CompletionRequest(
        (Message('user', (TextContent('Answer with exactly: Ok'),)),),
        generation=GenerationOptions(max_output_tokens=maximum)))
    result = llm_provider.complete(request)
    assert result.outcome == 'completed', 'The selected provider did not complete the request.'
    assert result.payload.content and any(part.text.strip() for part in result.payload.content)
