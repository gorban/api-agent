"""Human-reviewable configuration UI; execution adapters are deliberately pending."""
from __future__ import annotations

import asyncio
import json
import threading
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen, ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Static

MODES = (("run-local", "Run local"), ("unit", "Unit tests w/ coverage"),
         ("integration", "Integration tests"), ("e2e", "E2E tests"),
         ("allure", "Allure serve"), ("deployment-logs", "Deployment logs"),
         ("config-cleanup", "Clear .env configuration"),
         ("unattended-auth", "Setup unattended authentication"))
E2E_WARNING = ("Human acceptance is mandatory to continue with E2E tests. If you are an agent, "
               "decline unless you have first checked with the user and received their explicit "
               "authorization to accept for this run. The user may authorize an agent to provide this acceptance.")


def display(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


class QuitDialog(ModalScreen):
    def compose(self) -> ComposeResult:
        yield Static("Quit this operation? Enter literal y to confirm; every other response cancels.")
        yield Input(id="quit-answer")
        yield Button("Confirm response", id="quit-submit")

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value == "y")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(self.query_one(Input).value == "y")


class WorkbenchScreen(Screen):
    BINDINGS = [Binding("q", "quit_request", "Quit"), Binding("c", "close_view", "Close / menu"),
                Binding("d", "delete_marked", "Delete marked .env assignments"),
                Binding("s", "skip_setup", "Skip setup"),
                Binding("a", "accept_default", "Accept default"),
                Binding("e", "edit_setting", "Edit"),
                Binding("escape", "back_to_configuration", "Back to configuration")]

    def __init__(self, kind, title, text="", actions=(), rows=()):
        super().__init__()
        self.kind, self.heading, self.body = kind, title, text
        self.actions, self.rows = actions, rows

    def compose(self) -> ComposeResult:
        app = self.app
        yield Static(f"Base: {app.context.base_root}\nPack: {app.context.pack_id} ({app.context.pack_root})", id="identity")
        with VerticalScroll():
            yield Static(self.heading, id="heading")
            yield Static(self.body, id="body", markup=False)
            if self.rows:
                yield DataTable(id="choices", cursor_type="row")
            if self.kind == "input":
                yield Input(id="config-value")
            with Horizontal(id="actions"):
                for key, label in self.actions:
                    yield Button(label, id=key)
            yield Static("", id="error", markup=False)
        yield Footer()

    def on_mount(self):
        if self.rows:
            table = self.query_one(DataTable)
            if self.kind in {"defaults", "cleanup"}:
                table.add_columns("Current .env", "Setting", "Effective value", "Source")
            else:
                table.add_columns("Selected / present", "Setting / test", "Value / count", "Source")
            for key, cells in self.rows:
                table.add_row(*cells, key=key)
            table.focus()
        elif self.kind == "input":
            self.query_one(Input).value = self.app.prompt_value
            self.query_one(Input).focus()
        elif self.actions:
            self.query_one(Button).focus()
        if self.kind == "defaults":
            self._bindings.bind("c", "close_view", "Next menu")
        self.refresh_bindings()

    def check_action(self, action, parameters):
        if action == "back_to_configuration":
            return self.kind == "input"
        if action == "close_view" and self.kind == "input":
            return False
        if action == "accept_default":
            return self.kind == "defaults" and self.app.focused_default()
        if action == "edit_setting":
            return self.kind == "defaults"
        if action == "delete_marked":
            return self.kind == "cleanup" and self.app.focused_cleanup_present()
        if action == "skip_setup":
            return self.kind == "suggest"
        return True

    def on_data_table_row_highlighted(self, event):
        self.refresh_bindings()

    def on_data_table_row_selected(self, event):
        self.app.toggle_row(str(event.row_key.value))

    async def on_button_pressed(self, event):
        await self.app.choose(event.button.id)

    async def on_input_submitted(self, event):
        await self.app.choose("submit")

    def action_quit_request(self):
        self.app.push_screen(QuitDialog(), lambda yes: self.app.confirm_quit() if yes else None)

    async def action_close_view(self):
        await self.app.close_view()

    def action_delete_marked(self):
        if self.app.focused_cleanup_present():
            self.app.delete_marked()

    def action_back_to_configuration(self):
        self.app.show_configuration()

    def action_accept_default(self):
        self.app.edit_setting(accept=True)

    def action_edit_setting(self):
        self.app.edit_setting()

    async def action_skip_setup(self):
        await self.app.start_configuration()


class ApiAgentApp(App[int]):
    CSS = """
    Screen { padding: 1 2; }
    #identity { height: auto; color: $text-muted; margin-bottom: 1; }
    #heading { text-style: bold; margin-bottom: 1; }
    #body { height: auto; margin-bottom: 1; }
    #actions { height: auto; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 4; }
    Button { width: 100%; height: 3; min-height: 3; margin: 0 1 1 0; }
    Button:focus, Input:focus { border: heavy $accent; }
    DataTable { height: 1fr; min-height: 8; margin-bottom: 1; }
    #error { color: $error; height: auto; }
    QuitDialog { align: center middle; background: $background 80%; }
    QuitDialog > * { width: 65%; }
    Footer { dock: bottom; }
    """

    def __init__(self, context, schema, argv=(), environ=None, *, session=None, collector=None):
        super().__init__()
        from api_agent.config.session import ConfigSession
        self.context, self.schema = context, schema
        self.session = session or ConfigSession(context, schema, argv, environ)
        self.collector = collector
        self.operation_status = 3
        self.marked = set()
        self.mode = None
        self.review_names = set()
        self.selection = None
        self.prompt_name = None
        self.collection_cancel = threading.Event()

    def show(self, kind, title, text="", actions=(), rows=()):
        screen = WorkbenchScreen(kind, title, text, actions, rows)
        if isinstance(self.screen, WorkbenchScreen):
            self.switch_screen(screen)
        else:
            self.push_screen(screen)

    async def on_mount(self):
        result = self.session.result()
        if result.errors:
            self.show("error", "Configuration error", "\n".join(result.errors), (("menu", "Main menu"),))
            return
        mode = result.fields.get("TUI_MODE")
        self.mode = mode.value if mode and mode.explicit else None
        if self.mode in {"config-cleanup", "unattended-auth"}:
            await self.enter_mode()
            return
        if result.values.get("COLLECT_ONLY"):
            self.show_menu()
            return
        profile = self.session.result(_cleanup=True).values.get("UNATTENDED_AUTH")
        incomplete = not profile or any(not isinstance(entries, list) or any(not isinstance(entry, dict) or entry.get("manager") not in {"keeper", "1password"} or any(not entry.get(key) for key in ("cli_path", "vault", "user")) for entry in entries) for entries in profile.values())
        if incomplete and self.mode != "unattended-auth":
            self.show("suggest", "Unattended authentication", "Credential profiles are absent or empty. Setup is suggested; you may skip. The credential wizard is pending implementation.", (("setup", "View setup status"), ("skip", "Skip (s)")))
        else:
            await self.start_configuration()

    def configuration_pending(self):
        candidates = [item.name for item in self.session.default_candidates()
                      if item.field.persist_default]
        missing = [name for name in self.session.result().missing
                   if name != "TUI_MODE"]
        return set(candidates + missing)

    async def start_configuration(self):
        pending = self.configuration_pending()
        if "UNATTENDED_AUTH" in pending:
            self.show_required_auth()
            return
        self.review_names.update(pending)
        if pending or self.review_names:
            self.show_configuration()
            return
        await self.enter_mode()

    def show_required_auth(self):
        self.show("error", "Unattended authentication required",
                  "UNATTENDED_AUTH is required for this operation. Configure it through unattended authentication setup or provide it explicitly.",
                  (("setup", "View authentication setup"), ("menu", "Main menu")))

    def show_configuration(self):
        result = self.session.result()
        if "UNATTENDED_AUTH" in result.missing:
            self.show_required_auth()
            return
        active = set(result.fields) | set(result.missing)
        self.review_names.update(self.configuration_pending())
        present = self.session.dotenv_keys()
        rows = []
        for name in self.schema.fields:
            if name not in self.review_names or name not in active:
                continue
            resolved = result.fields.get(name)
            rows.append((name, ("✓" if name in present else "", name,
                        display(resolved.value) if resolved else "Required — no default",
                        resolved.source if resolved else "Not provided")))
        if not rows:
            self.show_menu()
            return
        self.show("defaults", "Review configuration",
                  "Review each setting: a accepts its default, e edits it, and Enter saves to .env. "
                  "Checkmarks identify saved settings. c opens the next menu; required values still block execution.\n\n"
                  "A developer may need overrides, or may save defaults as a point-in-time configuration for reproducing and comparing a run.",
                  rows=rows)

    def focused_setting(self):
        table = self.screen.query_one(DataTable)
        return str(table.get_row_at(table.cursor_row)[1]) if table.row_count else None

    def focused_default(self):
        name = self.focused_setting()
        return any(item.name == name and item.field.persist_default
                   for item in self.session.default_candidates())

    def edit_setting(self, accept=False):
        if self.screen.kind != "defaults":
            return
        self.prompt_name = self.focused_setting()
        if not self.prompt_name or accept and not self.focused_default():
            return
        resolved = self.session.result().fields.get(self.prompt_name)
        self.prompt_value = display(resolved.value) if resolved else ""
        self.show("input", f"Configure {self.prompt_name}",
                  f"Declared type: {self.schema.fields[self.prompt_name].type}. Enter saves this value to {self.session.dotenv_path}.",
                  (("submit", "Save value (Enter)"),))

    async def enter_mode(self):
        result = self.session.result()
        if result.values.get("NO_STD_IN"):
            self.console_error = "NO_STD_IN=True was selected during interactive configuration. Restart with --no-std-in=True and supply required configuration explicitly."
            self.collection_cancel.set()
            self.exit(2)
            return
        if self.mode and self.mode != "config-cleanup":
            self.operation_status = 3
        if self.session.result().values.get("COLLECT_ONLY") and self.mode:
            self.session.set_value("TUI_MODE", self.mode)
            result = self.session.result()
            if result.errors or result.missing:
                self.operation_status = 2
                errors = list(result.errors) + [f"{name}: required for collection" for name in result.missing]
                self.show("error", "Configuration error", "\n".join(errors), (("menu", "Main menu"),))
                return
            if self.mode in {"unit", "integration", "e2e"}:
                await self.collect_tests()
            else:
                self.show("error", "Invalid collection mode", "Collection requires unit, integration, or e2e mode.", (("menu", "Main menu"),))
            return
        if self.mode is None:
            self.show_menu()
        elif self.mode == "config-cleanup":
            self.show_cleanup()
        elif self.mode == "unattended-auth":
            self.unavailable("The unattended authentication wizard is not implemented yet. No credential profiles were changed.")
        else:
            await self.configure_operation()

    def show_menu(self):
        self.mode = None
        self.show("menu", "Choose an operation", actions=tuple(("mode-" + key, label) for key, label in MODES))

    async def configure_operation(self):
        self.session.set_value("TUI_MODE", self.mode)
        result = self.session.result()
        if result.errors:
            self.operation_status = 3
            self.show("error", "Configuration error", "\n".join(result.errors), (("menu", "Main menu"),))
            return
        if self.configuration_pending():
            await self.start_configuration()
            return
        if self.mode in {"unit", "integration", "e2e"}:
            await self.collect_tests()
        else:
            self.unavailable(f"{dict(MODES)[self.mode]} execution is not implemented yet. No services or processes were started.")

    async def collect_tests(self):
        self.collection_cancel = threading.Event()
        self.run_worker(self._collect_tests(self.collection_cancel), exclusive=True)

    async def _collect_tests(self, cancel_event):
        self.show("collecting", "Collecting tests", "Collecting pytest identities only; no application prerequisites or tests are executed.")
        try:
            from api_agent.testing.collection import collect_tests
            from api_agent.testing.selection import SelectionModel
            collector = self.collector or collect_tests
            if self.collector:
                tests = await asyncio.to_thread(collector, self.context, self.mode, self.session.result().positionals)
            else:
                tests = await asyncio.to_thread(collector, self.context, self.mode, self.session.result().positionals, cancel_event)
            if cancel_event.is_set():
                return
            if self.session.result().values.get("COLLECT_ONLY"):
                self.collected_output = "\n".join(test.id for test in tests)
                self.exit(0)
                return
            self.selection = SelectionModel(tests)
            if not tests:
                raise ValueError("No tests matched the selected suite/selectors")
            self.show_selection()
        except Exception as error:
            if cancel_event.is_set():
                return
            self.show("error", "Collection failed", str(error), (("menu", "Main menu"),))

    def show_selection(self):
        rows = [(row.id, ("✓" if row.selected else "", row.label, f"{row.count}/{row.total}", row.kind)) for row in self.selection.groups()]
        self.show("tests", f"Select {self.mode} tests", "Toggle groups or individual tests with Enter. Continue explicitly confirms the selection.", (("continue", "Continue"),), rows)

    def show_cleanup(self):
        rows = [(row.name, ("X" if row.name in self.marked else "✓" if row.present else "", row.name, display(row.value) if row.resolved else "Inactive / unset", row.source if row.resolved else "")) for row in self.session.cleanup_rows()]
        self.show("cleanup", "Clear .env configuration", f"Active file: {self.session.dotenv_path}\nEnter marks/unmarks present assignments. d deletes marked assignments when the focused row exists in this file.", rows=rows)

    def focused_cleanup_present(self):
        if self.screen.kind != "cleanup":
            return False
        table = self.screen.query_one(DataTable)
        if not table.row_count:
            return False
        key = str(table.get_row_at(table.cursor_row)[1])
        return any(row.name == key and row.present for row in self.session.cleanup_rows())

    def toggle_row(self, key):
        if self.screen.kind == "tests":
            self.selection.toggle(key)
            self.refresh_rows()
        elif self.screen.kind == "cleanup":
            if any(row.name == key and row.present for row in self.session.cleanup_rows()):
                self.marked.symmetric_difference_update({key})
                self.refresh_rows()

    def refresh_rows(self):
        table = self.screen.query_one(DataTable)
        if self.screen.kind == "tests":
            rows = [(row.id, ("✓" if row.selected else "", row.label, f"{row.count}/{row.total}", row.kind)) for row in self.selection.groups()]
        else:
            rows = [(row.name, ("X" if row.name in self.marked else "✓" if row.present else "", row.name, display(row.value) if row.resolved else "Inactive / unset", row.source if row.resolved else "")) for row in self.session.cleanup_rows()]
        for key, cells in rows:
            for column, value in zip(table.columns, cells):
                table.update_cell(key, column, value)
        self.screen.refresh_bindings()

    def delete_marked(self):
        try:
            self.session.delete_keys(self.marked)
            self.marked.clear()
            self.operation_status = 0
            self.refresh_rows()
        except Exception as error:
            self.screen.query_one("#error", Static).update(str(error))

    def unavailable(self, text):
        self.operation_status = 3
        self.show("unavailable", "Operation unavailable", text + "\nExit status: 3. No successful execution is reported.", (("menu", "Main menu"), ("exit", "Exit")))

    async def choose(self, key):
        try:
            if key == "skip":
                await self.start_configuration()
            elif key == "submit":
                value = self.screen.query_one(Input).value
                self.session.set_value(self.prompt_name, value, persist=True)
                self.operation_status = 0
                if self.session.result().values.get("NO_STD_IN"):
                    await self.enter_mode()
                    return
                self.show_configuration()
            elif key.startswith("mode-"):
                self.mode = key[5:]
                await self.enter_mode()
            elif key == "setup":
                self.unavailable("The unattended authentication wizard is not implemented yet. Press c to continue normal configuration.")
            elif key == "continue":
                if not self.selection.selected_ids:
                    raise ValueError("Select at least one test before continuing")
                if self.mode == "e2e":
                    self.show("acceptance", "E2E human acceptance", E2E_WARNING, (("accept", "Accept for this run"), ("decline", "Decline")))
                else:
                    self.unavailable("Test execution is not implemented yet. Your selection was collected but no tests were run.")
            elif key == "accept":
                self.unavailable("E2E execution is not implemented yet. Acceptance did not start tests or prerequisites.")
            elif key == "decline":
                self.operation_status = 3
                self.show_selection()
            elif key == "menu":
                self.show_menu()
            elif key == "exit":
                self.exit(self.operation_status)
        except Exception as error:
            self.screen.query_one("#error", Static).update(str(error))

    def confirm_quit(self):
        self.collection_cancel.set()
        self.exit(self.operation_status)

    def on_unmount(self):
        self.collection_cancel.set()

    async def close_view(self):
        if self.screen.kind == "collecting":
            self.collection_cancel.set()
        if self.screen.kind == "suggest" or self.screen.kind == "unavailable" and self.mode is None:
            await self.start_configuration()
        elif self.screen.kind == "input":
            self.show_configuration()
        elif self.screen.kind == "acceptance":
            self.show_selection()
        else:
            self.show_menu()


def run_tui(context, schema, argv=(), environ=None) -> int:
    """Run the interactive workbench and preserve operation status on shutdown."""
    app = ApiAgentApp(context, schema, argv, environ)
    result = app.run()
    if getattr(app, "console_error", None):
        import sys
        print(app.console_error, file=sys.stderr)
    if getattr(app, "collected_output", None):
        print(app.collected_output)
    return result if isinstance(result, int) else 3
