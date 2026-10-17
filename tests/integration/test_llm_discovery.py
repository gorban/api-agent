"""Selected-provider authenticated catalog access without inference."""
import pytest


pytestmark = pytest.mark.feature('LLM discovery')

def test_llm_discovery(llm_provider):
    choices = llm_provider.describe()
    assert choices, 'The selected LLM provider returned no usable selections.'
    assert all(choice.selected_llm for choice in choices)
