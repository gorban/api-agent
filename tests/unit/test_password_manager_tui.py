"""The editor presents selected-pack manager declarations without loading them."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import DataTable, Input, Select, Static

from api_agent.config import schema_from_dict
from api_agent.config.editor import DotenvEditor
from api_agent.config.session import ConfigSession
from api_agent.tui.auth_setup import AuthSetupScreen, _manager_draft
from api_agent.tui.app import ApiAgentApp


class _Wizard(App):
    CSS = ApiAgentApp.CSS

    def __init__(self, tmp_path):
        super().__init__()
        base, pack = tmp_path / 'base', tmp_path / 'pack'
        base.mkdir()
        (pack / 'cfg').mkdir(parents=True)
        self.implementation = pack / 'manager.py'
        self.implementation.write_text('version = 1\n', encoding='utf-8')
        (pack / 'cfg/password_managers.toml').write_text(
            'version = 1\n[[managers]]\nid = "synthetic"\nlabel = "Synthetic Vault"\n'
            'implementation = "../manager.py"\ncli_default = "synthetic-cli"\n', encoding='utf-8')
        self.context = SimpleNamespace(base_root=base, pack_root=pack, launcher_root=base,
                                       dotenv_path=base / '.env', external_pack=True, anchors={})
        self.session = ConfigSession(self.context, schema_from_dict({'version': 1, 'fields': {
            'UNATTENDED_AUTH': {'type': 'jsonobject'}}}), environ={})

    async def on_mount(self):
        self.push_screen(AuthSetupScreen(('LLM User',)))

    def confirm_quit(self):
        self.exit(0)


def test_pack_choice_updates_readonly_implementation_path_and_default(tmp_path):
    async def scenario():
        app = _Wizard(tmp_path)
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            editor = app.screen
            picker = editor.query_one('#auth-manager', Select)
            implementation = editor.query_one('#auth-implementation', Static)
            path = editor.query_one('#auth-path', Input)
            assert ('Synthetic Vault', 'synthetic') in app.session.password_manager_registry.choices
            assert implementation.render() == '-'
            children = list(editor.query_one('#auth-edit-content').children)
            assert children.index(picker) < children.index(implementation) < children.index(path)
            assert implementation.can_focus is False
            picker.value = 'synthetic'
            await pilot.pause()
            assert implementation.render() == str(app.implementation.resolve())
            assert path.value == 'synthetic-cli'
            assert app.focused is picker
            await pilot.press('tab')
            assert app.focused is path
            path.value = '/custom/synthetic'
            picker.value = 'keeper'
            await pilot.pause()
            assert path.value == '/custom/synthetic'
            assert str(implementation.render()).endswith('/keeper.py')
            picker.value = ''
            await pilot.pause()
            assert implementation.render() == '-'
            assert path.value == '/custom/synthetic'
            path.value = ''
            picker.value = 'synthetic'
            await pilot.pause()
            assert path.value == 'synthetic-cli'
            editor.save_entry()
            await pilot.pause()
            assert app.session.result(_review=True).values['UNATTENDED_AUTH']['password_manager'] == {
                'manager': 'synthetic', 'cli_path': 'synthetic-cli'}
    asyncio.run(scenario())


@pytest.mark.parametrize('cold', [False, True])
def test_removed_pack_manager_editor_opens_blank_and_repairs_explicitly(tmp_path, cold):
    async def scenario():
        app = _Wizard(tmp_path)
        app.session.save_auth_role('LLM User', [{'vault': 'work', 'user': 'first'}])
        app.session.save_auth_manager({'manager': 'synthetic', 'cli_path': '/custom/old-cli'})
        if cold:
            app.context.external_pack = False
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            if not cold:
                app.context.external_pack = False
                app.screen._refresh()
            assert 'unavailable' in app.screen.load_error
            assert app.session.result(_review=True).values['UNATTENDED_AUTH'] == {
                '_auth_error': 'manager_unavailable'}
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            editor = app.screen
            picker = editor.query_one('#auth-manager', Select)
            assert picker.value == ''
            assert editor.query_one('#auth-implementation', Static).render() == '-'
            assert editor.query_one('#auth-path', Input).value == '/custom/old-cli'
            picker.value = 'keeper'
            await pilot.pause()
            assert editor.query_one('#auth-path', Input).value == '/custom/old-cli'
            editor.save_entry()
            await pilot.pause()
            result = app.session.result(_review=True).values['UNATTENDED_AUTH']
            assert result['password_manager'] == {'manager': 'keeper', 'cli_path': '/custom/old-cli'}
            assert result['roles'] == {'LLM User': [{'vault': 'work', 'user': 'first'}]}
    asyncio.run(scenario())


@pytest.mark.parametrize('source', ['environment', 'cli'])
def test_unavailable_override_editor_uses_effective_cli_draft(tmp_path, source):
    async def scenario():
        app = _Wizard(tmp_path)
        app.session.save_auth_manager({'manager': 'synthetic', 'cli_path': '/saved-cli'})
        override = json.dumps({'password_manager': {'manager': 'synthetic', 'cli_path': '/override-cli'}})
        if source == 'environment':
            app.session.environ['UNATTENDED_AUTH'] = override
        else:
            app.session.argv = (f'--unattended-auth={override}',)
        app.context.external_pack = False
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            assert 'unavailable' in app.screen.load_error
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.query_one('#auth-manager', Select).value == ''
            assert app.screen.query_one('#auth-path', Input).value == '/override-cli'
            app.screen.query_one('#auth-manager', Select).value = 'keeper'
            app.screen.save_entry()
            await pilot.pause()
            assert app.session.result(_review=True).values['UNATTENDED_AUTH']['password_manager']['cli_path'] == '/override-cli'
    asyncio.run(scenario())


@pytest.mark.parametrize('source', ['saved', 'environment', 'cli'])
def test_uniform_legacy_unavailable_manager_keeps_cli_draft(tmp_path, source):
    async def scenario():
        app = _Wizard(tmp_path)
        saved = {'LLM User': [{'manager': 'synthetic', 'cli_path': '/saved-cli',
                               'vault': 'work', 'user': 'first'}]}
        DotenvEditor(app.session.dotenv_path).update({'UNATTENDED_AUTH': json.dumps(saved)})
        if source != 'saved':
            override = {'LLM User': [{'manager': 'synthetic', 'cli_path': '/override-cli',
                                      'vault': 'work', 'user': 'first'}]}
            if source == 'environment':
                app.session.environ['UNATTENDED_AUTH'] = json.dumps(override)
            else:
                app.session.argv = (f'--unattended-auth={json.dumps(override)}',)
        app.context.external_pack = False
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            assert 'unavailable' in app.screen.load_error
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            path = app.screen.query_one('#auth-path', Input)
            assert path.value == ('/saved-cli' if source == 'saved' else '/override-cli')
            app.screen.query_one('#auth-manager', Select).value = 'keeper'
            await pilot.pause()
            assert path.value == ('/saved-cli' if source == 'saved' else '/override-cli')
    asyncio.run(scenario())


def test_legacy_draft_rejects_conflicting_or_malformed_descriptors():
    good = {'manager': 'synthetic', 'cli_path': '/one', 'vault': 'work', 'user': 'first'}
    assert _manager_draft({'LLM User': [good]}) == {'manager': 'synthetic', 'cli_path': '/one'}
    assert _manager_draft({'LLM User': [good, {**good, 'cli_path': '/two'}]}) is None
    assert _manager_draft({'LLM User': [{**good, 'vault': ''}]}) is None
    assert _manager_draft({'LLM User': [{**good, 'extra': 'value'}]}) is None
    assert _manager_draft({'LLM User': {'not': 'array'}}) is None
    assert _manager_draft(None) is None
    assert _manager_draft({'password_manager': {'manager': 'synthetic'}}) is None


def test_unavailable_manager_draft_skips_empty_higher_priority_file(tmp_path):
    async def scenario():
        app = _Wizard(tmp_path)
        app.session.save_auth_manager({'manager': 'synthetic', 'cli_path': '/base-cli'})
        (app.context.pack_root / 'cfg/password_managers.toml').unlink()
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            assert 'unavailable' in app.screen.load_error
            assert app.screen.manager['cli_path'] == '/base-cli'
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['read', 'decode'])
def test_unavailable_manager_draft_recovery_failure_keeps_blank_editor(tmp_path, monkeypatch, failure):
    async def scenario():
        app = _Wizard(tmp_path)
        app.session.save_auth_manager({'manager': 'synthetic', 'cli_path': '/saved-cli'})
        app.context.external_pack = False
        if failure == 'read':
            monkeypatch.setattr('api_agent.tui.auth_setup.read_dotenv',
                                lambda path: (_ for _ in ()).throw(OSError('synthetic read failure')))
        else:
            monkeypatch.setattr('api_agent.tui.auth_setup.read_dotenv',
                                lambda path: {'UNATTENDED_AUTH': 'not-json'})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            assert 'unavailable' in app.screen.load_error
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.query_one('#auth-manager', Select).value == ''
            assert app.screen.query_one('#auth-path', Input).value == ''
    asyncio.run(scenario())


def test_saved_cli_equal_other_manager_default_survives_edit_save_reload(tmp_path):
    async def scenario():
        app = _Wizard(tmp_path)
        app.session.save_auth_manager({'manager': '1password', 'cli_path': 'synthetic-cli'})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            for expected_manager in ('1password', 'keeper'):
                app.screen.query_one('#auth-manager-table', DataTable).focus()
                await pilot.press('e')
                await pilot.pause()
                editor = app.screen
                picker = editor.query_one('#auth-manager', Select)
                path = editor.query_one('#auth-path', Input)
                assert picker.value == expected_manager
                assert path.value == 'synthetic-cli'
                picker.value = 'synthetic'
                await pilot.pause()
                assert path.value == 'synthetic-cli'
                picker.value = 'keeper'
                await pilot.pause()
                assert path.value == 'synthetic-cli'
                editor.save_entry()
                await pilot.pause()
                assert ConfigSession(app.context, app.session.schema, environ={}).result(_review=True).values['UNATTENDED_AUTH']['password_manager'] == {
                    'manager': 'keeper', 'cli_path': 'synthetic-cli'}
    asyncio.run(scenario())


@pytest.mark.parametrize('cli_path', ['op', 'keeper', 'synthetic-cli'])
def test_explicit_cli_edit_equal_manager_default_stays_on_switch(tmp_path, cli_path):
    async def scenario():
        app = _Wizard(tmp_path)
        app.session.save_auth_manager({'manager': '1password', 'cli_path': '/custom'})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            editor = app.screen
            path = editor.query_one('#auth-path', Input)
            path.value = cli_path
            await pilot.pause()
            for manager in ('synthetic', 'keeper', '1password', ''):
                editor.query_one('#auth-manager', Select).value = manager
                await pilot.pause()
                assert path.value == cli_path
    asyncio.run(scenario())


def test_unedited_cli_follows_each_selected_managers_own_default(tmp_path):
    async def scenario():
        app = _Wizard(tmp_path)
        app.session.save_auth_manager({'manager': '1password', 'cli_path': 'op'})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            editor = app.screen
            for manager in ('synthetic', 'keeper', '', '1password'):
                editor.query_one('#auth-manager', Select).value = manager
                await pilot.pause()
                definition = app.session.password_manager_registry.get(manager) if manager else None
                assert editor.query_one('#auth-path', Input).value == (definition.cli_default if definition else '')
    asyncio.run(scenario())


def test_stale_clear_event_keeps_generated_default_following_through_save_reload(tmp_path):
    async def scenario():
        app = _Wizard(tmp_path)
        app.session.save_auth_manager({'manager': '1password', 'cli_path': 'op'})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            # Repeat the queued sequence across saved/reopened editors.
            for _ in range(3):
                app.screen.query_one('#auth-manager-table', DataTable).focus()
                await pilot.press('e')
                await pilot.pause()
                editor = app.screen
                picker = editor.query_one('#auth-manager', Select)
                path = editor.query_one('#auth-path', Input)
                assert path.value == 'op'
                picker.value = 'synthetic'
                path.value = ''
                picker.value = 'keeper'
                await pilot.pause()
                assert path.value == 'keeper'
                assert editor._cli_draft_value == 'keeper'
                assert not editor._cli_explicit
                picker.value = '1password'
                await pilot.pause()
                assert path.value == 'op'
                editor.save_entry()
                await pilot.pause()
                saved = ConfigSession(app.context, app.session.schema, environ={}).result(_review=True)
                assert saved.values['UNATTENDED_AUTH']['password_manager'] == {
                    'manager': '1password', 'cli_path': 'op'}
    asyncio.run(scenario())


@pytest.mark.parametrize('initial_manager, initial_cli', [('1password', 'op'), ('synthetic', 'synthetic-cli')])
def test_queued_clear_restore_preserves_explicit_previous_default_through_save_reload(
        tmp_path, initial_manager, initial_cli):
    async def scenario():
        for attempt in range(3):
            root = tmp_path / str(attempt)
            root.mkdir()
            app = _Wizard(root)
            app.session.save_auth_manager({'manager': initial_manager, 'cli_path': initial_cli})
            async with app.run_test(size=(110, 55)) as pilot:
                await pilot.pause()
                app.screen.query_one('#auth-manager-table', DataTable).focus()
                await pilot.press('e')
                await pilot.pause()
                editor = app.screen
                picker = editor.query_one('#auth-manager', Select)
                path = editor.query_one('#auth-path', Input)
                picker.value = 'synthetic' if initial_manager != 'synthetic' else '1password'
                path.value = ''
                picker.value = 'keeper'
                path.value = initial_cli
                await pilot.pause()
                assert picker.value == 'keeper'
                assert path.value == initial_cli
                assert editor._cli_explicit
                # Future choices must preserve the same deliberate executable.
                picker.value = '1password'
                await pilot.pause()
                assert path.value == initial_cli
                picker.value = 'keeper'
                await pilot.pause()
                assert path.value == initial_cli
                editor.save_entry()
                await pilot.pause()
                saved = ConfigSession(app.context, app.session.schema, environ={}).result(_review=True)
                assert saved.values['UNATTENDED_AUTH']['password_manager'] == {
                    'manager': 'keeper', 'cli_path': initial_cli}
                app.screen.query_one('#auth-manager-table', DataTable).focus()
                await pilot.press('e')
                await pilot.pause()
                assert app.screen.query_one('#auth-manager', Select).value == 'keeper'
                assert app.screen.query_one('#auth-path', Input).value == initial_cli
    asyncio.run(scenario())


@pytest.mark.parametrize('manager, cli', [('1password', 'op'), ('synthetic', 'synthetic-cli')])
def test_same_text_selected_paste_is_an_explicit_executable_edit(tmp_path, manager, cli):
    from textual import events
    async def scenario():
        app = _Wizard(tmp_path)
        app.session.save_auth_manager({'manager': manager, 'cli_path': cli})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            editor = app.screen
            path = editor.query_one('#auth-path', Input)
            path.focus()
            await pilot.pause()
            await pilot.press('ctrl+shift+a')
            assert not path.selection.is_empty
            path.post_message(events.Paste(cli))
            await pilot.pause()
            assert path.value == cli
            assert editor._cli_explicit
            await pilot.press('backspace', cli[-1])
            assert path.value == cli
            assert editor._cli_explicit
            editor.query_one('#auth-manager', Select).value = 'keeper'
            await pilot.pause()
            assert path.value == cli
            editor.save_entry()
            await pilot.pause()
            saved = ConfigSession(app.context, app.session.schema, environ={}).result(_review=True)
            assert saved.values['UNATTENDED_AUTH']['password_manager'] == {'manager': 'keeper', 'cli_path': cli}
            app.screen.query_one('#auth-manager-table', DataTable).focus()
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.query_one('#auth-path', Input).value == cli
    asyncio.run(scenario())
