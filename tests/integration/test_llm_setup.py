"""Shared setup conformance without an inference request."""


def test_llm_setup(llm_provider):
    llm_provider.ensure_ready()
