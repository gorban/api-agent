"""Synthetic unattended selection and profile safety in Setup."""
import asyncio

from textual.widgets import DataTable, Static

from api_agent.config import schema_from_dict
from api_agent.config.session import ConfigSession
from test_auth_setup import WizardApp, account


def make_app(tmp_path, *, argv=(), environ=None):
    app = WizardApp(tmp_path, roles=('LLM User', 'Partner'))
    app.session = ConfigSession(app.context, schema_from_dict({'version': 1, 'fields': {
        'UNATTENDED_AUTH': {'type': 'jsonobject'},
        'UNATTENDED_USER_INDEX': {'type': 'int', 'allow_empty': True, 'validation': 'nonnegative'},
    }}), argv=argv, environ=environ or {})
    app.session.save_auth_role('LLM User', [account('one'), account('two')])
    app.session.save_auth_role('Partner', [account('other'), account('another')])
    return app


def test_select_index_and_manual_are_distinct_from_highlight(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert str(screen.query_one('#auth-index', Static).content).startswith('Attended mode')
            await pilot.press('u')
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 0
            screen.query_one('#auth-table', DataTable).move_cursor(row=1)
            await pilot.pause()
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 0
            await pilot.press('u')
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 1
            await pilot.press('m')
            assert app.session.result().values['UNATTENDED_USER_INDEX'] is None
            assert app.session.result().fields['UNATTENDED_USER_INDEX'].explicit
    asyncio.run(scenario())


def test_override_and_invalid_selection_are_explained(tmp_path):
    async def scenario():
        app = make_app(tmp_path, argv=['--unattended-user-index=9'])
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            screen = app.screen
            text = str(screen.query_one('#auth-index', Static).content).lower()
            assert '9' in text and 'unavailable' in text and 'cli' in text
            await pilot.press('u')
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 9
            assert 'cli' in str(screen.query_one('#auth-error', Static).content).lower()
    asyncio.run(scenario())


def test_removing_or_reordering_selected_position_is_blocked(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        app.session.set_value('UNATTENDED_USER_INDEX', '1', persist=True)
        before = app.session.result().values['UNATTENDED_AUTH']
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press('r')
            assert app.session.result().values['UNATTENDED_AUTH'] == before
            assert 'selection' in str(screen.query_one('#auth-error', Static).content).lower()
            await pilot.press('right_square_bracket')
            assert app.session.result().values['UNATTENDED_AUTH'] == before
    asyncio.run(scenario())


def test_missing_role_at_selected_index_and_edit_are_safe(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        app.session.save_auth_role('Partner', [account('other')])
        app.session.set_value('UNATTENDED_USER_INDEX', '1', persist=True)
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert 'unavailable' in str(screen.query_one('#auth-index', Static).content).lower()
            screen.query_one('#auth-table', DataTable).move_cursor(row=1)
            screen.action_edit_entry()
            await pilot.pause()
            editor = app.screen
            editor.query_one('#auth-user').value = 'replacement'
            editor.save_entry()
            await pilot.pause()
            assert app.screen is screen
            assert 'replacement' in str(screen.query_one('#auth-index', Static).content)
    asyncio.run(scenario())


def test_selection_write_failure_preserves_effective_choice(tmp_path, monkeypatch):
    async def scenario():
        app = make_app(tmp_path)
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            def fail(*args, **kwargs):
                raise OSError('synthetic save failure')
            monkeypatch.setattr(app.session, 'set_value', fail)
            await pilot.press('u')
            assert app.session.result().values.get('UNATTENDED_USER_INDEX') is None
            assert 'synthetic save failure' in str(app.screen.query_one('#auth-error', Static).content)
    asyncio.run(scenario())


def test_removing_after_selection_is_allowed_and_target_is_shared(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        app.context.external_pack = True
        app.session.switch_target(app.context.base_root / '.env')
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press('u')
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 0
            table = screen.query_one('#auth-table', DataTable)
            table.move_cursor(row=3)
            await pilot.pause()
            assert app.session.dotenv_path == app.context.pack_root / '.env'
            await pilot.press('m')
            assert app.session.saved_path('UNATTENDED_USER_INDEX') == app.context.base_root / '.env'
            assert app.session.dotenv_path == app.context.pack_root / '.env'
            await pilot.press('u')
            table.move_cursor(row=2)
            await pilot.pause()
            # The later index can be removed without changing selected zero.
            await pilot.press('m')
            await pilot.press('u')
            table.move_cursor(row=3)
            await pilot.press('r')
            assert len(app.session.result().values['UNATTENDED_AUTH']['roles']['Partner']) == 1
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 0
    asyncio.run(scenario())


def test_absent_index_is_attended_and_allows_structural_changes(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert str(screen.query_one('#auth-index', Static).content).startswith('Attended mode')
            await pilot.press('r')
            assert len(app.session.result().values['UNATTENDED_AUTH']['roles']['LLM User']) == 1
    asyncio.run(scenario())


def test_conditional_index_uses_review_resolution_after_save(tmp_path):
    async def scenario():
        from dataclasses import replace
        app = make_app(tmp_path)
        app.session.schema.fields['TUI_MODE'] = schema_from_dict({'version': 1, 'fields': {
            'TUI_MODE': {'type': 'str'}}}).fields['TUI_MODE']
        field = app.session.schema.fields['UNATTENDED_USER_INDEX']
        app.session.schema.fields['UNATTENDED_USER_INDEX'] = replace(field, applies_when={'TUI_MODE': 'unit'})
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            await pilot.press('u')
            assert app.session.result().values.get('UNATTENDED_USER_INDEX') is None
            assert app.session.result(_review=True).values['UNATTENDED_USER_INDEX'] == 0
            assert 'overrides' not in str(app.screen.query_one('#auth-error', Static).content)
            assert 'index 0' in str(app.screen.query_one('#auth-index', Static).content)
    asyncio.run(scenario())


def test_selection_partial_write_failure_restores_target(tmp_path, monkeypatch):
    from api_agent.config.editor import DotenvEditor
    async def scenario():
        app = make_app(tmp_path)
        path = app.session.dotenv_path
        original = path.read_bytes()
        update = DotenvEditor.update
        def fail_after_write(editor, values):
            update(editor, values)
            raise OSError('synthetic partial write')
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(DotenvEditor, 'update', fail_after_write)
            await pilot.press('u')
            assert path.read_bytes() == original
            assert app.session.result(_review=True).values.get('UNATTENDED_USER_INDEX') is None
            assert 'synthetic partial write' in str(app.screen.query_one('#auth-error', Static).content)
    asyncio.run(scenario())


def test_selection_rollback_attempts_other_paths_and_reports_original_error(tmp_path, monkeypatch):
    async def scenario():
        app = make_app(tmp_path)
        app.context.external_pack = True
        base = app.context.base_root / '.env'
        pack = app.context.pack_root / '.env'
        pack.write_text('OTHER="original"\n')
        original_base = base.read_bytes()
        restore = app.session._restore
        restored = []

        def fail_save(*args, **kwargs):
            base.write_text('UNATTENDED_USER_INDEX=0\n')
            pack.write_text('OTHER="changed"\n')
            raise OSError('synthetic primary failure')

        def fail_pack_restore(path, content):
            restored.append(path)
            if path == pack:
                raise OSError('synthetic restore failure')
            restore(path, content)

        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            monkeypatch.setattr(app.session, 'set_value', fail_save)
            monkeypatch.setattr(app.session, '_restore', fail_pack_restore)
            await pilot.press('u')
            error = str(app.screen.query_one('#auth-error', Static).content)
            assert 'synthetic primary failure' in error
            assert 'synthetic restore failure' in error
            assert pack in restored and base in restored
            assert base.read_bytes() == original_base
            assert pack.read_text() == 'OTHER="changed"\n'
    asyncio.run(scenario())


def test_invalid_index_is_visible_and_prevents_structural_changes(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        app.session.environ['UNATTENDED_USER_INDEX'] = 'not-an-index'
        before = app.session.result(_review=True).values['UNATTENDED_AUTH']
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert 'selection is invalid' in str(screen.query_one('#auth-index', Static).content).lower()
            await pilot.press('r')
            assert 'correct the invalid' in str(screen.query_one('#auth-error', Static).content).lower()
            assert app.session.result(_review=True).values['UNATTENDED_AUTH'] == before
            await pilot.press('u')
            assert 'invalid higher-priority value' in str(screen.query_one('#auth-error', Static).content)
    asyncio.run(scenario())


def test_adding_missing_selected_index_requires_new_selection(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        app.session.save_auth_role('Partner', [account('other')])
        app.session.set_value('UNATTENDED_USER_INDEX', '1', persist=True)
        before = app.session.result(_review=True).values['UNATTENDED_AUTH']
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.query_one('#auth-table', DataTable).move_cursor(row=2)
            await pilot.press('a')
            editor = app.screen
            editor.query_one('#auth-vault').value = 'Synthetic vault'
            editor.save_entry()
            assert app.screen is editor
            assert 'change the unattended selection' in str(editor.query_one('#auth-error', Static).content).lower()
            assert app.session.result(_review=True).values['UNATTENDED_AUTH'] == before
    asyncio.run(scenario())


def test_selection_shortcuts_are_inactive_while_editor_is_open(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.action_edit_entry()
            await pilot.pause()
            editor = app.screen
            screen.action_select_unattended()
            screen.action_select_attended()
            assert app.screen is editor
            assert app.session.result(_review=True).values.get('UNATTENDED_USER_INDEX') is None
    asyncio.run(scenario())
