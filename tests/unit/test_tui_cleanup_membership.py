"""Clear configuration retains only the visit's persisted setting names."""
import asyncio

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
