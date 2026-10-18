"""A real Studio page can load mixed catalog capabilities without inference."""
import asyncio
import importlib.util
from pathlib import Path

import pytest
from playwright.async_api import expect

from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent
from api_agent.testing.ui_runtime import UIIntegrationOwner
from api_agent.ui import provider as included


@pytest.fixture
def connection():
    path = Path(__file__).parents[1] / 'integration/test_studio_request.py'
    spec = importlib.util.spec_from_file_location('mixed_catalog_connection', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MixedProvider:
    def __init__(self): self.calls = []
    def describe(self):
        return (AvailableSelection('sample', features=('reasoning_effort',), reasoning_efforts=('off', 'low')),
            AvailableSelection('unknown'),
            AvailableSelection('alternate', features=('reasoning_effort',), reasoning_efforts=('off',)))
    def complete(self, request):
        self.calls.append(request)
        return LLMResult(Message('assistant', (TextContent('Ok'),)))


def test_original_page_loads_mixed_catalog_and_validates_only_selected_effort(connection, tmp_path):
    async def run():
        provider = MixedProvider()
        backend = connection.ui_provider_backend.__wrapped__(None, provider, 'sample')
        owner = UIIntegrationOwner({'config': {
            'UI_LISTENER_URL': 'http://127.0.0.1:9443',
            'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider',
            'LLM_DEFAULT_MODEL': 'sample', 'LLM_ALLOWED_MODELS': 'sample|unknown|alternate',
            'LLM_REASONING_EFFORT': 'low'}, 'owner_root': str(tmp_path)}, None, backend=backend)
        try:
            prepared = await owner.start()
            page = prepared.session.page
            await expect(page.locator('#model')).to_have_value('sample')
            await expect(page.locator('#model option')).to_have_count(3)
            assert provider.calls == []
            assert await connection.browser_prompt(prepared) == 'Ok'
            await page.locator('#settings-nav').click()
            await page.locator('#model').select_option('unknown')
            await expect(page.locator('#model-status')).to_contain_text('support for this model is unknown')
            await expect(page.locator('#reasoning option')).to_have_count(1)
            assert len(provider.calls) == 1
            await page.locator('#model').select_option('alternate')
            await expect(page.locator('#model-status')).to_contain_text('unsupported')
            await page.locator('#reasoning').select_option('off')
            await expect(page.locator('#model-status')).to_have_text('')
            await page.keyboard.press('Escape')
            await page.locator('#prompt').fill('synthetic second request')
            await page.locator('#send').click()
            await expect(page.locator('.assistant .metadata-grid dd').last).to_have_text('Complete')
            assert len(provider.calls) == 2
            assert provider.calls[0].selected_llm == 'sample'
            assert provider.calls[0].payload.generation.reasoning_effort == 'low'
            assert provider.calls[1].selected_llm == 'alternate'
            assert provider.calls[1].payload.generation.reasoning_effort == 'off'
            assert prepared.session.page is page
        finally:
            await owner.aclose()
    asyncio.run(run())


def test_catalog_http_failure_is_visible_in_browser_request_diagnostic(connection, tmp_path):
    from api_agent.providers.contracts import ProviderAccessError
    class Rejected(MixedProvider):
        def describe(self): raise ProviderAccessError('invalid_request', 'synthetic-secret-catalog')
    async def run():
        provider = Rejected()
        backend = connection.ui_provider_backend.__wrapped__(None, provider, 'sample')
        owner = UIIntegrationOwner({'config': {'UI_LISTENER_URL': 'http://127.0.0.1:9443',
            'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider'},
            'owner_root': str(tmp_path)}, None, backend=backend)
        try:
            prepared = await owner.start()
            status = prepared.session.page.locator('#model-status')
            await expect(status).to_have_text('Model list unavailable (HTTP 400). Reconnect before sending.')
            with pytest.raises(AssertionError, match=r'Studio model choices are unavailable: .*HTTP 400'):
                await connection.browser_prompt(prepared)
            assert provider.calls == []
            assert 'synthetic-secret' not in await status.inner_text()
        finally:
            await owner.aclose()
    asyncio.run(run())
