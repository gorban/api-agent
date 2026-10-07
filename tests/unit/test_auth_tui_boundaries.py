"""Inactive editors and failed writes must preserve focus and stored descriptors."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static

from test_auth_setup import WizardApp, account, profile
from test_auth_wiring import app_fixture
from test_tui import fixture
from api_agent.tui.auth_setup import AuthSetupScreen


def test_unmounted_auth_table_sizing_does_not_query_widgets(monkeypatch):
    screen = AuthSetupScreen(('LLM User',))
    assert not screen.is_mounted
    query = Mock(side_effect=AssertionError('unmounted widgets must not be queried'))
    monkeypatch.setattr(screen, 'query_one', query)
    screen._fit_tables()
    query.assert_not_called()


def test_background_auth_actions_preserve_editor_and_target(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, external=True)
        app.session.save_auth_role('LLM User', [account('First'), account('Second')])
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            listing = app.screen
            listing.action_edit_entry()
            await pilot.pause()
            editor = app.screen
            target = app.session.dotenv_path
            original = target.read_bytes()
            table = listing.query_one('#auth-table', DataTable)
            for action in (listing.action_add_entry, listing.action_edit_entry,
                           listing.action_remove_entry, listing.action_move_entry_up,
                           listing.action_switch_target):
                action()
            listing.on_descendant_focus(None)
            listing.on_data_table_row_highlighted(None)
            listing.on_data_table_row_selected(SimpleNamespace(data_table=table))
            assert app.screen is editor
            assert listing.editing == ('LLM User', 0)
            assert app.session.dotenv_path == target
            assert target.read_bytes() == original
            assert editor.query_one('#auth-user', Input).value == 'First'
            assert app.focused.id == 'auth-vault'
            # A dialog over the editor must also block its write-target shortcut.
            editor.action_quit_request()
            await pilot.pause()
            editor.action_switch_target()
            assert app.session.dotenv_path == target
            app.screen.query_one('#quit-answer', Input).value = 'n'
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen is editor
            assert target.read_bytes() == original
    asyncio.run(scenario())


def test_single_target_editor_ignores_unrelated_events_and_restricts_switch(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            listing = app.screen
            # Before the target is initialized, selection events have no effect.
            target = listing.shared_target
            listing.shared_target = None
            listing.on_data_table_row_highlighted(None)
            listing.on_descendant_focus(None)
            listing.shared_target = target
            listing.action_edit_entry()
            await pilot.pause()
            editor = app.screen
            focused = app.focused
            editor.action_switch_target()
            editor.on_button_pressed(SimpleNamespace(button=Button('Other', id='other')))
            editor.on_select_changed(SimpleNamespace(select=SimpleNamespace(id='other'), value='keeper'))
            editor.on_advancing_select_submitted(None)
            assert app.screen is editor
            assert app.focused is focused
            assert app.session.dotenv_path == target
            assert not target.exists()
            editor.action_cancel()
            await pilot.pause()
            assert app.screen is listing
    asyncio.run(scenario())


@pytest.mark.parametrize('manager', [False, True])
def test_auth_remove_failure_keeps_saved_value_and_shows_error(tmp_path, monkeypatch, manager):
    async def scenario():
        app = WizardApp(tmp_path)
        app.session.save_auth_role('LLM User', [account()])
        app.session.save_auth_manager({'manager': '1password', 'cli_path': 'op'})
        original = app.session.dotenv_path.read_bytes()
        before = profile(app)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            if manager:
                screen.query_one('#auth-manager-table', DataTable).focus()
                await pilot.pause()
                assert app.focused.id == 'auth-manager-table'
            def fail(*args, **kwargs):
                raise OSError('synthetic write refusal')
            monkeypatch.setattr(app.session, 'reset_auth_manager' if manager else 'save_auth_role', fail)
            screen.action_remove_entry()
            await pilot.pause()
            assert 'Unable to remove: synthetic write refusal' in str(screen.query_one('#auth-error', Static).content)
            assert profile(app) == before
            assert app.session.dotenv_path.read_bytes() == original
            assert screen.entries['LLM User'] == [account()]
            assert not screen.changed
    asyncio.run(scenario())


def test_auth_editor_quit_acceptance_runs_only_after_yes(tmp_path, monkeypatch):
    async def scenario():
        app = WizardApp(tmp_path)
        confirm = Mock()
        monkeypatch.setattr(app, 'confirm_quit', confirm)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.screen.action_edit_entry()
            await pilot.pause()
            editor = app.screen
            editor.action_quit_request()
            await pilot.pause()
            app.screen.query_one('#quit-answer', Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            confirm.assert_called_once_with()
            assert not app.session.dotenv_path.exists()
    asyncio.run(scenario())


def test_blank_optional_pack_role_does_not_create_secondary_credentials(tmp_path):
    app = app_fixture(tmp_path, extra=True, environ={'SECONDARY_ROLE': ''})
    assert app.credential_roles() == ('LLM User',)


def test_closing_auth_suggestion_continues_to_configuration(tmp_path):
    async def scenario():
        app = app_fixture(tmp_path)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'suggest'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert not app.session.dotenv_path.exists()
    asyncio.run(scenario())


def test_select_confirmation_ignores_non_input_screens_and_missing_choice(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={'CHOICE': {'type': 'bool', 'required': True}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            defaults = app.screen
            focused = app.focused
            defaults.on_advancing_select_submitted(SimpleNamespace(select=SimpleNamespace(value=True)))
            assert app.focused is focused
            app.edit_setting()
            await pilot.pause()
            editor = app.screen
            select = editor.query_one('#config-value', Select)
            assert select.value is Select.NULL
            select.focus()
            editor.on_advancing_select_submitted(SimpleNamespace(select=select))
            assert app.focused is select
            assert not app.session.dotenv_path.exists()
            assert not editor.query_one('#submit', Button).has_focus
    asyncio.run(scenario())
