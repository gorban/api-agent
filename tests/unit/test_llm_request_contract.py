"""The existing public inference smoke test remains bounded and substantive."""
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.providers.llm import AvailableSelection


@pytest.fixture
def smoke_test():
    path = Path(__file__).parents[1] / 'integration' / 'test_llm_request.py'
    return runpy.run_path(str(path))['test_llm_request']


@pytest.mark.parametrize('supports_bound,advertised,expected', [
    (True, 128, 128), (True, 256, 256), (True, 1024, 256),
    (True, None, 256), (False, 128, None), (False, None, None),
])
def test_smoke_uses_selected_provider_and_bounded_text_request(
        smoke_test, supports_bound, advertised, expected):
    calls = []

    def complete(request):
        calls.append(request)
        return SimpleNamespace(outcome='completed', payload=SimpleNamespace(
            content=(SimpleNamespace(text='Ok'),)))

    selection = AvailableSelection('synthetic.model',
        features=('max_output_tokens',) if supports_bound else (),
        max_output_tokens=advertised)
    smoke_test('synthetic.model', SimpleNamespace(complete=complete,
        describe=lambda: (AvailableSelection('other.model'), selection)))

    assert len(calls) == 1
    request = calls[0]
    assert request.selected_llm == 'synthetic.model'
    assert request.payload.messages[0].role == 'user'
    assert request.payload.messages[0].content[0].text == 'Answer with exactly: Ok'
    assert request.payload.generation.max_output_tokens == expected
    assert request.payload.tools == ()
    selection.validate(request)


@pytest.mark.parametrize('outcome,texts', [
    ('length', ['Ok']), ('completed', []), ('completed', ['  ']),
])
def test_smoke_rejects_incomplete_or_empty_inference(smoke_test, outcome, texts):
    provider = SimpleNamespace(describe=lambda: (AvailableSelection(
        'synthetic.model', features=('max_output_tokens',)),), complete=lambda request: SimpleNamespace(
        outcome=outcome, payload=SimpleNamespace(
            content=tuple(SimpleNamespace(text=text) for text in texts))))

    with pytest.raises(AssertionError):
        smoke_test('synthetic.model', provider)
