"""Standalone review keeps settings reachable and returns to the main menu."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.color import Color
from textual.widgets import DataTable

from api_agent.config import compose_schema, load_declarations, load_shared_schema
from api_agent.testing import TestIdentity as Identity
from api_agent.tui.app import ApiAgentApp
from test_tui import fixture


@pytest.mark.parametrize('explicit_mode', [False, True])
@pytest.mark.parametrize('size', [(80, 24), (80, 12)])
def test_startup_review_reaches_requested_destination_and_valid_operation_ignores_unsaved_defaults(tmp_path, explicit_mode, size):
    root = Path(__file__).resolve().parents[2]
    shared = load_shared_schema(root)
    schema = compose_schema(shared, load_declarations(root / 'demo/cfg/config.toml', base_schema=shared, source='pack_default'))
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path / 'demo', owner_root=tmp_path,
        pack_id='demo', external_pack=False, dotenv_path=tmp_path / '.env',
        anchors={name: tmp_path for name in ('base_root', 'pack_root', 'owner_root')}, suite_paths=lambda mode: ())
    argv = (['--tui-mode=integration', '--llm-provider=' + str(root / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory',
             '--gpt-state-dir=.provider-state/gpt', '--allure-s3-use-localstack=True'] if explicit_mode else [])
    app = ApiAgentApp(context, schema, argv=argv, environ={'ALLURE_S3_USE_LOCALSTACK': 'True'},
        collector=lambda *args, **kwargs: [Identity('base', root, 'tests/integration/test_sample.py', 'test_sample', 'integration')],
        container_runtime=SimpleNamespace(inventory=lambda: ()))

    async def scenario():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            if app.screen.kind == 'suggest':
                await app.choose('skip')
                await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert 'Unsaved defaults' in app.screen.body
            assert app.screen._bindings.key_to_bindings['c'][0].description == 'Close'
            table = app.screen.query_one(DataTable)
            assert table.row_count > 0
            viewport = app.screen.query_one('#workbench-content')
            assert min(table.region.bottom, viewport.region.bottom) - max(table.region.y, viewport.region.y) >= 3
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == ('tests' if explicit_mode else 'menu')
            assert not (tmp_path / '.env').exists()
            app.show_menu()
            await app.choose('mode-integration')
            await pilot.pause()
            assert app.screen.kind == 'tests'
            assert not (tmp_path / '.env').exists()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
@pytest.mark.parametrize('size', [(80, 12), (100, 35)])
def test_menu_operation_review_repairs_required_value_and_continues_to_picker(tmp_path, mode, size):
    from unittest.mock import Mock
    from textual.widgets import Input
    collector = Mock(return_value=[Identity('base::a.py::test_one', 'base', 'a.py', 'a.py::test_one')])
    app = fixture(tmp_path, fields={
        'REQUIRED': {'type': 'int', 'required_when': {'TUI_MODE': mode}},
        'COUNT': {'type': 'int', 'default': 8}}, collector=collector)

    async def scenario():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            app.show_menu()
            await app.choose('mode-' + mode)
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.review_destination == mode
            assert not app.startup_review
            assert app.screen._bindings.key_to_bindings['c'][0].description == 'Close'
            assert 'main menu' not in app.screen.body
            assert 'c close / menu' not in app.screen.body
            assert 'Required settings: REQUIRED' in app.screen.body
            collector.assert_not_called()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.review_destination == mode
            assert 'REQUIRED' in app.session.result().missing
            error = app.screen.query_one('#error')
            assert str(error.content) == f'REQUIRED required for TUI_MODE={mode}.'
            assert error.styles.color == Color.parse(app.get_css_variables()['error'])
            collector.assert_not_called()
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=next(i for i, row in enumerate(table.ordered_rows) if row.key.value == 'REQUIRED'))
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.kind == 'input'
            app.screen.query_one(Input).value = 'invalid'
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen.kind == 'input'
            assert 'REQUIRED' in app.session.result().missing
            collector.assert_not_called()
            app.screen.query_one(Input).value = '7'
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert 'REQUIRED="7"' in (tmp_path / '.env').read_text()
            assert not app.session.result().missing
            assert 'COUNT' in app.configuration_pending()
            assert str(app.screen.query_one('#error').content) == ''
            collector.assert_not_called()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'tests'
            assert app.mode == mode
            assert app.review_destination is None
            assert app.test_run is None
            assert app.unit_result is None
            collector.assert_called_once()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert app.mode is None
    asyncio.run(scenario())


def test_bare_demo_names_missing_storage_and_allows_editing(tmp_path):
    root = Path(__file__).resolve().parents[2]
    shared = load_shared_schema(root)
    schema = compose_schema(shared, load_declarations(root / 'demo/cfg/config.toml', base_schema=shared, source='pack_default'))
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path / 'demo', owner_root=tmp_path,
        pack_id='demo', external_pack=False, dotenv_path=tmp_path / '.env',
        anchors={name: tmp_path for name in ('base_root', 'pack_root', 'owner_root')})
    app = ApiAgentApp(context, schema, environ={}, container_runtime=SimpleNamespace(inventory=lambda: ()))

    async def scenario():
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            app.show_menu()
            await pilot.pause()
            await app.choose('mode-integration')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert 'Required settings: ALLURE_S3_BUCKET' in app.screen.body
            table = app.screen.query_one(DataTable)
            viewport = app.screen.query_one('#workbench-content')
            body = app.screen.query_one('#body')
            assert body.region.y >= viewport.region.y
            assert body.region.bottom <= viewport.region.bottom
            assert min(table.region.bottom, viewport.region.bottom) - max(table.region.y, viewport.region.y) >= 3
            assert str(app.screen.query_one('#error').content) == 'ALLURE_S3_BUCKET required for TUI_MODE=integration.'
            error = app.screen.query_one('#error')
            assert error.region.y >= viewport.region.bottom
            assert error.region.bottom <= app.screen.query_one('ButtonFooter').region.y
            assert error.styles.color == Color.parse(app.get_css_variables()['error'])
            table = app.screen.query_one(DataTable)
            index = next(i for i, row in enumerate(table.ordered_rows) if row.key.value == 'ALLURE_S3_BUCKET')
            table.move_cursor(row=index)
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.kind == 'input'
            assert app.prompt_name == 'ALLURE_S3_BUCKET'
    asyncio.run(scenario())


@pytest.mark.parametrize('width', [40, 80])
def test_many_long_required_names_fit_review_and_survive_resize(tmp_path, width):
    names = [f'REQUIRED_SETTING_WITH_A_LONG_NAME_{i}' for i in range(8)]
    app = fixture(tmp_path, fields={name: {'type': 'str', 'required_when': {'TUI_MODE': 'integration'}} for name in names})

    def assert_visible_review():
        viewport = app.screen.query_one('#workbench-content')
        body = app.screen.query_one('#body')
        table = app.screen.query_one(DataTable)
        assert body.region.y >= viewport.region.y
        assert body.region.bottom <= viewport.region.bottom
        assert min(table.region.bottom, viewport.region.bottom) - max(table.region.y, viewport.region.y) >= 3
        assert table.row_count == len(names)
        assert all(str(table.get_row_at(i)[1]) == 'Required' for i in range(table.row_count))

    async def scenario():
        async with app.run_test(size=(width, 12)) as pilot:
            await pilot.pause()
            app.show_menu()
            await pilot.pause()
            await app.choose('mode-integration')
            await pilot.pause()
            assert '8 required settings' in app.screen.body
            assert_visible_review()
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=5)
            await pilot.pause()
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            assert app.focused_setting() == names[5]
            await pilot.resize_terminal(width, 12)
            await pilot.pause()
            assert app.focused_setting() == names[5]
            assert_visible_review()
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 12), (100, 35)])
def test_start_authorized_review_labels_continue_operation(tmp_path, size):
    from unittest.mock import Mock
    from test_start_tui import START
    collector = Mock(side_effect=AssertionError('Label inspection must not collect or execute tests'))
    app = fixture(tmp_path, 'integration', fields={'START': START, 'COUNT': {'type': 'int', 'default': 8}},
        argv=['--start'], collector=collector)

    async def scenario():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.mode is None and app.auto_start_pending
            assert app.review_operation() == 'integration'
            assert 'c close' in app.screen.body
            assert 'main menu' not in app.screen.body
            assert app.screen._bindings.key_to_bindings['c'][0].description == 'Close'
            collector.assert_not_called()
    asyncio.run(scenario())


def test_removing_final_review_row_reports_active_hidden_requirement(tmp_path):
    from unittest.mock import Mock
    collector = Mock(side_effect=AssertionError('Missing hidden configuration must not collect tests'))
    app = fixture(tmp_path, fields={'PROVIDER_SETTING': {'type': 'str', 'default': 'synthetic'}}, collector=collector)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            app.mode = 'integration'
            app.session.set_value('TUI_MODE', 'integration')
            from api_agent.config import schema_from_dict
            app.schema = schema_from_dict({'version': 1, 'fields': {
                'TUI_MODE': {'type': 'str', 'acknowledge_default': False},
                'SECRET': {'type': 'str', 'required_when': {'TUI_MODE': 'integration'}, 'acknowledge_default': False}}})
            app.refresh_configuration_rows(app.screen)
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert app.mode is None
            collector.assert_not_called()
    asyncio.run(scenario())


def test_cleanup_review_close_returns_to_menu(tmp_path):
    app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 8}})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.show_menu()
            await app.choose('mode-config-cleanup')
            await pilot.pause()
            assert app.screen.kind == 'cleanup'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
    asyncio.run(scenario())


def test_review_footer_advances_actual_required_values_and_ignores_defaults(tmp_path):
    app = fixture(tmp_path, fields={
        'FIRST': {'type': 'str', 'required_when': {'TUI_MODE': 'integration'}},
        'SECOND': {'type': 'str', 'required_when': {'TUI_MODE': 'integration'}},
        'COUNT': {'type': 'int', 'default': 8}})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.show_menu()
            await app.choose('mode-integration')
            await pilot.pause()
            assert str(app.screen.query_one('#error').content) == 'SECOND required for TUI_MODE=integration.'
            app.session.set_value('SECOND', 'provided')
            app.refresh_configuration_rows(app.screen)
            assert str(app.screen.query_one('#error').content) == 'FIRST required for TUI_MODE=integration.'
            app.session.set_value('FIRST', 'provided')
            app.refresh_configuration_rows(app.screen)
            assert str(app.screen.query_one('#error').content) == ''
            assert 'COUNT' in app.configuration_pending()
            assert not (tmp_path / '.env').exists()
            app.session.save_defaults(['COUNT'])
            app.refresh_configuration_rows(app.screen)
            assert str(app.screen.query_one('#error').content) == ''
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
@pytest.mark.parametrize('required', [False, True])
def test_explicit_startup_review_preserves_selectors_and_required_gate(tmp_path, mode, required):
    from unittest.mock import Mock
    first = Identity('base::a.py::test_one', 'base', 'a.py', 'a.py::test_one')
    second = Identity('base::b.py::test_two', 'base', 'b.py', 'b.py::test_two')
    collector = Mock(return_value=[first, second])
    fields = {'COUNT': {'type': 'int', 'default': 8}}
    if required:
        fields['REQUIRED'] = {'type': 'str', 'required_when': {'TUI_MODE': mode}}
    app = fixture(tmp_path, mode, fields=fields, argv=('base::a.py',), collector=collector)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.review_destination == mode
            collector.assert_not_called()
            await pilot.press('c')
            await pilot.pause()
            if required:
                assert app.screen.kind == 'defaults'
                assert str(app.screen.query_one('#error').content) == f'REQUIRED required for TUI_MODE={mode}.'
                collector.assert_not_called()
                assert app.pending_test_selectors == ('base::a.py',)
                app.session.set_value('REQUIRED', 'provided')
                await pilot.press('c')
                await pilot.pause()
            assert app.screen.kind == 'tests'
            assert app.selection.selected_tests == (first,)
            assert app.active_test_selectors == ('base::a.py',)
            assert app.review_destination is None
            assert not app.auto_start_pending
            assert app.test_run is None
            assert app.unit_result is None
            collector.assert_called_once()
            assert not (tmp_path / '.env').exists()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            await app.choose('mode-' + mode)
            await pilot.pause()
            assert app.screen.kind == 'tests'
            assert app.selection.selected_tests == (first, second)
            assert app.active_test_selectors == ()
            assert app.test_run is None
            assert app.unit_result is None
            assert collector.call_count == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('entry', ['explicit', 'suggestion', 'manual'])
def test_auth_wizard_has_explicit_return_context_and_one_time_review(tmp_path, entry):
    from api_agent.tui.auth_setup import AuthSetupScreen
    app = fixture(tmp_path, 'unattended-auth' if entry == 'explicit' else None,
                  fields={'COUNT': {'type': 'int', 'default': 8}})
    if entry == 'suggestion':
        app.session.set_value('UNATTENDED_AUTH', '{}')

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            if entry == 'suggestion':
                assert app.screen.kind == 'suggest'
                await app.choose('setup')
            elif entry == 'manual':
                app.show_menu()
                await app.choose('mode-unattended-auth')
            await pilot.pause()
            assert isinstance(app.screen, AuthSetupScreen)
            assert app.screen.startup == (entry != 'manual')
            assert app.screen._bindings.key_to_bindings['c'][0].description == (
                'Close / menu' if entry == 'manual' else 'Close')
            await pilot.press('c')
            await pilot.pause()
            if entry != 'manual':
                assert app.screen.kind == 'defaults'
                assert app.review_destination is None
                await pilot.press('c')
                await pilot.pause()
            assert app.screen.kind == 'menu'
            assert app.mode is None
            await app.choose('mode-unattended-auth')
            await pilot.pause()
            assert isinstance(app.screen, AuthSetupScreen)
            assert not app.screen.startup
            assert app.screen._bindings.key_to_bindings['c'][0].description == 'Close / menu'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
    asyncio.run(scenario())


def test_remembered_skip_still_visits_startup_review(tmp_path):
    from textual.widgets import Checkbox
    app = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 8},
                                  'SKIP_UNATTENDED_SETUP': {'type': 'bool', 'default': False}})
    app.session.set_value('UNATTENDED_AUTH', '{}')

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'suggest'
            app.screen.query_one('#remember-skip', Checkbox).value = True
            await app.choose('skip')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.session.result().values['SKIP_UNATTENDED_SETUP']
            assert app.screen._bindings.key_to_bindings['c'][0].description == 'Close'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert not app.startup_review
    asyncio.run(scenario())

    restarted = fixture(tmp_path, fields={'COUNT': {'type': 'int', 'default': 8},
                                         'SKIP_UNATTENDED_SETUP': {'type': 'bool', 'default': False}})
    restarted.session.set_value('UNATTENDED_AUTH', '{}')

    async def restart():
        async with restarted.run_test() as pilot:
            await pilot.pause()
            assert restarted.session.result().values['SKIP_UNATTENDED_SETUP']
            assert restarted.auth_setup_needed()
            assert restarted.screen.kind == 'defaults'
            await pilot.press('c')
            await pilot.pause()
            assert restarted.screen.kind == 'menu'
    asyncio.run(restart())


def test_startup_cleanup_consumes_destination_before_later_review(tmp_path):
    app = fixture(tmp_path, 'config-cleanup', fields={'COUNT': {'type': 'int', 'default': 8}})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'cleanup'
            assert app.review_destination is None
            assert not app.startup_review
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.screen._bindings.key_to_bindings['c'][0].description == 'Close / menu'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
    asyncio.run(scenario())


def test_collect_only_auth_mode_rejects_without_opening_wizard(tmp_path):
    from unittest.mock import Mock
    collector = Mock(side_effect=AssertionError('Invalid collection mode must not discover tests'))
    app = fixture(tmp_path, 'unattended-auth',
                  fields={'COLLECT_ONLY': {'type': 'bool', 'default': False}},
                  argv=('--collect-only=True',), collector=collector)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'error'
            assert app.screen.heading == 'Invalid collection mode'
            assert app.operation_status == 3
            collector.assert_not_called()
    asyncio.run(scenario())


def test_manual_auth_wizard_closes_to_menu_despite_stale_start_intent(tmp_path):
    from unittest.mock import Mock
    from api_agent.tui.auth_setup import AuthSetupScreen
    collector = Mock(side_effect=AssertionError('Manual authentication setup must not resume START'))
    app = fixture(tmp_path, collector=collector)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'menu'
            await app.choose('mode-unattended-auth')
            await pilot.pause()
            assert isinstance(app.screen, AuthSetupScreen)
            assert not app.screen.startup
            app.auto_start_pending = True
            app.startup_mode = 'unit'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert app.mode is None
            assert not app.auto_start_pending
            collector.assert_not_called()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['unit', 'integration', 'e2e'])
def test_clear_env_review_consumes_startup_then_new_operation_restores_destination(tmp_path, mode):
    from unittest.mock import Mock
    from textual.widgets import Input
    (tmp_path / '.env').write_text('REQUIRED="initial"\n')
    collector = Mock(return_value=[Identity('base::a.py::test_one', 'base', 'a.py', 'a.py::test_one')])
    app = fixture(tmp_path, mode, fields={
        'REQUIRED': {'type': 'str', 'required_when': {'TUI_MODE': mode}},
        'COUNT': {'type': 'int', 'default': 8}}, collector=collector)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.review_destination == mode
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'tests'
            assert app.test_run is None
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            await app.choose('mode-config-cleanup')
            await pilot.pause()
            assert app.screen.kind == 'cleanup'
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=next(i for i in range(table.row_count) if table.get_row_at(i)[1] == 'REQUIRED'))
            await pilot.press('d')
            await pilot.pause()
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            assert 'REQUIRED=' not in (tmp_path / '.env').read_text()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.review_destination is None
            assert not app.startup_review
            assert app.screen._bindings.key_to_bindings['c'][0].description == 'Close / menu'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            await app.choose('mode-' + mode)
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.review_destination == mode
            assert app.screen._bindings.key_to_bindings['c'][0].description == 'Close'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert str(app.screen.query_one('#error').content) == f'REQUIRED required for TUI_MODE={mode}.'
            assert collector.call_count == 1
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=next(i for i, row in enumerate(table.ordered_rows) if row.key.value == 'REQUIRED'))
            await pilot.press('e')
            await pilot.pause()
            app.screen.query_one(Input).value = 'repaired'
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'tests'
            assert app.mode == mode
            assert app.review_destination is None
            assert app.test_run is None
            assert collector.call_count == 2
    asyncio.run(scenario())
