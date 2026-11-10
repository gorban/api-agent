"""Normal integration plans have no calibration-maintenance dependency."""
from types import SimpleNamespace
import pytest
from unittest.mock import Mock
from api_agent.testing.requirements import plan_integration_requirements


def test_workflow_coverage_needs_no_local_calibration_artifact(tmp_path):
    plan = plan_integration_requirements([SimpleNamespace(tags=frozenset())])
    assert not plan.requires_pack_api
    plan.validate(None, {})
    assert list(tmp_path.iterdir()) == []


def test_pack_dependency_remains_explicit(monkeypatch):
    validate = Mock()
    monkeypatch.setattr('api_agent.auth.extensions.validate_operation', validate)
    context, config = object(), {'API_ENDPOINT': 'https://example.test/graphql'}
    plan = plan_integration_requirements([SimpleNamespace(tags={'requires_pack_api'})])
    assert plan.requires_pack_api
    plan.validate(context, config)
    validate.assert_called_once_with(context, config)


@pytest.mark.parametrize('tag, expected', [
    ('requires_ui_session', {'ui'}), ('requires_owned_ui_session', {'ui'}), ('requires_pack_api', set())])
def test_ui_tests_prepare_workspaces_without_changing_provider_kinds(tag, expected):
    plan = plan_integration_requirements([SimpleNamespace(tags={tag})])
    assert plan.workspace_kinds == frozenset(expected)
    assert ('ui' in plan.provider_kinds) is (tag == 'requires_ui_session')
    assert plan_integration_requirements([SimpleNamespace(tags=frozenset())]).workspace_kinds == frozenset()
