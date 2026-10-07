"""Synthetic account descriptors and manager settings; no credential commands."""
import asyncio
import json
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Input, Select, Static

from api_agent.config import schema_from_dict
from api_agent.config.session import ConfigSession
from api_agent.config.dotenv import read_dotenv
from api_agent.config.editor import DotenvEditor
from api_agent.tui.auth_setup import AuthSetupScreen
from api_agent.tui.app import ApiAgentApp


class WizardApp(App):
    CSS = ApiAgentApp.CSS

    def __init__(self, tmp_path, *, environ=None, roles=('LLM User',), external=False, argv=()):
        super().__init__()
        base, pack = tmp_path / 'base', tmp_path / 'pack'
        base.mkdir(parents=True, exist_ok=True)
        pack.mkdir(parents=True, exist_ok=True)
        self.context = SimpleNamespace(base_root=base, pack_root=pack, launcher_root=base,
                                       dotenv_path=base / '.env', anchors={}, external_pack=external)
        self.session = ConfigSession(self.context, schema_from_dict({'version': 1, 'fields': {
            'UNATTENDED_AUTH': {'type': 'jsonobject'}}}), argv=argv, environ=environ or {})
        self.roles = roles

    async def on_mount(self):
        self.push_screen(AuthSetupScreen(self.roles))

    def confirm_quit(self):
        self.exit(0)


def profile(app):
    return app.session.result(_review=True).values['UNATTENDED_AUTH']


def saved(path):
    return json.loads(read_dotenv(path)['UNATTENDED_AUTH'])


def account(name='LLM', vault='Example'):
    return {'vault': vault, 'user': name}


def test_account_add_edit_remove_only_one_at_a_time(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press('a')
            assert not app.screen.query('#auth-manager')
            app.screen.query_one('#auth-vault', Input).value = 'Team [A]'
            await pilot.click('#auth-save')
            await pilot.pause()
            assert profile(app)['roles'] == {'LLM User': [account('LLM User', 'Team [A]')]}
            assert set(saved(app.session.dotenv_path)['roles']['LLM User'][0]) == {'vault', 'user'}
            await pilot.press('e')
            assert app.screen.query_one('#auth-vault', Input).value == 'Team [A]'
            assert app.screen.query_one('#auth-user', Input).value == 'LLM User'
            app.screen.query_one('#auth-user', Input).value = 'Second'
            await pilot.click('#auth-save')
            await pilot.pause()
            await pilot.press('a')
            app.screen.query_one('#auth-vault', Input).value = 'Another'
            await pilot.click('#auth-save')
            await pilot.pause()
            assert len(profile(app)['roles']['LLM User']) == 2
            screen.query_one('#auth-table', DataTable).move_cursor(row=0)
            await pilot.press('r')
            await pilot.pause()
            assert len(profile(app)['roles']['LLM User']) == 1
            await pilot.press('r')
            await pilot.pause()
            assert profile(app)['roles']['LLM User'] == []
            assert screen.check_action('edit_entry', ())
            assert not screen.check_action('remove_entry', ())
            assert {binding.key for binding in screen.BINDINGS}.isdisjoint({'x', 't'})
    asyncio.run(scenario())


def test_shared_manager_edit_default_path_custom_path_and_reset(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        DotenvEditor(app.session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps({'roles': {'LLM User': [account()]}})})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.pause()
            assert not screen.check_action('add_entry', ())
            await pilot.press('a')
            assert screen.editing is None
            await pilot.press('e')
            assert screen.editing == 'manager'
            assert not app.screen.query('#auth-vault')
            app.screen.query_one('#auth-manager', Select).value = '1password'
            await pilot.pause()
            assert app.screen.query_one('#auth-path', Input).value == 'op'
            app.screen.query_one('#auth-path', Input).value = '/custom path/op'
            app.screen.query_one('#auth-manager', Select).value = 'keeper'
            await pilot.pause()
            assert app.screen.query_one('#auth-path', Input).value == '/custom path/op'
            for key in ('a', 'r', 's'):
                assert not screen.check_action({'a': 'add_entry', 'r': 'remove_entry', 's': 'switch_target'}[key], ())
            app.screen.save_entry()
            await pilot.pause()
            assert profile(app)['password_manager'] == {'manager': 'keeper', 'cli_path': '/custom path/op'}
            assert profile(app)['roles']['LLM User'] == [account()]
            await pilot.press('r')
            await pilot.pause()
            assert profile(app)['password_manager'] == {'manager': '', 'cli_path': ''}
            assert 'password_manager' not in saved(app.session.dotenv_path)
            assert profile(app)['roles']['LLM User'] == [account()]
    asyncio.run(scenario())


def test_manager_confirmation_advances_path_enter_saves_cancel_does_not(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            assert app.focused.id == 'auth-manager'
            await pilot.press('enter', 'down', 'enter')
            assert app.focused.id == 'auth-path'
            assert app.screen.query_one('#auth-path', Input).value == 'op'
            assert str(app.screen.query_one('#auth-save', Button).label) == 'Save value (Enter)'
            await pilot.press('enter')
            await pilot.pause()
            assert profile(app)['password_manager'] == {'manager': '1password', 'cli_path': 'op'}
            await pilot.press('e')
            await pilot.pause()
            await pilot.press('enter', 'enter')
            assert app.focused.id == 'auth-path'
            app.screen.query_one('#auth-path', Input).value = '/changed/op'
            app.screen.query_one('#auth-cancel', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert profile(app)['password_manager']['cli_path'] == 'op'
            await pilot.press('e')
            await pilot.pause()
            app.screen.query_one('#auth-path', Input).value = '/changed/op'
            app.screen.query_one('#auth-save', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert profile(app)['password_manager']['cli_path'] == '/changed/op'
    asyncio.run(scenario())


def test_enter_on_account_and_manager_rows_opens_edit(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            listing = app.screen
            await pilot.press('enter')
            await pilot.pause()
            assert listing.editing == ('LLM User', None)
            assert str(app.screen.query_one('#auth-form-title', Static).content) == 'Edit account for LLM User'
            app.screen.query_one('#auth-vault', Input).value = 'Example'
            app.screen.query_one('#auth-user', Input).value = 'Saved login'
            await pilot.press('enter')
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            assert listing.editing == ('LLM User', 0)
            assert app.screen.query_one('#auth-user', Input).value == 'Saved login'
            await pilot.press('escape')
            await pilot.pause()
            listing.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert listing.editing == 'manager'
            assert str(app.screen.query_one('#auth-form-title', Static).content) == 'Edit shared password manager'
            assert app.focused.id == 'auth-manager'
    asyncio.run(scenario())


def test_new_manager_path_renders_fully_while_still_focused(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        path = '/opt/example-vault/fixture-runtime1/bin/keeper'
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            table = app.screen.query_one('#auth-manager-table', DataTable)
            assert [str(cell) for cell in table.get_row_at(0)] == ['-', '-', 'Not provided']
            table.focus()
            await pilot.press('enter')
            await pilot.pause()
            await pilot.press('enter', 'down', 'down', 'enter')
            assert app.focused.id == 'auth-path'
            app.screen.query_one('#auth-path', Input).value = path
            await pilot.press('enter')
            await pilot.pause()
            assert app.focused is table
            assert profile(app)['password_manager'] == {'manager': 'keeper', 'cli_path': path}
            rendered = ''.join(ElementTree.fromstring(app.export_screenshot()).itertext()).replace('\xa0', ' ')
            assert path in rendered
    asyncio.run(scenario())


def test_auth_tables_show_component_sources_independent_of_target(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, external=True, roles=('LLM User', 'Partner'))
        app.session.save_auth_role('LLM User', [account('First'), account('Second')])
        app.session.save_auth_role('Partner', [account('Pack')])
        app.session.save_auth_manager({'manager': 'keeper', 'cli_path': 'keeper'})
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            users = screen.query_one('#auth-table', DataTable)
            manager = screen.query_one('#auth-manager-table', DataTable)
            assert str(users.ordered_columns[-1].label) == 'Source'
            assert str(manager.ordered_columns[-1].label) == 'Source'
            assert [str(users.get_row_at(i)[-1]) for i in range(3)] == ['../base/.env', '../base/.env', '../pack/.env']
            assert str(manager.get_row_at(0)[-1]) == '../base/.env'
            before = {path: path.read_bytes() for path in app.session.target_paths}
            await pilot.press('s')
            assert app.session.dotenv_path == app.context.pack_root / '.env'
            assert str(users.get_row_at(0)[-1]) == '../base/.env'
            assert str(manager.get_row_at(0)[-1]) == '../base/.env'
            assert before == {path: path.read_bytes() for path in app.session.target_paths}
            manager.focus()
            await pilot.press('enter')
            await pilot.pause()
            app.screen.query_one('#auth-save', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert str(manager.get_row_at(0)[-1]) == '../pack/.env'
            assert str(users.get_row_at(0)[-1]) == '../base/.env'
    asyncio.run(scenario())


def test_account_source_renders_fully_immediately_after_first_save(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        root = tmp_path / 'api-agent'
        root.mkdir()
        app.context.base_root = app.context.pack_root = app.context.launcher_root = root
        app.session.switch_target(root / '.env')
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            app.screen.query_one('#auth-vault', Input).value = 'vault'
            await pilot.press('enter')
            await pilot.pause()
            assert app.focused.id == 'auth-table'
            rendered = ''.join(ElementTree.fromstring(app.export_screenshot()).itertext()).replace('\xa0', ' ')
            assert '../api-agent/.env' in rendered
    asyncio.run(scenario())


def test_auth_edit_keeps_displayed_values_aligned_with_override_source(tmp_path):
    async def scenario():
        value = json.dumps({'roles': {'LLM User': [account('Environment login')]}})
        app = WizardApp(tmp_path, environ={'UNATTENDED_AUTH': value})
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            app.screen.query_one('#auth-user', Input).value = 'Saved login'
            app.screen.query_one('#auth-user', Input).focus()
            await pilot.press('enter')
            await pilot.pause()
            row = app.screen.query_one('#auth-table', DataTable).get_row_at(0)
            assert str(row[3]) == 'Environment login'
            assert str(row[4]) == 'environment'
            assert saved(app.session.dotenv_path)['roles']['LLM User'][0]['user'] == 'Saved login'
    asyncio.run(scenario())


@pytest.mark.parametrize('component', ('role', 'manager'))
@pytest.mark.parametrize('switch_in_editor', (False, True))
def test_auth_editor_switch_and_save_moves_only_edited_component(tmp_path, component, switch_in_editor):
    async def scenario():
        app = WizardApp(tmp_path, external=True, roles=('LLM User', 'Partner'))
        app.session.save_auth_role('LLM User', [account('A'), account('B')])
        app.session.save_auth_role('Partner', [account('Pack')])
        app.session.save_auth_manager({'manager': 'keeper', 'cli_path': '/custom/keeper'})
        base, pack = app.context.base_root / '.env', app.context.pack_root / '.env'
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            listing = app.screen
            if component == 'manager':
                listing.query_one('#auth-manager-table', DataTable).focus()
            before = {path: path.read_bytes() for path in (base, pack)}
            if not switch_in_editor:
                await pilot.press('s')
            await pilot.press('enter')
            await pilot.pause()
            editor = app.screen
            if switch_in_editor:
                field_id = '#auth-path' if component == 'manager' else '#auth-user'
                field_value = editor.query_one(field_id, Input).value
                editor.query_one('#auth-save', Button).focus()
                await pilot.press('s')
                assert editor.query_one(field_id, Input).value == field_value
            assert app.session.dotenv_path == pack
            assert f'✓ {pack}' in str(editor.query_one('#auth-targets', Static).content)
            assert before == {path: path.read_bytes() for path in (base, pack)}
            editor.query_one('#auth-save', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            base_data, pack_data = saved(base), saved(pack)
            if component == 'manager':
                assert 'password_manager' not in base_data
                assert pack_data['password_manager'] == {'manager': 'keeper', 'cli_path': '/custom/keeper'}
                assert base_data['roles']['LLM User'] == [account('A'), account('B')]
                assert str(listing.query_one('#auth-manager-table', DataTable).get_row_at(0)[-1]) == '../pack/.env'
            else:
                assert 'LLM User' not in base_data.get('roles', {})
                assert pack_data['roles']['LLM User'] == [account('A'), account('B')]
                assert base_data['password_manager'] == {'manager': 'keeper', 'cli_path': '/custom/keeper'}
                assert str(listing.query_one('#auth-table', DataTable).get_row_at(0)[-1]) == '../pack/.env'
            assert pack_data['roles']['Partner'] == [account('Pack')]
    asyncio.run(scenario())


def test_auth_editor_switch_button_preserves_edits_and_cancel_does_not_move(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, external=True, roles=('LLM User', 'Partner'))
        app.session.save_auth_role('LLM User', [account('Original')])
        base, pack = app.context.base_root / '.env', app.context.pack_root / '.env'
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            app.screen.query_one('#auth-user', Input).value = 'Unsaved edit'
            before = base.read_bytes()
            await pilot.click('#auth-switch-target')
            await pilot.pause()
            assert app.screen.query_one('#auth-user', Input).value == 'Unsaved edit'
            assert app.session.dotenv_path == pack
            app.screen.query_one('#auth-cancel', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert base.read_bytes() == before and not pack.exists()
            app.screen.query_one('#auth-table', DataTable).move_cursor(row=1)
            await pilot.press('enter')
            await pilot.pause()
            assert not app.screen.query('#auth-switch-target')
            assert not app.screen.check_action('switch_target', ())
            assert str(app.screen.query_one('#auth-targets', Static).content) == f'Target .env files:\n✓ {pack}'
    asyncio.run(scenario())


def test_account_validation_cancel_and_target_switch(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, external=True)
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press('a')
            app.screen.save_entry()
            assert 'nonempty' in str(app.screen.query_one('#auth-error', Static).content)
            app.screen.query_one('#auth-vault', Input).value = 'V'
            await pilot.press('enter')
            await pilot.pause()
            assert screen.editing is None
            await pilot.press('s')
            assert app.session.dotenv_path == app.context.pack_root / '.env'
            await pilot.press('a')
            await pilot.press('escape')
            await pilot.pause()
            assert len(profile(app)['roles']['LLM User']) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('external', ('environment', 'cli'))
def test_external_override_warning_and_quit_cancel(tmp_path, external):
    async def scenario():
        value = json.dumps({'roles': {'LLM User': []}})
        app = WizardApp(tmp_path, environ={'UNATTENDED_AUTH': value} if external == 'environment' else {},
                        argv=(f'--UNATTENDED_AUTH={value}',) if external == 'cli' else ())
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert external in str(app.screen.query_one('#auth-override', Static).content)
            await pilot.press('q')
            assert app.screen.query_one(Input).id == 'quit-answer'
            await pilot.press('escape')
            await pilot.pause()
            assert isinstance(app.screen, AuthSetupScreen)
            assert profile(app)['roles'] == {'LLM User': []}
    asyncio.run(scenario())


def test_invalid_profile_never_echoes_unexpected_secret(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, environ={'UNATTENDED_AUTH': '{"roles":{"LLM User":[{"secret":"do-not-render"}]}}'})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            error = str(app.screen.query_one('#auth-error', Static).content)
            assert 'Invalid' in error
            assert 'do-not-render' not in error
            assert not app.screen.check_action('add_entry', ())
    asyncio.run(scenario())


def test_other_pack_entries_preserved_without_validation_or_display(tmp_path):
    async def scenario():
        other = [{'unused': 'hidden'}]
        app = WizardApp(tmp_path)
        DotenvEditor(app.session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps({'roles': {'Other pack': other}})})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert not screen.load_error
            assert screen.query_one('#auth-table', DataTable).row_count == 1
            await pilot.press('a')
            app.screen.query_one('#auth-vault', Input).value = 'Example'
            app.screen.save_entry()
            assert saved(app.session.dotenv_path)['roles']['Other pack'] == other
    asyncio.run(scenario())


@pytest.mark.parametrize('start_in_pack', (False, True))
def test_pack_role_scope_and_shared_manager_restore_target(tmp_path, start_in_pack):
    async def scenario():
        app = WizardApp(tmp_path, external=True, roles=('LLM User', 'Partner'))
        base, pack = app.context.base_root / '.env', app.context.pack_root / '.env'
        previous = pack if start_in_pack else base
        app.session.switch_target(previous)
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one('#auth-table', DataTable)
            workflow = screen.query_one('#auth-workflow', Static).content.plain
            table.move_cursor(row=1)
            await pilot.pause()
            assert screen.query_one('#auth-workflow', Static).content.plain == workflow
            assert app.session.dotenv_path == pack
            assert str(screen.query_one('#auth-targets', Static).content) == f'Target .env files:\n✓ {pack}'
            assert screen.check_action('switch_target', ())
            await pilot.press('s')
            assert app.session.dotenv_path == pack
            assert screen.shared_target == previous
            await pilot.press('a')
            app.screen.query_one('#auth-vault', Input).value = 'Pack vault'
            app.screen.save_entry()
            await pilot.pause()
            assert saved(pack)['roles']['Partner'] == [account('Partner', 'Pack vault')]
            screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.pause()
            assert app.session.dotenv_path == previous
            assert str(base) in str(screen.query_one('#auth-targets', Static).content)
            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            assert app.session.dotenv_path == previous
            await pilot.press('a')
            app.screen.query_one('#auth-vault', Input).value = 'LLM vault'
            app.screen.save_entry()
            await pilot.pause()
            assert profile(app)['roles'] == {'LLM User': [account('LLM User', 'LLM vault')],
                                            'Partner': [account('Partner', 'Pack vault')]}
    asyncio.run(scenario())


def test_legacy_pack_account_in_base_can_be_removed(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, external=True, roles=('LLM User', 'Partner'))
        legacy = {role: [{'manager': 'keeper', 'cli_path': 'keeper', **account(role)}] for role in app.roles}
        DotenvEditor(app.session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps(legacy), 'KEEP': 'unchanged'})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.entries['Partner'] == [account('Partner')]
            screen.query_one('#auth-table', DataTable).move_cursor(row=1)
            await pilot.pause()
            await pilot.press('r')
            await pilot.pause()
            assert profile(app)['roles'] == {'LLM User': [account('LLM User')], 'Partner': []}
            assert 'Partner' not in saved(app.context.base_root / '.env')['roles']
            assert read_dotenv(app.context.base_root / '.env')['KEEP'] == 'unchanged'
    asyncio.run(scenario())


def test_explicit_manager_choice_repairs_mixed_legacy_accounts(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        legacy = {'LLM User': [{'manager': 'keeper', 'cli_path': 'keeper', **account('first')},
                               {'manager': '1password', 'cli_path': 'op', **account('second')}]}
        DotenvEditor(app.session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps(legacy)})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert 'choose one shared' in screen.load_error
            screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            app.screen.query_one('#auth-manager', Select).value = '1password'
            await pilot.pause()
            app.screen.save_entry()
            await pilot.pause()
            assert not screen.load_error
            assert screen.entries['LLM User'] == [account('first'), account('second')]
            assert screen.query_one('#auth-table', DataTable).row_count == 2
            assert profile(app)['password_manager']['manager'] == '1password'
    asyncio.run(scenario())


def test_readonly_workflow_title_and_form_labels(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, external=True)
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            storage = screen.query_one('#auth-storage', Static)
            heading = screen.query_one('#auth-heading', Static)
            assert str(storage.content) == '* Only a pointer to your password-manager entry is saved.'
            assert storage.styles.text_align == 'right'
            assert storage.region.y == heading.region.y
            assert not screen.query('#auth-source')
            help_text = str(screen.query_one('#auth-profile-help', Static).content)
            assert 'Duplicates of each role allowed; pair like users on same index to select them together.' in help_text
            guide = screen.query_one('#auth-workflow', Static)
            assert not guide.can_focus
            workflow = guide.content
            assert 'Enter login/item name' in workflow.plain
            # Read down the left column, then the right column.
            assert workflow.plain.splitlines()[2].startswith('3. (a) Add account')
            assert [workflow.plain[span.start:span.end] for span in workflow.spans] == ['s', 'Enter', 'a', 'e', 'c']
            for span in workflow.spans:
                assert workflow.plain[span.start - 1] == '(' and workflow.plain[span.end] == ')'
                assert span.style == f"bold {app.get_css_variables()['footer-key-foreground']}"
            await pilot.resize_terminal(45, 40)
            await pilot.pause()
            lines = str(guide.content).splitlines()
            assert len(lines) == 9
            assert lines[2].endswith('(a) Add account')
            assert lines[3].endswith('Enter vault name')
            assert lines[4].endswith('Enter login/item name')
            assert lines[7].endswith('(e) Edit password manager')
            await pilot.resize_terminal(100, 40)
            await pilot.pause()
            await pilot.press('a')
            for selector, label in (('#auth-save', 'Save'), ('#auth-cancel', 'Cancel')):
                app.screen.query_one(selector).scroll_visible(animate=False)
                await pilot.pause()
                rendered = ''.join(ElementTree.fromstring(app.export_screenshot()).itertext())
                assert label in rendered
    asyncio.run(scenario())


@pytest.mark.parametrize('count', (1, 8, 40))
def test_tables_fit_grow_and_recover_after_resize(tmp_path, count):
    async def scenario():
        app = WizardApp(tmp_path)
        DotenvEditor(app.session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps({'roles': {'LLM User': [account(str(i)) for i in range(count)]}})})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            screen = app.screen
            users = screen.query_one('#auth-table', DataTable)
            manager = screen.query_one('#auth-manager-table', DataTable)
            viewport = screen.query_one('#auth-content', VerticalScroll)
            assert users.size.height >= 5
            assert manager.region.y == users.region.bottom + 1
            assert manager.size.height >= 2
            assert viewport.max_scroll_y == 0
            if count > 1:
                assert users.size.height > 5
            if count == 40:
                assert users.max_scroll_y > 0
            await pilot.resize_terminal(100, 14)
            await pilot.pause()
            assert users.size.height >= 5 and manager.size.height >= 2
            assert viewport.max_scroll_y > 0
            await pilot.resize_terminal(100, 35)
            await pilot.pause()
            assert viewport.max_scroll_y == 0
            assert manager.region.bottom <= viewport.region.bottom
    asyncio.run(scenario())


def test_wide_tables_reserve_horizontal_scrollbar_space(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        DotenvEditor(app.session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps({
            'password_manager': {'manager': '1password', 'cli_path': '/long/' + 'x' * 150},
            'roles': {'LLM User': [account('y' * 150) for _ in range(30)]}})})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            users = screen.query_one('#auth-table', DataTable)
            manager = screen.query_one('#auth-manager-table', DataTable)
            viewport = screen.query_one('#auth-content', VerticalScroll)
            assert users.max_scroll_x > 0 and manager.max_scroll_x > 0
            assert users.size.height >= 6 and manager.size.height == 3
            assert viewport.max_scroll_y == 0
            manager.focus()
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.query_one('#auth-path', Input).value.startswith('/long/')
            await pilot.press('escape')
            await pilot.pause()
            assert viewport.max_scroll_y == 0
            assert manager.region.bottom <= viewport.region.bottom
    asyncio.run(scenario())


def test_edit_blank_role_creates_account_and_edit_uses_last_saved_values(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.check_action('edit_entry', ())
            assert screen.check_action('add_entry', ())
            await pilot.press('e')
            assert screen.editing == ('LLM User', None)
            assert app.screen.query_one('#auth-vault', Input).value == ''
            app.screen.query_one('#auth-vault', Input).value = 'Saved vault'
            app.screen.query_one('#auth-user', Input).value = 'Saved login'
            app.screen.save_entry()
            await pilot.pause()
            await pilot.press('e')
            assert app.screen.query_one('#auth-vault', Input).value == 'Saved vault'
            assert app.screen.query_one('#auth-user', Input).value == 'Saved login'
            app.screen.query_one('#auth-user', Input).value = 'Edited login'
            app.screen.save_entry()
            await pilot.pause()
            assert profile(app)['roles']['LLM User'] == [account('Edited login', 'Saved vault')]
            await pilot.press('a')
            assert screen.editing == ('LLM User', None)
            assert app.screen.query_one('#auth-vault', Input).value == ''
            await pilot.press('escape')
    asyncio.run(scenario())


@pytest.mark.parametrize('down,up', [('down', 'up'), ('j', 'k')])
def test_row_navigation_crosses_tables_only_at_boundary(tmp_path, down, up):
    async def scenario():
        app = WizardApp(tmp_path, external=True, roles=('LLM User', 'Partner'))
        DotenvEditor(app.session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps({
            'roles': {'LLM User': [account('first'), account('second')]}})})
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            screen = app.screen
            users = screen.query_one('#auth-table', DataTable)
            manager = screen.query_one('#auth-manager-table', DataTable)
            assert app.focused is users and users.cursor_row == 0
            await pilot.press(up)
            assert app.focused is users and users.cursor_row == 0
            await pilot.press(down)
            assert app.focused is users and users.cursor_row == 1
            await pilot.press(down)
            assert app.focused is users and users.cursor_row == 2
            assert app.session.dotenv_path == app.context.pack_root / '.env'
            await pilot.press(down)
            assert app.focused is manager and manager.cursor_row == 0
            assert not screen.check_action('add_entry', ())
            assert screen.check_action('edit_entry', ())
            assert app.session.dotenv_path == app.context.base_root / '.env'
            await pilot.press(down)
            assert app.focused is manager
            await pilot.press(up)
            assert app.focused is users and users.cursor_row == 2
            assert screen.check_action('add_entry', ()) and screen.check_action('edit_entry', ())
            assert app.session.dotenv_path == app.context.pack_root / '.env'
    asyncio.run(scenario())


def test_tab_has_only_tables_without_form_and_manager_stays_last(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            screen = app.screen
            users = screen.query_one('#auth-table', DataTable)
            manager = screen.query_one('#auth-manager-table', DataTable)
            assert screen.focus_chain == [users, manager]
            await pilot.press('shift+tab')
            assert app.focused is manager
            await pilot.press('tab')
            assert app.focused is users
            await pilot.press('e')
            await pilot.pause()
            assert screen.focus_chain[0] is users
            assert screen.focus_chain[-1] is manager
            assert screen.query_one('#auth-content') not in screen.focus_chain
            await pilot.press('escape')
            assert app.focused is users
    asyncio.run(scenario())


@pytest.mark.parametrize('role', ['LLM User', 'Partner'])
def test_reorder_persists_role_and_keeps_moved_account_selected(tmp_path, role):
    async def scenario():
        app = WizardApp(tmp_path, external=True, roles=('LLM User', 'Partner'))
        app.session.save_auth_role(role, [account('A'), account('B'), account('C')])
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one('#auth-table', DataTable)
            offset = 0 if role == 'LLM User' else 1
            table.move_cursor(row=offset)
            await pilot.pause()
            assert screen.check_action('move_entry_up', ())
            assert screen.check_action('move_entry_down', ())
            await pilot.press('[')
            assert screen.entries[role] == [account('A'), account('B'), account('C')]
            await pilot.press(']')
            await pilot.pause()
            assert screen.entries[role] == [account('B'), account('A'), account('C')]
            assert table.cursor_row == offset + 1
            assert str(table.get_row_at(table.cursor_row)[3]) == 'A'
            await pilot.press(']')
            await pilot.pause()
            assert screen.entries[role] == [account('B'), account('C'), account('A')]
            assert table.cursor_row == offset + 2
            assert screen.check_action('move_entry_up', ()) and screen.check_action('move_entry_down', ())
            await pilot.press(']')
            assert table.cursor_row == offset + 2
            await pilot.press('[')
            await pilot.pause()
            assert table.cursor_row == offset + 1
            assert str(table.get_row_at(table.cursor_row)[3]) == 'A'
            target = app.context.pack_root / '.env' if role == 'Partner' else app.context.base_root / '.env'
            assert saved(target)['roles'][role] == [account('B'), account('A'), account('C')]
            screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.pause()
            assert not screen.check_action('move_entry_up', ())
            assert not screen.check_action('move_entry_down', ())
    asyncio.run(scenario())


def test_reorder_hidden_for_singleton_and_failed_save_preserves_selection(tmp_path, monkeypatch):
    async def scenario():
        app = WizardApp(tmp_path)
        app.session.save_auth_role('LLM User', [account('A')])
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert not screen.check_action('move_entry_up', ())
            assert not screen.check_action('move_entry_down', ())
            app.session.save_auth_role('LLM User', [account('A'), account('B')])
            screen._load()
            screen._refresh()
            await pilot.pause()
            before = app.session.dotenv_path.read_bytes()
            def fail(*args):
                raise OSError('Synthetic write failure')
            monkeypatch.setattr(app.session, 'save_auth_role', fail)
            await pilot.press(']')
            await pilot.pause()
            assert screen.entries['LLM User'] == [account('A'), account('B')]
            assert screen.query_one('#auth-table', DataTable).cursor_row == 0
            assert app.session.dotenv_path.read_bytes() == before
            assert 'Unable to move' in str(screen.query_one('#auth-error', Static).content)
    asyncio.run(scenario())


@pytest.mark.parametrize('source', ['environment', 'cli'])
def test_reorder_override_keeps_account_identity_for_following_remove(tmp_path, source):
    async def scenario():
        external = json.dumps({'roles': {'LLM User': [account('A'), account('B')]}})
        app = WizardApp(tmp_path, environ={'UNATTENDED_AUTH': external} if source == 'environment' else None,
                        argv=(f'--unattended-auth={external}',) if source == 'cli' else ())
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one('#auth-table', DataTable)
            assert str(table.get_row_at(table.cursor_row)[3]) == 'A'
            await pilot.press(']')
            await pilot.pause()
            assert saved(app.session.dotenv_path)['roles']['LLM User'] == [account('B'), account('A')]
            assert screen.entries['LLM User'] == [account('A'), account('B')]
            assert str(table.get_row_at(table.cursor_row)[3]) == 'A'
            await pilot.press('r')
            await pilot.pause()
            assert saved(app.session.dotenv_path)['roles']['LLM User'] == [account('B')]
            assert screen.entries['LLM User'] == [account('A'), account('B')]
    asyncio.run(scenario())


def test_edit_save_preserves_both_table_scroll_offsets(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        app.session.save_auth_role('LLM User', [account(str(i) + 'x' * 140) for i in range(30)])
        app.session.save_auth_manager({'manager': 'keeper', 'cli_path': '/' + 'p' * 160})
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            listing = app.screen
            users = listing.query_one('#auth-table', DataTable)
            manager = listing.query_one('#auth-manager-table', DataTable)
            users.move_cursor(row=20)
            users.scroll_to(x=50, y=20, animate=False)
            manager.scroll_to(x=30, animate=False)
            await pilot.pause()
            before = (users.scroll_x, users.scroll_y, manager.scroll_x, manager.scroll_y)
            assert before[0] > 0 and before[1] > 0 and before[2] > 0
            users.focus()
            await pilot.press('e')
            await pilot.pause()
            app.screen.query_one('#auth-user', Input).value = 'saved-' + 'x' * 140
            app.screen.query_one('#auth-save', Button).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen is listing
            assert (users.scroll_x, users.scroll_y, manager.scroll_x, manager.scroll_y) == before
    asyncio.run(scenario())


def test_manager_starts_unselected_and_path_defaults_follow_selection(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one('#auth-manager-table', DataTable)
            assert [str(cell) for cell in table.get_row_at(0)] == ['-', '-', 'Not provided']
            table.focus()
            await pilot.press('e')
            assert app.screen.query_one('#auth-manager', Select).value == ''
            assert app.screen.query_one('#auth-path', Input).value == ''
            app.screen.query_one('#auth-manager', Select).value = 'keeper'
            await pilot.pause()
            assert app.screen.query_one('#auth-path', Input).value == 'keeper'
            app.screen.query_one('#auth-manager', Select).value = '1password'
            await pilot.pause()
            assert app.screen.query_one('#auth-path', Input).value == 'op'
            app.screen.query_one('#auth-manager', Select).value = ''
            await pilot.pause()
            assert app.screen.query_one('#auth-path', Input).value == ''
            app.screen.save_entry()
            await pilot.pause()
            assert [str(cell) for cell in table.get_row_at(0)] == ['-', '-', '../base/.env']
    asyncio.run(scenario())


def test_standalone_keeps_target_step_and_single_target_switch_is_noop(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            screen = app.screen
            before = screen.query_one('#auth-workflow', Static).content.plain
            assert before.startswith('1. (s) Choose .env target')
            assert screen.check_action('switch_target', ())
            target = app.session.dotenv_path
            await pilot.press('s')
            assert app.session.dotenv_path == target and screen.shared_target == target
            screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.pause()
            assert screen.query_one('#auth-workflow', Static).content.plain == before
            await pilot.press('s')
            assert app.session.dotenv_path == target
            await pilot.resize_terminal(45, 35)
            await pilot.pause()
            assert str(screen.query_one('#auth-workflow', Static).content).splitlines()[0] == '1. (s) Choose .env target'
    asyncio.run(scenario())


def test_child_cancel_keeps_list_geometry_cursor_and_scroll(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        app.session.save_auth_role('LLM User', [account(str(i)) for i in range(30)])
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            listing = app.screen
            users = listing.query_one('#auth-table', DataTable)
            manager = listing.query_one('#auth-manager-table', DataTable)
            viewport = listing.query_one('#auth-content', VerticalScroll)
            users.move_cursor(row=20)
            users.focus()
            await pilot.wait_for_scheduled_animations()
            before = (users.region, manager.region, viewport.scroll_y, users.cursor_row)
            await pilot.press('e')
            await pilot.pause()
            assert app.screen is not listing
            assert not app.screen.query('#auth-manager')
            assert app.screen.query_one('#auth-vault', Input).value == 'Example'
            await pilot.press('escape')
            await pilot.pause()
            await pilot.wait_for_scheduled_animations()
            assert app.screen is listing
            assert app.focused is users
            assert (users.region, manager.region, users.cursor_row) == (before[0], before[1], before[3])
            assert abs(viewport.scroll_y - before[2]) < 0.1
    asyncio.run(scenario())


def test_child_spacing_validation_and_save_returns_focus(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            listing = app.screen
            users = listing.query_one('#auth-table', DataTable)
            await pilot.press('a')
            await pilot.pause()
            editor = app.screen
            title = editor.query_one('#auth-form-title', Static)
            vault_label = editor.query_one('#auth-first-label', Static)
            vault = editor.query_one('#auth-vault', Input)
            login_label = editor.query_one('#auth-login-label', Static)
            assert title.styles.text_style.bold
            assert vault_label.region.y >= title.region.bottom + 1
            assert login_label.region.y >= vault.region.bottom + 1
            assert not editor.query('#auth-manager') and not editor.query('#auth-path')
            editor.save_entry()
            assert 'nonempty' in str(editor.query_one('#auth-error', Static).content)
            assert app.screen is editor
            assert vault.value == ''
            vault.value = 'Team vault'
            editor.query_one('#auth-user', Input).value = 'Saved user'
            await pilot.click('#auth-save')
            await pilot.pause()
            assert app.screen is listing and app.focused is users
            assert listing.query_one('#auth-table', DataTable).cursor_row == 0
            assert profile(app)['roles']['LLM User'] == [account('Saved user', 'Team vault')]
    asyncio.run(scenario())


def test_manager_editor_cancel_and_dropdown_escape(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            listing = app.screen
            manager_table = listing.query_one('#auth-manager-table', DataTable)
            manager_table.focus()
            await pilot.press('e')
            await pilot.pause()
            editor = app.screen
            assert not editor.query('#auth-vault') and not editor.query('#auth-user')
            assert app.focused is editor.query_one('#auth-manager', Select)
            await pilot.press('enter')
            await pilot.pause()
            await pilot.press('escape')
            await pilot.pause()
            assert app.screen is editor
            await pilot.press('escape')
            await pilot.pause()
            assert app.screen is listing and app.focused is manager_table
            assert not (app.context.base_root / '.env').exists()
            assert [str(cell) for cell in manager_table.get_row_at(0)] == ['-', '-', 'Not provided']
    asyncio.run(scenario())
