"""Credential setup UI uses temporary .env files and no password-manager process."""
import asyncio
import json
from types import SimpleNamespace
from xml.etree import ElementTree

from textual.app import App
from textual.widgets import DataTable, Input, Static

from api_agent.config import schema_from_dict
from api_agent.config.session import ConfigSession
from api_agent.config.dotenv import read_dotenv
from api_agent.config.editor import DotenvEditor
from api_agent.tui.auth_setup import AuthSetupScreen
from api_agent.tui.app import ApiAgentApp


class WizardApp(App):
    CSS = ApiAgentApp.CSS
    def __init__(self, tmp_path, *, environ=None, roles=("LLM User",), external=False, argv=()):
        super().__init__()
        base = tmp_path / "base"
        pack = tmp_path / "pack"
        base.mkdir(parents=True, exist_ok=True)
        pack.mkdir(parents=True, exist_ok=True)
        self.context = SimpleNamespace(base_root=base, pack_root=pack, launcher_root=base,
                                       dotenv_path=base / ".env", anchors={}, external_pack=external)
        self.session = ConfigSession(self.context, schema_from_dict({"version": 1, "fields": {
            "UNATTENDED_AUTH": {"type": "jsonobject"}}}), argv=argv, environ=environ or {})
        self.roles = roles

    def target_summary(self):
        return "Target .env files:\n" + "\n".join(
            f"{'✓' if path == self.session.dotenv_path else ' '} {path}" for path in self.session.target_paths)

    async def on_mount(self):
        self.push_screen(AuthSetupScreen(self.roles))

    def confirm_quit(self):
        self.exit(0)


def _profile(app):
    return app.session.result(_review=True).values["UNATTENDED_AUTH"]


def test_add_edit_remove_and_empty_role(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, roles=("LLM User", "Partner"))
        async with app.run_test(size=(110, 60)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 2
            await pilot.press("a")
            app.screen.query_one("#auth-path", Input).value = "/some path/keeper"
            app.screen.query_one("#auth-vault", Input).value = "Team [A]"
            await pilot.click("#auth-save")
            await pilot.pause()
            assert _profile(app) == {"LLM User": [{"manager": "keeper", "cli_path": "/some path/keeper", "vault": "Team [A]", "user": "LLM User"}]}
            assert 'UNATTENDED_AUTH=' in (tmp_path / "base" / ".env").read_text()
            table.move_cursor(row=0)
            await pilot.press("e")
            app.screen.query_one("#auth-user", Input).value = "Second"
            await pilot.click("#auth-save")
            await pilot.pause()
            assert _profile(app)["LLM User"][0]["user"] == "Second"
            await pilot.press("a")
            app.screen.query_one("#auth-vault", Input).value = "Another"
            await pilot.click("#auth-save")
            await pilot.pause()
            assert len(_profile(app)["LLM User"]) == 2
            table.move_cursor(row=0)
            await pilot.press("r")
            await pilot.pause()
            assert len(_profile(app)["LLM User"]) == 1
            assert "Partner" not in _profile(app)
            table.move_cursor(row=1)
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert _profile(app)["Partner"] == []
    asyncio.run(scenario())


def test_validation_clear_and_target_switch(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, external=True)
        async with app.run_test(size=(110, 60)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.click("#auth-save")
            assert "nonempty" in str(app.screen.query_one("#auth-error", Static).render())
            app.screen.query_one("#auth-vault", Input).value = "V"
            app.screen.query_one("#auth-vault", Input).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.editing is None, (app.screen.query_one("#auth-manager").value, app.screen.query_one("#auth-path", Input).value, app.screen.query_one("#auth-vault", Input).value, app.screen.query_one("#auth-user", Input).value)
            await pilot.press("s")
            assert app.session.dotenv_path == tmp_path / "pack" / ".env"
            assert str(tmp_path / "pack" / ".env") in str(app.screen.query_one("#auth-targets", Static).render())
            await pilot.press("x")
            app.screen.query_one(Input).value = "n"
            await pilot.press("enter")
            await pilot.pause()
            assert _profile(app)["LLM User"]
            await pilot.press("x")
            app.screen.query_one(Input).value = "y"
            await pilot.press("enter")
            await pilot.pause()
            assert _profile(app) == {}
            assert app.screen.query_one(DataTable).row_count == 1
    asyncio.run(scenario())


def test_environment_override_is_explained(tmp_path):
    async def scenario():
        value = '{"LLM User":[]}'
        app = WizardApp(tmp_path, environ={"UNATTENDED_AUTH": value})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            text = str(app.screen.query_one("#auth-override", Static).render())
            assert "environment" in text
            assert "overrides" in text
    asyncio.run(scenario())


def test_invalid_profile_is_reported_without_echoing_value(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, environ={"UNATTENDED_AUTH": '{"LLM User":[{"secret":"do-not-render"}]}'})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            error = str(app.screen.query_one("#auth-error", Static).render())
            assert "invalid" in error
            assert "do-not-render" not in error
            assert app.screen.query_one(DataTable).row_count == 1
    asyncio.run(scenario())


def test_form_blocks_mutating_hotkeys_and_manager_defaults(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path)
        async with app.run_test(size=(110, 60)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            screen = app.screen
            for key in ("r", "x", "a", "s"):
                await pilot.press(key)
            assert screen.editing == ("LLM User", None)
            assert not (tmp_path / "base" / ".env").exists()
            screen.query_one("#auth-path", Input).value = "keeper"
            screen.query_one("#auth-manager").value = "1password"
            await pilot.pause()
            assert screen.query_one("#auth-path", Input).value == "op"
            screen.query_one("#auth-path", Input).value = "/custom/op"
            screen.query_one("#auth-manager").value = "keeper"
            await pilot.pause()
            assert screen.query_one("#auth-path", Input).value == "/custom/op"
            screen.query_one("#auth-vault", Input).value = "vault"
            screen.query_one("#auth-vault", Input).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert screen.editing is None
            assert _profile(app)["LLM User"][0]["cli_path"] == "/custom/op"
    asyncio.run(scenario())


def test_attended_choice_and_external_clear(tmp_path):
    async def scenario():
        external = '{"LLM User":[{"manager":"keeper","cli_path":"keeper","vault":"v","user":"u"}]}'
        app = WizardApp(tmp_path, environ={"UNATTENDED_AUTH": external})
        async with app.run_test(size=(110, 60)) as pilot:
            await pilot.pause()
            await pilot.press("t")
            app.screen.query_one(Input).value = "n"
            await pilot.press("enter")
            await pilot.pause()
            assert _profile(app)["LLM User"]
            await pilot.press("t")
            app.screen.query_one(Input).value = "y"
            await pilot.press("enter")
            await pilot.pause()
            assert (tmp_path / "base" / ".env").exists()
            assert app.session.environ["UNATTENDED_AUTH"] == external
            assert _profile(app)["LLM User"]
            await pilot.press("x")
            app.screen.query_one(Input).value = "y"
            await pilot.press("enter")
            await pilot.pause()
            assert _profile(app) == {}
            message = str(app.screen.query_one("#auth-error", Static).render())
            assert "environment" in message
            assert "attended login" in message
            assert app.session.environ["UNATTENDED_AUTH"] == external
    asyncio.run(scenario())


def test_cli_override_clear_and_close_quit_keys(tmp_path):
    async def scenario():
        external = '{"LLM User":[]}'
        app = WizardApp(tmp_path, argv=(f"--UNATTENDED_AUTH={external}",))
        async with app.run_test(size=(110, 60)) as pilot:
            await pilot.pause()
            assert "cli" in str(app.screen.query_one("#auth-override", Static).render())
            await pilot.press("q")
            assert app.screen.query_one(Input).id == "quit-answer"
            app.screen.query_one(Input).value = "n"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, AuthSetupScreen)
            await pilot.press("x")
            app.screen.query_one(Input).value = "y"
            await pilot.press("enter")
            await pilot.pause()
            assert "cli" in str(app.screen.query_one("#auth-error", Static).render())
            assert app.session.argv == (f"--UNATTENDED_AUTH={external}",)
            assert _profile(app) == {}
            await pilot.press("c")
            await pilot.pause()
            assert not isinstance(app.screen, AuthSetupScreen)
    asyncio.run(scenario())


def test_other_pack_role_is_preserved_without_validation_or_display(tmp_path):
    async def scenario():
        other = [{"manager": "unknown", "cli_path": "", "vault": "", "user": "hidden"}]
        raw = '{"Other pack":' + __import__("json").dumps(other) + ',"LLM User":[]}'
        app = WizardApp(tmp_path)
        (tmp_path / "base" / ".env").write_text(f"UNATTENDED_AUTH='{raw}'\n")
        async with app.run_test(size=(110, 60)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 1
            assert "hidden" not in str(table.get_row_at(0))
            assert "invalid" not in str(app.screen.query_one("#auth-error", Static).render())
            await pilot.press("t")
            await pilot.pause()
            saved = __import__("api_agent.config.dotenv", fromlist=["read_dotenv"]).read_dotenv(tmp_path / "base" / ".env")
            assert __import__("json").loads(saved["UNATTENDED_AUTH"]) == {"Other pack": other, "LLM User": []}
    asyncio.run(scenario())


def test_footer_actions_without_duplicate_buttons(tmp_path):
    async def scenario(size):
        app = WizardApp(tmp_path / f"size-{size[1]}", external=True)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            screen = app.screen
            assert not screen.query("#auth-actions Button")
            assert not screen.query("#tab-select-hint")
            rendered = "".join(ElementTree.fromstring(app.export_screenshot()).itertext()).replace("\xa0", " ")
            for label in ("Add", "Edit", "Remove", "Clear", "Use attended"):
                assert label in rendered
            await pilot.press("a")
            await pilot.pause()
            assert screen.query("#tab-select-hint")
            for selector, label in (("#auth-save", "Save account"), ("#auth-cancel", "Cancel")):
                button = screen.query_one(selector)
                button.scroll_visible(animate=False)
                await pilot.pause()
                rendered = "".join(ElementTree.fromstring(app.export_screenshot()).itertext()).replace("\xa0", " ")
                assert label in rendered, (size, selector)
            await pilot.press("escape")
            await pilot.pause()
            assert not screen.query("#tab-select-hint")
    asyncio.run(scenario((100, 35)))
    asyncio.run(scenario((80, 24)))


def test_readonly_workflow_reflows_on_narrow_terminal(tmp_path):
    async def scenario(external):
        app = WizardApp(tmp_path / str(external), external=external)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            guide = app.screen.query_one('#auth-workflow', Static)
            account_help = app.screen.query_one('#auth-profile-help', Static)
            assert not account_help.can_focus
            assert 'password and optional TOTP' in str(account_help.content)
            assert 'exact vault and login/item names' in str(account_help.content)
            assert 'matching indexes' in str(account_help.content)
            assert not guide.can_focus
            assert app.focused is app.screen.query_one(DataTable)
            lines = str(guide.content).splitlines()
            assert len(lines) == 5
            assert lines[0].startswith('1. ')
            assert '6. ' in lines[0]
            assert ('Choose .env target' in str(guide.content)) == external
            await pilot.resize_terminal(45, 35)
            await pilot.pause()
            lines = str(guide.content).splitlines()
            assert len(lines) == (10 if external else 9)
            assert lines[-1].endswith('(c) Close setup')
            await pilot.resize_terminal(100, 35)
            await pilot.pause()
            assert len(str(guide.content).splitlines()) == 5
    asyncio.run(scenario(True))
    asyncio.run(scenario(False))


def test_header_spacing_and_workflow_shortcut_styles(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, external=True)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            screen = app.screen
            storage = screen.query_one('#auth-storage', Static)
            help_text = screen.query_one('#auth-profile-help')
            selection_help = screen.query_one('#auth-selection-help')
            targets = screen.query_one('#auth-targets')
            assert not screen.query('#auth-source')
            assert str(storage.content) == 'Only a pointer to your password-manager entry is saved.'
            assert storage.styles.text_align == 'right'
            assert help_text.region.y == storage.region.bottom + 1
            assert not screen.query_one('#auth-error').display
            assert targets.region.y == selection_help.region.bottom + 1
            workflow = screen.query_one('#auth-workflow', Static).content
            assert [workflow.plain[span.start:span.end] for span in workflow.spans] == ['s', 'a', 'Enter', 'c']
            assert all(span.style == f"bold {app.get_css_variables()['footer-key-foreground']}"
                       for span in workflow.spans)
            for span in workflow.spans:
                assert workflow.plain[span.start - 1] == '('
                assert workflow.plain[span.end] == ')'
            screen._error('Example error')
            await pilot.pause()
            assert screen.query_one('#auth-error').display
            screen._error('')
            await pilot.pause()
            assert not screen.query_one('#auth-error').display
            assert targets.region.y == selection_help.region.bottom + 1
    asyncio.run(scenario())


def test_pack_role_target_is_temporary_and_form_only_saves_to_pack(tmp_path):
    async def scenario(start_in_pack):
        app = WizardApp(tmp_path / str(start_in_pack), external=True, roles=('LLM User', 'Partner'))
        base, pack = app.session.context.base_root / '.env', app.session.context.pack_root / '.env'
        previous = pack if start_in_pack else base
        app.session.switch_target(previous)
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one(DataTable)
            table.move_cursor(row=1)
            await pilot.pause()
            assert app.session.dotenv_path == pack
            targets = str(screen.query_one('#auth-targets', Static).content)
            assert targets.splitlines() == ['Target .env files:', f'✓ {pack}']
            assert not screen.check_action('switch_target', ())
            assert 'Choose .env target' not in str(screen.query_one('#auth-workflow').content)
            screen.action_switch_target()
            assert app.session.dotenv_path == pack
            await pilot.press('a')
            screen.query_one('#auth-vault', Input).value = 'Example vault'
            assert str(screen.query_one('#auth-targets', Static).content) == targets
            await pilot.click('#auth-save')
            await pilot.pause()
            assert table.cursor_row == 1
            assert json.loads(read_dotenv(pack)['UNATTENDED_AUTH'])['Partner'][0]['vault'] == 'Example vault'
            assert 'UNATTENDED_AUTH' not in read_dotenv(base)
            table.move_cursor(row=0)
            await pilot.pause()
            assert app.session.dotenv_path == previous
            assert str(base) in str(screen.query_one('#auth-targets', Static).content)
            assert str(pack) in str(screen.query_one('#auth-targets', Static).content)
            await pilot.press('a')
            screen.query_one('#auth-vault', Input).value = 'LLM vault'
            await pilot.click('#auth-save')
            await pilot.pause()
            profiles = app.session.result(_review=True).values['UNATTENDED_AUTH']
            assert profiles['LLM User'][0]['vault'] == 'LLM vault'
            assert profiles['Partner'][0]['vault'] == 'Example vault'
            if not start_in_pack:
                assert set(json.loads(read_dotenv(base)['UNATTENDED_AUTH'])) == {'LLM User'}
                assert set(json.loads(read_dotenv(pack)['UNATTENDED_AUTH'])) == {'Partner'}
    asyncio.run(scenario(False))
    asyncio.run(scenario(True))


def test_pack_role_saved_in_agent_file_can_still_be_read_and_removed(tmp_path):
    async def scenario():
        app = WizardApp(tmp_path, external=True, roles=('LLM User', 'Partner'))
        base, pack = app.session.context.base_root / '.env', app.session.context.pack_root / '.env'
        llm_accounts = [{'manager': 'keeper', 'cli_path': 'keeper', 'vault': 'Example', 'user': 'LLM'}]
        secondary = [{'manager': 'keeper', 'cli_path': 'keeper', 'vault': 'Example', 'user': 'Partner'}]
        DotenvEditor(base).update({'UNATTENDED_AUTH': json.dumps({'LLM User': llm_accounts, 'Partner': secondary}),
                                 'KEEP': 'unchanged'})
        async with app.run_test(size=(110, 55)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.entries['Partner'] == secondary
            screen.query_one(DataTable).move_cursor(row=1)
            await pilot.pause()
            await pilot.press('r')
            await pilot.pause()
            assert json.loads(read_dotenv(base)['UNATTENDED_AUTH']) == {'LLM User': llm_accounts}
            assert read_dotenv(base)['KEEP'] == 'unchanged'
            assert json.loads(read_dotenv(pack)['UNATTENDED_AUTH']) == {'Partner': []}
            assert screen.entries['Partner'] == []
    asyncio.run(scenario())
