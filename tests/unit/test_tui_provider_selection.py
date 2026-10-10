"""Rendered provider selection refreshes contributed declarations without imports."""
import asyncio
from types import SimpleNamespace

from textual.widgets import DataTable, Input

from api_agent.config import load_shared_schema
from api_agent.config.dotenv import read_dotenv
from api_agent.tui.app import ApiAgentApp


def test_bundled_provider_is_reviewable_and_switching_refreshes_tables(tmp_path):
    alternate = tmp_path / 'alternate'
    (alternate / 'cfg').mkdir(parents=True)
    factory = alternate / 'provider.py'
    factory.write_text("raise AssertionError('configuration must not import the factory')\n")
    (alternate / 'cfg/config.toml').write_text(
        'version=1\n[fields.ALTERNATE_ENDPOINT]\ntype="str"\ndefault="https://alternate.example.test"\n')
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path,
                              pack_id='fixture', external_pack=False, dotenv_path=tmp_path / '.env',
                              anchors={name: tmp_path for name in ('base_root', 'pack_root', 'owner_root')})
    schema = load_shared_schema()
    expected = schema.fields['LLM_PROVIDER'].default
    app = ApiAgentApp(context, schema, environ={},
                      container_runtime=SimpleNamespace(inventory=lambda: ()))

    def names():
        table = app.screen.query_one(DataTable)
        return [row.key.value for row in table.ordered_rows]

    async def scenario():
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'suggest'
            await pilot.press('s')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert 'LLM_PROVIDER' in names()
            assert 'GPT_AUTH_MODE' in names()
            assert 'GPT_PROFILE' not in app.schema.fields
            assert 'GPT_STATE_DIR' in names()
            assert app.credential_roles() == ('LLM User',) and app.auth_setup_needed()
            table = app.screen.query_one(DataTable)
            index = names().index('LLM_PROVIDER')
            assert str(table.get_row_at(index)[1]) == expected
            table.move_cursor(row=index)
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.kind == 'input' and app.prompt_name == 'LLM_PROVIDER'
            app.screen.query_one(Input).value = str(factory) + ':factory'
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert 'ALTERNATE_ENDPOINT' in names()
            assert not any(name.startswith('GPT_') for name in names())
            assert read_dotenv(tmp_path / '.env')['LLM_PROVIDER'] == str(factory) + ':factory'
            assert app.session.result().values['LLM_PROVIDER'] == str(factory) + ':factory'
            assert not any(name.startswith('GPT_') for name in app.session.schema.fields)
            app.show_cleanup()
            await pilot.pause()
            assert 'LLM_PROVIDER' in names() and 'ALTERNATE_ENDPOINT' in names()
            assert not any(name.startswith('GPT_') for name in names())
            app.session.delete_keys(['LLM_PROVIDER'])
            app.review_names.clear()
            app.show_configuration()
            await pilot.pause()
            assert 'LLM_PROVIDER' in names() and 'GPT_AUTH_MODE' in names()
            assert 'ALTERNATE_ENDPOINT' not in names()
            assert app.session.result().values['LLM_PROVIDER'] == expected

    asyncio.run(scenario())


def test_actual_gpt_declaration_suggests_setup_until_account_and_manager_complete(tmp_path):
    import json
    from api_agent.tui.auth_setup import AuthSetupScreen
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path,
        pack_id='fixture', external_pack=False, dotenv_path=tmp_path / '.env',
        anchors={name: tmp_path for name in ('base_root', 'pack_root', 'owner_root')})
    account = {'vault': 'fixture', 'user': 'login'}
    cases = [({}, True),
             ({'roles': {'LLM User': []}}, True),
             ({'roles': {'LLM User': [account]}}, True),
             ({'roles': {'LLM User': [account]}, 'password_manager': {'manager': '1password', 'cli_path': ''}}, False),
             ({'roles': {'LLM User': [account]}, 'password_manager': {'manager': '1password', 'cli_path': 'op'}}, False)]
    async def scenario():
        for profile, needed in cases:
            app = ApiAgentApp(context, load_shared_schema(), environ={'UNATTENDED_AUTH': json.dumps(profile)},
                container_runtime=SimpleNamespace(inventory=lambda: ()))
            async with app.run_test(size=(110, 40)) as pilot:
                await pilot.pause()
                assert app.credential_roles() == ('LLM User',)
                assert app.auth_setup_needed() is needed
                assert (app.screen.kind == 'suggest') is needed
                if needed:
                    assert 'skip' in {key for key, label in app.screen.actions}
                    await pilot.press('enter')
                    await pilot.pause()
                    assert isinstance(app.screen, AuthSetupScreen)
                    await pilot.press('c')
                    await pilot.pause()
                    assert app.screen.kind == 'defaults'
                else:
                    assert app.screen.kind == 'defaults'
    asyncio.run(scenario())


def test_gpt_modes_reuse_llm_user_entries_and_paired_indexes(tmp_path):
    import json
    from api_agent.tui.accounts import account_options
    profile = {'password_manager': {'manager': '1password', 'cli_path': 'op'},
        'roles': {'LLM User': [{'vault': 'fixture', 'user': 'subscription'},
                               {'vault': 'fixture', 'user': 'api-key'}],
                  'Partner': [{'vault': 'fixture', 'user': 'UserA'},
                              {'vault': 'fixture', 'user': 'UserA'}]}}
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path,
        pack_id='fixture', external_pack=False, dotenv_path=tmp_path / '.env',
        anchors={name: tmp_path for name in ('base_root', 'pack_root', 'owner_root')})
    app = ApiAgentApp(context, load_shared_schema(),
        environ={'UNATTENDED_AUTH': json.dumps(profile), 'UNATTENDED_USER_INDEX': '1'},
        container_runtime=SimpleNamespace(inventory=lambda: ()))
    for mode in ('subscription', 'api_key', 'subscription'):
        app.session.environ['GPT_AUTH_MODE'] = mode
        result = app.session.result(_review=True)
        assert app.credential_roles() == ('LLM User',)
        assert not app.auth_setup_needed()
        assert result.values['UNATTENDED_AUTH'] == profile
        assert result.values['UNATTENDED_USER_INDEX'] == 1
        assert account_options(profile, (*app.credential_roles(), 'Partner'), paired=True) == (
            (0, 'subscription / UserA'), (1, 'api-key / UserA'))
    assert not (tmp_path / '.env').exists()
