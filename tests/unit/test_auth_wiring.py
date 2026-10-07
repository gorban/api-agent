"""Credential setup uses real app/session wiring and only synthetic descriptors."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Input, Static

from api_agent.config import schema_from_dict
from api_agent.config.dotenv import read_dotenv
from api_agent.config.session import ConfigSession
from api_agent.tui.app import ApiAgentApp
from api_agent.tui.auth_setup import AuthSetupScreen


def app_fixture(tmp_path, *, extra=False, argv=(), environ=None):
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir(exist_ok=True)
    pack.mkdir(exist_ok=True)
    context = SimpleNamespace(base_root=base, pack_root=pack, launcher_root=base,
                              pack_id='fixture', dotenv_path=base / '.env',
                              anchors={}, external_pack=extra,
                              credential_role_fields=('SECONDARY_ROLE',) if extra else ())
    fields = {'TUI_MODE': {'type': 'str'},
              'UNATTENDED_AUTH': {'type': 'jsonobject', 'default': {}},
              'SECONDARY_ROLE': {'type': 'str', 'default': 'Partner',
                                 'acknowledge_default': False},
              'NO_STD_IN': {'type': 'bool', 'default': False, 'acknowledge_default': False}}
    schema = schema_from_dict({'version': 1, 'fields': fields})
    return ApiAgentApp(context, schema, argv=argv, environ=environ or {},
                           container_runtime=SimpleNamespace(inventory=lambda: ()))


def test_direct_wizard_mode_and_return_to_menu(tmp_path):
    async def scenario():
        app = app_fixture(tmp_path, extra=True, argv=('--tui-mode=unattended-auth',))
        async with app.run_test(size=(110, 42)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, AuthSetupScreen)
            assert app.screen.roles == ('LLM User', 'Partner')
            app.screen.action_add_entry()
            app.screen.query_one('#auth-path', Input).value = '/fake path/keeper'
            app.screen.query_one('#auth-vault', Input).value = 'Example vault'
            app.screen.save_entry()
            await pilot.pause()
            assert app.session.result(_review=True).values['UNATTENDED_AUTH']['LLM User'][0]['vault'] == 'Example vault'
            assert app.session.saved_path('UNATTENDED_AUTH') == app.context.base_root / '.env'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert app.session.dotenv_path == app.context.pack_root / '.env'
            assert app.operation_status == 0
    asyncio.run(scenario())


def test_startup_suggestion_closes_to_defaults_then_empty_array_skips_it(tmp_path):
    async def scenario():
        app = app_fixture(tmp_path)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'suggest'
            await pilot.click('#setup')
            await pilot.pause()
            assert isinstance(app.screen, AuthSetupScreen)
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
        app = app_fixture(tmp_path, environ={'UNATTENDED_AUTH': '{"LLM User":[]}'})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'menu'
    asyncio.run(scenario())


@pytest.mark.parametrize('external', ('environment', 'cli'))
def test_clear_uses_attended_for_current_run_preserves_other_sources(tmp_path, external):
    profile = json.dumps({'LLM User': []})
    app = app_fixture(tmp_path, extra=True,
                      argv=(f'--unattended-auth={profile}',) if external == 'cli' else (),
                      environ={'UNATTENDED_AUTH': profile} if external == 'environment' else {})
    base, pack = app.context.base_root / '.env', app.context.pack_root / '.env'
    base.write_text(f"UNATTENDED_AUTH='{profile}'\nKEEP=base\n")
    pack.write_text(f"UNATTENDED_AUTH='{profile}'\nKEEP=pack\n")
    before_argv, before_environment = app.session.argv, dict(app.session.environ)
    app.session.clear_auth_config()
    assert 'UNATTENDED_AUTH' not in read_dotenv(base)
    assert read_dotenv(pack)['UNATTENDED_AUTH'] == profile
    assert read_dotenv(base)['KEEP'] == 'base'
    assert app.session.result(_review=True).values['UNATTENDED_AUTH'] == {}
    assert app.session.argv == before_argv
    assert app.session.environ == before_environment
    # A subsequent launch sees its unchanged higher-precedence configuration.
    restarted = ConfigSession(app.context, app.schema, before_argv, before_environment)
    assert restarted.result(_review=True).values['UNATTENDED_AUTH'] == {'LLM User': []}


def test_primary_role_only_ignores_unselected_pack_profiles(tmp_path):
    app = app_fixture(tmp_path, environ={'UNATTENDED_AUTH': '{"LLM User":[],"Other pack":[{"unused":"value"}]}'})
    assert app.credential_roles() == ('LLM User',)
    assert not app.auth_setup_needed()
    app.context.credential_role_fields = ('UNKNOWN',)
    assert app.auth_setup_needed()
    with pytest.raises(ValueError, match='declared string'):
        app.credential_roles()


@pytest.mark.parametrize('label', ('LLM User', '  '))
def test_pack_role_labels_must_be_unique_and_nonblank(tmp_path, label):
    app = app_fixture(tmp_path, extra=True, environ={'SECONDARY_ROLE': label})
    with pytest.raises(ValueError, match='nonblank and unique'):
        app.credential_roles()


def test_invalid_direct_wizard_roles_show_actionable_screen(tmp_path):
    async def scenario():
        app = app_fixture(tmp_path, extra=True,
                          environ={'TUI_MODE': 'unattended-auth', 'SECONDARY_ROLE': 'LLM User'})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'error'
            assert app.screen.heading == 'Authentication setup configuration error'
    asyncio.run(scenario())


def test_pack_role_confirmation_displays_brackets_literally(tmp_path):
    async def scenario():
        label = '[red]Partner[/red]'
        app = app_fixture(tmp_path, extra=True,
                          environ={'TUI_MODE': 'unattended-auth',
                                   'SECONDARY_ROLE': label,
                                   'UNATTENDED_AUTH': json.dumps({'LLM User': [], label: [
                                       {'manager': 'keeper', 'cli_path': 'keeper',
                                        'vault': 'Example', 'user': 'Partner'}]})})
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one('#auth-table')
            table.move_cursor(row=1)
            app.screen.action_attended_role()
            await pilot.pause()
            message = app.screen.query_one(Static)
            assert label in str(message.content)
            assert label in message.visual.plain
            await pilot.press('escape')
            await pilot.pause()
            assert len(app.screen.entries[label]) == 1
    asyncio.run(scenario())
