"""UI intent preserves existing test dependency semantics without inference."""
from types import SimpleNamespace
import pytest
from api_agent.testing.requirements import plan_integration_requirements


@pytest.mark.parametrize('tags,kinds', [([], set()), ([()], {'llm'}),
    ([('requires_ui_session',)], {'ui'}),
    ([('requires_ui_session', 'requires_llm')], {'ui','llm'}),
    ([('requires_ui_session',), ()], {'ui','llm'}),
    ([('requires_ui_session','requires_pack_api')], {'ui','api','upstream','access'})])
def test_explicit_provider_intent(tags, kinds):
    plan = plan_integration_requirements(SimpleNamespace(tags=item) for item in tags)
    assert plan.provider_kinds == kinds
    assert plan.requires_ui_session == ('ui' in kinds)
    assert plan.requires_llm == ('llm' in kinds)


def test_plan_validates_pack_only_when_consumed(monkeypatch):
    from unittest.mock import Mock
    from api_agent.auth import extensions
    validate = Mock()
    monkeypatch.setattr(extensions, 'validate_operation', validate)
    ui = plan_integration_requirements([SimpleNamespace(tags=('requires_ui_session',))])
    ui.validate('context', {})
    validate.assert_not_called()
    pack = plan_integration_requirements([SimpleNamespace(tags=('requires_pack_api',))])
    pack.validate('context', {'selected':True})
    validate.assert_called_once_with('context', {'selected':True})
