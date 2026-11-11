"""A Studio browser turn traverses the launch-admitted application graph."""
from contextlib import asynccontextmanager

import pytest

from api_agent.runtime.local_tools import selected_application
from api_agent.testing.studio_browser import browser_prompt
from api_agent.tooling.runtime import bind_workflow_tools
from api_agent.testing.studio_http import studio_backend_factory

pytestmark = [pytest.mark.feature('Studio application E2E'),
              pytest.mark.requires_ui_session, pytest.mark.requires_llm,
              pytest.mark.credential_authorities('ui', 'workflow', 'tools')]


@pytest.fixture
def ui_provider_backend(integration_config, llm_provider, llm_selection, workflow_tools):
    """Use the same selected application adapter as Run local."""
    application = selected_application(workflow_tools.definition, config=integration_config.values)

    selected_backend = studio_backend_factory(llm_provider, llm_selection, application=application)

    @asynccontextmanager
    async def backend(values):
        async with workflow_tools.open(workflow_tools.definition) as operation:
            with bind_workflow_tools(operation.tools):
                async with selected_backend(values) as binding:
                    yield binding
    backend.retire_test = selected_backend.retire_test
    return backend


def test_studio_graph(ui_provider_session, ui_provider_loop):
    answer = ui_provider_loop.run_until_complete(browser_prompt(ui_provider_session))
    assert answer.strip().lower().rstrip('.') == 'ok'
