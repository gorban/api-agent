"""Coverage measurements admit emitted events from the actual fixture executor."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from api_agent.workflows.fixture_driver import run_graph, USER_INPUT
from api_agent.workflows.sampling import DECLARED, FIRST, measurement, observed_targets


@pytest.mark.parametrize('route', ['skip', 'next_a', 'next_b'])
@pytest.mark.parametrize('retry', [False, True])
def test_measurement_accepts_actual_fixture_events(route, retry):
    root = Path(__file__).parents[2]
    events = []
    config = {'LLM_PROVIDER': str(root / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory'}

    def complete(request):
        attempt = json.loads(request.payload.messages[-1].content[0].text)['attempt']
        draft = dict(original_input=USER_INPUT, route=route, ready=not retry or attempt > 1,
                     final_output=USER_INPUT)
        return LLMResult(Message('assistant', (TextContent(json.dumps(draft)),)), Usage(3, 1))

    values, nodes = run_graph(SimpleNamespace(complete=complete), 'synthetic', 1,
                             config, root=root, owner_root=root, observe=events.append)
    record = measurement(1, values, nodes, events)
    expected = set(FIRST if route == 'skip' else DECLARED) | {route}
    if retry:
        expected.add('internal_retry')
    assert observed_targets(record) == expected
