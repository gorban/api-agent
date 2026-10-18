"""Select a different model and effort through the original Studio controls."""
from pathlib import Path
import json
import re

import pytest

from api_agent.providers.loading import provider_location
from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.llm_providers.gpt import factory as gpt
from api_agent.llm_providers.gpt.endpoint import OFFICIAL_BASE_URL
from api_agent.providers.progress import component_timeout, report_timeout_sync
from test_studio_request import studio_configuration, studio_backend_factory, browser_prompt


pytestmark = pytest.mark.feature('Studio model selection')
DEFAULT_MODEL = 'gpt-6-sol'
SELECTED_MODEL = 'gpt-5.6-luna'
DEFAULT_EFFORT = 'medium'


def require_selection_configuration(requirements):
    """Reject unsupported routes/policies before requesting provider fixtures."""
    values = requirements.values
    selected, factory = provider_location(values['LLM_PROVIDER'], owner_root=requirements.owner_root)
    requirements.require_combination(
        selected.resolve() == Path(gpt.__file__).resolve() and factory == 'factory',
        'Studio selection requires the included GPT provider.')
    requirements.require_combination(
        (values.get('GPT_AUTH_MODE', 'subscription') == 'subscription'
         and values.get('GPT_API_MODE', 'responses') == 'responses')
        or (values.get('GPT_AUTH_MODE') == 'api_key'
            and values.get('GPT_BASE_URL', OFFICIAL_BASE_URL) == OFFICIAL_BASE_URL
            and values.get('GPT_API_MODE', 'completions') in ('responses', 'completions')),
        'Studio selection requires an official GPT route and supported API mode.')
    policy = ModelSelectionPolicy(values)
    for model in (DEFAULT_MODEL, SELECTED_MODEL):
        policy.require_allowed(model)


@pytest.fixture
def selection_configuration(studio_configuration, integration_config):
    require_selection_configuration(integration_config)


@pytest.fixture
def selection_catalog(selection_configuration, llm_provider):
    """Prepare the selected provider before the UI's short catalog fetch."""
    observed = SelectionProvider(llm_provider)
    with report_timeout_sync('Studio selection catalog preparation', 300):
        catalog = observed.describe()
    for identifier, family, version, effort in (
            (DEFAULT_MODEL, 'gpt-sol', '6', DEFAULT_EFFORT),
            (SELECTED_MODEL, 'gpt-luna', '5.6', 'off')):
        rows = [row for row in catalog if row.selected_llm == identifier]
        assert len(rows) == 1, 'Required exact model must be uniquely advertised: ' + identifier
        row = rows[0]
        assert (row.family, row.version) == (family, version), 'Required model family/version is unavailable: ' + identifier
        assert row.reasoning_efforts is not None and effort in row.reasoning_efforts, 'Required reasoning effort is unavailable: ' + identifier
    return observed


@pytest.fixture
def ui_provider_backend(selection_catalog):
    observed = selection_catalog
    backend = studio_backend_factory(observed, DEFAULT_MODEL, backend_config={
        'LLM_DEFAULT_MODEL': re.escape(DEFAULT_MODEL), 'LLM_REASONING_EFFORT': DEFAULT_EFFORT})
    backend.observed = observed
    return backend


class SelectionProvider:
    """Keep normal accounting; unavailable runtime setup is a failed integration."""
    def __init__(self, provider):
        self.provider, self.calls = provider, []

    def describe(self):
        try:
            return self.provider.describe()
        except pytest.skip.Exception as error:
            raise AssertionError('Studio selection catalog setup was unavailable.') from error

    def complete(self, request):
        try:
            result = self.provider.complete(request)
        except pytest.skip.Exception as error:
            raise AssertionError('Studio selection inference setup was unavailable.') from error
        self.calls.append((request, result))
        return result


async def selection_prompt(prepared):
    """Exercise family, version and effort controls; observe actual outgoing JSON."""
    from playwright.async_api import expect
    page = prepared.session.page
    async with component_timeout('Studio settings selection', 30):
        await expect(page.locator('#model-status')).to_have_attribute('data-catalog-state', 'settled', timeout=15000)
        await expect(page.locator('#model-status')).to_have_text('')
        # This is the backend's default, before any control changes.
        catalog = await page.evaluate('''async () => {
            const config = await (await fetch('/config.json')).json();
            const response = await fetch(config.apiOrigin + '/app/agent/models', {headers: config.apiHeaders});
            if (!response.ok) throw Error('Model catalog HTTP ' + response.status);
            return response.json();
        }''')
        assert catalog['default'] == DEFAULT_MODEL
        assert catalog['reasoning_default'] == DEFAULT_EFFORT
        await page.locator('#settings-nav').click()
        family = page.locator('#model option').filter(has_text=re.compile(r'^gpt-luna$', re.I))
        await expect(family).to_have_count(1)
        await page.locator('#model').select_option(await family.get_attribute('value'))
        await page.locator('#version').select_option(SELECTED_MODEL)
        await expect(page.locator('#version option:checked')).to_have_text('5.6')
        await page.locator('#reasoning').select_option('off')
        await expect(page.locator('#model-status')).to_have_text('')
        await page.locator('#close-settings').click()
    async with page.expect_request(lambda request: request.method == 'POST' and request.url.endswith('/app/agent/chat/stream')) as outgoing:
        answer = await browser_prompt(prepared, prompt='Respond with exactly: Maybe')
    request = (await outgoing.value).post_data_json
    assert request['model'] == SELECTED_MODEL != DEFAULT_MODEL
    assert request['reasoning_effort'] == 'off' != DEFAULT_EFFORT
    assert request['messages'][-1] == {'role': 'user', 'content': 'Respond with exactly: Maybe'}
    assert answer.strip() == 'Maybe'
    return request


@pytest.mark.requires_ui_session
@pytest.mark.requires_llm
def test_studio_selection(ui_provider_session, ui_provider_loop, ui_provider_backend):
    from api_agent.providers.inference import usage_to_wire
    outgoing = ui_provider_loop.run_until_complete(selection_prompt(ui_provider_session))
    assert len(ui_provider_backend.observed.calls) == 1
    request, result = ui_provider_backend.observed.calls[0]
    assert request.selected_llm == SELECTED_MODEL
    assert request.payload.generation.reasoning_effort == 'off'
    assert result.usage is not None, 'Completed selection must report normal provider usage.'
    print('Studio selection evidence: ' + json.dumps({'backend_default': DEFAULT_MODEL, 'backend_reasoning_default': DEFAULT_EFFORT,
        'requested_model': outgoing['model'], 'requested_version': '5.6',
        'requested_reasoning': outgoing['reasoning_effort'], 'usage': usage_to_wire(result.usage)}, sort_keys=True))
