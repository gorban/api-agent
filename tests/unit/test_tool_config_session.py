"""Actual launch configuration retains the selected graph's tool membership."""
from dataclasses import replace
from pathlib import Path

import pytest

from api_agent.config import schema_from_dict
from api_agent.config.session import ConfigSession
from api_agent.testing.collection import TestIdentity
from api_agent.tooling.models import OperationSelection
from test_tool_selection import selection_fixture

PRIMARY = 'base::tests/test_flow.py::test_primary'
DUAL = 'base::tests/test_flow.py::test_dual'


def session_for(fixture, *, argv=(), environ=None):
    context, base = fixture
    additions = schema_from_dict({'version': 1, 'fields': {
        'TUI_MODE': {'type': 'str', 'acknowledge_default': False},
        'SKIP_UNATTENDED_SETUP': {'type': 'bool', 'default': True, 'acknowledge_default': False}}})
    schema = type(base)({**base.fields, **additions.fields})
    return ConfigSession(context, schema, argv=argv, environ={} if environ is None else environ)


def test_session_rebuilds_selected_slots_and_retains_dormant_saved_values(selection_fixture):
    session = session_for(selection_fixture)
    assert set(session.result(_review=True).missing) == {'API_ENDPOINT'}
    assert 'API_2_ENDPOINT' not in session.schema.fields

    session.select_operation((DUAL,))
    session.set_value('SECONDARY_IMPLEMENTATION', 'rest.toml')
    session.set_value('API_2_PATH', '/custom', persist=True)
    selected = session.result(_review=True)

    assert 'API_2_PATH' in selected.fields
    assert 'API_2_HEADERS' not in session.schema.fields
    assert {'API_ENDPOINT', 'API_2_ENDPOINT'} <= set(selected.missing)
    assert 'SECONDARY_IMPLEMENTATION' not in {item.name for item in session.default_candidates()}

    session.select_operation()
    restored = session.result(_review=True)
    assert 'API_2_ENDPOINT' not in session.schema.fields
    assert set(restored.missing) == {'API_ENDPOINT'}
    from api_agent.config.dotenv import read_dotenv
    assert read_dotenv(session.dotenv_path)['API_2_PATH'] == '/custom'
    assert 'API_2_PATH' in {row.name for row in session.cleanup_rows() if row.present}


def test_collection_parses_tool_option_arity_then_rejects_inactive_explicit_cli(selection_fixture):
    session = session_for(selection_fixture, argv=(
        '--tui-mode', 'integration', '--api-2-endpoint', 'https://two.invalid', DUAL))

    collection = session.result()
    assert collection.positionals == (DUAL,)
    assert not collection.errors and not collection.missing

    session.select_operation((PRIMARY,))
    inactive = session.result()
    assert inactive.positionals == (DUAL,)
    assert 'API_2_ENDPOINT: inactive for the selected workflow/test context' in inactive.errors

    session.select_operation((DUAL,))
    active = session.result()
    assert not active.errors
    assert active.values['API_2_ENDPOINT'] == 'https://two.invalid'
    assert active.tool_plan.selection.test_ids == (DUAL,)


def test_schema_aware_deferred_parse_preserves_duplicate_and_unknown_errors(selection_fixture):
    session = session_for(selection_fixture, argv=(
        '--tui-mode=integration', '--api-2-endpoint=one', '--api-2-endpoint=two', '--bogus', DUAL))
    session.select_operation((DUAL,))

    result = session.result()

    assert result.errors.count('Duplicate option --api-2-endpoint') == 1
    assert 'Unknown option --bogus' in result.errors
    assert result.values['API_2_ENDPOINT'] == 'one'
    assert result.positionals == (DUAL,)


def test_missing_inactive_option_value_defers_only_until_exact_selection(selection_fixture):
    session = session_for(selection_fixture, argv=('--tui-mode=integration', '--api-2-endpoint'))

    assert not session.result().errors
    result = session.select_operation((PRIMARY,))

    assert result.errors == ('API_2_ENDPOINT: option requires a value',)


def test_binding_retains_option_errors_when_caller_has_already_filtered_its_errors(selection_fixture):
    from api_agent.config.resolve import resolve_config
    from api_agent.config.tool_resolution import bind_operation_tools
    from api_agent.tooling.selection import compose_tools
    context, schema = selection_fixture
    plan = compose_tools(context, schema, environ={})
    result = replace(resolve_config(plan.schema, environ={}), errors=('Retained caller error',))

    bound = bind_operation_tools(plan, result, argv=('--unknown',))

    assert bound.errors == ('Retained caller error', 'Unknown option --unknown')


def test_shared_profile_conflict_becomes_a_configuration_error(selection_fixture):
    context, _ = selection_fixture
    text = context.tool_catalog_path.read_text().replace('entry="dual_graph.md.html"',
        'entry="dual_graph.md.html"\nprofile="shared"')
    context.tool_catalog_path.write_text(text + '\n[profiles.shared]\nversion=1\nkind="shared-primary"\n'
        'primary="sample/primary"\nsecondary=["sample/secondary"]\n')
    session = session_for(selection_fixture, environ={'API_ENDPOINT': 'https://one.invalid',
                                                     'API_2_ENDPOINT': 'https://two.invalid'})

    result = session.select_operation((DUAL,))

    assert any('conflicts' in error.lower() for error in result.errors)
    assert result.tool_plan is None


def test_dormant_invalid_environment_does_not_block_primary(selection_fixture):
    session = session_for(selection_fixture, environ={'API_2_ENDPOINT': 'saved', 'API_2_HEADERS': 'not-json'})

    result = session.select_operation((PRIMARY,))

    assert not result.errors
    assert 'API_2_HEADERS' not in result.fields
    assert session.environ['API_2_HEADERS'] == 'not-json'


@pytest.mark.asyncio
async def test_actual_review_rows_change_with_case_and_implementation(selection_fixture):
    from api_agent.tui.app import ApiAgentApp
    from textual.widgets import DataTable
    session = session_for(selection_fixture)
    app = ApiAgentApp(session.context, session.schema, session=session)
    async with app.run_test(size=(110, 35)) as pilot:
        app.review_names.update(session.schema.fields)
        app.show_configuration()
        await pilot.pause()
        assert 'API_ENDPOINT' in {str(key.value) for key in app.screen.query_one(DataTable).rows}
        assert 'API_2_ENDPOINT' not in {str(key.value) for key in app.screen.query_one(DataTable).rows}

        session.select_operation((DUAL,))
        app.show_configuration()
        await pilot.pause()
        assert 'API_2_ENDPOINT' in {str(key.value) for key in app.screen.query_one(DataTable).rows}
        session.set_value('SECONDARY_IMPLEMENTATION', 'rest.toml')
        app.show_configuration()
        await pilot.pause()
        names = {str(key.value) for key in app.screen.query_one(DataTable).rows}
        assert 'API_2_PATH' in names and 'API_2_HEADERS' not in names

        session.select_operation()
        app.show_configuration()
        await pilot.pause()
        assert 'API_2_ENDPOINT' not in {str(key.value) for key in app.screen.query_one(DataTable).rows}


@pytest.mark.parametrize('conditional', [False, True])
def test_effective_default_test_mode_does_not_require_application_tools(selection_fixture, conditional):
    context, base = selection_fixture
    mode = {'type': 'str', 'acknowledge_default': False}
    if conditional:
        mode['conditional_defaults'] = [{'when': {'SHARED': 'shared'}, 'value': 'integration'}]
    else:
        mode['default'] = 'integration'
    additions = schema_from_dict({'version': 1, 'fields': {
        'TUI_MODE': mode, 'START': {'type': 'str', 'default': 'True'}}}, base_schema=base)
    session = ConfigSession(context, type(base)({**base.fields, **additions.fields}), environ={})

    result = session.result()

    assert result.values['TUI_MODE'] == 'integration'
    assert result.tool_plan.selection.mode == 'collection'
    assert 'API_ENDPOINT' not in result.missing


def test_selected_ui_intent_is_shared_by_tool_projection_and_review(selection_fixture):
    context, _ = selection_fixture
    implementation = context.base_root / 'graphql.toml'
    implementation.write_text(implementation.read_text() + '\n[inputs.ENDPOINT.applies_when]\noperation="ui_session"\n')
    session = session_for(selection_fixture)
    identity = TestIdentity(PRIMARY, 'base', 'tests/test_flow.py', 'tests/test_flow.py::test_primary',
                            tags=frozenset({'requires_ui_session'}))

    result = session.select_operation((identity,))

    assert result.missing == ('API_ENDPOINT',)
    assert 'API_ENDPOINT' in session.review_field_names()
    assert 'API_ENDPOINT' in session.result(_review=True).missing


@pytest.mark.asyncio
async def test_close_selected_test_review_returns_to_picker_without_stale_context(selection_fixture):
    from api_agent.tui.app import ApiAgentApp
    from api_agent.testing.selection import SelectionModel
    session = session_for(selection_fixture)
    app = ApiAgentApp(session.context, session.schema, session=session)
    identity = TestIdentity(DUAL, 'base', 'tests/test_flow.py', 'tests/test_flow.py::test_dual')
    async with app.run_test(size=(110, 35)) as pilot:
        app.mode = 'integration'
        app.selection = SelectionModel((identity,))
        app.show_selection()
        await app.choose('review-config')
        await pilot.pause()
        assert app.screen.kind == 'defaults'

        await pilot.press('c')
        await pilot.pause()
        assert app.screen.kind == 'tests'
        assert app.session.tool_plan.selection.test_ids == (DUAL,)
        assert app._review_return_to_tests is False

        app.show_menu()
        await pilot.pause()
        assert app.session.tool_plan.selection.mode == 'application'
        assert app._review_return_to_tests is False


def test_frozen_json_defaults_display_as_json():
    from types import MappingProxyType
    from api_agent.tui.app import display

    assert display(MappingProxyType({'nested': (MappingProxyType({'ready': True}),)})) == '{"nested":[{"ready":true}]}'


@pytest.mark.asyncio
@pytest.mark.parametrize('visible', [True, False])
async def test_selected_missing_tool_input_returns_to_review_or_blocks(selection_fixture, visible):
    from api_agent.tui.app import ApiAgentApp
    from api_agent.testing.selection import SelectionModel
    from textual.widgets import DataTable, Static
    if not visible:
        context, _ = selection_fixture
        implementation = context.base_root / 'graphql.toml'
        implementation.write_text(implementation.read_text().replace('required=true', 'required=true\nacknowledge_default=false'))
    session = session_for(selection_fixture, argv=('--tui-mode=integration',))
    app = ApiAgentApp(session.context, session.schema, session=session)
    identity = TestIdentity(DUAL, 'base', 'tests/test_flow.py', 'tests/test_flow.py::test_dual')
    async with app.run_test(size=(110, 35)) as pilot:
        app.mode = 'integration'
        app.selection = SelectionModel((identity,))
        app.show_selection()
        await pilot.pause()
        await app.choose('confirmed-continue')
        await pilot.pause()
        if visible:
            assert app.screen.kind == 'defaults'
            assert app._review_return_to_tests is True
            assert {'API_ENDPOINT', 'API_2_ENDPOINT'} <= app.review_names
            rows = {str(key.value) for key in app.screen.query_one(DataTable).rows}
            assert {'API_ENDPOINT', 'API_2_ENDPOINT'} <= rows
        else:
            assert app.screen.kind == 'tests'
            assert 'Missing required configuration: API_ENDPOINT, API_2_ENDPOINT' in str(app.screen.query_one('#error', Static).render())
        assert not app.test_run_active()


def test_legacy_session_selection_uses_its_current_result():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from api_agent.tui.app import ApiAgentApp
    result = object()
    session = SimpleNamespace(result=Mock(return_value=result))

    assert ApiAgentApp.select_tool_context(SimpleNamespace(session=session), (PRIMARY,)) is result
    session.result.assert_called_once_with()


@pytest.mark.asyncio
async def test_recollected_integration_rejects_new_missing_configuration(tmp_path):
    from test_tui import fixture
    from dataclasses import replace
    from unittest.mock import Mock
    identity = TestIdentity('base::test_flow.py::test_primary', 'base', 'test_flow.py', 'test_flow.py::test_primary')
    app = fixture(tmp_path, 'integration', collector=lambda *args: [identity])
    app.integration_runner = Mock(side_effect=AssertionError('invalid configuration must prevent execution'))
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        original = app.session.result
        def selected_result(**kwargs):
            result = original(**kwargs)
            return replace(result, missing=('SELECTED_REQUIRED',)) if 'operation_intents' in kwargs else result
        app.session.result = selected_result
        await app.choose('confirmed-continue')
        await pilot.pause(.5)

        assert app.screen.kind == 'error'
        assert 'Missing required configuration: SELECTED_REQUIRED' in app.screen.body
        assert app.operation_status == 3
        app.integration_runner.assert_not_called()
