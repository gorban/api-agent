"""Credential setup uses real app/session wiring and only synthetic descriptors."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from textual.widgets import Input, Static

from api_agent.config import schema_from_dict
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
            await pilot.pause()
            app.screen.query_one('#auth-vault', Input).value = 'Example vault'
            app.screen.save_entry()
            await pilot.pause()
            assert app.session.result(_review=True).values['UNATTENDED_AUTH']['roles']['LLM User'][0]['vault'] == 'Example vault'
            assert app.session.saved_path('UNATTENDED_AUTH') == app.context.base_root / '.env'
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert app.session.dotenv_path == app.context.pack_root / '.env'
            assert app.operation_status == 0
    asyncio.run(scenario())


def test_startup_suggestion_closes_to_defaults_then_empty_array_still_suggests_it(tmp_path):
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
            assert app.screen.kind == 'suggest'
    asyncio.run(scenario())



def test_primary_role_only_ignores_unselected_pack_profiles(tmp_path):
    app = app_fixture(tmp_path, environ={'UNATTENDED_AUTH': json.dumps({'password_manager': {'manager': 'keeper', 'cli_path': 'keeper'}, 'roles': {'LLM User': [{'vault': 'Example', 'user': 'LLM'}], 'Other pack': [{'unused': 'value'}]}})})
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


def test_pack_role_form_displays_brackets_literally(tmp_path):
    async def scenario():
        label = '[red]Partner[/red]'
        app = app_fixture(tmp_path, extra=True,
                          environ={'TUI_MODE': 'unattended-auth',
                                   'SECONDARY_ROLE': label,
                                   'UNATTENDED_AUTH': json.dumps({'roles': {'LLM User': [], label: [
                                       {'vault': 'Example', 'user': 'Partner'}]}})})
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one('#auth-table')
            table.move_cursor(row=1)
            await pilot.press('e')
            await pilot.pause()
            title = app.screen.query_one('#auth-form-title', Static)
            assert label in title.visual.plain
            await pilot.press('escape')
            await pilot.pause()
            assert len(app.screen.entries[label]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('roles,manager,needed', [
    ({'LLM User': []}, {'manager': '', 'cli_path': ''}, True),
    ({'LLM User': [{'vault': 'Example', 'user': 'LLM'}]}, {'manager': '', 'cli_path': ''}, True),
    ({'LLM User': [{'vault': 'Example', 'user': 'LLM'}]}, {'manager': 'keeper', 'cli_path': 'keeper'}, True),
    ({'LLM User': [{'vault': 'Example', 'user': 'LLM'}], 'Partner': []}, {'manager': 'keeper', 'cli_path': 'keeper'}, True),
    ({'LLM User': [{'vault': 'Example', 'user': 'LLM'}], 'Partner': [{'vault': 'Example', 'user': 'Partner'}]}, {'manager': '', 'cli_path': ''}, True),
    ({'LLM User': [{'vault': 'Example', 'user': 'LLM'}], 'Partner': [{'vault': 'Example', 'user': 'Partner'}]}, {'manager': 'keeper', 'cli_path': 'keeper'}, False),
    ({'LLM User': [{'vault': 'Example', 'user': 'LLM'}, {'vault': 'Example', 'user': 'LLM2'}], 'Partner': [{'vault': 'Example', 'user': 'Partner'}]}, {'manager': '1password', 'cli_path': 'op'}, False),
])
def test_setup_readiness_requires_every_role_and_manager(tmp_path, roles, manager, needed):
    app = app_fixture(tmp_path, extra=True, environ={'UNATTENDED_AUTH': json.dumps({'password_manager': manager, 'roles': roles})})
    assert app.auth_setup_needed() is needed
