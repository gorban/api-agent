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
