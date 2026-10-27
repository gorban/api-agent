"""Ten real workflow cases; legacy calibration is explicit maintenance only."""
import pytest
from api_agent.providers.progress import report_timeout_sync
from api_agent.testing.model_choices import exact_model, catalog_choice
from api_agent.workflows.fixture_driver import coverage_preflight, node_coverage

pytestmark = pytest.mark.feature('Workflow execution')


def test_workflow_node_coverage(request, integration_config):
    config = integration_config.values
    owner_root = getattr(integration_config, 'owner_root', None)
    with report_timeout_sync('Workflow integration test', config.get('LLM_WORKFLOW_TEST_TIMEOUT_SECONDS', 600)) as budget:
        coverage_preflight(config)
        selection = exact_model(config, 'LLM_WORKFLOW_TEST_MODEL')
        if budget.remaining == 0:
            raise TimeoutError('Workflow test deadline expired before provider setup')
        provider = request.getfixturevalue('llm_provider')
        catalog_choice(provider.describe(), selection, (config.get('LLM_REASONING_EFFORT') or '',),
                       require_metadata=False)
        node_coverage(provider, selection, config, owner_root=owner_root)
