"""Browser proof for the optional-summary graph, not the default application."""
import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from api_agent.providers.llm import LLMRequest
from api_agent.testing.studio_browser import browser_prompt
from api_agent.testing.studio_http import studio_backend_factory
from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import NodeFunction, Workflow

pytestmark = [pytest.mark.feature('Studio optional node summaries'),
              pytest.mark.requires_ui_session, pytest.mark.requires_llm,
              pytest.mark.credential_authorities('ui', 'workflow')]

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / 'tests/e2e/fixtures/node_summary/flow.md.html'


class SummaryApplication:
    def __init__(self, config, observed):
        self.config, self.observed = config, observed
        self.definition = load_definition(ENTRY, base_root=ROOT, pack_root=ROOT)
        self.registry = {}
        for step in self.definition.layout.steps:
            row = next(row for row in self.definition.layout.tables['Functions'] if row['ID'] == step.id)
            function = load_native_catalog(self.definition, step.source)[row['Registry']]
            self.registry[row['Registry']] = NodeFunction(function, row['Kind'])

    async def complete(self, request: LLMRequest, *, llm, context):
        async def complete(value):
            return await llm.complete(value, context=context)
        async def before_final():
            from api_agent.providers.progress import component_timeout
            async with component_timeout('Browser observes node reasoning', 20):
                await self.observed.wait()
        results = {}
        workflow = Workflow(self.definition, self.registry, config=self.config,
            services={'selection': request.selected_llm, 'generation': request.payload.generation,
                'llm': complete, 'before_final': before_final, 'result_store': results})
        prompt = '\n'.join(part.text for message in request.payload.messages for part in message.content)
        await workflow.arun({'prompt': prompt})
        return results['result']


@pytest.fixture
def reasoning_observed():
    return asyncio.Event()


@pytest.fixture
def ui_provider_backend(integration_config, llm_provider, llm_selection, reasoning_observed):
    app = SummaryApplication(integration_config.values, reasoning_observed)
    selected = studio_backend_factory(llm_provider, llm_selection, application=app)
    @asynccontextmanager
    async def backend(values):
        async with selected(values) as binding:
            yield binding
    backend.retire_test = selected.retire_test
    return backend


def test_studio_reasoning(ui_provider_session, ui_provider_loop, reasoning_observed):
    """The first visible prefix survives final-answer replacement and expansion."""
    async def scenario():
        from playwright.async_api import expect
        async def observe(page):
            preview = page.locator('.assistant .reasoning-preview').last
            await page.wait_for_function("""() => {
                const row=[...document.querySelectorAll('.assistant')].at(-1);
                return row && (row.querySelector('.reasoning-preview')?.textContent.trim()
                    || !row.classList.contains('draft'));
            }""",timeout=120000)
            assert await preview.count(), await page.locator('.assistant .message-body').last.inner_text()
            await expect(preview).to_be_visible(timeout=1000)
            await expect(preview).to_have_text(re.compile(r'\S'), timeout=10000)
            captured = await preview.inner_text()
            assert await page.locator('.assistant .thinking-indicator').last.is_visible()
            reasoning_observed.set()
            await expect(page.locator('.assistant .metadata-grid dd').last).to_have_text('Complete', timeout=120000)
            details = page.locator('.assistant details').last
            await expect(details).not_to_have_attribute('open', '')
            await details.locator('summary').click()
            retained = details.locator('.reasoning-summary')
            await expect(retained).to_be_visible()
            assert captured in await retained.inner_text()
            assert await page.locator('.assistant .reasoning-preview').count() == 0
        try:
            answer = await browser_prompt(ui_provider_session,
                prompt='Check that 17 times 19 equals 323. Return the answer field exactly Ok.',
                observe_response=observe)
            assert answer.strip() == 'Ok'
        finally:
            reasoning_observed.set()
    ui_provider_loop.run_until_complete(scenario())
