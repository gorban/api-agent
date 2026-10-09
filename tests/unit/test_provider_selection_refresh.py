"""Selected-provider declarations replace old UI inventories without execution."""
import asyncio

import pytest
from textual.widgets import DataTable

from api_agent.config import compose_schema, schema_from_dict
from api_agent.providers.loading import bootstrap_schema
from test_tui import fixture


def adapter(root, name, field, shared):
    folder = root / name
    (folder / 'cfg').mkdir(parents=True)
    (folder / 'provider.py').write_text("raise AssertionError('configuration executed adapter')\n")
    (folder / 'cfg/config.toml').write_text(
        f'version=1\n[fields.{field}]\ntype="str"\nrequired=true\n'
        f'[fields.SHARED]\ntype="int"\ndefault={shared}\nhelp="{name} description"\n')
    return str(folder / 'provider.py') + ':create'


def test_optional_override_does_not_introduce_an_inactive_provider_field():
    base = schema_from_dict({'version': 1})
    data = {'version': 1, 'fields': {'CALLBACK': {
        'type': 'str', 'override': True, 'override_if_present': True, 'default': 'callback'}}}
    extension = schema_from_dict(data)
    assert compose_schema(base, extension).fields == {}
    active = schema_from_dict({'version': 1, 'fields': {'CALLBACK': {'type': 'str'}}})
    assert compose_schema(active, extension).fields['CALLBACK'].default == 'callback'
    with pytest.raises(ValueError, match='preserve the shared type'):
        compose_schema(schema_from_dict({'version': 1, 'fields': {'CALLBACK': {'type': 'int'}}}), extension)
    for spec in ({'type': 'str', 'override_if_present': True},
                 {'type': 'str', 'override': True, 'override_if_present': 'True'}):
        with pytest.raises(ValueError):
            schema_from_dict({'version': 1, 'fields': {'CALLBACK': spec}})


def test_provider_optional_override_is_skipped_until_its_target_exists(tmp_path):
    location = adapter(tmp_path, 'optional', 'ACCOUNT', 1)
    config = tmp_path / 'optional/cfg/config.toml'
    config.write_text('version=1\n[fields.CALLBACK]\ntype="str"\noverride=true\noverride_if_present=true\ndefault="callback"\n')
    base = schema_from_dict({'version': 1, 'fields': {'UI_PROVIDER': {'type': 'str', 'default': location}}})
    assert 'CALLBACK' not in bootstrap_schema(base, [], {}, {}).fields
    fields = dict(base.fields)
    fields.update(schema_from_dict({'version': 1, 'fields': {'CALLBACK': {'type': 'str'}}}).fields)
    assert bootstrap_schema(type(base)(fields), [], {}, {}).fields['CALLBACK'].default == 'callback'


@pytest.mark.parametrize('cleanup', [False, True])
def test_open_configuration_table_rebuilds_after_provider_switch(tmp_path, cleanup):
    first = adapter(tmp_path, 'first', 'FIRST_REQUIRED', 3)
    second = adapter(tmp_path, 'second', 'SECOND_REQUIRED', 7)
    async def scenario():
        app = fixture(tmp_path, 'config-cleanup' if cleanup else None, fields={
            'LLM_PROVIDER': {'type': 'str', 'default': first, 'review_optional': True}})
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            names = lambda: [row.key.value for row in table.ordered_rows]
            assert 'FIRST_REQUIRED' in names() and 'SECOND_REQUIRED' not in names()
            app.session.set_value('SHARED', '9', persist=True)
            # A provider change made through a saved setting must reload schemas.
            app.session.set_value('LLM_PROVIDER', second, persist=True)
            if cleanup:
                app.refresh_rows()
            else:
                app.refresh_configuration_rows(app.screen)
            await pilot.pause()
            assert 'FIRST_REQUIRED' not in names() and 'SECOND_REQUIRED' in names()
            assert app.schema.fields['SHARED'].help == 'second description'
            row = names().index('SHARED')
            table.move_cursor(row=row)
            await pilot.pause()
            assert str(table.get_row_at(row)[2 if cleanup else 1]) == '9'
            assert app.session.saved_path('SHARED') is not None
            app.session.delete_keys(['SHARED'])
            if cleanup:
                app.refresh_rows()
            else:
                app.refresh_configuration_rows(app.screen)
            await pilot.pause()
            row = names().index('SHARED')
            assert str(table.get_row_at(row)[2 if cleanup else 1]) == '7'
            # Returning to the old provider reloads its required field/default.
            app.session.set_value('LLM_PROVIDER', first, persist=True)
            if cleanup:
                app.refresh_rows()
            else:
                app.refresh_configuration_rows(app.screen)
            await pilot.pause()
            assert 'FIRST_REQUIRED' in names() and 'SECOND_REQUIRED' not in names()
            assert app.schema.fields['SHARED'].default == 3
    asyncio.run(scenario())


def test_unloading_last_review_field_returns_to_menu(tmp_path):
    first = adapter(tmp_path, 'first', 'FIRST_REQUIRED', 3)
    (tmp_path / 'first/cfg/config.toml').write_text('version=1\n[fields.FIRST_REQUIRED]\ntype="str"\nrequired=true\n')
    async def scenario():
        app = fixture(tmp_path, fields={'LLM_PROVIDER': {
            'type': 'str', 'default': first, 'acknowledge_default': False}})
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.focused_setting() == 'FIRST_REQUIRED'
            app.session.set_value('LLM_PROVIDER', '', persist=True)
            app.refresh_configuration_rows(app.screen)
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert not app.configuration_pending()
            assert 'FIRST_REQUIRED' not in app.schema.fields
    asyncio.run(scenario())


@pytest.mark.parametrize('selector', ['LLM_PROVIDER', 'ACCESS_PROVIDER'])
@pytest.mark.parametrize('source', ['defaults', 'dotenv', 'environment', 'cli'])
def test_optional_override_waits_for_full_selected_graph(tmp_path, selector, source):
    ui = adapter(tmp_path, 'ui', 'IGNORED', 1)
    (tmp_path / 'ui/cfg/config.toml').write_text('version=1\n[fields.CALLBACK]\ntype="str"\noverride=true\noverride_if_present=true\ndefault="pack-origin"\n')
    target = adapter(tmp_path, 'target', 'IGNORED', 2)
    (tmp_path / 'target/cfg/config.toml').write_text('version=1\n[fields.CALLBACK]\ntype="str"\ndefault="provider-origin"\n')
    base = schema_from_dict({'version': 1, 'fields': {
        'UI_PROVIDER': {'type': 'str', 'default': ui}, selector: {'type': 'str', 'default': target}}})
    args = ['--callback=cli-origin'] if source == 'cli' else []
    env = {'CALLBACK': 'environment-origin'} if source == 'environment' else {}
    dotenv = {'CALLBACK': 'dotenv-origin'} if source == 'dotenv' else {}
    schema = bootstrap_schema(base, args, env, dotenv)
    assert schema.fields['CALLBACK'].default == 'pack-origin'
    from api_agent.config import resolve_config
    resolved = resolve_config(schema, argv=args, environ=env, dotenv=dotenv)
    assert resolved.values['CALLBACK'] == ('pack-origin' if source == 'defaults' else source + '-origin')


def test_optional_override_rejects_late_incompatible_target(tmp_path):
    ui = adapter(tmp_path, 'ui', 'IGNORED', 1)
    (tmp_path / 'ui/cfg/config.toml').write_text('version=1\n[fields.CALLBACK]\ntype="str"\noverride=true\noverride_if_present=true\ndefault="pack-origin"\n')
    access = adapter(tmp_path, 'access', 'IGNORED', 2)
    (tmp_path / 'access/cfg/config.toml').write_text('version=1\n[fields.CALLBACK]\ntype="int"\ndefault=2\n')
    base = schema_from_dict({'version': 1, 'fields': {
        'UI_PROVIDER': {'type': 'str', 'default': ui}, 'ACCESS_PROVIDER': {'type': 'str', 'default': access}}})
    with pytest.raises(ValueError, match='preserve the shared type'):
        bootstrap_schema(base, [], {}, {})


def test_deferred_override_cannot_silently_change_already_loaded_selector(tmp_path):
    ui = adapter(tmp_path, 'ui', 'IGNORED', 1)
    (tmp_path / 'ui/cfg/config.toml').write_text('version=1\n[fields.ACCESS_PROVIDER]\ntype="str"\noverride=true\noverride_if_present=true\ndefault=""\n')
    access = adapter(tmp_path, 'access', 'IGNORED', 2)
    base = schema_from_dict({'version': 1, 'fields': {
        'UI_PROVIDER': {'type': 'str', 'default': ui}, 'ACCESS_PROVIDER': {'type': 'str', 'default': access}}})
    from api_agent.providers.loading import ProviderError
    with pytest.raises(ProviderError, match='provider_selection_cycle'):
        bootstrap_schema(base, [], {}, {})


@pytest.mark.parametrize('selector', ['LLM_PROVIDER', 'API_PROVIDER', 'UI_PROVIDER', 'ACCESS_PROVIDER', 'UPSTREAM_API_PROVIDER'])
@pytest.mark.parametrize('initial', ['empty', 'unset'])
@pytest.mark.parametrize('target_kind', ['required', 'missing', 'relative'])
def test_deferred_override_cannot_enable_an_undiscovered_selector(tmp_path, selector, initial, target_kind):
    target = adapter(tmp_path, 'target', 'REQUIRED_VALUE', 1)
    if target_kind == 'missing':
        target = str(tmp_path / 'absent.py') + ':create'
    elif target_kind == 'relative':
        target = 'target/provider.py:create'
    contribution = tmp_path / 'overrides.toml'
    contribution.write_text(f'version=1\n[fields.{selector}]\ntype="str"\noverride=true\noverride_if_present=true\ndefault="{target}"\n')
    field = {'type': 'str'}
    if initial == 'empty':
        field['default'] = ''
    base = schema_from_dict({'version': 1, 'fields': {selector: field}})
    from api_agent.providers.loading import ProviderError
    with pytest.raises(ProviderError, match='^provider_selection_cycle$'):
        bootstrap_schema(base, [], {}, {}, owner_root=tmp_path,
                         configuration_paths=(contribution,))


def test_deferred_override_cannot_enable_a_duplicate_provider(tmp_path):
    ui = adapter(tmp_path, 'ui', 'IGNORED', 1)
    (tmp_path / 'ui/cfg/config.toml').write_text(f'version=1\n[fields.ACCESS_PROVIDER]\ntype="str"\noverride=true\noverride_if_present=true\ndefault="{ui}"\n')
    base = schema_from_dict({'version': 1, 'fields': {
        'UI_PROVIDER': {'type': 'str', 'default': ui},
        'ACCESS_PROVIDER': {'type': 'str', 'default': ''}}})
    from api_agent.providers.loading import ProviderError
    with pytest.raises(ProviderError, match='^provider_selection_cycle$'):
        bootstrap_schema(base, [], {}, {})


@pytest.mark.parametrize('source', ['dotenv', 'environment', 'cli', 'override'])
def test_explicit_selection_still_loads_required_fields_despite_deferred_default(tmp_path, source):
    from api_agent.config import resolve_config
    target = adapter(tmp_path, 'target', 'REQUIRED_VALUE', 1)
    contribution = tmp_path / 'overrides.toml'
    # Explicit selection wins even over an unusable optional default.
    contribution.write_text('version=1\n[fields.ACCESS_PROVIDER]\ntype="str"\noverride=true\noverride_if_present=true\ndefault="missing.py:create"\n')
    base = schema_from_dict({'version': 1, 'fields': {'ACCESS_PROVIDER': {'type': 'str', 'default': ''}}})
    dotenv = {'ACCESS_PROVIDER': target} if source == 'dotenv' else {}
    env = {'ACCESS_PROVIDER': target} if source == 'environment' else {}
    args = ['--access-provider=' + target] if source == 'cli' else []
    overrides = {'ACCESS_PROVIDER': target} if source == 'override' else {}
    schema = bootstrap_schema(base, args, env, dotenv, overrides,
                              configuration_paths=(contribution,))
    assert schema.fields['REQUIRED_VALUE'].required
    resolution_env = dict(env, **overrides)
    result = resolve_config(schema, argv=args, environ=resolution_env, dotenv=dotenv)
    assert 'REQUIRED_VALUE' in result.missing
    assert result.errors == ('REQUIRED_VALUE: required; provide REQUIRED_VALUE or --required-value',)
    supplied = resolve_config(schema, argv=[*args, '--required-value=provided'],
                              environ=resolution_env, dotenv=dotenv)
    assert not supplied.errors and 'REQUIRED_VALUE' not in supplied.missing
    assert supplied.values['REQUIRED_VALUE'] == 'provided'


@pytest.mark.parametrize('selector', ['LLM_PROVIDER', 'API_PROVIDER', 'UI_PROVIDER',
                                      'ACCESS_PROVIDER', 'UPSTREAM_API_PROVIDER'])
@pytest.mark.parametrize('initial', ['empty', 'unset', 'active'])
def test_conditional_deferred_selector_cannot_escape_discovery(tmp_path, selector, initial):
    from api_agent.providers.loading import ProviderError
    target = adapter(tmp_path, 'conditional', 'CONDITIONAL_REQUIRED', 1)
    field = {'type': 'str'}
    if initial == 'active':
        field['default'] = adapter(tmp_path, 'original', 'ORIGINAL_REQUIRED', 2)
    elif initial == 'empty':
        field['default'] = ''
    base = schema_from_dict({'version': 1, 'fields': {
        selector: field, 'ENABLE': {'type': 'bool', 'default': True}}})
    contribution = tmp_path / 'optional.toml'
    raw = field.get('default', '')
    contribution.write_text(f'version=1\n[fields.{selector}]\ntype="str"\n'
        f'override=true\noverride_if_present=true\ndefault="{raw}"\n'
        f'conditional_defaults=[{{when={{ENABLE=true}},value="{target}"}}]\n')
    with pytest.raises(ProviderError, match='^provider_selection_cycle$'):
        bootstrap_schema(base, [], {}, {}, owner_root=tmp_path, configuration_paths=(contribution,))
