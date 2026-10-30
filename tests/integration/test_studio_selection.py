"""Select a different model and effort through the original Studio controls."""
import json
import re

import pytest

from api_agent.providers.progress import component_timeout, report_timeout_sync
from api_agent.testing.model_choices import exact_model, catalog_choice
from test_studio_request import studio_configuration, studio_backend_factory, browser_prompt


pytestmark = [pytest.mark.feature('Studio model selection'), pytest.mark.credential_authorities('ui')]


def require_selection_configuration(requirements):
    """Reject incomplete choices before requesting provider fixtures."""
    values = requirements.values
    default = exact_model(values, 'LLM_STUDIO_TEST_DEFAULT_MODEL')
    selected = exact_model(values, 'LLM_STUDIO_TEST_SELECTED_MODEL')
    efforts = tuple(values.get(key, '') for key in (
        'LLM_STUDIO_TEST_DEFAULT_EFFORT', 'LLM_STUDIO_TEST_SELECTED_EFFORT'))
    requirements.require_combination(default != selected, 'Studio selection requires two distinct exact models.')
    requirements.require_combination(
        all(type(effort) is str and effort in ('', 'off', 'low', 'medium', 'high') for effort in efforts)
        and efforts[0] != efforts[1], 'Studio selection requires two distinct supported effort choices.')
    return default, selected, *efforts


@pytest.fixture
def selection_configuration(studio_configuration, integration_config):
    return require_selection_configuration(integration_config)


@pytest.fixture
def selection_catalog(selection_configuration, llm_provider):
    """Prepare the selected provider before the UI's short catalog fetch."""
    observed = SelectionProvider(llm_provider)
    with report_timeout_sync('Studio selection catalog preparation', getattr(llm_provider, 'config', {}).get('LLM_SETUP_REQUEST_TIMEOUT_SECONDS', 300)):
        catalog = observed.describe()
    default, selected, default_effort, selected_effort = selection_configuration
    observed.choices = selection_configuration
    observed.rows = tuple(catalog_choice(catalog, model, (effort,), require_metadata=False) for model, effort in (
        (default, default_effort), (selected, selected_effort)))
    assert all(row.family for row in observed.rows), 'Required advertised model family for Studio controls.'
    observed.catalog = catalog
    return observed


@pytest.fixture
def ui_provider_backend(selection_catalog):
    observed = selection_catalog
    default, selected, default_effort, selected_effort = observed.choices
    backend = studio_backend_factory(observed, default, backend_config={
        'LLM_DEFAULT_MODEL': re.escape(default), 'LLM_REASONING_EFFORT': default_effort})
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


async def selection_prompt(prepared, observed):
    """Exercise family, version and effort controls; observe actual outgoing JSON."""
    from playwright.async_api import expect
    default, selected, default_effort, selected_effort = observed.choices
    selected_row = observed.rows[1]
    version_label = selected_row.version or 'Unknown version'
    same_version = sum(row.family == selected_row.family and row.version == selected_row.version for row in observed.catalog)
    if not selected_row.version or same_version > 1:
        version_label += ' — ' + selected_row.selected_llm
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
        assert catalog['default'] == default
        assert catalog['reasoning_default'] == default_effort
        await page.locator('#settings-nav').click()
        family = page.locator('#model option').filter(has_text=re.compile('^' + re.escape(selected_row.family) + '$', re.I))
        await expect(family).to_have_count(1)
        await page.locator('#model').select_option(await family.get_attribute('value'))
        await page.locator('#version').select_option(selected)
        await expect(page.locator('#version option:checked')).to_have_text(version_label)
        await page.locator('#reasoning').select_option(selected_effort)
        await expect(page.locator('#model-status')).to_have_text('')
        await page.locator('#close-settings').click()
    async with page.expect_request(lambda request: request.method == 'POST' and request.url.endswith('/app/agent/chat/stream')) as outgoing:
        answer = await browser_prompt(prepared, prompt='Respond with exactly: Maybe')
    request = (await outgoing.value).post_data_json
    assert request['model'] == selected != default
    assert request['reasoning_effort'] == selected_effort != default_effort
    assert request['messages'][-1] == {'role': 'user', 'content': 'Respond with exactly: Maybe'}
    assert answer.strip() == 'Maybe'
    return request


@pytest.mark.requires_ui_session
@pytest.mark.requires_llm
def test_studio_selection(ui_provider_session, ui_provider_loop, ui_provider_backend):
    from api_agent.providers.inference import usage_to_wire
    observed = ui_provider_backend.observed
    default, selected, default_effort, selected_effort = observed.choices
    outgoing = ui_provider_loop.run_until_complete(selection_prompt(ui_provider_session, observed))
    assert len(ui_provider_backend.observed.calls) == 1
    request, result = ui_provider_backend.observed.calls[0]
    assert request.selected_llm == selected
    assert request.payload.generation.reasoning_effort == selected_effort
    assert result.usage is not None, 'Completed selection must report normal provider usage.'
    print('Studio selection evidence: ' + json.dumps({'backend_default': default, 'backend_reasoning_default': default_effort,
        'requested_model': outgoing['model'], 'requested_version': observed.rows[1].version, 'family_changed': observed.rows[0].family != observed.rows[1].family,
        'requested_reasoning': outgoing['reasoning_effort'], 'usage': usage_to_wire(result.usage)}, sort_keys=True))
