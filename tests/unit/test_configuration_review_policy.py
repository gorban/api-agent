"""One visibility flag controls required, optional and default review rows."""
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import DataTable

from api_agent.config import compose_schema, load_declarations, load_shared_schema, resolve_config, schema_from_dict
from test_tui import fixture


@pytest.fixture(autouse=True)
def restore_private_input_filters(monkeypatch):
    from textual import Logger
    from textual._xterm_parser import XTermParser
    monkeypatch.setattr(Logger, '__call__', Logger.__call__)
    monkeypatch.setattr(XTermParser, 'debug_log', XTermParser.debug_log)


HIDDEN = {'HELP', 'NO_STD_IN', 'COLLECT_ONLY', 'TUI_MODE', 'API_AGENT_DIR',
          'UI_SOURCE_DIR', 'UI_NX_APP', 'E2E_DEBUG', 'DEBUG', 'UNATTENDED_AUTH', 'API_PROVIDER', 'UI_PROVIDER',
          'CLIENT_CA_FILE', 'SKIP_UNATTENDED_SETUP'}


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
            assert app.screen.kind == 'defaults'
            await pilot.press('c')
            await pilot.pause()
            if not explicit_mode:
                assert app.screen.kind == 'menu'
                await app.choose('mode-integration')
                await pilot.pause()
            assert app.screen.kind == 'error'
            assert app.screen.heading == 'Missing configuration'
            body = str(app.screen.query_one('#body', Static).content)
            assert 'SECRET: .env / environment SECRET or --secret VALUE' in body
            assert 'VISIBLE_REQUIRED: .env / environment VISIBLE_REQUIRED or --visible-required VALUE' in body
            assert app.operation_status == 3
            collector.assert_not_called()
    asyncio.run(scenario())


def test_real_remote_langfuse_required_keys_are_reviewable_with_help(tmp_path):
    from unittest.mock import Mock
    from textual.widgets import Static
    async def scenario():
        app = fixture(tmp_path)
        app.schema = load_shared_schema()
        app.session.schema = app.schema
        app.session.environ = {'TUI_MODE':'run-local', 'TRACE_MODE':'LangFuse',
                               'TRACE_TARGET':'https://trace.example.invalid'}
        app.session.context.anchors.update({key:tmp_path for key in ('owner_root','base_root','pack_root')})
        app.check_operation_start = Mock(side_effect=AssertionError('Missing keys must block startup'))

        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            for name in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):
                table = app.screen.query_one(DataTable)
                names = [row.key.value for row in table.ordered_rows]
                assert name in names
                assert 'Required' in str(table.get_row(name)[1])
                table.move_cursor(row=names.index(name))
                await pilot.press('e')
                await pilot.pause()
                assert app.screen.kind == 'input'
                description = str(app.screen.query_one('#config-description', Static).content)
                assert description == app.schema.fields[name].help
                assert 'trace ingestion' in description
                assert 'dashboard email/password do not replace API keys' in description
                await pilot.press('escape')
                await pilot.pause()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert set(('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY')) <= set(app.session.result().missing)
            app.check_operation_start.assert_not_called()
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


def test_model_catalog_cache_ttl_is_visible_in_review_defaults(tmp_path):
    app = fixture(tmp_path)
    app.session.schema = load_shared_schema()
    app.session.environ = {'TUI_MODE': 'unit'}
    app.session.context.anchors.update({key: tmp_path for key in ('owner_root', 'base_root', 'pack_root')})

    candidates = {item.name: item for item in app.session.default_candidates()}

    assert candidates['LLM_CATALOG_CACHE_TTL_SECONDS'].value == 3600.0
    assert candidates['LLM_CATALOG_CACHE_TTL_SECONDS'].field.acknowledge_default is True


def test_remote_langfuse_secret_stays_masked_when_saved_and_reopened(tmp_path):
    from textual.widgets import Input, Static
    async def scenario():
        app = fixture(tmp_path)
        app.schema = load_shared_schema()
        app.session.schema = app.schema
        app.session.environ = {'TUI_MODE': 'run-local', 'TRACE_MODE': 'LangFuse',
                               'TRACE_TARGET': 'https://trace.example.invalid'}
        app.session.context.anchors.update({key: tmp_path for key in ('owner_root', 'base_root', 'pack_root')})
        name, secret = 'LANGFUSE_SECRET_KEY', 'synthetic-review-secret'
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=[row.key.value for row in table.ordered_rows].index(name))
            await pilot.press('e')
            await pilot.pause()
            control = app.screen.query_one(Input)
            assert control.password
            control.value = secret
            await pilot.click('#submit')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert str(table.get_row(name)[1]) == 'Configured'
            assert secret not in str(table.get_row(name))
            assert app.session.result().values[name] == secret
            assert name in (tmp_path / '.env').read_text()
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.query_one(Input).password
            assert app.screen.query_one(Input).value == secret
            assert secret not in str(app.screen.query_one('#body', Static).content)
    asyncio.run(scenario())


@pytest.mark.parametrize('metadata', [
    {'sensitive': 'True'}, {'sensitive': True, 'type': 'int'},
    {'sensitive': True, 'enum': ['synthetic-choice']},
])
def test_sensitive_metadata_rejects_nonboolean_or_nontext_entry(metadata):
    with pytest.raises(ValueError, match='sensitive'):
        schema_from_dict({'version': 1, 'fields': {'TOKEN': {'type': 'str', **metadata}}})


@pytest.mark.parametrize('default', ['', 'synthetic-private-default'])
def test_sensitive_setting_masks_defaults_review_and_cleanup_without_changing_value(tmp_path, default):
    from textual.widgets import Input, Static
    async def scenario():
        app = fixture(tmp_path, fields={'TOKEN': {'type': 'str', 'sensitive': True, 'default': default}})
        expected = 'Configured' if default else ''
        async with app.run_test() as pilot:
            await pilot.pause()
            assert str(app.screen.query_one(DataTable).get_row('TOKEN')[1]) == expected
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.query_one(Input).password
            assert app.screen.query_one(Input).value == default
            assert f'Default: {expected}.' in str(app.screen.query_one('#body', Static).content)
            await pilot.press('escape')
            await pilot.pause()
            await pilot.press('a')
            await pilot.pause()
            assert app.session.result().values['TOKEN'] == default
            assert str(app.screen.query_one(DataTable).get_row('TOKEN')[1]) == expected
            app.show_cleanup()
            await pilot.pause()
            assert str(app.screen.query_one(DataTable).get_row('TOKEN')[2]) == expected
            app.refresh_rows()
            assert str(app.screen.query_one(DataTable).get_row('TOKEN')[2]) == expected
    asyncio.run(scenario())


def test_pack_override_inherits_sensitive_presentation_and_cannot_remove_it():
    shared = schema_from_dict({'version': 1, 'fields': {
        'TOKEN': {'type': 'str', 'sensitive': True}}})
    extension = schema_from_dict({'version': 1, 'fields': {
        'TOKEN': {'type': 'str', 'override': True, 'default': 'synthetic-pack-secret'}}}, base_schema=shared)

    combined = compose_schema(shared, extension)

    assert combined.fields['TOKEN'].sensitive
    assert resolve_config(combined, environ={}).values['TOKEN'] == 'synthetic-pack-secret'
    unsafe = schema_from_dict({'version': 1, 'fields': {
        'TOKEN': {'type': 'str', 'override': True, 'sensitive': False}}}, base_schema=shared)
    with pytest.raises(ValueError, match='weakens'):
        compose_schema(shared, unsafe)


@pytest.mark.parametrize('use_base_context', [False, True])
def test_sensitive_override_rejects_inherited_enum_choices(use_base_context):
    shared = schema_from_dict({'version': 1, 'fields': {
        'TOKEN': {'type': 'str', 'enum': ['synthetic-enum-secret']}}})
    spec = {'version': 1, 'fields': {
        'TOKEN': {'type': 'str', 'override': True, 'sensitive': True}}}
    with pytest.raises(ValueError, match='sensitive'):
        extension = schema_from_dict(spec, base_schema=shared if use_base_context else None)
        compose_schema(shared, extension)


def test_sensitive_editor_protects_existing_and_future_input_diagnostics(tmp_path):
    import os
    import subprocess
    import sys
    script = r"""
import asyncio
from pathlib import Path
from textual import events, log
from textual._xterm_parser import XTermParser
from textual.widgets import Input
from test_tui import fixture
root = Path.cwd()
parser = XTermParser(debug=True)
list(parser.feed('ordinary'))
before = (root / 'keys.log').read_text()
secret = 'synthetic-keyboard-secret'
async def scenario():
    app = fixture(root, fields={'TOKEN': {'type': 'str', 'sensitive': True, 'default': ''}})
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press('e')
        await pilot.pause()
        control = app.screen.query_one(Input)
        assert control.password
        control.value = secret
        list(parser.feed(secret))
        list(parser.feed(''))
        future = XTermParser(debug=True)
        list(future.feed('\x1b[200~' + secret + '\x1b[201~'))
        list(future.feed(''))
        log.event(Input.Changed(control, secret), key=events.Key('s', 's'), paste=events.Paste(secret))
        await pilot.press('escape')
        await pilot.pause()
        log.event(events.Paste(secret))
        log.info('ordinary diagnostic remains available')
asyncio.run(scenario())
assert (root / 'keys.log').read_text() == before
framework = (root / 'textual.log').read_text()
assert secret not in framework
assert 'ordinary diagnostic remains available' in framework
"""
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join((str(repo / 'src'), str(repo / 'tests/unit'))),
           'TEXTUAL_LOG': str(tmp_path / 'textual.log'), 'TEXTUAL_DEBUG': '1'}

    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=20)

    assert result.returncode == 0, result.stdout + result.stderr
