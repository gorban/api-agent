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


@pytest.mark.parametrize('default,diagnostic', [('unknown', 'support for this model is unknown'), ('alternate', 'unsupported')])
def test_browser_helper_rejects_incompatible_default_before_sending(connection, tmp_path, default, diagnostic):
    async def run():
        provider = MixedProvider()
        backend = connection.ui_provider_backend.__wrapped__(None, provider, default)
        owner = UIIntegrationOwner({'config': {
            'UI_LISTENER_URL': 'http://127.0.0.1:9443',
            'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider',
            'LLM_DEFAULT_MODEL': default, 'LLM_ALLOWED_MODELS': 'sample|unknown|alternate',
            'LLM_REASONING_EFFORT': 'low'}, 'owner_root': str(tmp_path)}, None, backend=backend)
        try:
            prepared = await owner.start()
            page = prepared.session.page
            await expect(page.locator('#model')).to_have_value(default)
            await expect(page.locator('#model-status')).to_contain_text(diagnostic)
            with pytest.raises(AssertionError, match='Studio model selection is unavailable: .*' + diagnostic):
                await asyncio.wait_for(connection.browser_prompt(prepared), 2)
            assert provider.calls == []
            assert await page.locator('#prompt').input_value() == ''
            assert await page.locator('.assistant').count() == 0
            assert await page.locator('.user').count() == 0
            await page.locator('#settings-nav').click()
            await page.locator('#model').select_option('sample')
            await expect(page.locator('#model-status')).to_have_text('')
            await page.keyboard.press('Escape')
            assert await connection.browser_prompt(prepared) == 'Ok'
            assert len(provider.calls) == 1
            assert provider.calls[0].selected_llm == 'sample'
            assert await page.locator('.user').count() == 1
        finally:
            await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('saved,outcome', [('sample', 'valid'), ('unknown', 'unknown'), ('alternate', 'unsupported'), ('sample', 'failure')])
def test_saved_selection_waits_for_real_catalog_settlement(connection, tmp_path, monkeypatch, saved, outcome):
    import playwright.async_api
    real_expect = playwright.async_api.expect
    async def run():
        provider = MixedProvider()
        backend = connection.ui_provider_backend.__wrapped__(None, provider, 'sample')
        owner = UIIntegrationOwner({'config': {
            'UI_LISTENER_URL': 'http://127.0.0.1:9443',
            'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider',
            'LLM_DEFAULT_MODEL': 'sample', 'LLM_ALLOWED_MODELS': 'sample|unknown|alternate',
            'LLM_REASONING_EFFORT': 'low'}, 'owner_root': str(tmp_path)}, None, backend=backend)
        release, entered, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        prompt = None
        class ObservedExpectation:
            def __init__(self, locator): self.assertion = real_expect(locator)
            def __getattr__(self, name): return getattr(self.assertion, name)
            async def to_have_attribute(self, name, value, **options):
                if name == 'data-catalog-state': waiting.set()
                return await self.assertion.to_have_attribute(name, value, **options)
        try:
            prepared = await owner.start()
            page = prepared.session.page
            await expect(page.locator('#model-status')).to_have_attribute('data-catalog-state', 'settled')
            await page.evaluate("""model => localStorage.setItem('api-agent-studio-v2', JSON.stringify({
                session: crypto.randomUUID(), messages: [], draft: '', reply: null,
                settings: {model, version: '', reasoning: ''}
            }))""", saved)
            async def hold_models(route):
                entered.set()
                await release.wait()
                if outcome == 'failure':
                    await route.fulfill(status=400, content_type='application/json', body='{"error":{"code":"invalid_request","message":"synthetic-private-detail"}}')
                else:
                    await route.continue_()
            await page.route('**/app/agent/models', hold_models)
            await page.reload(wait_until='domcontentloaded')
            await entered.wait()
            await expect(page.locator('#model')).to_have_value(saved)
            await expect(page.locator('#model-status')).to_have_attribute('data-catalog-state', 'loading')
            monkeypatch.setattr(playwright.async_api, 'expect', ObservedExpectation)
            prompt = asyncio.create_task(connection.browser_prompt(prepared))
            observation = asyncio.create_task(waiting.wait())
            done, _ = await asyncio.wait({prompt, observation}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            try:
                assert observation in done and waiting.is_set()
                assert not prompt.done()
                assert await page.locator('#prompt').input_value() == ''
                assert provider.calls == []
                assert await page.locator('.user').count() == 0
            finally:
                observation.cancel()
                await asyncio.gather(observation, return_exceptions=True)
            release.set()
            if outcome == 'valid':
                assert await prompt == 'Ok'
                assert len(provider.calls) == 1
                assert provider.calls[0].selected_llm == saved
                assert await page.locator('.user').count() == 1
            else:
                message = {'unknown': 'support for this model is unknown', 'unsupported': 'unsupported', 'failure': 'HTTP 400'}[outcome]
                with pytest.raises(AssertionError, match=message): await prompt
                assert provider.calls == []
                assert await page.locator('#prompt').input_value() == ''
                assert 'synthetic-private-detail' not in await page.locator('#model-status').inner_text()
            await expect(page.locator('#model-status')).to_have_attribute('data-catalog-state', 'settled')
            assert await page.locator('#model').input_value() == saved
        finally:
            release.set()
            if prompt is not None:
                if not prompt.done(): prompt.cancel()
                await asyncio.gather(prompt, return_exceptions=True)
            await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('state', [None, 'unexpected'])
def test_missing_catalog_readiness_fails_bounded_before_prompt(connection, tmp_path, monkeypatch, state):
    import playwright.async_api
    real_expect = playwright.async_api.expect
    class BoundedExpectation:
        def __init__(self, locator): self.assertion = real_expect(locator)
        def __getattr__(self, name): return getattr(self.assertion, name)
        async def to_have_attribute(self, name, value, **options):
            assert name == 'data-catalog-state' and value == 'settled'
            assert options['timeout'] == 15000
            return await self.assertion.to_have_attribute(name, value, timeout=30)
    async def run():
        provider = MixedProvider()
        backend = connection.ui_provider_backend.__wrapped__(None, provider, 'sample')
        owner = UIIntegrationOwner({'config': {'UI_LISTENER_URL': 'http://127.0.0.1:9443',
            'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider'},
            'owner_root': str(tmp_path)}, None, backend=backend)
        try:
            prepared = await owner.start()
            page = prepared.session.page
            await expect(page.locator('#model-status')).to_have_attribute('data-catalog-state', 'settled')
            await page.locator('#model-status').evaluate("(element, value) => value === null ? element.removeAttribute('data-catalog-state') : element.setAttribute('data-catalog-state', value)", state)
            monkeypatch.setattr(playwright.async_api, 'expect', BoundedExpectation)
            with pytest.raises(AssertionError, match='Studio catalog did not settle:'):
                await connection.browser_prompt(prepared)
            assert await page.locator('#prompt').input_value() == ''
            assert provider.calls == []
        finally:
            await owner.aclose()
    asyncio.run(run())
