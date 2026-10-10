"""Selected-provider authenticated catalog access without inference."""


def test_llm_discovery(llm_provider):
    choices = llm_provider.describe()
    assert choices, 'The selected LLM provider returned no usable selections.'
    assert all(choice.selected_llm for choice in choices)
