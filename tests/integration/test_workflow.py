"""Mandatory permanent fixture coverage; pilot refresh is an explicit CLI action."""
import pytest
from api_agent.testing.model_choices import exact_model, catalog_choice
from api_agent.workflows.fixture_driver import (
    baseline_limit, fingerprint, node_coverage, preflight_baselines, run_graph,
)


pytestmark = pytest.mark.feature('Workflow execution')

def test_workflow_node_coverage(request, integration_config):
    # Refresh reviewed bounds explicitly with the workflow calibration command;
    # ordinary collection never includes the long pilot or reads a local receipt.
    config = integration_config.values
    owner_root = getattr(integration_config, 'owner_root', None)
    preflight_baselines(config, owner_root=owner_root)
    selection = exact_model(config, 'LLM_WORKFLOW_TEST_MODEL')
    baseline_limit(selection, config, owner_root=owner_root)
    provider = request.getfixturevalue('llm_provider')
    catalog_choice(provider.describe(), selection, (config.get('LLM_REASONING_EFFORT') or '',),
                   require_metadata=False)
    node_coverage(provider, selection, config, owner_root=owner_root)
