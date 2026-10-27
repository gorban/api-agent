"""Clear configuration retains only the visit's persisted setting names."""
import asyncio
import threading

import pytest
from textual.widgets import DataTable, Input

from api_agent.config.session import ConfigSession

from test_tui import fixture


def rows(app):
    table = app.screen.query_one(DataTable)
    return {table.get_row_at(i)[1]: table.get_row_at(i) for i in range(table.row_count)}


@pytest.mark.parametrize('create_file', [False, True])
def test_empty_cleanup_omits_defaults_and_returns_to_review(tmp_path, create_file):
    if create_file:
        (tmp_path / '.env').write_text('')
    app = fixture(tmp_path, 'config-cleanup', fields={
        'COUNT': {'type': 'int', 'default': 4},
        'HIDDEN': {'type': 'str', 'default': '', 'acknowledge_default': False}})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            await pilot.press('c')
            assert app.screen.kind == 'cleanup'
            assert rows(app) == {}
            assert not app.focused_cleanup_present()
            await pilot.press('c')
            assert app.screen.kind == 'defaults'
            table = app.screen.query_one(DataTable)
            assert [row.key.value for row in table.ordered_rows] == ['COUNT']
    asyncio.run(scenario())


@pytest.mark.parametrize('source', ['environment', 'cli', 'invalid'])
def test_saved_assignment_remains_clearable_independent_of_effective_source(tmp_path, source):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    for directory in (base, pack):
        directory.mkdir()
        (directory / '.env').write_text('COUNT=5\n')
    app = fixture(tmp_path, 'config-cleanup', fields={
        'COUNT': {'type': 'int', 'default': 4}},
        argv=('--count=9',) if source == 'cli' else ())
    app.context.base_root, app.context.pack_root = base, pack
    app.context.external_pack = True
    app.context.dotenv_path = pack / '.env'
    if source == 'environment':
        app.session.environ['COUNT'] = '8'
    app.session = ConfigSession(app.context, app.schema, argv=app.session.argv,
                                environ=app.session.environ)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'cleanup'
            if source == 'invalid':
                for directory in (base, pack):
                    (directory / '.env').write_text('COUNT=invalid\n')
                app.refresh_rows()
                await pilot.pause()
            assert rows(app)['COUNT'][0] == '✓'
            assert app.screen.check_action('delete_assignment', ())
            await pilot.press('d')
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            for directory in (base, pack):
                assert 'COUNT=' not in (directory / '.env').read_text()
            assert rows(app)['COUNT'][0] == ''
            assert not app.focused_cleanup_present()
            assert app.session.result().values['COUNT'] == {
                'environment': 8, 'cli': 9, 'invalid': 4}[source]
    asyncio.run(scenario())


def test_cleanup_membership_is_refreshed_only_on_entry(tmp_path):
    dotenv = tmp_path / '.env'
    dotenv.write_text('HIDDEN=\n')
    app = fixture(tmp_path, 'config-cleanup', fields={
        'HIDDEN': {'type': 'str', 'default': '', 'acknowledge_default': False},
        'OTHER': {'type': 'int', 'default': 4},
        'ENV_ONLY': {'type': 'str', 'acknowledge_default': False}})
    app.session.environ['ENV_ONLY'] = 'external'

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('c')
            assert app.screen.kind == 'cleanup'
            assert set(rows(app)) == {'HIDDEN'}
            assert rows(app)['HIDDEN'][0] == '✓'

            app.delete_assignment('HIDDEN')
            await pilot.pause()
            assert set(rows(app)) == {'HIDDEN'}
            assert rows(app)['HIDDEN'][0] == ''
            assert not app.focused_cleanup_present()
            dotenv.write_text('OTHER=6\n')
            app.refresh_rows()
            assert set(rows(app)) == {'HIDDEN'}

            await pilot.press('c')
            assert app.screen.kind == 'menu'
            await app.choose('mode-config-cleanup')
            await pilot.pause()
            assert set(rows(app)) == {'OTHER'}
            assert rows(app)['OTHER'][0:3] == ['✓', 'OTHER', '6']
    asyncio.run(scenario())


def test_cleanup_navigation_uses_displayed_presence_without_resolving(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('FIRST=1\nSECOND=2\n')
    app = fixture(tmp_path, 'config-cleanup', fields={
        name: {'type': 'int', 'default': 0} for name in ('FIRST', 'SECOND')})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            calls = []
            original = type(app.session).cleanup_rows
            ui_thread = threading.get_ident()

            def counted(session, *args, **kwargs):
                if threading.get_ident() == ui_thread:
                    calls.append(True)
                return original(session, *args, **kwargs)

            monkeypatch.setattr(type(app.session), 'cleanup_rows', counted)
            await pilot.press('down', 'up', 'down')
            assert app.screen.query_one(DataTable).cursor_row == 1
            assert app.focused_cleanup_present()
            assert app.screen.check_action('delete_assignment', ())
            assert calls == []

    asyncio.run(scenario())


def test_cleanup_external_refresh_and_current_file_deletion(tmp_path):
    dotenv = tmp_path / '.env'
    dotenv.write_text('FIRST=1\nSECOND=2\n')
    app = fixture(tmp_path, 'config-cleanup', fields={
        name: {'type': 'int', 'default': 0} for name in ('FIRST', 'SECOND')})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            dotenv.write_text('SECOND=8\n')
            app.refresh_rows()
            await pilot.pause()
            assert rows(app)['FIRST'][0] == ''
            assert not app.focused_cleanup_present()
            await pilot.press('down')
            assert rows(app)['SECOND'][2] == '8'
            assert app.focused_cleanup_present()
            await pilot.press('d')
            # Confirmation must delete the current assignment, preserving other
            # assignments written since the displayed snapshot was resolved.
            dotenv.write_text('FIRST=9\nSECOND=10\n')
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            assert dotenv.read_text() == 'FIRST=9\n'
            assert rows(app)['SECOND'][0] == ''
            assert not app.focused_cleanup_present()
            assert rows(app)['FIRST'][2] == '9'

    asyncio.run(scenario())


def test_cleanup_refresh_updates_help_without_replacing_live_schema(tmp_path, monkeypatch):
    from dataclasses import replace

    (tmp_path / '.env').write_text('FIRST=1\nSECOND=2\n')
    app = fixture(tmp_path, 'config-cleanup', fields={
        name: {'type': 'int', 'default': 0, 'help': 'Original help'}
        for name in ('FIRST', 'SECOND')})

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            original = type(app.session).cleanup_rows
            description = {'help': 'Updated help'}

            def changed(session, **kwargs):
                return [replace(row, field=replace(row.field, help=description['help']))
                        for row in original(session, **kwargs)]

            monkeypatch.setattr(type(app.session), 'cleanup_rows', changed)
            await pilot.press('down')
            await pilot.pause()
            assert app.schema.fields['SECOND'].help == 'Original help'
            assert app.screen.selected_setting_help() == ('SECOND', 'Updated help')
            assert app.screen.check_action('setting_help', ())
            description['help'] = ''
            await pilot.press('up')
            await pilot.pause()
            assert app.screen.selected_setting_help() is None
            assert not app.screen.check_action('setting_help', ())

    asyncio.run(scenario())


def test_empty_cleanup_explicit_refresh_before_navigation(tmp_path):
    app = fixture(tmp_path, 'config-cleanup', fields={
        'HIDDEN': {'type': 'str', 'acknowledge_default': False}})
    app.session.environ['HIDDEN'] = 'external'

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'cleanup'
            assert rows(app) == {}
            assert app.cleanup_names == frozenset()
            assert not hasattr(app.screen, 'configuration_refresher')
            session = app.session
            before = session.result()
            app.refresh_rows()
            await pilot.pause()
            assert rows(app) == {}
            assert app.cleanup_names == frozenset()
            assert not hasattr(app.screen, 'configuration_refresher')
            assert app.session is session
            assert session.result() == before
            assert not (tmp_path / '.env').exists()

    asyncio.run(scenario())
