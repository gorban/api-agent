"""One visibility flag controls required, optional and default review rows."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import DataTable

from api_agent.config import compose_schema, load_declarations, load_shared_schema, resolve_config, schema_from_dict
from test_tui import fixture


HIDDEN = {'HELP', 'NO_STD_IN', 'COLLECT_ONLY', 'TUI_MODE', 'API_AGENT_DIR',
          'UI_SOURCE_DIR', 'UI_NX_APP', 'E2E_DEBUG', 'UNATTENDED_AUTH', 'API_PROVIDER', 'UI_PROVIDER',
          'CLIENT_CA_FILE', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'}


def test_declarations_have_one_visibility_flag_and_pack_override():
    shared = load_shared_schema()
    extension = schema_from_dict({'version': 1, 'fields': {
        'PROVIDER_SCOPE': {'type': 'str', 'default': 'synthetic', 'acknowledge_default': False},
    }}, base_schema=shared, source='pack_default')
    combined = compose_schema(shared, extension)
    for name in HIDDEN | {'PROVIDER_SCOPE'}:
        assert combined.fields[name].acknowledge_default is False
        assert not hasattr(combined.fields[name], 'persist_default')
    assert combined.fields['LOG_CONTENT'].acknowledge_default is True
    assert combined.fields['LLM_PROVIDER'].acknowledge_default is True
    for mode in ('run-local', 'integration', 'e2e'):
        env = {'TUI_MODE': mode, 'ALLURE_S3_USE_LOCALSTACK': 'True',
               'API_ENDPOINT': 'https://api.example/graphql', 'UI_SOURCE_DIR': '/example/frontend'}
        result = resolve_config(combined, environ=env)
        assert result.values['PROVIDER_SCOPE'] == 'synthetic'
        assert result.fields['PROVIDER_SCOPE'].source == 'pack_default'
        override = resolve_config(combined, environ=env | {'PROVIDER_SCOPE': 'custom'})
        assert override.values['PROVIDER_SCOPE'] == 'custom'
        assert override.fields['PROVIDER_SCOPE'].source == 'environment'
        saved = resolve_config(combined, environ=env, dotenv={'PROVIDER_SCOPE': 'saved'})
        assert saved.values['PROVIDER_SCOPE'] == 'saved'
        cli = resolve_config(combined, argv=['--provider-scope=cli'],
                             environ=env | {'PROVIDER_SCOPE': 'env'}, dotenv={'PROVIDER_SCOPE': 'saved'})
        assert cli.values['PROVIDER_SCOPE'] == 'cli'
        assert cli.fields['PROVIDER_SCOPE'].source == 'cli'


def test_removed_persistence_metadata_is_rejected():
    with pytest.raises(ValueError, match='unknown metadata'):
        schema_from_dict({'version': 1, 'fields': {'STALE': {'type': 'str', 'persist_default': False}}})


def test_hidden_required_optional_and_default_stay_hidden_but_saved_cleanup_visible(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={
            'VISIBLE': {'type': 'int', 'default': 8},
            'HIDDEN_DEFAULT': {'type': 'bool', 'default': False, 'acknowledge_default': False},
            'HIDDEN_REQUIRED': {'type': 'str', 'required': True, 'acknowledge_default': False},
            'HIDDEN_OPTIONAL': {'type': 'str', 'review_optional': True, 'acknowledge_default': False},
        })
        assert app.session.schema.fields['VISIBLE'].acknowledge_default is True
        assert app.configuration_pending() == {'VISIBLE'}
        assert 'HIDDEN_REQUIRED' in app.session.result().missing
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert [row.key.value for row in app.screen.query_one(DataTable).ordered_rows] == ['VISIBLE']
            await pilot.press('a')
            assert app.session.result().values['VISIBLE'] == 8
            app.session.set_value('HIDDEN_OPTIONAL', 'explicit', persist=True)
            rows = {row.name: row for row in app.session.cleanup_rows()}
            assert rows['HIDDEN_OPTIONAL'].present
            assert 'HIDDEN_DEFAULT' not in rows and 'HIDDEN_REQUIRED' not in rows
            app.session.delete_keys(['HIDDEN_OPTIONAL'])
            assert 'HIDDEN_OPTIONAL' not in {row.name for row in app.session.cleanup_rows()}
    asyncio.run(scenario())


def test_visibility_change_removes_previous_review_rows(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={'SETTING': {'type': 'str', 'default': 'visible'}})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            app.schema = replace(app.schema, fields={name: replace(field, acknowledge_default=False)
                for name, field in app.schema.fields.items()})
            app.session.schema = app.schema
            app.show_configuration()
            await pilot.pause()
            assert app.screen.kind == 'menu'
    asyncio.run(scenario())


@pytest.mark.parametrize('explicit_mode', [False, True])
def test_hidden_required_settings_fail_operation_without_config_recursion(tmp_path, explicit_mode):
    from unittest.mock import Mock
    from textual.widgets import Static
    async def scenario():
        collector = Mock(side_effect=AssertionError('Invalid configuration must not collect tests'))
        fields = {'SECRET': {'type': 'str', 'required': True, 'acknowledge_default': False},
                  'VISIBLE_REQUIRED': {'type': 'str', 'required': True}}
        app = fixture(tmp_path, 'integration' if explicit_mode else None, fields=fields, collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            if not explicit_mode:
                assert app.screen.kind == 'defaults'
                app.mode = 'integration'
                await app.configure_operation(skip_defaults=True)
                await pilot.pause()
            assert app.screen.kind == 'error'
            assert app.screen.heading == 'Missing configuration'
            body = str(app.screen.query_one('#body', Static).content)
            assert 'SECRET: .env / environment SECRET or --secret VALUE' in body
            assert 'VISIBLE_REQUIRED: .env / environment VISIBLE_REQUIRED or --visible-required VALUE' in body
            assert app.operation_status == 3
            collector.assert_not_called()
    asyncio.run(scenario())


def test_real_remote_langfuse_hidden_keys_report_error_without_startup(tmp_path):
    from textual.widgets import Static
    async def scenario():
        app = fixture(tmp_path)
        app.schema = load_shared_schema()
        app.session.schema = app.schema
        app.session.environ = {'TUI_MODE':'run-local', 'TRACE_MODE':'LangFuse',
                               'TRACE_TARGET':'https://trace.example.invalid'}
        app.session.context.anchors.update({key:tmp_path for key in ('owner_root','base_root','pack_root')})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'error'
            body = str(app.screen.query_one('#body', Static).content)
            for name in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):
                assert name in body and f'--{name.lower().replace("_", "-")} VALUE' in body
            assert app.operation_status == 3
    asyncio.run(scenario())


def test_hidden_required_auth_uses_dedicated_setup_error(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'integration', {'UNATTENDED_AUTH':
            {'type':'jsonobject', 'required':True, 'acknowledge_default':False}})
        app.session.environ.pop('UNATTENDED_AUTH')
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'error'
            assert app.screen.heading == 'Unattended authentication required'
            assert app.operation_status == 3
    asyncio.run(scenario())
