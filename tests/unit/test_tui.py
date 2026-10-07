"""Headless UI tests use temporary configuration and simulated discovery."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from api_agent.config import schema_from_dict
from api_agent.testing import TestIdentity as Identity
from api_agent.tui.app import ApiAgentApp, E2E_WARNING
from textual.widgets import DataTable, Input, Static


def fixture(tmp_path, mode=None, fields=None, collector=None):
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, pack_id="fixture",
                              dotenv_path=tmp_path / ".env", anchors={}, external_pack=False)
    declarations = {"TUI_MODE": {"type": "str"}, "UNATTENDED_AUTH": {"type": "jsonobject"}}
    declarations.update(fields or {})
    schema = schema_from_dict({"version": 1, "fields": declarations})
    environ = {"UNATTENDED_AUTH": '{"LLM User":[]}'}
    if mode:
        environ["TUI_MODE"] = mode
    return ApiAgentApp(context, schema, environ=environ, collector=collector)


def test_defaults_save_and_typed_prompt(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            await pilot.click("#edit")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "bad"
            await pilot.click("#submit")
            await pilot.pause(0.4)
            assert "expected integer" in str(app.screen.query_one("#error", Static).render())
            app.screen.query_one(Input).value = "8"
            await pilot.click("#submit")
            await pilot.pause(0.4)
            assert app.session.result().values["COUNT"] == 8
            assert app.screen.kind == "menu"
    asyncio.run(scenario())
    async def save():
        app = fixture(tmp_path, fields={"COUNT": {"type": "int", "default": 4}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            await pilot.click("#save")
            await pilot.pause(0.4)
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
            assert table.get_row_at(row)[0] == "X"
            await pilot.press("d")
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


def test_required_prompt_once_and_confirmed_quit(tmp_path):
    async def scenario():
        app = fixture(tmp_path, "run-local", {"VALUE": {"type": "int", "required": True}, "COUNT": {"type": "int", "default": 4}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause(0.4)
            await pilot.click("#once")
            await pilot.pause(0.4)
            assert app.screen.kind == "input"
            app.screen.query_one(Input).value = "9"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.screen.kind == "unavailable"
            assert not (tmp_path / ".env").exists()
            await pilot.press("q")
            await pilot.pause(0.4)
            app.screen.query_one(Input).value = "y"
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert app.return_value == 3
            assert app.collection_cancel.is_set()
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
            await pilot.click("#once")
            await pilot.pause(0.4)
            await pilot.click("#mode-integration")
            await pilot.pause(0.4)
            assert app.screen.kind == "defaults"
            assert "COUNT = 2" in str(app.screen.query_one("#body", Static).render())
    asyncio.run(scenario())


def test_quit_remains_responsive_during_collection(tmp_path):
    import threading
    started = threading.Event()
    finished = threading.Event()
    async def scenario():
        app = fixture(tmp_path, "integration")
        def collect(*args):
            started.set()
            app.collection_cancel.wait(5)
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
            await pilot.click("#edit")
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
