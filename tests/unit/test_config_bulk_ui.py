"""Configuration actions retain row identities across live discovery."""
import asyncio
import pytest
from textual.widgets import Static
from api_agent.config.dotenv import read_dotenv
from api_agent.tui.config_table import ConfigTable
from test_tui import fixture, setting_row

FIELDS = {'FIRST': {'type': 'str', 'default': 'one'},
          'SECOND': {'type': 'str', 'default': 'two'}}


def test_keyboard_marks_accepts_and_unsets_both_defaults(tmp_path):
    async def exercise():
        app = fixture(tmp_path, fields=FIELDS)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(ConfigTable)
            table.move_cursor(row=setting_row(table, 'FIRST'))
            await pilot.press('space', 'down', 'space', 'a')
            await pilot.pause()
            assert read_dotenv(app.session.dotenv_path) == {'FIRST': 'one', 'SECOND': 'two'}
            assert table.marked_keys == {'FIRST', 'SECOND'}
            await pilot.press('u')
            await pilot.pause()
            assert read_dotenv(app.session.dotenv_path) == {}
            await pilot.press('escape')
            assert not table.selection_explicit
    asyncio.run(exercise())


def test_marked_copy_uses_values_and_skips_secrets(tmp_path, monkeypatch):
    async def exercise():
        import api_agent.tui.app as module
        copied = []
        async def copy(app, text):
            copied.append(text)
            return True
        monkeypatch.setattr(module, 'copy_text', copy)
        app = fixture(tmp_path, fields={**FIELDS, 'SECRET': {'type': 'str', 'default': 'private', 'sensitive': True}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(ConfigTable)
            table.set_marked_keys(('FIRST', 'SECOND', 'SECRET'))
            await app.screen.action_copy_value()
            assert copied == ['FIRST=one\nSECOND=two']
            table.set_marked_keys(('FIRST', 'SECRET'))
            await app.screen.action_copy_value()
            assert copied[-1] == 'FIRST=one'
            assert 'SECRET' in str(app.screen.query_one('#error', Static).content)
            table.set_marked_keys(('FIRST', 'HIDDEN'))
            await app.screen.action_copy_value()
            assert copied[-1] == 'FIRST=one'
            assert 'HIDDEN' in str(app.screen.query_one('#error', Static).content)
            table.set_marked_keys(('SECRET',))
            await app.screen.action_copy_value()
            assert len(copied) == 3
    asyncio.run(exercise())


def test_hidden_mark_does_not_fall_back_to_an_unrelated_saved_row(tmp_path):
    async def exercise():
        app = fixture(tmp_path, fields={**FIELDS,
            'ENABLED': {'type': 'bool', 'default': True},
            'CONDITIONAL': {'type': 'str', 'default': 'candidate', 'applies_when': {'ENABLED': True}}})
        app.review_names.update(('FIRST', 'CONDITIONAL'))
        app.session.set_value('FIRST', 'keep', persist=True)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(ConfigTable)
            table.set_marked_keys(('CONDITIONAL',))
            app.session.set_value('ENABLED', 'False', persist=True)
            app.refresh_configuration_rows(app.screen)
            await pilot.pause()
            assert 'CONDITIONAL' not in [row.key.value for row in table.ordered_rows]
            assert app.screen.config_selection() == ('CONDITIONAL',)
            table.move_cursor(row=setting_row(table, 'FIRST'))
            app.screen.action_unset_setting()
            assert read_dotenv(app.session.dotenv_path)['FIRST'] == 'keep'
            assert 'CONDITIONAL' in str(app.screen.query_one('#error', Static).content)
    asyncio.run(exercise())


def test_default_action_rechecks_a_changed_conditional_value(tmp_path):
    async def exercise():
        app = fixture(tmp_path, fields={
            'ENABLED': {'type': 'bool', 'default': True},
            'OPTION': {'type': 'str', 'default': 'off', 'conditional_defaults': [
                {'when': {'ENABLED': True}, 'value': 'on'}]}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.screen.query_one(ConfigTable).set_marked_keys(('OPTION',))
            app.session.dotenv_path.write_text('ENABLED=False\n')
            await app.accept_default()
            assert read_dotenv(app.session.dotenv_path)['OPTION'] == 'off'
    asyncio.run(exercise())


def test_cleanup_range_deletes_once_after_confirmation(tmp_path):
    async def exercise():
        app = fixture(tmp_path, fields=FIELDS)
        for name in FIELDS:
            app.session.set_value(name, 'saved', persist=True)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.show_cleanup()
            await pilot.pause()
            table = app.screen.query_one(ConfigTable)
            table.focus()
            await pilot.press('ctrl+a', 'd')
            await pilot.pause()
            assert read_dotenv(app.session.dotenv_path)
            await pilot.press('y', 'enter')
            await pilot.pause()
            assert not read_dotenv(app.session.dotenv_path)
            assert table.marked_keys == set(FIELDS)
    asyncio.run(exercise())


def test_restore_failure_is_visible_with_the_original_error(tmp_path, monkeypatch):
    async def exercise():
        app = fixture(tmp_path, fields=FIELDS)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            def fail(names):
                error = OSError('original write failed')
                error.add_note('Could not restore synthetic target')
                raise error
            monkeypatch.setattr(app.session, 'save_defaults', fail)
            app.apply_config_action('accept', ('FIRST',))
            message = str(app.screen.query_one('#error', Static).content)
            assert 'original write failed' in message
            assert 'Could not restore synthetic target' in message
    asyncio.run(exercise())


def test_enter_toggles_a_mark_without_opening_the_value_editor(tmp_path):
    async def exercise():
        app = fixture(tmp_path, fields=FIELDS)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(ConfigTable)
            table.set_marked_keys(('FIRST', 'SECOND'))
            table.move_cursor(row=setting_row(table, 'FIRST'))
            await pilot.press('enter')
            assert table.marked_keys == {'SECOND'}
            assert app.screen.kind == 'defaults'
            await pilot.resize_terminal(40, 12)
            await pilot.pause()
            assert '1 marked' in str(app.screen.query_one('#heading', Static).content)
    asyncio.run(exercise())


@pytest.mark.parametrize('screen_kind', ['defaults', 'cleanup'])
def test_copy_handles_a_malformed_source_without_touching_clipboard(tmp_path, monkeypatch, screen_kind):
    async def exercise():
        import api_agent.tui.app as module
        copied = []
        async def copy(app, text):
            copied.append(text)
            return True
        monkeypatch.setattr(module, 'copy_text', copy)
        app = fixture(tmp_path, fields=FIELDS)
        app.review_names.update(FIELDS)
        app.session.set_value('FIRST', 'saved', persist=True)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            if screen_kind == 'cleanup':
                app.show_cleanup()
                await pilot.pause()
            app.screen.query_one(ConfigTable).set_marked_keys(('FIRST',))
            app.session.dotenv_path.write_text('FIRST="unclosed\n')
            try:
                await app.screen.action_copy_value()
            finally:
                app.session.dotenv_path.unlink()
            assert not copied
            assert app.screen.kind == screen_kind
            assert 'unclosed quoted value' in str(app.screen.query_one('#error', Static).content)
    asyncio.run(exercise())


def test_reopening_review_does_not_restore_marks_from_an_earlier_visit(tmp_path):
    async def exercise():
        app = fixture(tmp_path, fields=FIELDS)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.screen.query_one(ConfigTable).set_marked_keys(('FIRST',))
            app.show_configuration()
            await pilot.pause()
            assert app.screen.query_one(ConfigTable).marked_keys == {'FIRST'}
            app.show_menu()
            await pilot.pause()
            app.show_configuration()
            await pilot.pause()
            table = app.screen.query_one(ConfigTable)
            assert not table.selection_explicit
            table.move_cursor(row=setting_row(table, 'SECOND'))
            await app.accept_default()
            await pilot.pause()
            assert read_dotenv(app.session.dotenv_path) == {'SECOND': 'two'}
    asyncio.run(exercise())


@pytest.mark.parametrize('screen_kind', ['defaults', 'cleanup'])
def test_single_copy_suppresses_a_masked_value_but_preserves_empty_values(tmp_path, monkeypatch, screen_kind):
    async def exercise():
        import api_agent.tui.app as module
        copied = []
        async def copy(app, text):
            copied.append(text)
            return True
        monkeypatch.setattr(module, 'copy_text', copy)
        app = fixture(tmp_path, fields={**FIELDS, 'SECRET': {'type': 'str', 'sensitive': True}})
        app.review_names.add('SECRET')
        app.session.set_value('SECRET', 'synthetic-private-value', persist=True)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            if screen_kind == 'cleanup':
                app.show_cleanup()
                await pilot.pause()
            table = app.screen.query_one(ConfigTable)
            table.move_cursor(row=setting_row(table, 'SECRET'))
            await pilot.pause()
            assert not table.selection_explicit
            assert app.screen.selected_effective_value() is None
            assert not app.screen.check_action('copy_value', ())
            await app.screen.action_copy_value()
            assert not copied
            app.session.set_value('SECRET', '', persist=True)
            if screen_kind == 'cleanup':
                app.refresh_rows()
            else:
                app.refresh_configuration_rows(app.screen)
            assert app.screen.selected_effective_value() == ''
            await app.screen.action_copy_value()
            assert copied == ['']
    asyncio.run(exercise())


def test_bulk_action_can_run_before_a_background_refresher_is_installed(tmp_path, monkeypatch):
    async def exercise():
        app = fixture(tmp_path, fields=FIELDS)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            refresher = app.screen.configuration_refresher
            if refresher.worker:
                await refresher.worker.wait()
            monkeypatch.delattr(app.screen, 'configuration_refresher')
            app.apply_config_action('accept', ('FIRST',))
            assert read_dotenv(app.session.dotenv_path) == {'FIRST': 'one'}
    asyncio.run(exercise())


@pytest.mark.parametrize('action', ['accept', 'unset', 'delete'])
def test_single_actions_show_failed_rollback_diagnostics(tmp_path, monkeypatch, action):
    async def exercise():
        app = fixture(tmp_path, fields=FIELDS)
        app.review_names.update(FIELDS)
        if action != 'accept':
            app.session.set_value('FIRST', 'saved', persist=True)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            if action == 'delete':
                app.show_cleanup()
                await pilot.pause()
            app.screen.query_one(ConfigTable).move_cursor(row=0)
            def failed_write(names):
                error = OSError('original write failed')
                error.add_note('Could not restore synthetic target')
                raise error
            if action == 'accept':
                monkeypatch.setattr(app.session, 'save_defaults', failed_write)
                await app.accept_default()
            elif action == 'unset':
                monkeypatch.setattr(app.session, 'unset_saved', failed_write)
                app.screen.action_unset_setting()
            else:
                monkeypatch.setattr(app.session, 'delete_keys', failed_write)
                app.delete_assignment('FIRST')
            message = str(app.screen.query_one('#error', Static).content)
            assert 'original write failed' in message
            assert 'Could not restore synthetic target' in message
    asyncio.run(exercise())
