"""Headless UI tests use temporary configuration and simulated discovery."""
import asyncio
from rich.text import Text
from pathlib import Path
from types import SimpleNamespace

from api_agent.config import schema_from_dict
from api_agent.testing import TestIdentity as Identity
from api_agent.tui.app import ApiAgentApp, E2E_WARNING
from textual.widgets import Button, DataTable, Input, Static


def fixture(tmp_path, mode=None, fields=None, collector=None, argv=()):
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, pack_id="fixture",
                              dotenv_path=tmp_path / ".env", anchors={}, external_pack=False)
    declarations = {"TUI_MODE": {"type": "str"}, "UNATTENDED_AUTH": {"type": "jsonobject"}}
    declarations.update(fields or {})
    schema = schema_from_dict({"version": 1, "fields": declarations})
    environ = {"UNATTENDED_AUTH": '{"LLM User":[{"manager":"keeper","cli_path":"/bin/keeper","vault":"fixture","user":"fixture"}]}'}
    if mode:
        environ["TUI_MODE"] = mode
    return ApiAgentApp(context, schema, argv=argv, environ=environ, collector=collector, container_runtime=SimpleNamespace(inventory=lambda: ()))


def test_defaults_save_and_typed_prompt(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            await pilot.press("e")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "bad"
            await pilot.click("#submit")
            await pilot.pause(0.4)
            assert "expected integer" in str(app.screen.query_one("#error", Static).render())
            app.screen.query_one(Input).value = "8"
            await pilot.click("#submit")
            await pilot.pause(0.4)
            assert app.session.result().values["COUNT"] == 8
            assert app.screen.kind == "defaults"
            assert (tmp_path / ".env").read_text() == 'COUNT="8"\n'
            assert app.screen.query_one(DataTable).get_row_at(0)[2] == app.session._source(app.session.dotenv_path)
    asyncio.run(scenario())
    (tmp_path / ".env").unlink()
    async def save():
        app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("a")
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            assert app.screen.query_one(DataTable).get_row_at(0)[2] == app.session._source(app.session.dotenv_path)
            assert app.session.result().values["COUNT"] == 4
            assert "COUNT=" in (tmp_path / ".env").read_text()
    asyncio.run(save())


def test_cleanup_context_and_deletion(tmp_path):
    (tmp_path / ".env").write_text("# keep\nCOUNT=1\nCOUNT=2\nUNATTENDED_AUTH='{}'\n")
    async def scenario():
        app = fixture(tmp_path, "config-cleanup", {"COUNT": {"type": "int", "default": 4}, "OTHER": {"type": "str", "default": "fallback"}})
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(0.4)
            assert app.screen.kind == "cleanup"
            table = app.screen.query_one(DataTable)
            assert "UNATTENDED_AUTH" not in [table.get_row_at(i)[1] for i in range(table.row_count)]
            row = next(i for i in range(table.row_count) if table.get_row_at(i)[1] == "COUNT")
            table.move_cursor(row=row)
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert table.get_row_at(row)[0] == "✓"
            assert "Select a row and delete it (with confirmation) from agent .env file." in str(app.screen.query_one("#body", Static).render())
            await pilot.press("d")
            await pilot.pause(0.4)
            assert not app.screen_stack[-2].check_action("switch_target", ())
            assert not app.screen_stack[-2].check_action("delete_assignment", ())
            app.screen.query_one(Input).value = "n"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert table.get_row_at(row)[0] == "✓"
            assert "COUNT=" in (tmp_path / ".env").read_text()
            await pilot.press("d")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "y"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert table.get_row_at(row)[0] == ""
            assert table.get_row_at(row)[2] == "4"
            assert not app.focused_cleanup_present()
            assert "# keep" in (tmp_path / ".env").read_text()
            assert "COUNT=" not in (tmp_path / ".env").read_text()
    asyncio.run(scenario())


def test_e2e_picker_acceptance_and_unavailable(tmp_path):
    identities = [Identity("base::tests/test_x.py::test_a", "base", "tests/test_x.py", "tests/test_x.py::test_a", frozenset({"mutation"}))]
    async def scenario():
        app = fixture(tmp_path, "e2e", collector=lambda *args: identities)
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause(0.4)
            await pilot.pause(0.4)
            assert app.screen.kind == "tests"
            await pilot.press("enter")
            await pilot.click("#continue")
            await pilot.pause(0.4)
            assert app.screen.kind == "tests"
            assert "at least one" in str(app.screen.query_one("#error", Static).render())
            app.toggle_row("all")
            await pilot.click("#continue")
            await pilot.pause(0.4)
            assert app.screen.kind == "acceptance"
            assert E2E_WARNING in str(app.screen.query_one("#body", Static).render())
            await pilot.click("#accept")
            await pilot.pause(0.4)
            assert app.screen.kind == "unavailable"
            assert app.operation_status == 3
            await pilot.press("q")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "n"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.screen.kind == "unavailable"
    asyncio.run(scenario())


def test_required_rows_allow_explicit_empty_and_next_menu_blocks_run(tmp_path):
    async def scenario():
        app = fixture(tmp_path, "run-local", {"VALUE": {"type": "str", "required": True}, "COUNT": {"type": "int", "default": 4}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            table = app.screen.query_one(DataTable)
            assert table.get_row_at(0)[0] == "VALUE"
            assert not app.focused_default()
            await pilot.press("c")
            await pilot.pause(0.4)
            await pilot.click("#mode-run-local")
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            await pilot.press("e")
            await pilot.pause(0.4)
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert 'VALUE=""' in (tmp_path / ".env").read_text()
            assert app.session.result().values["VALUE"] == ""
            table = app.screen.query_one(DataTable)
            assert table.get_row_at(0)[2] == app.session._source(app.session.dotenv_path)
            table.move_cursor(row=1)
            await pilot.press("a")
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            assert app.screen.query_one(DataTable).get_row_at(1)[2] == app.session._source(app.session.dotenv_path)
            await pilot.press("c")
            await pilot.pause(0.4)
            await pilot.click("#mode-run-local")
            await pilot.pause(0.4)
            assert app.screen.kind == "unavailable"
    asyncio.run(scenario())


def test_collection_only_skips_configuration(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={"COLLECT_ONLY": {"type": "bool", "default": False}, "PASSWORD": {"type": "str", "required_when": {"COLLECT_ONLY": False}}}, collector=lambda *args: [Identity("base::test.py::test_a", "base", "test.py", "test.py::test_a", frozenset())])
        app.session.environ["COLLECT_ONLY"] = "True"
        app.session.environ.pop("UNATTENDED_AUTH")
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.4)
            assert app.screen.kind == "menu"
            await pilot.click("#mode-integration")
            await pilot.pause(0.4)
            assert app.return_value == 0
            assert app.collected_output == "base::test.py::test_a"
    asyncio.run(scenario())


def test_newly_active_invalid_value_blocks_collection(tmp_path):
    async def scenario():
        calls = []
        app = fixture(tmp_path, fields={"BAD": {"type": "int", "applies_when": {"TUI_MODE": "integration"}}}, collector=lambda *args: calls.append(args))
        app.session.environ["BAD"] = "invalid"
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.4)
            assert app.screen.kind == "menu"
            await pilot.click("#mode-integration")
            await pilot.pause(0.4)
            assert app.screen.kind == "error"
            assert "BAD: expected integer" in str(app.screen.query_one("#body", Static).render())
            assert not calls
            assert app.operation_status == 3
    asyncio.run(scenario())


def test_conditional_default_is_acknowledged_again(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 1, "conditional_defaults": [{"when": {"TUI_MODE": "integration"}, "value": 2}]}})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("c")
            await pilot.pause(0.4)
            await pilot.click("#mode-integration")
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            assert app.screen.query_one(DataTable).get_row_at(0)[1] == "2"
    asyncio.run(scenario())


def test_quit_remains_responsive_during_collection(tmp_path):
    import threading
    started = threading.Event()
    finished = threading.Event()
    async def scenario():
        app = fixture(tmp_path, "integration")
        def collect(*args):
            started.set()
            app.collection_cancel.wait(20)
            finished.set()
            return []
        app.collector = collect
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.4)
            assert started.is_set()
            assert app.screen.kind == "collecting"
            await pilot.press("q")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "y"
            await pilot.press("escape")
            await pilot.pause(0.4)
            assert app.screen.kind == "collecting"
            await pilot.press("q")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "y"
            await pilot.click("#quit-cancel")
            await pilot.pause(0.4)
            assert app.screen.kind == "collecting"
            await pilot.press("q")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "y"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.return_value == 3
            assert finished.is_set()
    asyncio.run(scenario())


def test_valid_inactive_profile_skips_setup_suggestion(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={"UNATTENDED_AUTH": {"type": "jsonobject", "applies_when": {"TUI_MODE": ["run-local", "integration", "e2e"]}}})
        app.session.environ["UNATTENDED_AUTH"] = '{"LLM User":[{"manager":"keeper","cli_path":"/bin/keeper","vault":"vault","user":"person"}]}'
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.4)
            assert app.screen.kind == "menu"
    asyncio.run(scenario())


def test_control_defaults_follow_declaration_metadata(tmp_path):
    app = fixture(tmp_path, fields={"NO_STD_IN": {"type": "bool", "default": False}, "COLLECT_ONLY": {"type": "bool", "default": False}})
    assert {item.name for item in app.session.default_candidates()} == {"NO_STD_IN", "COLLECT_ONLY"}
    opted_out = fixture(tmp_path, fields={"NO_STD_IN": {"type": "bool", "default": False, "acknowledge_default": False}, "COLLECT_ONLY": {"type": "bool", "default": False}})
    assert {item.name for item in opted_out.session.default_candidates()} == {"COLLECT_ONLY"}


def test_no_stdin_choice_exits_without_more_prompts(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={"NO_STD_IN": {"type": "bool", "default": False}, "COUNT": {"type": "int", "default": 4}})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("e")
            await pilot.pause(0.4)
            assert app.prompt_name == "NO_STD_IN"
            app.screen.query_one(Input).value = "True"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.return_value == 2
            assert "Restart" in app.console_error
            assert "COUNT" not in app.session._choices
    asyncio.run(scenario())


def test_collect_only_validates_newly_active_settings(tmp_path):
    async def scenario():
        calls = []
        app = fixture(tmp_path, fields={"COLLECT_ONLY": {"type": "bool", "default": False}, "COUNT": {"type": "int", "applies_when": {"TUI_MODE": "unit"}}}, collector=lambda *args: calls.append(args))
        app.session.environ.update(COLLECT_ONLY="True", COUNT="bad")
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.click("#mode-unit")
            await pilot.pause(0.4)
            assert app.screen.kind == "error"
            assert app.operation_status == 2
            assert not calls
    asyncio.run(scenario())


def test_config_checklist_and_save_button_visible_at_terminal_sizes(tmp_path):
    async def scenario():
        for size in ((80, 24), (100, 35)):
            app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4}})
            async with app.run_test(size=size) as pilot:
                await pilot.pause(0.4)
                table = app.screen.query_one(DataTable)
                assert table.get_row_at(0)[0] == "COUNT"
                targets = app.screen.query_one("#dotenv-targets", Static)
                assert targets.region.y >= table.region.bottom
                if size == (100, 35):
                    assert targets.region.bottom <= app.screen.query_one("Footer").region.y
                await pilot.press("e")
                await pilot.pause(0.4)
                button = app.screen.query_one("#submit", Button)
                assert button.content_region.height >= 1
                assert button.region.bottom <= app.screen.query_one("Footer").region.y
    asyncio.run(scenario())


def test_checklist_refreshes_conditional_rows_and_excludes_default_optouts(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={
            "ENABLED": {"type": "bool", "default": True},
            "DETAIL": {"type": "str", "required_when": {"ENABLED": True}, "applies_when": {"ENABLED": True}},
            "DEBUG": {"type": "bool", "default": False, "acknowledge_default": False, "persist_default": False},
        })
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.4)
            table = app.screen.query_one(DataTable)
            assert [table.get_row_at(i)[0] for i in range(table.row_count)] == ["ENABLED", "DETAIL"]
            table.move_cursor(row=1)
            await pilot.press("e")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "saved detail"
            await pilot.press("enter")
            await pilot.pause(0.4)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=0)
            await pilot.press("e")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "False"
            await pilot.press("enter")
            await pilot.pause(0.4)
            table = app.screen.query_one(DataTable)
            assert table.row_count == 2
            assert table.get_row_at(0)[0:3] == ["ENABLED", "False", app.session._source(app.session.dotenv_path)]
            await pilot.press("e")
            await pilot.pause(0.4)
            await pilot.press("escape")
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            await pilot.press("e")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "True"
            await pilot.press("enter")
            await pilot.pause(0.4)
            table = app.screen.query_one(DataTable)
            assert table.row_count == 2
            assert table.get_row_at(1)[0:3] == ["DETAIL", "saved detail", app.session._source(app.session.dotenv_path)]
    asyncio.run(scenario())


def test_required_auth_does_not_enter_execution_or_general_checklist(tmp_path):
    async def scenario():
        calls = []
        app = fixture(tmp_path, "integration", {"UNATTENDED_AUTH": {"type": "jsonobject", "required": True}}, collector=lambda *args: calls.append(args))
        app.session.environ.pop("UNATTENDED_AUTH")
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("s")
            await pilot.pause(0.4)
            assert app.screen.kind == "error"
            assert "UNATTENDED_AUTH is required" in str(app.screen.query_one("#body", Static).render())
            assert not calls
    asyncio.run(scenario())


def test_quit_dialog_from_configuration_checklist(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            await pilot.press("q")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "n"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            await pilot.press("q")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "y"
            await pilot.press("escape")
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            await pilot.press("q")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "y"
            await pilot.click("#quit-cancel")
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            await pilot.press("q")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "y"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.return_value == 3
            assert app.collection_cancel.is_set()
    asyncio.run(scenario())


def test_unset_saved_default_and_required_value(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4},
                                       "VALUE": {"type": "str", "required": True}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            assert not app.screen.check_action("unset_setting", ())
            await pilot.press("e")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "8"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.screen.check_action("unset_setting", ())
            await pilot.press("u")
            await pilot.pause(0.4)
            table = app.screen.query_one(DataTable)
            assert table.get_row_at(0)[0:3] == ["COUNT", "4", "shared_default"]
            assert "COUNT" not in app.session._choices
            assert "COUNT=" not in (tmp_path / ".env").read_text()
            assert not app.screen.check_action("unset_setting", ())
            table.move_cursor(row=1)
            await pilot.press("e")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "provided"
            await pilot.press("enter")
            await pilot.pause(0.4)
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=1)
            await pilot.press("u")
            await pilot.pause(0.4)
            assert "VALUE" in app.session.result().missing
            assert app.screen.query_one(DataTable).get_row_at(1) == ["VALUE", Text("Required", justify="right"), "Not provided"]
            assert app.screen.query_one(DataTable).get_row_at(1)[1].justify == "right"
            app.screen.query_one(DataTable).move_cursor(row=1)
            assert not app.screen.check_action("accept_default", ())
    asyncio.run(scenario())


def test_empty_or_absent_auth_suggested_with_saved_defaults(tmp_path):
    async def scenario():
        for profile in (None, '{"LLM User":[]}'):
            (tmp_path / ".env").write_text('COUNT="4"\n')
            app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4}})
            if profile is None:
                app.session.environ.pop("UNATTENDED_AUTH")
            else:
                app.session.environ["UNATTENDED_AUTH"] = profile
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause(0.2)
                assert app.screen.kind == "suggest"
                await pilot.press("s")
                await pilot.pause(0.2)
                assert app.screen.kind == "menu"
    asyncio.run(scenario())


def test_switch_target_and_resave_prefilled_value(tmp_path):
    async def scenario():
        base = tmp_path / "base"
        pack = tmp_path / "pack"
        base.mkdir()
        pack.mkdir()
        (base / ".env").write_text('')
        app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4}, "OTHER": {"type": "int", "default": 1}})
        app.context.base_root = base
        app.context.pack_root = pack
        app.context.dotenv_path = pack / ".env"
        app.context.external_pack = True
        # Recreate session so launcher target selection uses the pack context.
        from api_agent.config.session import ConfigSession
        app.session = ConfigSession(app.context, app.schema, environ=app.session.environ)
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause(0.2)
            summary = str(app.screen.query_one("#dotenv-targets", Static).render())
            assert str(base / ".env") in summary
            assert str(pack / ".env") in summary
            targets = app.screen.query_one("#dotenv-targets", Static)
            table = app.screen.query_one(DataTable)
            assert [str(column.label) for column in table.columns.values()] == ["Setting", "Effective value", "Source"]
            assert len(table.get_row_at(0)) == 3
            assert targets.parent == table.parent
            assert targets.region.y >= table.region.bottom
            assert summary.splitlines()[1:] == [f"  {base / '.env'}", f"✓ {pack / '.env'}"]
            await pilot.press("a")
            await pilot.pause(0.2)
            assert app.screen.kind == "defaults"
            assert app.screen.query_one(DataTable).get_row_at(0)[2] == app.session._source(app.session.dotenv_path)
            await pilot.press("e")
            await pilot.pause(0.2)
            assert app.screen.query_one(Input).value == "4"
            app.screen.query_one(Input).value = "8"
            await pilot.click("#switch-target")
            await pilot.pause(0.2)
            assert app.session.dotenv_path == base / ".env"
            targets = app.screen.query_one("#dotenv-targets", Static)
            value_input = app.screen.query_one(Input)
            assert targets.parent == value_input.parent
            assert targets.region.y >= value_input.region.bottom
            assert targets.region.bottom <= app.screen.query_one("#actions").region.y
            assert str(targets.render()).splitlines()[1:] == [f"✓ {base / '.env'}", f"  {pack / '.env'}"]
            assert value_input.value == "8"
            await pilot.press("s")
            await pilot.pause(0.2)
            assert app.session.dotenv_path == pack / ".env"
            app.screen.query_one(Input).focus()
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert 'COUNT="8"' in (pack / ".env").read_text()
            assert "COUNT=" not in (base / ".env").read_text()
            await pilot.press("s")
            await pilot.pause(0.2)
            assert app.session.dotenv_path == base / ".env"
            assert app.screen.query_one(DataTable).get_row_at(0)[2] == app.session._source(pack / ".env")
            await pilot.press("e")
            await pilot.pause(0.2)
            assert app.screen.query_one(Input).value == "8"
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert 'COUNT="8"' in (base / ".env").read_text()
            assert "COUNT=" not in (pack / ".env").read_text()
    asyncio.run(scenario())


def test_unset_from_other_dotenv_without_switching_back(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4}})
        pack = tmp_path / "pack"
        pack.mkdir()
        app.context.pack_root = pack
        app.context.external_pack = True
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("a")
            await pilot.pause(0.4)
            assert app.session.saved_path("COUNT") == tmp_path / ".env"
            await pilot.press("s")
            await pilot.pause(0.4)
            assert app.session.dotenv_path == pack / ".env"
            assert app.screen.check_action("unset_setting", ())
            await pilot.press("u")
            await pilot.pause(0.4)
            assert app.session.saved_path("COUNT") is None
            assert app.session.dotenv_path == pack / ".env"
            assert app.screen.query_one(DataTable).get_row_at(0) == ["COUNT", "4", "shared_default"]
            assert app.screen.check_action("accept_default", ())
            await pilot.press("a")
            await pilot.pause(0.4)
            assert app.session.saved_path("COUNT") == pack / ".env"
    asyncio.run(scenario())


def test_identity_paths_include_checkout_branches(tmp_path):
    from unittest.mock import patch
    with patch('api_agent.runtime.worktrees.checkout_branch', return_value='feature/rewrite'):
        app = fixture(tmp_path)
    assert app.identity_summary() == (f"Base: {tmp_path} (feature/rewrite)\n"
                                      f"Pack: {tmp_path} (feature/rewrite)")
    with patch('api_agent.runtime.worktrees.checkout_branch', return_value='detached HEAD'):
        app = fixture(tmp_path)
    assert '(detached HEAD)' in app.identity_summary()


def test_identity_omits_parentheses_when_git_branch_check_fails(tmp_path):
    from unittest.mock import patch
    from api_agent.runtime.worktrees import WorktreeGateError
    with patch('api_agent.runtime.worktrees.checkout_branch', side_effect=WorktreeGateError('cannot check')):
        app = fixture(tmp_path)
    assert app.identity_summary() == f"Base: {tmp_path}\nPack: {tmp_path}"


def test_shared_defaults_review_before_mode_and_saved_next_launch(tmp_path):
    from api_agent.config import load_shared_schema, read_dotenv

    async def scenario():
        app = fixture(tmp_path)
        app.session.environ['UI_SOURCE_DIR'] = str(tmp_path / 'ui')
        app.schema = load_shared_schema()
        app.session.schema = app.schema
        app.context.anchors = {name: tmp_path for name in ('owner_root', 'base_root', 'pack_root')}
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert app.mode is None
            assert app.screen.kind == 'defaults'
            table = app.screen.query_one(DataTable)
            rows = {table.get_row_at(i)[0]: table.get_row_at(i) for i in range(table.row_count)}
            assert rows['TEST_PARALLEL_JOBS'][1] == '8'
            assert rows['E2E_RERUNS'][1] == '2'
            assert rows['E2E_BROWSER_RESOLUTION'][1] == '1440x900'
            assert {'RUN_UI', 'RUN_LOG_SOURCE'} <= rows.keys()
            assert not {'HELP', 'TUI_MODE', 'UNATTENDED_AUTH', 'NO_STD_IN', 'COLLECT_ONLY',
                        'API_AGENT_DIR', 'UI_SOURCE_DIR', 'UI_NX_APP', 'E2E_DEBUG',
                        'AUTH_BROWSER_DEBUG', 'E2E_LOG_INDEX'} & rows.keys()
            app.session.save_defaults(item.name for item in app.session.default_candidates()
                                      if item.field.persist_default)
            app.show_configuration()
            await pilot.pause()
            assert app.screen.query_one(DataTable).row_count == len(rows)
            assert read_dotenv(tmp_path / '.env')['TEST_PARALLEL_JOBS'] == '8'
        relaunched = fixture(tmp_path)
        relaunched.session.environ['UI_SOURCE_DIR'] = str(tmp_path / 'ui')
        relaunched.schema = app.schema
        relaunched.session.schema = app.schema
        relaunched.context.anchors = app.context.anchors
        async with relaunched.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert relaunched.screen.kind == 'defaults'
            table = relaunched.screen.query_one(DataTable)
            assert [table.get_row_at(i)[0] for i in range(table.row_count)] == ['TRACE_MODE', 'TRACE_TARGET']
    asyncio.run(scenario())


def test_new_inactive_default_and_required_field_review(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={
            'FUTURE_DEFAULT': {'type': 'int', 'default': 9, 'applies_when': {'TUI_MODE': 'unit'}},
            'FUTURE_REQUIRED': {'type': 'str', 'required': True, 'applies_when': {'TUI_MODE': 'unit'}},
        })
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.screen.query_one(DataTable).get_row_at(0)[:2] == ['FUTURE_DEFAULT', '9']
            assert app.screen.query_one(DataTable).row_count == 2
            assert app.screen.query_one(DataTable).get_row_at(1)[:2] == ['FUTURE_REQUIRED', Text("Required", justify="right")]
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.query_one(Input).value == '9'
            await pilot.press('escape')
            await pilot.pause()
            app.session.save_defaults(['FUTURE_DEFAULT'])
            app.session.set_value('TUI_MODE', 'unit')
            app.show_configuration()
            await pilot.pause()
            assert app.screen.query_one(DataTable).get_row_at(1)[:2] == ['FUTURE_REQUIRED', Text("Required", justify="right")]
            app.session.set_value('FUTURE_REQUIRED', 'saved', persist=True)
            app.session.set_value('TUI_MODE', 'integration')
            app.show_configuration()
            await pilot.pause()
            assert app.screen.query_one(DataTable).row_count == 2
            assert app.screen.query_one(DataTable).get_row_at(0)[0] == 'FUTURE_DEFAULT'
            app.session.set_value('TUI_MODE', 'unit')
            app.show_configuration()
            await pilot.pause()
            assert app.screen.query_one(DataTable).get_row_at(1)[:2] == ['FUTURE_REQUIRED', 'saved']
    asyncio.run(scenario())


def test_conditional_only_default_with_no_matching_branch_keeps_required_row(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={
            'CONDITIONAL': {'type': 'str', 'required': True,
                            'conditional_defaults': [{'when': {'TUI_MODE': 'unit'}, 'value': 'unit default'}]},
        })
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert app.screen.query_one(DataTable).get_row_at(0)[:2] == ['CONDITIONAL', Text("Required", justify="right")]
    asyncio.run(scenario())


def test_optional_trace_rows_and_conditional_target_review(tmp_path):
    from api_agent.config.declarations import load_shared_schema
    declarations = load_shared_schema().fields
    fields = {name: {'type': declarations[name].type,
                     'enum': list(declarations[name].enum),
                     'conditional_defaults': list(declarations[name].conditional_defaults),
                     'review_optional': True}
              for name in ('TRACE_MODE', 'TRACE_TARGET')}

    async def scenario():
        app = fixture(tmp_path, fields=fields)
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            table = app.screen.query_one(DataTable)
            assert table.get_row_at(0) == ['TRACE_MODE', '', 'Not provided']
            assert table.get_row_at(1) == ['TRACE_TARGET', '', 'Not provided']
            assert not app.focused_default()
            await pilot.press('a')
            assert not (tmp_path / '.env').exists()
            await pilot.press('e')
            await pilot.pause()
            assert app.screen.query_one(Input).value == ''
            app.screen.query_one(Input).value = 'LangFuse'
            await pilot.click('#submit')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.get_row_at(1) == ['TRACE_TARGET', 'http://localhost:3000', 'shared_default']
            app.session.set_value('TRACE_MODE', 'OTEL')
            app.show_configuration()
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.get_row_at(1) == ['TRACE_TARGET', 'http://localhost:4318/v1/traces', 'shared_default']
            table.move_cursor(row=1)
            assert app.focused_default()
            await pilot.press('a')
            await pilot.pause()
            app.session.set_value('TRACE_MODE', 'LangFuse')
            app.show_configuration()
            await pilot.pause()
            assert app.screen.query_one(DataTable).get_row_at(1)[1] == 'http://localhost:4318/v1/traces'
    asyncio.run(scenario())


def test_optional_trace_review_does_not_block_mode_or_repeat_explicit_settings(tmp_path):
    fields = {'TRACE_MODE': {'type': 'str', 'review_optional': True},
              'TRACE_TARGET': {'type': 'str', 'review_optional': True}}
    async def scenario():
        app = fixture(tmp_path, fields=fields)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            await pilot.press('c')
            await pilot.pause()
            await pilot.click('#mode-run-local')
            await pilot.pause()
            assert app.screen.kind == 'unavailable'
        configured = fixture(tmp_path, fields=fields)
        configured.session.set_value('TRACE_MODE', '', persist=True)
        configured.session.set_value('TRACE_TARGET', '', persist=True)
        async with configured.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert configured.screen.kind == 'menu'
    asyncio.run(scenario())


def test_enum_edit_lists_blank_choice_and_spaces_target_section(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={'TRACE_MODE': {'type': 'str', 'enum': ['', 'LangFuse', 'OTEL'], 'review_optional': True}})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press('e')
            await pilot.pause()
            hint = app.screen.query_one('#enum-info', Static)
            assert str(hint.content) == 'Enum (str) — exact values: <blank>, LangFuse, OTEL'
            value = app.screen.query_one(Input)
            assert hint.region.y > value.region.bottom
            assert app.screen.query_one('#dotenv-targets').region.y > hint.region.bottom
            value.value = 'langfuse'
            await pilot.press('enter')
            await pilot.pause()
            assert 'expected one of <blank>, LangFuse, OTEL' in str(app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())


def test_external_dotenv_removal_refreshes_source_and_unset_together(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={'TRACE_MODE': {'type': 'str', 'review_optional': True},
                                       'OTHER': {'type': 'str', 'review_optional': True}})
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press('e')
            await pilot.pause()
            app.screen.query_one(Input).value = ''
            await pilot.press('enter')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.get_row_at(0)[2].endswith('/.env')
            assert app.screen.check_action('unset_setting', ())
            (tmp_path / '.env').write_text('')
            # Nothing re-reads just the shortcut state behind the displayed row.
            assert app.screen.check_action('unset_setting', ())
            assert table.get_row_at(0)[2].endswith('/.env')
            await pilot.press('down', 'up')
            await pilot.pause()
            assert table.get_row_at(0) == ['TRACE_MODE', '', 'Not provided']
            assert not app.screen.check_action('unset_setting', ())
    asyncio.run(scenario())


def test_button_directional_navigation_and_edges(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.show_menu()
            await pilot.pause()
            assert app.focused.id == "mode-run-local"
            for key in ("up", "k", "left", "h"):
                await pilot.press(key)
                assert app.focused.id == "mode-run-local"
            for key, expected in (("right", "mode-unit"), ("right", "mode-unit"),
                                  ("j", "mode-e2e"), ("h", "mode-integration"),
                                  ("k", "mode-run-local"), ("l", "mode-unit"),
                                  ("down", "mode-e2e"), ("left", "mode-integration"),
                                  ("up", "mode-run-local")):
                await pilot.press(key)
                assert app.focused.id == expected
            for expected in ("mode-integration", "mode-allure", "mode-config-cleanup", "mode-unattended-auth"):
                await pilot.press("down")
                assert app.focused.id == expected
            for key in ("down", "j", "right", "l"):
                await pilot.press(key)
                assert app.focused.id == "mode-unattended-auth"
            await pilot.press("q")
            await pilot.pause()
            app.screen.query_one("#quit-submit", Button).focus()
            await pilot.press("l")
            assert app.focused.id == "quit-cancel"
            await pilot.press("right")
            assert app.focused.id == "quit-cancel"
            await pilot.press("h")
            assert app.focused.id == "quit-submit"
            await pilot.press("escape")
            await pilot.pause()
            app.show("suggest", "Authentication", actions=(("setup", "Setup"), ("skip", "Skip")))
            await pilot.pause()
            assert app.focused.id == "setup"
            await pilot.press("l")
            assert app.focused.id == "skip"
            await pilot.press("h")
            assert app.focused.id == "setup"
    asyncio.run(scenario())


def test_main_menu_resets_target_to_active_pack_for_either_launcher(tmp_path):
    from api_agent.config.session import ConfigSession

    async def scenario(launcher_is_pack):
        root = tmp_path / str(launcher_is_pack)
        base, pack = root / "base", root / "pack"
        base.mkdir(parents=True)
        pack.mkdir()
        (base / ".env").write_text('COUNT="4"\n')
        (pack / ".env").write_text('COUNT="9"\n')
        app = fixture(root, fields={"COUNT": {"type": "int", "default": 4},
                                        "OTHER": {"type": "int", "default": 1}})
        app.context.base_root = base
        app.context.pack_root = pack
        app.context.dotenv_path = pack / ".env"
        app.context.external_pack = True
        app.context.launcher_root = pack if launcher_is_pack else base
        app.session = ConfigSession(app.context, app.schema, environ=app.session.environ)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.session.switch_target(base / ".env")
            app.show_configuration()
            await pilot.pause()
            assert app.session.dotenv_path == base / ".env"
            await pilot.press("c")
            await pilot.pause()
            assert app.screen.kind == "menu"
            assert app.session.dotenv_path == pack / ".env"
            assert app.session.result().values["COUNT"] == 9
            assert (base / ".env").read_text() == 'COUNT="4"\n'
            app.session.switch_target(base / ".env")
            app.show_menu()
            await pilot.pause()
            assert app.session.dotenv_path == pack / ".env"
    for launcher_is_pack in (False, True):
        asyncio.run(scenario(launcher_is_pack))


def test_button_footer_tab_hint_precedes_shortcuts_without_binding(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.show_menu()
            await pilot.pause()
            footer = app.screen.query_one("Footer")
            assert footer.children[0].id == "tab-select-hint"
            assert str(footer.children[0].render()) == " Tab select "
            assert all(binding.action == "app.focus_next" and not binding.show
                       for binding in app.screen._bindings.key_to_bindings["tab"])
            assert app.focused.id == "mode-run-local"
            await pilot.press("tab")
            assert app.focused.id == "mode-unit"
            app.show("list", "Settings", rows=(("VALUE", ("VALUE", "", "Not provided")),))
            await pilot.pause()
            assert not app.screen.query("#tab-select-hint")
    asyncio.run(scenario())


def test_cleanup_both_dotenvs_hidden_setting_and_default_review(tmp_path):
    from api_agent.config.session import ConfigSession

    async def scenario():
        base, pack = tmp_path / 'base', tmp_path / 'pack'
        base.mkdir()
        pack.mkdir()
        for directory in (base, pack):
            (directory / '.env').write_text('UI_NX_APP="shell"\nCOUNT="6"\n')
        app = fixture(tmp_path, 'config-cleanup', fields={
            'UI_NX_APP': {'type': 'str', 'default': 'agent',
                          'acknowledge_default': False, 'persist_default': False},
            'COUNT': {'type': 'int', 'default': 4}})
        app.context.base_root, app.context.pack_root = base, pack
        app.context.external_pack = True
        app.context.dotenv_path = pack / '.env'
        app.session = ConfigSession(app.context, app.schema, environ=app.session.environ)
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'cleanup'
            assert not app.screen.query('#dotenv-targets')
            assert not app.screen.check_action('switch_target', ())
            table = app.screen.query_one(DataTable)
            assert 'from all agent / resource pack .env files.' in str(app.screen.query_one('#body', Static).render())
            assert len(table.columns) == 4
            assert next(iter(table.columns.values())).width == 1
            count_row = next(i for i in range(table.row_count) if table.get_row_at(i)[1] == 'COUNT')
            table.move_cursor(row=count_row)
            await pilot.press('enter')
            await pilot.pause()
            assert table.get_row_at(count_row)[0] == '✓'
            for name in ('UI_NX_APP', 'COUNT'):
                index = next(i for i in range(table.row_count) if table.get_row_at(i)[1] == name)
                table.move_cursor(row=index)
                await pilot.pause()
                assert table.get_row_at(index)[0] == '✓'
                assert app.screen.check_action('delete_assignment', ())
                await pilot.press('d')
                await pilot.pause()
                app.screen.query_one(Input).value = 'y'
                await pilot.press('enter')
                await pilot.pause()
                if name == 'UI_NX_APP':
                    for directory in (base, pack):
                        assert 'COUNT=' in (directory / '.env').read_text()
                    assert name not in [table.get_row_at(i)[1] for i in range(table.row_count)]
                else:
                    assert not app.focused_cleanup_present()
                for directory in (base, pack):
                    assert name + '=' not in (directory / '.env').read_text()
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            table = app.screen.query_one(DataTable)
            names = [table.get_row_at(i)[0] for i in range(table.row_count)]
            assert 'COUNT' in names
            assert 'UI_NX_APP' not in names
    asyncio.run(scenario())


def test_cleanup_refresh_restores_hidden_rows_after_external_edit(tmp_path):
    async def scenario():
        dotenv = tmp_path / '.env'
        dotenv.write_text('UI_NX_APP=agent\nCOUNT=6\n')
        app = fixture(tmp_path, 'config-cleanup', fields={
            'UI_NX_APP': {'type': 'str', 'default': 'agent',
                          'acknowledge_default': False, 'persist_default': False},
            'COUNT': {'type': 'int', 'default': 4},
            'OTHER': {'type': 'int', 'default': 1}})
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            def rows():
                return {table.get_row_at(i)[1]: table.get_row_at(i) for i in range(table.row_count)}
            index = next(i for i in range(table.row_count) if table.get_row_at(i)[1] == 'UI_NX_APP')
            table.move_cursor(row=index)
            await pilot.pause()
            await pilot.press('d')
            await pilot.pause()
            app.screen.query_one(Input).value = 'y'
            await pilot.press('enter')
            await pilot.pause()
            assert 'UI_NX_APP' not in rows()
            dotenv.write_text('UI_NX_APP=shell\nCOUNT=7\n')
            await pilot.press('down')
            await pilot.pause()
            assert rows()['UI_NX_APP'][0:3] == ['✓', 'UI_NX_APP', 'shell']
            assert rows()['UI_NX_APP'][3].endswith('/.env')
            assert rows()['COUNT'][2] == '7'
            index = next(i for i in range(table.row_count) if table.get_row_at(i)[1] == 'UI_NX_APP')
            table.move_cursor(row=index)
            await pilot.pause()
            assert app.screen.check_action('delete_assignment', ())
            dotenv.write_text('COUNT=7\n')
            await pilot.press('down')
            await pilot.pause()
            assert 'UI_NX_APP' not in rows()
    asyncio.run(scenario())


def test_main_menu_bootstrap_hint_docked_above_footer_only_when_needed(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        app.context.show_pack_bootstrap = True
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            warning = app.screen.query_one('#pack-bootstrap', Static)
            footer = app.screen.query_one('Footer')
            assert warning.region.bottom == footer.region.y
            assert footer.region.bottom == app.screen.content_region.bottom
            assert 'RESOURCE_PACK_DIR' in str(warning.render())
            app.show('unavailable', 'Status', actions=(('menu', 'Main menu'),))
            await pilot.pause()
            assert not app.screen.query('#pack-bootstrap')
            app.context.show_pack_bootstrap = False
            app.show_menu()
            await pilot.pause()
            assert not app.screen.query('#pack-bootstrap')
    asyncio.run(scenario())


def test_demo_directory_highlighted_in_pack_identity_only(tmp_path):
    app = fixture(tmp_path)
    app.context.pack_root = tmp_path / 'demo'
    text = app.identity_text()
    start = text.plain.index('\nPack: ') + len('\nPack: ') + len(str(tmp_path)) + 1
    assert text.plain[start:start + 4] == 'demo'
    assert [(span.start, span.end, span.style) for span in text.spans] == [(start, start + 4, 'red')]
    app.context.external_pack = True
    assert not app.identity_text().spans


def test_unit_runner_events_final_failure_and_view(tmp_path):
    from api_agent.testing.runner import RunnerEvent, TestResult, UnitRunResult, CoverageArea
    identity = Identity('base::test.py::test_fail', 'base', 'test.py', 'test.py::test_fail')
    calls = []
    def runner(context, tests, **kwargs):
        calls.append((tests, kwargs['parallel_jobs']))
        kwargs['on_event'](RunnerEvent('test_started', 'base', identity))
        # No finished event: final state must still replace PENDING/RUNNING.
        return UnitRunResult(1, (TestResult(identity, 'failed', .125, 'AssertionError: expected Ok'),),
                             (CoverageArea('base', 'src/example.py', 1, 2, 0, 0),), '')
    async def scenario():
        app = fixture(tmp_path, collector=lambda *args: [identity])
        app.unit_runner = runner
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.click('#mode-unit')
            await pilot.pause()
            assert app.screen.kind == 'tests'
            await pilot.press('s')
            for _ in range(20):
                await pilot.pause(.05)
                if app.unit_result is not None:
                    break
            assert calls == [((identity,), 8)]
            assert app.operation_status == 1, str(app.screen.query_one("#body", Static).content)
            assert app.screen.kind == 'unit-results'
            table = app.screen.query_one(DataTable)
            assert table.get_row_at(0) == ['FAILED', identity.id, '0.125s']
            assert '50.00%' in str(app.screen.query_one('#body', Static).render())
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen.kind == 'unit-failure'
            assert 'AssertionError' in str(app.screen.query_one('#body', Static).render())
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'unit-results'
    asyncio.run(scenario())


def test_completed_unit_failures_sort_stably_first_and_reset_selection(tmp_path):
    from api_agent.testing.runner import UnitRunResult
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.unit_rows = {
                'pass-first': ('PASSED', 'pass-first', '1s'),
                'fail-first': ('FAILED', 'fail-first', '2s'),
                'pass-second': ('PASSED', 'pass-second', '3s'),
                'fail-second': ('FAILED', 'fail-second', '4s'),
            }
            app.show_unit_results('Running')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert [table.get_row_at(i)[1] for i in range(4)] == list(app.unit_rows)
            table.move_cursor(row=3)
            app.unit_result = UnitRunResult(1, (), (), '')
            app.show_unit_results('Done')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert [table.get_row_at(i)[1] for i in range(4)] == [
                'fail-first', 'fail-second', 'pass-first', 'pass-second']
            assert table.cursor_row == 0
    asyncio.run(scenario())


def test_unit_summary_scroll_and_full_coverage_color(tmp_path):
    from api_agent.testing.runner import CoverageArea, UnitRunResult
    from textual.containers import VerticalScroll
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.unit_rows = {str(i): ('FAILED', str(i), '1s') for i in range(12)}
            app.unit_result = UnitRunResult(1, (), tuple(
                CoverageArea('base', f'file{i}.py', 0, 1, 0, 0) for i in range(40)), '')
            app.show_unit_results()
            await pilot.pause()
            summary = app.screen.query_one('#unit-summary', VerticalScroll)
            table = app.screen.query_one(DataTable)
            assert summary.size.height >= 6
            assert table.size.height >= 6  # header + horizontal scrollbar + four rows
            assert summary.max_scroll_y > 0
            app.unit_result = UnitRunResult(1, (), (CoverageArea('base', 'empty.py', 0, 0, 0, 0),), '')
            app.show_unit_results()
            await pilot.pause()
            content = app.screen.query_one('#body', Static).content
            assert isinstance(content, Text)
            coverage_start = content.plain.index('Production coverage: 100%')
            assert any(span.start == coverage_start and span.style == 'green' for span in content.spans)
            viewport = app.screen.query_one('#workbench-content', VerticalScroll)
            initial_scroll = viewport.max_scroll_y
            initial_summary_height = app.screen.query_one('#unit-summary').size.height
            await pilot.resize_terminal(70, 15)
            await pilot.pause()
            assert app.screen.query_one('#unit-summary').size.height >= 6
            assert app.screen.query_one(DataTable).size.height >= 6
            assert viewport.max_scroll_y > 0
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert viewport.max_scroll_y == initial_scroll
            assert app.screen.query_one('#unit-summary').size.height == initial_summary_height
            summary = app.screen.query_one('#unit-summary')
            table = app.screen.query_one(DataTable)
            assert table.region.y - summary.region.bottom >= 1
    asyncio.run(scenario())


def test_unit_progress_counts_and_colors(tmp_path):
    app = fixture(tmp_path)
    app.unit_rows = {str(i): (status, str(i), '') for i, status in enumerate(
        ['PASSED', 'FAILED', 'ERROR', 'RUNNING', 'PENDING', 'PENDING'])}
    text = app.unit_progress()
    assert text.plain.endswith('1 passed / 2 failed / 1 running / 2 pending')
    assert any(text.plain[span.start:span.end] == '1 passed' and span.style == 'green' for span in text.spans)
    assert any(text.plain[span.start:span.end] == '2 failed' and span.style == 'red' for span in text.spans)
    app.unit_rows = {'pending': ('PENDING', 'pending', '')}
    text = app.unit_progress()
    assert '0 failed' in text.plain
    assert not any(span.style == 'red' for span in text.spans)


def test_start_shortcut_e2e_requires_acceptance_with_selected_count(tmp_path):
    identity = Identity('base::a.py::test_one', 'base', 'a.py', 'a.py::test_one')
    async def scenario():
        app = fixture(tmp_path, collector=lambda *args: [identity])
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.click('#mode-e2e')
            await pilot.pause()
            await pilot.press('s')
            await pilot.pause()
            assert app.screen.kind == 'acceptance'
            assert '1 tests selected to run' in str(app.screen.query_one('#body', Static).content)
            assert E2E_WARNING in str(app.screen.query_one('#body', Static).content)
    asyncio.run(scenario())


def test_running_results_stay_mounted_and_horizontal_scroll_stays_inside_table(tmp_path):
    from textual.containers import VerticalScroll
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            key = 'base::' + 'very_long_test_name_' * 15
            app.unit_rows = {key: ('PENDING', key, '')}
            app.show_unit_results(app.unit_progress())
            await pilot.pause()
            screen = app.screen
            table = screen.query_one(DataTable)
            position = table.region
            viewport = screen.query_one('#workbench-content', VerticalScroll)
            assert table.max_scroll_x > 0
            assert viewport.max_scroll_y == 0, (viewport.size, viewport.content_size, [(w.id, w.region, w.outer_size, w.display) for w in viewport.children])
            app.show_unit_results(app.unit_progress())
            await pilot.pause()
            assert app.screen is screen
            assert app.screen.query_one(DataTable) is table
            assert table.region == position
            assert viewport.max_scroll_y == 0, (viewport.size, viewport.content_size, [(w.id, w.region, w.outer_size, w.display) for w in viewport.children])
    asyncio.run(scenario())


def test_close_running_tests_confirms_and_waits_for_runner_stop(tmp_path):
    import threading
    import time
    from api_agent.testing.runner import UnitRunResult
    identity = Identity('base::a.py::test_one', 'base', 'a.py', 'a.py::test_one')
    started, stopped = threading.Event(), threading.Event()
    def runner(context, tests, **kwargs):
        started.set()
        assert kwargs['cancel_event'].wait(5)
        time.sleep(.15)  # Simulate synchronous process-group teardown.
        stopped.set()
        return UnitRunResult(130, (), (), '', True)
    async def scenario():
        app = fixture(tmp_path, collector=lambda *args: [identity])
        app.unit_runner = runner
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.click('#mode-unit')
            await pilot.pause()
            await pilot.press('s')
            for _ in range(20):
                await pilot.pause(.02)
                if started.is_set():
                    break
            assert started.is_set()
            await pilot.press('c')
            await pilot.pause()
            assert 'Stop running tests' in str(app.screen.query_one(Static).content)
            await pilot.press('escape')
            await pilot.pause()
            assert app.screen.kind == 'unit-results'
            assert not app.collection_cancel.is_set()
            await pilot.press('c', 'y', 'enter')
            for _ in range(30):
                await pilot.pause(.02)
                if getattr(app.screen, 'kind', '') == 'menu':
                    break
            assert app.screen.kind == 'menu'
            assert stopped.is_set() and app.unit_stopped.is_set()
    asyncio.run(scenario())


def test_partial_selection_coverage_is_neutral_and_pass_summary_green(tmp_path):
    from api_agent.testing.runner import UnitRunResult, CoverageArea
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.unit_rows = {'one': ('PASSED', 'one', '1s')}
            app.unit_result = UnitRunResult(0, (), (CoverageArea('base', 'partial.py', 1, 2, 0, 0),), '', require_full_coverage=False)
            app.show_unit_results()
            await pilot.pause()
            text = app.screen.query_one('#body', Static).content
            assert text.spans[0].style == 'green'
            assert not any(span.style == 'red' for span in text.spans)
            coverage_start = text.plain.index('base::partial.py')
            assert not any(span.start <= coverage_start < span.end for span in text.spans)
    asyncio.run(scenario())


def test_startup_selectors_open_picker_once_then_menu_reentry_selects_all(tmp_path):
    first = Identity('base::a.py::test_one', 'base', 'a.py', 'a.py::test_one')
    second = Identity('base::b.py::test_two', 'base', 'b.py', 'b.py::test_two')
    calls = []
    def collector(context, mode, selectors):
        calls.append(tuple(selectors))
        return [first] if selectors else [first, second]
    async def scenario():
        app = fixture(tmp_path, mode='unit', collector=collector, argv=('base::a.py',))
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'tests'
            assert app.selection.selected_tests == (first,)
            assert app.unit_result is None
            assert app.active_test_selectors == ('base::a.py',)
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            await pilot.click('#mode-unit')
            await pilot.pause()
            assert app.screen.kind == 'tests'
            assert app.selection.selected_tests == (first, second)
            assert calls == [('base::a.py',), ()]
    asyncio.run(scenario())
