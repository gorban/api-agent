"""The live integration's controls use real Studio HTTP and the original page."""
import asyncio
import json
from types import SimpleNamespace
import importlib.util
from pathlib import Path
import sys

import pytest

from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, Usage
from api_agent.testing.ui_runtime import UIIntegrationOwner
from api_agent.ui import provider as included


def selection_module():
    root = Path(__file__).parents[1] / 'integration'
    # Match pytest's integration-module import environment without installed code.
    sys.path.insert(0, str(root))
    try:
        spec = importlib.util.spec_from_file_location('selection_browser_control', root / 'test_studio_selection.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(root))


class Provider:
    def __init__(self, answer="Maybe", scoped=False):
        self.scoped = scoped
        self.answer = answer
        from api_agent.testing.inference_content import content_boundary
        self.calls = []
        self.config = {'LLM_REASONING_EFFORT': 'low'}
        self._provider_config = self.config
        self.closed = False
        self.emissions = content_boundary({})
        self.provider = SimpleNamespace(implementation=object())
    _run_private = staticmethod(asyncio.run)
    from api_agent.testing.provider_setup import IntegrationLLM
    complete = IntegrationLLM.complete
    def describe(self):
        if self.scoped:
            return tuple(AvailableSelection(identifier, display_name=name, family='gpt-luna-us', version=version,
                features=('reasoning_effort',), reasoning_efforts=('off', 'low', 'medium', 'high'))
                for identifier, name, version in (
                    ('us.openai.gpt-6-luna', 'GPT-6-Luna US', '6'),
                    ('us.openai.gpt-5.6-luna', 'GPT-5.6-Luna US', '5.6')))
        return tuple(AvailableSelection(identifier, display_name=name, family=family, version=version,
            features=('reasoning_effort',), reasoning_efforts=('off', 'low', 'medium', 'high'))
            for identifier, name, family, version in (
                ('gpt-6-sol', 'GPT-6-Sol', 'gpt-sol', '6'),
                ('gpt-6-luna', 'GPT-6-Luna', 'gpt-luna', '6'),
                ('gpt-5.6-luna', 'GPT-5.6-Luna', 'gpt-luna', '5.6')))
    async def _complete(self, request):
        self.calls.append(request)
        return LLMResult(Message('assistant', (TextContent(self.answer),)), usage=Usage(2, 3))


@pytest.mark.parametrize('scoped', [False, True])
def test_original_page_selects_exact_version_and_off_before_only_request(tmp_path, monkeypatch, scoped):
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("API_AGENT_INTEGRATION_EVENTS", str(events))
    module = selection_module()
    provider = Provider(scoped=scoped)
    default, chosen = ('us.openai.gpt-6-luna', 'us.openai.gpt-5.6-luna') if scoped else ('gpt-6-luna', 'gpt-5.6-luna')
    observed = module.selection_catalog.__wrapped__((default, chosen, 'low', 'off'), provider)
    backend = module.ui_provider_backend.__wrapped__(observed)
    owner = UIIntegrationOwner({'config': {'UI_LISTENER_URL': 'http://127.0.0.1:9443',
        'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider',
        'LLM_ALLOWED_MODELS': '(us.openai.)?gpt-.*', 'LLM_DEFAULT_MODEL': default,
        'LLM_REASONING_EFFORT': 'low'}, 'owner_root': str(tmp_path)}, None, backend=backend)
    loop = asyncio.new_event_loop()
    try:
        prepared = loop.run_until_complete(owner.start())
        original_page = prepared.session.page
        assert provider.calls == []
        module.test_studio_selection(prepared, loop, backend)
        if scoped:
            assert loop.run_until_complete(original_page.locator('#model option:checked').text_content()) == 'gpt-luna'
        assert len(provider.calls) == 1
        assert backend.observed.calls[0][1].usage == Usage(2, 3)
        request = provider.calls[0]
        assert request.selected_llm == chosen
        assert request.payload.generation.reasoning_effort == 'off'
        assert request.payload.messages[-1].content[0].text == 'Respond with exactly: Maybe'
        assert prepared.session.page is original_page
        usage_events = [json.loads(line) for line in events.read_text().splitlines()]
        usage_events = [row['data'] for row in usage_events if row['kind'] == 'inference_usage']
        assert len(usage_events) == 2
        assert usage_events[0]['call_id'] == usage_events[1]['call_id']
        assert usage_events[1]['selection'] == chosen
        assert usage_events[1]['reasoning_effort'] == 'off'
        assert usage_events[1]['usage']['input_tokens'] == 2
        assert usage_events[1]['usage']['output_tokens'] == 3
        assert usage_events[1]['outcome'] == 'completed'
    finally:
        loop.run_until_complete(owner.aclose())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


@pytest.mark.parametrize('answer,accepted', [('MAYBE.',False),('Maybe....',False),('  Maybe\n',True)])
def test_original_page_requires_exact_maybe_after_surrounding_whitespace(tmp_path, answer, accepted):
    module = selection_module()
    provider = Provider(answer)
    observed = module.selection_catalog.__wrapped__(('gpt-6-luna','gpt-5.6-luna','low','off'), provider)
    backend = module.ui_provider_backend.__wrapped__(observed)
    owner = UIIntegrationOwner({'config': {'UI_LISTENER_URL': 'http://127.0.0.1:9443',
        'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider',
        'LLM_ALLOWED_MODELS': 'gpt-.*'}, 'owner_root': str(tmp_path)}, None, backend=backend)
    loop = asyncio.new_event_loop()
    try:
        prepared = loop.run_until_complete(owner.start())
        if accepted:
            loop.run_until_complete(module.selection_prompt(prepared, observed))
        else:
            with pytest.raises(AssertionError):
                loop.run_until_complete(module.selection_prompt(prepared, observed))
        assert len(provider.calls) == 1
        assert provider.calls[0].selected_llm == 'gpt-5.6-luna'
        assert provider.calls[0].payload.generation.reasoning_effort == 'off'
    finally:
        loop.run_until_complete(owner.aclose())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


def test_original_page_rejects_incorrect_advertised_version_text(tmp_path):
    module = selection_module()
    provider = Provider()
    observed = module.selection_catalog.__wrapped__(('gpt-6-luna', 'gpt-5.6-luna', 'low', 'off'), provider)
    backend = module.ui_provider_backend.__wrapped__(observed)
    owner = UIIntegrationOwner({'config': {'UI_LISTENER_URL': 'http://127.0.0.1:9443',
        'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider',
        'LLM_ALLOWED_MODELS': 'gpt-.*'}, 'owner_root': str(tmp_path)}, None, backend=backend)
    loop = asyncio.new_event_loop()
    try:
        prepared = loop.run_until_complete(owner.start())
        page = prepared.session.page
        page.set_default_timeout(500)
        loop.run_until_complete(page.evaluate('''() => {
            const versions = document.querySelector('#version');
            new MutationObserver(() => {
                const selected = [...versions.options].find(option => option.value === 'gpt-5.6-luna');
                if (selected && selected.textContent === '5.6') selected.textContent = '15.6';
            }).observe(versions, {childList: true, subtree: true, characterData: true});
        }'''))
        with pytest.raises(AssertionError, match='Locator expected to have text'):
            loop.run_until_complete(module.selection_prompt(prepared, observed))
        assert provider.calls == []
    finally:
        loop.run_until_complete(owner.aclose())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


def test_cold_catalog_preparation_finishes_before_original_page(tmp_path):
    import threading
    module=selection_module()
    entered,release=threading.Event(),threading.Event()
    prepared_catalog=[]
    class Cold(Provider):
        def describe(self):
            if not prepared_catalog:
                entered.set()
                assert release.wait(10),'Cold preparation gate was not released'
                prepared_catalog.append(True)
            return super().describe()
    provider=Cold()
    async def prepare():
        preparation=asyncio.create_task(asyncio.to_thread(module.selection_catalog.__wrapped__,('gpt-6-luna','gpt-5.6-luna','low','off'),provider))
        try:
            assert await asyncio.to_thread(entered.wait,3), 'Catalog prerequisite was omitted'
            assert not preparation.done()
            assert provider.calls==[]
        finally:
            release.set()
        return await preparation
    loop=asyncio.new_event_loop()
    owner=None
    try:
        observed=loop.run_until_complete(prepare())
        assert prepared_catalog==[True]
        backend=module.ui_provider_backend.__wrapped__(observed)
        owner=UIIntegrationOwner({'config':{'UI_LISTENER_URL':'http://127.0.0.1:9443',
            'UI_PROVIDER':str(Path(included.__file__))+':create_provider',
            'LLM_ALLOWED_MODELS':'gpt-.*'},'owner_root':str(tmp_path)},None,backend=backend)
        prepared=loop.run_until_complete(owner.start())
        assert provider.calls==[]
        module.test_studio_selection(prepared,loop,backend)
        assert len(provider.calls)==1
    finally:
        release.set()
        if owner is not None:loop.run_until_complete(owner.aclose())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
