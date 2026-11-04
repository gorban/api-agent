"""Owner defaults retain the composed catalog and scope coverage separately."""
from types import SimpleNamespace
import asyncio

import pytest

from api_agent.testing.collection import TestIdentity
from api_agent.testing.selection import SelectionModel
from api_agent.testing.unit_scope import owner_unit_only, unit_default_tests, unit_coverage_complete


def identity(repo, name):
    return TestIdentity(f'{repo}::{name}', repo, f'{name}.py', name)


def test_owner_defaults_select_only_owner_but_all_override_restores_catalog(tmp_path):
    context = SimpleNamespace(base_root=tmp_path / 'base', owner_root=tmp_path / 'owner')
    tests = (identity('base', 'a'), identity('dependency', 'b'), identity('owner', 'c'))
    assert owner_unit_only(context, {})
    assert not owner_unit_only(context, {'ALL_UNIT': True})
    assert unit_default_tests(context, tests) == tests[2:]
    assert unit_default_tests(context, tests, all_unit=True) == tests
    assert unit_coverage_complete(context, tests, tests[2:])
    assert not unit_coverage_complete(context, tests, tests[2:], all_unit=True)
    assert not unit_coverage_complete(context, tests, ())
    assert not unit_coverage_complete(context, tests[:2], tests[:2])


def test_base_owner_defaults_include_all_base_tests(tmp_path):
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    tests = (identity('base', 'a'),)
    assert not owner_unit_only(context, {})
    assert unit_default_tests(context, tests) == tests
    assert unit_coverage_complete(context, tests, tests)


def test_repository_scope_selects_new_owned_units_on_recollection():
    owner, dependency = identity('owner', 'a'), identity('dependency', 'b')
    selection = SelectionModel((owner, dependency))
    selection.set_defaults({'owner'})
    assert selection.tests == (owner, dependency)
    assert selection.selected_tests == (owner,)
    assert not selection.groups()[0].selected
    fresh_owner, fresh_dependency = identity('owner', 'c'), identity('dependency', 'd')
    selection.refresh_for_run((owner, dependency, fresh_owner, fresh_dependency))
    assert selection.selected_tests == (owner, fresh_owner)
    selection.toggle(owner.id)
    another = identity('owner', 'e')
    selection.refresh_for_run((owner, dependency, fresh_owner, fresh_dependency, another))
    assert selection.selected_tests == (fresh_owner,)


def test_all_defaults_and_explicit_selectors_replace_owner_scope():
    owner, dependency = identity('owner', 'a'), identity('dependency', 'b')
    selection = SelectionModel((owner, dependency))
    selection.set_defaults({'owner'})
    selection.preselect((dependency.id,))
    assert selection.selected_tests == (dependency,)
    selection.set_defaults()
    new = identity('dependency', 'c')
    selection.refresh_for_run((owner, dependency, new))
    assert selection.selected_tests == (owner, dependency, new)


def test_tui_defaults_and_configuration_change_preserve_explicit_selectors(tmp_path):
    from unittest.mock import Mock
    from api_agent.tui.app import ApiAgentApp
    context = SimpleNamespace(base_root=tmp_path / 'base', owner_root=tmp_path / 'owner')
    tests = (identity('base', 'a'), identity('owner', 'b'))
    values = {'ALL_UNIT': False}
    app = SimpleNamespace(mode='unit', active_test_selectors=(), context=context,
                          selection=SelectionModel(tests), session=SimpleNamespace(result=lambda: SimpleNamespace(values=values)))
    app.apply_unit_defaults = lambda: ApiAgentApp.apply_unit_defaults(app)
    app.apply_unit_defaults()
    assert app.selection.selected_tests == tests[1:]
    values['ALL_UNIT'] = True
    app._review_return_to_tests = True
    app.show_selection = Mock()
    ApiAgentApp.finish_configuration_review(app)
    assert app.selection.selected_tests == tests
    app.active_test_selectors = (tests[0].id,)
    app.selection.preselect(app.active_test_selectors)
    values['ALL_UNIT'] = False
    app._review_return_to_tests = True
    ApiAgentApp.finish_configuration_review(app)
    assert app.selection.selected_tests == tests[:1]


@pytest.mark.parametrize('entry', ['run', 'continue'])
@pytest.mark.parametrize('scope', ['owner', 'all', 'base'])
def test_tui_empty_selection_guidance_matches_effective_unit_scope(tmp_path, entry, scope):
    from unittest.mock import Mock
    from api_agent.testing.unit_scope import EMPTY_OWNER_UNITS
    from api_agent.tui.app import NO_TEST_SELECTION_MESSAGE

    async def run():
        from unittest.mock import AsyncMock
        import threading
        from api_agent.tui.app import ApiAgentApp
        tests = (identity('base', 'dependency'),)
        context = SimpleNamespace(base_root=tmp_path,
            owner_root=tmp_path if scope == 'base' else tmp_path / 'owner')
        selection = SelectionModel(tests)
        selection.set_defaults(set())
        error = Mock()
        app = SimpleNamespace(mode='unit', context=context, selection=selection,
            session=SimpleNamespace(result=lambda: SimpleNamespace(values={'ALL_UNIT': scope == 'all'})),
            unit_runner=Mock(), collection_cancel=threading.Event(),
            discover_tests=AsyncMock(return_value=tests), finish_test_run=Mock(), show=Mock(),
            inspection_cleanup_active=lambda: False,
            screen=SimpleNamespace(query_one=Mock(return_value=error)))
        if entry == 'run':
            await ApiAgentApp._run_units(app)
            message = app.show.call_args.args[2]
            generic = 'No selected tests remain after recollection'
            app.finish_test_run.assert_called_once()
        else:
            await ApiAgentApp.choose(app, 'continue')
            message = error.update.call_args.args[0]
            generic = NO_TEST_SELECTION_MESSAGE
        assert message == (EMPTY_OWNER_UNITS if scope == 'owner' else generic)
        app.unit_runner.assert_not_called()
    asyncio.run(run())
