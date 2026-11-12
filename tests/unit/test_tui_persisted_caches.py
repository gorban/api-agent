"""Clear configuration lists the pack's persisted caches and purges them with confirmation."""
import asyncio
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Static

from api_agent.runtime.persisted_caches import PersistedCache
from api_agent.tui.config_bulk import plan_cleanup
from api_agent.tui.config_table import ConfigTable

from test_tui import fixture

SIGN_IN = PersistedCache('sign-in', 'Cached sign-in', 'fixture-auth')
OTHER = PersistedCache('other', 'Other cache', 'fixture-other')


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path / 'home')
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    return tmp_path / 'home'


def populate(*caches):
    for cache in caches:
        (cache.directory / 'tokens').mkdir(parents=True)
        (cache.directory / 'tokens' / 'login.json').write_text('{}')


def cleanup_app(tmp_path, *caches):
    (tmp_path / '.env').write_text('COUNT=5\n')
    app = fixture(tmp_path, 'config-cleanup', fields={'COUNT': {'type': 'int', 'default': 4}})
    app.context.persisted_caches = caches
    return app


def rows(app):
    table = app.screen.query_one(DataTable)
    cells = [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]
    return {row[1]: row for row in cells}


async def open_cleanup(app, pilot):
    await pilot.pause()
    if app.screen.kind == 'defaults':
        await pilot.press('c')
    assert app.screen.kind == 'cleanup'


def test_only_declared_caches_with_content_are_listed(tmp_path, home):
    populate(SIGN_IN)
    app = cleanup_app(tmp_path, SIGN_IN, OTHER)

    async def scenario():
        async with app.run_test() as pilot:
            await open_cleanup(app, pilot)
            listed = rows(app)
            assert set(listed) == {'COUNT', 'cache:sign-in'}
            assert listed['cache:sign-in'][0] == '✓'
            assert str(SIGN_IN.directory) in listed['cache:sign-in'][2]
            assert listed['cache:sign-in'][3] == 'persisted cache'
            assert 'Persisted caches' in str(app.screen.query_one('#body').content)
    asyncio.run(scenario())


def test_without_declared_caches_the_screen_is_unchanged(tmp_path, home):
    app = cleanup_app(tmp_path)

    async def scenario():
        async with app.run_test() as pilot:
            await open_cleanup(app, pilot)
            assert set(rows(app)) == {'COUNT'}
            assert 'Persisted caches' not in str(app.screen.query_one('#body').content)
    asyncio.run(scenario())


def test_focused_cache_is_purged_after_literal_confirmation(tmp_path, home):
    populate(SIGN_IN, OTHER)
    app = cleanup_app(tmp_path, SIGN_IN, OTHER)

    async def scenario():
        async with app.run_test() as pilot:
            await open_cleanup(app, pilot)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=list(rows(app)).index('cache:sign-in'))
            await pilot.press('d')
            dialog = str(app.screen.query_one(Static).content)
            assert 'cache:sign-in' in dialog and str(SIGN_IN.directory) in dialog and '.env' not in dialog
            app.screen.query_one(Input).value = 'n'
            await pilot.press('enter')
            await pilot.pause()
            assert SIGN_IN.present()
            await pilot.press('d')
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            assert not SIGN_IN.present() and OTHER.present()
            assert rows(app)['cache:sign-in'][0] == ''
            assert 'COUNT=5' in (tmp_path / '.env').read_text()
    asyncio.run(scenario())


def test_marked_options_and_caches_are_deleted_together(tmp_path, home):
    populate(SIGN_IN, OTHER)
    app = cleanup_app(tmp_path, SIGN_IN, OTHER)

    async def scenario():
        async with app.run_test(size=(100, 35)) as pilot:
            await open_cleanup(app, pilot)
            table = app.screen.query_one(ConfigTable)
            table.set_marked_keys(('COUNT', 'cache:sign-in', 'cache:other'))
            await pilot.press('d')
            assert '3 selected settings' in str(app.screen.query_one(Static).content)
            assert '.env and persisted caches' in str(app.screen.query_one(Static).content)
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            assert not SIGN_IN.present() and not OTHER.present()
            assert 'COUNT' not in (tmp_path / '.env').read_text()
            assert {name: row[0] for name, row in rows(app).items()} == {
                'COUNT': '', 'cache:sign-in': '', 'cache:other': ''}
    asyncio.run(scenario())


def test_marked_caches_alone_leave_the_dotenv_files_untouched(tmp_path, home):
    populate(SIGN_IN, OTHER)
    app = cleanup_app(tmp_path, SIGN_IN, OTHER)

    async def scenario():
        async with app.run_test(size=(100, 35)) as pilot:
            await open_cleanup(app, pilot)
            app.screen.query_one(ConfigTable).set_marked_keys(('cache:sign-in', 'cache:other'))
            await pilot.press('d')
            assert 'from persisted caches' in str(app.screen.query_one(Static).content)
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            assert not SIGN_IN.present() and not OTHER.present()
            assert 'COUNT=5' in (tmp_path / '.env').read_text()
    asyncio.run(scenario())


def test_cache_rows_are_deletable_but_not_copyable():
    plan = plan_cleanup([], ('cache:sign-in', 'cache:gone'), 'delete', membership=set(),
                        caches={'cache:sign-in'})
    assert (plan.names, plan.skipped) == (('cache:sign-in',), ('cache:gone',))
    plan = plan_cleanup([], ('cache:sign-in',), 'copy', membership=set(), caches={'cache:sign-in'})
    assert (plan.names, plan.skipped) == ((), ('cache:sign-in',))
