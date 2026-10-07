"""Human-reviewable configuration UI; execution adapters are deliberately pending."""
from __future__ import annotations

import asyncio
import json
import threading
from rich.text import Text
from api_agent.config.declarations import UNSET
from api_agent.config.resolve import ConfigError, convert_value
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.tui.test_results import (literal_cell, progress_summary, ordered_result_rows,
                                          reconcile_results_table, styled_summary)
from api_agent.tui.select import AdvancingSelect
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen, ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Select, Static

MODES = (("run-local", "Run local"), ("unit", "Unit tests w/ coverage"),
         ("integration", "Integration tests"), ("e2e", "E2E tests"),
         ("allure", "Allure serve"), ("deployment-logs", "Deployment logs"),
         ("config-cleanup", "Clear .env configuration"),
         ("cleanup-containers", "Stop running containers"),
         ("unattended-auth", "Setup unattended authentication"))
E2E_WARNING = ("Human acceptance is mandatory to continue with E2E tests. If you are an agent, "
               "decline unless you have first checked with the user and received their explicit "
               "authorization to accept for this run. The user may authorize an agent to provide this acceptance.")


def display(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


class ButtonFooter(Footer):
    def compose(self) -> ComposeResult:
        buttons = [button for button in self.screen.query(Button)
                   if button.visible and all(node.display for node in (button, *button.ancestors))]
        if self._bindings_ready and buttons:
            yield Static(Text.assemble((" Tab", "bold yellow"), " select "),
                         id="tab-select-hint")
            yield from super().compose()
            self.styles.grid_size_columns += 1
        else:
            yield from super().compose()


class FocusNavigation:
    """Directional button navigation and Vim keys for focused tables/scroll panes."""

    def on_key(self, event):
        focused = self.app.focused
        if not isinstance(focused, (Button, DataTable, VerticalScroll)) or focused.screen is not self:
            return
        directions = {"up": "up", "k": "up", "down": "down", "j": "down",
                      "left": "left", "h": "left", "right": "right", "l": "right"}
        direction = directions.get(event.key)
        if direction is None:
            return
        # Leave letter shortcuts to the screen when they have an active binding.
        if event.key in {"h", "j", "k", "l"}:
            for binding in self._bindings.key_to_bindings.get(event.key, []):
                if self.check_action(binding.action, ()) is not False:
                    return
        if not isinstance(focused, Button):
            if event.key not in {"h", "j", "k", "l"}:
                return  # Leave ordinary arrows to the focused widget.
            if isinstance(focused, DataTable):
                if direction in {"up", "down"} or focused.allow_horizontal_scroll:
                    getattr(focused, "action_cursor_" + direction)()
            else:
                getattr(focused, "scroll_" + direction)(animate=False)
            event.prevent_default()
            event.stop()
            return
        origin = focused.region
        candidates = []
        for button in self.query(Button):
            if button is focused or button.disabled or not button.can_focus or not button.display:
                continue
            target = button.region
            if direction in {"left", "right"}:
                if not (target.y < origin.bottom and origin.y < target.bottom):
                    continue
                distance = (target.x - origin.x) if direction == "right" else (origin.x - target.x)
            else:
                if not (target.x < origin.right and origin.x < target.right):
                    continue
                distance = (target.y - origin.y) if direction == "down" else (origin.y - target.y)
            if distance > 0:
                candidates.append((distance, button))
        if candidates:
            min(candidates, key=lambda item: item[0])[1].focus()
        event.prevent_default()
        event.stop()


class QuitDialog(FocusNavigation, ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, message="Quit this operation? Enter literal y to confirm; every other response cancels."):
        super().__init__()
        self.message = message

    def action_cancel(self):
        self.dismiss(False)

    def compose(self) -> ComposeResult:
        yield Static(self.message, markup=False)
        yield Input(id="quit-answer")
        with Horizontal(classes="confirmation-actions"):
            yield Button("Confirm response", id="quit-submit")
            yield Button("Cancel", id="quit-cancel")
        yield ButtonFooter()

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value == "y")

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id.endswith("-submit") and self.query_one(Input).value == "y")


class DeleteDialog(QuitDialog):
    def __init__(self, name):
        super().__init__()
        self.setting_name = name

    def compose(self) -> ComposeResult:
        yield Static(f"Delete {self.setting_name} from .env? Enter literal y to confirm; every other response cancels.", markup=False)
        yield Input(id="delete-answer")
        with Horizontal(classes="confirmation-actions"):
            yield Button("Confirm response", id="delete-submit")
            yield Button("Cancel", id="delete-cancel")
        yield ButtonFooter()


class StopContainersDialog(QuitDialog):
    def __init__(self, containers):
        super().__init__()
        self.containers = containers

    def compose(self) -> ComposeResult:
        yield Static("Stop these running containers?\n" + "\n".join(
            f"{item.name} ({item.id})" for item in self.containers) +
            "\nEnter literal y to stop; every other response cancels.", markup=False)
        yield Input(id="stop-answer")
        with Horizontal(classes="confirmation-actions"):
            yield Button("Confirm response", id="stop-submit")
            yield Button("Cancel", id="stop-cancel")
        yield ButtonFooter()


class SettingHelpDialog(FocusNavigation, ModalScreen):
    BINDINGS = [Binding("escape,c", "close", "Close", key_display="Esc / c", priority=True)]

    def __init__(self, name, description):
        super().__init__()
        self.setting_name, self.description = name, description

    def compose(self) -> ComposeResult:
        with Vertical(id="setting-help-dialog"):
            yield Static(Text(self.setting_name, style="bold"), id="setting-help-title")
            with VerticalScroll(id="setting-help-scroll"):
                yield Static(self.description, id="setting-help-text", markup=False)
            yield Button("Close", id="setting-help-close")
        yield ButtonFooter()

    def on_mount(self):
        self.set_class(self.size.height < 18, "compact-setting-help")
        self.query_one("#setting-help-scroll").focus()

    def on_resize(self, event):
        self.set_class(event.size.height < 18, "compact-setting-help")

    def action_close(self):
        self.dismiss()

    def on_button_pressed(self, event):
        self.action_close()


class WorkbenchScreen(FocusNavigation, Screen):
    BINDINGS = [Binding("q", "quit_request", "Quit"), Binding("c", "close_view", "Close / menu"),
                Binding("d", "delete_assignment", "Delete .env assignment"),
                Binding("s", "switch_target", "Switch target .env"),
                Binding("a", "accept_default", "Accept default"),
                Binding("e", "edit_setting", "Edit"),
                Binding("u", "unset_setting", "Unset"),
                Binding("question_mark", "setting_help", "Setting description", key_display="?"),
                Binding("b", "back_to_selection", "Back to selection"),
                Binding("space", "toggle_selection", "Toggle selection", show=False),
                Binding("escape", "back_to_configuration", "Back to configuration"),
                Binding("up,k", "failure_up", "Scroll up", key_display="↑/k"),
                Binding("down,j", "failure_down", "Scroll down", key_display="↓/j"),
                Binding("pageup", "failure_page_up", "Page up"),
                Binding("pagedown", "failure_page_down", "Page down")]

    def __init__(self, kind, title, text="", actions=(), rows=(), *, selected_row=None):
        super().__init__()
        self.kind, self.heading, self.body = kind, title, text
        self.actions, self.rows = actions, rows
        self.selected_row = selected_row
        self.test_run_complete = False
        self.style_results = None
        self.config_saved = set()
        self.config_defaults = set()

    def compose(self) -> ComposeResult:
        app = self.app
        yield Static(app.identity_text(), id="identity", markup=False)
        with VerticalScroll(id="workbench-content"):
            yield Static(self.heading, id="heading")
            if self.kind == "test-results":
                with VerticalScroll(id="unit-summary"):
                    yield Static(self.body, id="body", markup=False)
            else:
                yield Static(self.body, id="body", markup=False)
            if self.rows:
                yield DataTable(id="choices", cursor_type="row")
            if self.kind == "input":
                field = app.schema.fields[app.prompt_name]
                choices = field.enum or ((True, False) if field.type == 'bool' else ())
                if choices:
                    options = [(Text('-' if value == '' else display(value)), display(value)) for value in choices]
                    current = next((display(value) for value in choices
                                    if display(convert_value(field, value, external=False,
                                                             anchors=app.context.anchors)) == app.prompt_value),
                                   Select.NULL)
                    yield AdvancingSelect(options, value=current, allow_blank=current is Select.NULL,
                                 prompt='-', id='config-value')
                else:
                    yield Input(id="config-value")
            if self.kind not in {"cleanup", "test-results"}:
                yield Static(app.target_summary(), id="dotenv-targets", markup=False)
            with Horizontal(id="actions") as actions:
                if self.kind == "test-results":
                    actions.display = False
                elif self.kind == "input":
                    actions.styles.dock = "bottom"
                if self.kind == "input" and len(app.session.target_paths) > 1:
                    yield Button("Switch target .env (s outside input)", id="switch-target")
                for key, label in self.actions:
                    yield Button(label, id=key)
            if self.kind != "test-results":
                yield Static("", id="error", markup=False)
        if self.kind == "menu" and getattr(app.context, "show_pack_bootstrap", False):
            with Vertical(id="menu-bottom-bar"):
                yield Static("Bootstrap: --resource-pack-dir PATH or RESOURCE_PACK_DIR selects a resource pack.",
                             id="pack-bootstrap", markup=False)
                yield ButtonFooter()
        else:
            yield ButtonFooter()

    def on_mount(self):
        if self.rows:
            table = self.query_one(DataTable)
            if self.kind == "defaults":
                table.add_columns("Setting", "Effective value", "Source")
            elif self.kind == "test-results":
                table.add_columns("Status", "Test", "Execution time")
            elif self.kind == "containers":
                table.add_columns("Selected", "ID", "Name", "Image", "Status")
            elif self.kind == "cleanup":
                table.add_column("", width=1)
                table.add_columns("Setting", "Effective value", "Source")
            else:
                table.add_columns("Selected / present", "Setting / test", "Value / count", "Source")
            for key, cells in self.rows:
                table.add_row(*(literal_cell(cell) for cell in cells), key=key)
            if self.kind == "defaults":
                self.app.refresh_configuration_rows(self)
            table.move_cursor(row=next((i for i, (key, _) in enumerate(self.rows)
                                        if key == self.selected_row), 0))
            table.focus()
        elif self.kind == "input":
            control = self.query_one('#config-value')
            if isinstance(control, Input):
                control.value = self.app.prompt_value
            control.focus()
        elif self.actions:
            self.query_one(Button).focus()
        if self.kind == "suggest":
            self._bindings.bind("s", "skip_setup", "Skip setup")
        if self.kind == "containers":
            self._bindings.bind("s", "stop_selected", "Stop selected containers")
        if self.kind == "tests":
            self._bindings.bind("s", "start_selected", "Start selected tests")
        if self.kind == "test-results":
            self.call_after_refresh(self.layout_test_results)
        if self.kind == "defaults":
            self._bindings.bind("c", "close_view", "Next menu")
        self.refresh_bindings()

    def on_resize(self, event):
        if self.kind == "test-results":
            self.call_after_refresh(self.layout_test_results)

    def layout_test_results(self):
        if not self.is_mounted or not self.rows or self.app.screen is not self or not self.query("#workbench-content"):
            return
        viewport = self.query_one("#workbench-content", VerticalScroll)
        heading = self.query_one("#heading")
        summary = self.query_one("#unit-summary")
        table = self.query_one(DataTable)
        margins = sum(widget.styles.margin.top + widget.styles.margin.bottom
                      for widget in (heading, summary, table))
        available = viewport.content_size.height - heading.outer_size.height - margins
        summary_height = 6 if not self.test_run_complete else max(6, (available - 8) // 2)
        self.query_one("#unit-summary").styles.height = summary_height
        self.query_one(DataTable).styles.height = max(8, available - summary_height)

    def action_toggle_selection(self):
        table = self.query_one(DataTable)
        if table.row_count:
            table.action_select_cursor()

    async def action_back_to_selection(self):
        self.app.run_worker(self.app.back_to_selection(), group="collection", exclusive=True)

    async def action_start_selected(self):
        await self.app.choose("continue")

    def check_action(self, action, parameters):
        # Textual evaluates underlying screen bindings while a modal is open.
        # Their contextual actions must not inspect the modal as this screen.
        if self.app.screen is not self:
            return False
        if action == "toggle_selection":
            return isinstance(self.app.focused, DataTable)
        if action == "close_view" and self.kind == "menu":
            return False
        if action == "back_to_selection":
            return self.kind == "test-results" and self.test_run_complete and not self.app.test_run_active()
        if action.startswith("failure_"):
            return self.kind == "test-failure"
        if action == "back_to_configuration":
            return self.kind == "input"
        if action == "close_view" and self.kind == "input":
            return False
        if action == "accept_default":
            return self.kind == "defaults" and self.app.focused_default()
        if action == "unset_setting":
            return self.kind == "defaults" and self.app.focused_setting() in self.config_saved
        if action == "edit_setting":
            return self.kind == "defaults"
        if action == "setting_help":
            return self.selected_setting_help() is not None
        if action == "delete_assignment":
            return self.kind == "cleanup" and self.app.focused_cleanup_present()
        if action == "switch_target":
            return (self.kind in {"defaults", "input"}
                    or self.kind == "unavailable" and self.app.mode in {None, "unattended-auth"}) and len(self.app.session.target_paths) > 1
        if action == "start_selected":
            return self.kind == "tests"
        if action == "stop_selected":
            return self.kind == "containers"
        if action == "skip_setup":
            return self.kind == "suggest"
        return True

    def on_data_table_row_highlighted(self, event):
        if self.kind == "defaults" and self.app.screen is self:
            self.app.refresh_configuration_rows(self)
        elif self.kind == "cleanup" and self.app.screen is self:
            self.app.refresh_rows()
        self.refresh_bindings()

    async def on_data_table_row_selected(self, event):
        if self.kind == 'defaults':
            if self.app.focused_default() and self.app.focused_setting() not in self.config_saved:
                await self.app.accept_default()
            else:
                self.app.edit_setting()
        else:
            self.app.toggle_row(str(event.row_key.value))

    def on_advancing_select_submitted(self, event):
        if self.kind == 'input' and event.select.value is not Select.NULL:
            self.query_one('#submit', Button).focus()

    async def on_button_pressed(self, event):
        await self.app.choose(event.button.id)

    def config_value(self):
        value = self.query_one('#config-value').value
        if value is Select.NULL:
            raise ValueError(f'Select a value for {self.app.prompt_name} before saving.')
        return value

    async def on_input_submitted(self, event):
        await self.app.choose("submit")

    def action_quit_request(self):
        self.app.push_screen(QuitDialog(), lambda yes: self.app.confirm_quit() if yes else None)

    async def action_close_view(self):
        await self.app.close_view()

    def action_delete_assignment(self):
        if self.app.focused_cleanup_present():
            table = self.query_one(DataTable)
            name = str(table.get_row_at(table.cursor_row)[1])
            self.app.push_screen(DeleteDialog(name),
                                 lambda yes: self.app.delete_assignment(name) if yes else None)

    def action_switch_target(self):
        self.app.switch_target()

    def action_back_to_configuration(self):
        self.app.show_configuration()

    async def action_accept_default(self):
        await self.app.accept_default()

    def action_edit_setting(self):
        self.app.edit_setting()

    def selected_setting_help(self):
        if self.kind not in {"defaults", "cleanup"} or not self.query(DataTable):
            return None
        field = self.app.schema.fields.get(self.app.focused_setting())
        return (field.name, field.help) if field and field.help.strip() else None

    def action_setting_help(self):
        description = self.selected_setting_help()
        if description is not None:
            self.app.push_screen(SettingHelpDialog(*description))

    def action_unset_setting(self):
        try:
            name = self.app.focused_setting()
            self.app.session.unset_saved(name)
            self.app.show_configuration()
        except Exception as error:
            self.query_one("#error", Static).update(str(error))

    def action_failure_up(self):
        self.query_one(VerticalScroll).scroll_up(animate=False)

    def action_failure_down(self):
        self.query_one(VerticalScroll).scroll_down(animate=False)

    def action_failure_page_up(self):
        self.query_one(VerticalScroll).scroll_page_up(animate=False)

    def action_failure_page_down(self):
        self.query_one(VerticalScroll).scroll_page_down(animate=False)

    async def action_stop_selected(self):
        await self.app.choose("stop-containers")

    async def action_skip_setup(self):
        await self.app.start_configuration()


class ApiAgentApp(App[int]):
    @property
    def schema(self):
        return getattr(self.session, "schema", self._initial_schema)

    @schema.setter
    def schema(self, value):
        self._initial_schema = value
        if hasattr(self, "session"):
            self.session.schema = value
            if hasattr(self.session, "_provider_base_schema"):
                self.session._provider_base_schema = value

    CSS = """
    Screen { padding: 1 2; }
    #dotenv-targets { height: auto; margin-top: 1; margin-bottom: 1; }
    #identity { height: auto; color: $text-muted; margin-bottom: 1; }
    #heading { text-style: bold; margin-bottom: 1; }
    #body { height: auto; margin-bottom: 1; }
    #unit-summary { height: 1fr; min-height: 6; margin-bottom: 1; }
    #actions { height: auto; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 4; }
    Button { width: 100%; height: 3; min-height: 3; margin: 0 1 1 0; }
    Button:focus, Input:focus { border: heavy $accent; }
    DataTable { height: 1fr; min-height: 8; max-height: 100vh; margin-bottom: 1; }
    #error { color: $error; height: auto; }
    QuitDialog, DeleteDialog, StopContainersDialog { align: center middle; background: $background 80%; }
    QuitDialog > *, DeleteDialog > *, StopContainersDialog > * { width: 65%; }
    .confirmation-actions { height: auto; }
    .confirmation-actions Button { width: 1fr; }
    SettingHelpDialog { align: center middle; background: $background 80%; }
    #setting-help-dialog { width: 85%; max-width: 100; height: 85%; border: round $accent; padding: 1 2; background: $surface; }
    #setting-help-title { height: auto; margin-bottom: 1; }
    #setting-help-scroll { height: 1fr; min-height: 1; margin-bottom: 1; }
    #setting-help-text { height: auto; }
    #setting-help-close { width: 16; margin: 0; }
    .compact-setting-help #setting-help-dialog { height: 100%; padding: 0 2; }
    .compact-setting-help #setting-help-title, .compact-setting-help #setting-help-scroll { margin-bottom: 0; }
    #tab-select-hint { width: auto; height: 1; }
    Footer { dock: bottom; width: 100%; }
    #menu-bottom-bar { dock: bottom; height: auto; }
    #menu-bottom-bar Footer { dock: none; }
    #pack-bootstrap { height: auto; color: red; }
    """

    def __init__(self, context, schema, argv=(), environ=None, *, session=None, collector=None, container_runtime=None, unit_runner=None):
        super().__init__()
        from api_agent.config.session import ConfigSession
        self.context, self._initial_schema = context, schema
        self.session = session or ConfigSession(context, schema, argv, environ)
        self.collector = collector
        self.unit_runner = unit_runner
        self.unit_result = None
        self.test_run = None
        self.unit_rows = {}
        from api_agent.runtime.containers import ContainerRuntime
        from api_agent.runtime.ownership import StackScope
        self.container_runtime = container_runtime or ContainerRuntime(
            scope=StackScope.from_roots(context.base_root, context.pack_root),
            ownership_directory=context.owner_root / 'artifacts' / 'prerequisites')
        self.containers = ()
        self.container_marked = set()
        self.container_summary = "Running containers: checking…"
        from api_agent.runtime.worktrees import checkout_branch, WorktreeGateError
        self.branches = {}
        for root in dict.fromkeys((context.base_root, context.pack_root)):
            try:
                self.branches[root] = checkout_branch(root)
            except WorktreeGateError:
                self.branches[root] = None
        self.operation_status = 3
        self.mode = None
        self.review_names = set()
        self.selection = None
        self.pending_test_selectors = tuple(self.session.result().positionals)
        self.active_test_selectors = ()
        self.prompt_name = None
        self.collection_cancel = threading.Event()

    def identity_summary(self):
        def path_with_branch(root):
            branch = self.branches.get(root)
            return f"{root} ({branch})" if branch else str(root)
        return (f"Base: {path_with_branch(self.context.base_root)}\n"
                f"Pack: {path_with_branch(self.context.pack_root)}")

    def identity_text(self):
        text = Text(self.identity_summary())
        pack = self.context.pack_root
        if not self.context.external_pack and pack.name == "demo":
            start = text.plain.index("\nPack: ") + len("\nPack: ") + len(str(pack)) - len("demo")
            text.stylize("red", start, start + len("demo"))
        return text

    def target_summary(self):
        return "Target .env files:\n" + "\n".join(
            f"{'✓' if path == self.session.dotenv_path else ' '} {path}"
            for path in self.session.target_paths)

    def switch_target(self):
        kind = self.screen.kind
        self.session.switch_target()
        if kind == "cleanup":
            self.show_cleanup()
        elif kind == "input":
            self.screen.query_one("#dotenv-targets", Static).update(self.target_summary())
            self.screen.query_one("#body", Static).update(
                self.setting_description())
        elif kind == "defaults":
            self.show_configuration()
        else:
            self.screen.query_one("#dotenv-targets", Static).update(self.target_summary())

    def show(self, kind, title, text="", actions=(), rows=(), *, selected_row=None):
        screen = WorkbenchScreen(kind, title, text, actions, rows, selected_row=selected_row)
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
        if self.mode is not None:
            await self.enter_mode(skip_defaults=True)
            return
        if result.values.get("COLLECT_ONLY"):
            self.show_menu()
            return
        incomplete = self.auth_setup_needed()
        if incomplete and self.mode != "unattended-auth":
            self.show("suggest", "Unattended authentication", "Credential profiles are absent or incomplete. Configure account sources, or skip to continue with attended authentication.", (("setup", "Setup authentication"), ("skip", "Skip (s)")))
        else:
            await self.start_configuration()

    def configuration_pending(self, *, include_optional=True):
        candidates = [item.name for item in self.session.default_candidates()]
        missing = [name for name in self.session.result(_review=True).missing
                   if self.schema.fields[name].acknowledge_default]
        review = self.session.result(_review=True)
        optional = [name for name, field in self.schema.fields.items()
                    if include_optional and field.review_optional and field.acknowledge_default
                    and (not field.pack_only or self.context.external_pack)
                    and (name not in review.fields or not review.fields[name].explicit)]
        return set(candidates + missing + optional)

    def credential_roles(self):
        """Read generic pack-declared role labels from executable configuration."""
        roles = ["LLM User"]
        result = self.session.result(_review=True)
        for name in getattr(self.context, 'credential_role_fields', ()):
            field = self.schema.fields.get(name)
            if field is None or field.type != 'str':
                raise ConfigError(f'{name}: credential role must name a declared string field')
            label = result.values.get(name)
            if not label:
                continue
            if not label.strip() or label in roles:
                raise ConfigError(f'{name}: credential role must be nonblank and unique')
            roles.append(label)
        return tuple(roles)

    def auth_setup_needed(self):
        from api_agent.auth.profiles import normalize_auth, parse_profiles
        profile = self.session.result(_review=True).values.get('UNATTENDED_AUTH')
        try:
            roles = self.credential_roles()
            canonical = normalize_auth(profile)
            if any(not canonical['roles'].get(role) for role in roles):
                return True
            parse_profiles({'password_manager': canonical['password_manager'],
                            'roles': {role: canonical['roles'][role] for role in roles}}, roles)
        except ValueError:
            return True
        # Every enabled role needs an account and valid manager/path settings.
        return False

    def show_auth_setup(self, *, startup=False):
        from api_agent.tui.auth_setup import AuthSetupScreen
        try:
            roles = self.credential_roles()
        except ConfigError as error:
            self.show('error', 'Authentication setup configuration error', str(error),
                      (("menu", "Main menu"),))
            return
        self.push_screen(AuthSetupScreen(roles),
                         lambda changed: self.auth_setup_closed(changed, startup=startup))

    async def auth_setup_closed(self, changed, *, startup=False):
        if changed:
            self.operation_status = 0
        if startup:
            self.mode = None
            await self.start_configuration()
        else:
            self.show_menu()

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
        selected_row = None
        if isinstance(self.screen, WorkbenchScreen):
            if self.screen.kind == "defaults":
                selected_row = self.focused_setting()
            elif self.screen.kind == "input":
                selected_row = self.prompt_name
        result = self.session.result(_review=True)
        if "UNATTENDED_AUTH" in result.missing:
            self.show_required_auth()
            return
        active = set(result.fields) | set(result.missing) | {
            name for name, field in self.schema.fields.items() if field.review_optional}
        review = self.session.result(_review=True)
        default_names = {name for name, field in self.schema.fields.items()
                         if field.default is not UNSET or field.conditional_defaults}
        self.review_names.update(self.configuration_pending())
        rows = []
        for name in self.schema.fields:
            if (not self.schema.fields[name].acknowledge_default or name not in self.review_names
                    or (name not in active and name not in default_names)):
                continue
            resolved = review.fields.get(name) if name in default_names else result.fields.get(name)
            if name in default_names and resolved is None and name not in result.missing and not self.schema.fields[name].review_optional:
                continue
            rows.append((name, (name,
                        display(resolved.value) if resolved else Text("Required", justify="right") if name in result.missing else "",
                        resolved.source if resolved else "Not provided")))
        if not rows:
            self.show_menu()
            return
        self.show("defaults", "Review configuration",
                  "Review each setting: a saves its default immediately; e edits its value, then Enter saves to .env. "
                  "Source identifies where each value comes from. c opens the next menu; required values still block execution.\n\n"
                  "A developer may need overrides, or may save defaults as a point-in-time configuration for reproducing and comparing a run.",
                  rows=rows, selected_row=selected_row)

    def refresh_configuration_rows(self, screen):
        """Refresh displayed values and contextual actions as one UI snapshot."""
        result = self.session.result(_review=True)
        table = screen.query_one(DataTable)
        saved, defaults = set(), set()
        for row in table.ordered_rows:
            name = row.key.value
            resolved = result.fields.get(name)
            field = self.schema.fields[name]
            if self.session.saved_path(name) is not None:
                saved.add(name)
            if resolved and not resolved.explicit and field.acknowledge_default:
                defaults.add(name)
            cells = (name, display(resolved.value) if resolved else
                     Text("Required", justify="right") if name in result.missing else "",
                     resolved.source if resolved else "Not provided")
            for column, value in zip(table.columns, cells):
                table.update_cell(row.key, column, literal_cell(value))
        screen.config_saved, screen.config_defaults = saved, defaults

    def focused_setting(self):
        table = self.screen.query_one(DataTable)
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value if table.row_count else None

    def focused_default(self):
        name = self.focused_setting()
        return name in self.screen.config_defaults

    async def accept_default(self):
        if self.screen.kind != "defaults" or not self.focused_default():
            return
        try:
            name = self.focused_setting()
            self.session.save_defaults([name])
            self.operation_status = 0
            if self.session.result().values.get("NO_STD_IN"):
                await self.enter_mode()
                return
            self.show_configuration()
        except Exception as error:
            self.screen.query_one("#error", Static).update(str(error))

    def setting_description(self):
        from api_agent.config.resolve import _matches
        field = self.schema.fields[self.prompt_name]
        default = field.default
        values = self.session.result(_review=True).values
        for choice in field.conditional_defaults:
            if _matches(choice['when'], values):
                default = choice['value']
                break
        description = f"Declared type: {field.type}."
        if default is not UNSET:
            description += f" Default: {display(default)}."
        return description + " Use Tab to focus actions; s switches the target outside the value input."

    def edit_setting(self):
        if self.screen.kind != "defaults":
            return
        self.prompt_name = self.focused_setting()
        if not self.prompt_name:
            return
        resolved = self.session.result(_review=True).fields.get(self.prompt_name)
        self.prompt_value = display(resolved.value) if resolved else ""
        self.show("input", f"Configure {self.prompt_name}",
                  self.setting_description(),
                  (("submit", "Save value (Enter)"),))

    async def enter_mode(self, *, skip_defaults=False):
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
        elif self.mode == "cleanup-containers":
            self.show_containers()
            self.refresh_container_inventory()
        elif self.mode == "unattended-auth":
            self.show_auth_setup()
        else:
            await self.configure_operation(skip_defaults=skip_defaults)

    def show_menu(self):
        # Keep acknowledged rows only for the current review, not later visits.
        self.review_names.clear()
        self.pending_test_selectors = ()
        self.active_test_selectors = ()
        if self.context.external_pack:
            self.session.switch_target(self.context.pack_root / ".env")
        self.mode = None
        actions = tuple(("mode-" + key, label) for key, label in MODES)
        if self.configuration_pending():
            actions += (('review-config', 'Review configuration'),)
        self.show("menu", "Choose an operation", self.container_summary, actions=actions)
        self.refresh_container_inventory()

    def refresh_container_inventory(self):
        self.run_worker(self._refresh_container_inventory(), group="containers", exclusive=True)

    async def _refresh_container_inventory(self):
        try:
            self.containers = await asyncio.to_thread(self.container_runtime.inventory)
            self.container_marked.intersection_update(item.id for item in self.containers)
            self.container_summary = f"Running containers: {len(self.containers)}"
        except Exception as error:
            self.containers = ()
            self.container_marked.clear()
            self.container_summary = str(error)
            if not self.container_summary.startswith("Container"):
                self.container_summary = "Container inventory unavailable."
        if self.mode == "cleanup-containers" and isinstance(self.screen, WorkbenchScreen):
            self.show_containers()
        elif isinstance(self.screen, WorkbenchScreen) and self.screen.kind == "menu":
            self.call_after_refresh(self.update_container_menu)

    def update_container_menu(self):
        if isinstance(self.screen, WorkbenchScreen) and self.screen.kind == "menu":
            self.screen.query_one("#body", Static).update(self.container_summary)
            button = self.screen.query_one("#mode-cleanup-containers", Button)
            confirmed_empty = self.container_summary == "Running containers: 0"
            button.label = "No running containers to stop" if confirmed_empty else "Stop running containers"
            button.disabled = confirmed_empty

    def show_containers(self):
        rows = [(item.id, ("✓" if item.id in self.container_marked else "", item.id, item.name, item.image, item.status)) for item in self.containers]
        if self.containers:
            all_selected = all(item.id in self.container_marked for item in self.containers)
            rows.insert(0, ("container-all", ("✓" if all_selected else "", "All", "", "", "")))
        self.show("containers", "Stop running containers", self.container_summary +
                  "\nEnter selects/unselects rows. Stop selected asks for confirmation. Containers and volumes are not removed.",
                  (("stop-containers", "Stop selected containers (s)"), ("refresh-containers", "Refresh inventory")), rows)

    def request_container_stop(self):
        selected = tuple(item for item in self.containers if item.id in self.container_marked)
        if not selected:
            raise ValueError("Select at least one running container.")
        self.push_screen(StopContainersDialog(selected), lambda yes: self.run_worker(
            self._stop_containers(selected), group="containers", exclusive=True) if yes else None)

    async def _stop_containers(self, selected):
        try:
            await asyncio.to_thread(self.container_runtime.stop, tuple(item.id for item in selected))
            self.operation_status = 0
            self.container_marked.clear()
            await self._refresh_container_inventory()
        except Exception as error:
            self.operation_status = 3
            await self._refresh_container_inventory()
            self.call_after_refresh(self.show_container_stop_error, str(error))

    def show_container_stop_error(self, message):
        if isinstance(self.screen, WorkbenchScreen) and self.screen.kind == "containers":
            self.screen.query_one("#error", Static).update(message)

    async def configure_operation(self, *, skip_defaults=False):
        self.session.set_value("TUI_MODE", self.mode)
        result = self.session.result()
        if result.errors:
            self.operation_status = 3
            self.show("error", "Configuration error", "\n".join(result.errors), (("menu", "Main menu"),))
            return
        hidden_missing = [name for name in result.missing
                          if not self.schema.fields[name].acknowledge_default]
        if hidden_missing:
            self.operation_status = 3
            if "UNATTENDED_AUTH" in hidden_missing:
                self.show_required_auth()
                return
            self.show("error", "Missing configuration",
                      "Required settings must be provided explicitly:\n" + "\n".join(
                          f"{name}: .env / environment {name} or --{name.lower().replace('_', '-')} VALUE"
                          for name in result.missing), (("menu", "Main menu"),))
            return
        if result.missing or (not skip_defaults and self.configuration_pending(include_optional=False)):
            await self.start_configuration()
            return
        if self.mode in {"unit", "integration", "e2e"}:
            await self.collect_tests()
        else:
            if self.mode == "run-local" and not self.check_operation_start():
                return
            self.unavailable(f"{dict(MODES)[self.mode]} execution is not implemented yet. No services or processes were started.")

    async def discover_tests(self, cancel_event):
        from api_agent.testing.collection import collect_tests
        collector = self.collector or collect_tests
        arguments = (self.context, self.mode, ())
        return await asyncio.to_thread(
            collector, *arguments, **({} if self.collector else {"cancel_event": cancel_event}))

    async def collect_tests(self):
        self.active_test_selectors = self.pending_test_selectors
        self.pending_test_selectors = ()
        self.collection_cancel = threading.Event()
        self.run_worker(self._collect_tests(self.collection_cancel), exclusive=True)

    async def _collect_tests(self, cancel_event):
        self.show("collecting", "Collecting tests", "Collecting pytest identities only; no application prerequisites or tests are executed.")
        try:
            from api_agent.testing.selection import SelectionModel
            tests = await self.discover_tests(cancel_event)
            if cancel_event.is_set() or self.collection_cancel is not cancel_event:
                return
            if self.session.result().values.get("COLLECT_ONLY"):
                from api_agent.testing.collection import select_tests
                self.collected_output = "\n".join(test.id for test in select_tests(tests, self.active_test_selectors))
                self.exit(0)
                return
            self.selection = SelectionModel(tests)
            if self.active_test_selectors:
                self.selection.preselect(self.active_test_selectors)
            if not tests:
                raise ValueError("No tests matched the selected suite/selectors")
            self.show_selection()
        except Exception as error:
            if cancel_event.is_set() or self.collection_cancel is not cancel_event:
                return
            self.show("error", "Collection failed", str(error), (("menu", "Main menu"),))

    async def back_to_selection(self):
        if self.test_run_active() or self.selection is None:
            return
        cancel_event = threading.Event()
        selection = self.selection
        self.collection_cancel = cancel_event
        self.show("collecting", "Refreshing test selection", "Collecting current tests while preserving your selection.")
        try:
            tests = await self.discover_tests(cancel_event)
            if cancel_event.is_set() or self.collection_cancel is not cancel_event or self.selection is not selection:
                return
            selection.refresh_for_run(tests)
            self.show_selection()
        except Exception as error:
            if (not cancel_event.is_set() and self.collection_cancel is cancel_event
                    and self.selection is selection):
                self.show("error", "Collection failed", str(error), (("menu", "Main menu"),))

    def start_unit_run(self):
        self.operation_status = 3
        self.test_run = TestRunLifecycle()
        self.collection_cancel = self.test_run.cancel_event
        self.unit_result = None
        self.unit_stopped = self.test_run.stopped
        self.unit_rows = {test.id: ("PENDING", test.id, "") for test in self.selection.selected_tests}
        self.show_unit_results(self.unit_progress())
        self.run_worker(self._run_units(), group="unit-run", exclusive=True)

    async def _run_units(self):
        from api_agent.testing.runner import run_unit_tests
        runner = self.unit_runner or run_unit_tests
        try:
            fresh_tests = await self.discover_tests(self.collection_cancel)
            if self.collection_cancel.is_set():
                return
            self.selection.refresh_for_run(fresh_tests)
            if not self.selection.selected_tests:
                raise ValueError("No selected tests remain after recollection")
            self.unit_rows = {test.id: ("PENDING", test.id, "") for test in self.selection.selected_tests}
            self.show_unit_results(self.unit_progress())
            result = await asyncio.to_thread(
                runner, self.context, self.selection.selected_tests,
                on_event=lambda event: self.call_from_thread(self.unit_event, event),
                cancel_event=self.collection_cancel,
                parallel_jobs=self.session.result().values.get("TEST_PARALLEL_JOBS", 8),
                require_full_coverage=self.selection.groups()[0].selected)
            self.unit_result = result
            for test in result.tests:
                self.unit_rows[test.identity.id] = (test.status.upper(), test.identity.id, f"{test.duration:.3f}s")
            self.operation_status = result.exit_code
            if not self.collection_cancel.is_set():
                self.show_unit_results()
        except Exception as error:
            self.operation_status = 3
            if not self.collection_cancel.is_set():
                self.show("error", "Unit execution failed", str(error), (("menu", "Main menu"),))
        finally:
            self.test_run.finish()

    def unit_progress(self):
        return progress_summary(self.mode or "unit", self.unit_rows)

    def unit_event(self, event):
        if self.collection_cancel.is_set():
            return
        if event.identity is not None and event.kind in {"test_started", "test_finished"}:
            key = event.identity.id
            self.unit_rows[key] = ((event.status or "RUNNING").upper(), key,
                                   f"{event.duration:.3f}s" if event.kind == "test_finished" else "")
            if isinstance(self.screen, WorkbenchScreen) and self.screen.kind == "test-results" and self.screen.query(DataTable) and self.screen.query("#body"):
                table = self.screen.query_one(DataTable)
                self.screen.query_one("#body", Static).update(self.unit_progress())
                for column, value in zip(table.columns, self.unit_rows[key]):
                    table.update_cell(key, column, literal_cell(value))

    def show_unit_results(self, summary=None):
        if summary is None:
            from api_agent.testing.runner import format_unit_summary
            summary = format_unit_summary(self.unit_result)
        rows = ordered_result_rows(self.unit_rows, completed=self.unit_result is not None)
        if isinstance(self.screen, WorkbenchScreen) and self.screen.kind == "test-results":
            screen = self.screen
            screen.body, screen.rows = summary, rows
            self.call_after_refresh(self.update_test_results, screen)
        else:
            self.show("test-results", "Unit test results", summary, rows=rows)
        self.screen.test_run_complete = self.unit_result is not None
        self.screen.style_results = self.style_unit_summary
        if self.unit_result is not None:
            self.call_after_refresh(self.style_unit_summary)

    def update_test_results(self, screen):
        if self.screen is not screen or not screen.query(DataTable):
            return
        screen.query_one("#body", Static).update(screen.body)
        table = screen.query_one(DataTable)
        reconcile_results_table(table, screen.rows, completed=screen.test_run_complete)
        if screen.test_run_complete and screen.style_results:
            screen.style_results()
        screen.layout_test_results()

    def style_unit_summary(self):
        if isinstance(self.screen, WorkbenchScreen) and self.screen.kind == "test-results":
            body = self.screen.query_one("#body", Static)
            body.update(styled_summary(
                body.content, failed=bool(self.unit_result.exit_code),
                neutral_details=not self.unit_result.require_full_coverage,
                green_prefixes=("Production coverage: 100%",)))

    def show_selection(self):
        rows = [(row.id, ("✓" if row.selected else "", row.label, f"{row.count}/{row.total}", row.kind)) for row in self.selection.groups()]
        self.show("tests", f"Select {self.mode} tests", "Toggle groups or individual tests with Enter. Press s to start selected tests.", (("continue", "(s) Start selected tests"),), rows)

    def show_cleanup(self):
        rows = [(row.name, ("✓" if row.present and str(row.source).endswith("/.env") else "", row.name, display(row.value) if row.resolved else "Inactive / unset", row.source if row.resolved else "")) for row in self.session.cleanup_rows()]
        location = "all agent / resource pack .env files" if self.context.external_pack else "agent .env file"
        self.show("cleanup", "Clear .env configuration",
                  f"Select a row and delete it (with confirmation) from {location}.", rows=rows)

    def focused_cleanup_present(self):
        if self.screen.kind != "cleanup":
            return False
        table = self.screen.query_one(DataTable)
        if not table.row_count:
            return False
        key = str(table.get_row_at(table.cursor_row)[1])
        return any(row.name == key and row.present and str(row.source).endswith("/.env") for row in self.session.cleanup_rows())

    def toggle_row(self, key):
        if self.screen.kind == "test-results" and self.unit_result is not None:
            failed = next((test for test in self.unit_result.tests if test.identity.id == key and test.traceback), None)
            if failed:
                self.show("test-failure", "Unit test failure", key + "\n\n" + failed.traceback)
                self.call_after_refresh(lambda: self.screen.query_one(VerticalScroll).focus())
        elif self.screen.kind == "tests":
            self.selection.toggle(key)
            self.refresh_rows()
        elif self.screen.kind == "containers":
            ids = {item.id for item in self.containers}
            if key == "container-all":
                self.container_marked = set() if ids <= self.container_marked else ids
            elif key in ids:
                self.container_marked.symmetric_difference_update({key})
            table = self.screen.query_one(DataTable)
            column = next(iter(table.columns))
            for item in self.containers:
                table.update_cell(item.id, column, "✓" if item.id in self.container_marked else "")
            if ids:
                table.update_cell("container-all", column, "✓" if ids <= self.container_marked else "")

    def refresh_rows(self):
        table = self.screen.query_one(DataTable)
        if self.screen.kind == "tests":
            rows = [(row.id, ("✓" if row.selected else "", row.label, f"{row.count}/{row.total}", row.kind)) for row in self.selection.groups()]
        else:
            rows = [(row.name, ("✓" if row.present and str(row.source).endswith("/.env") else "", row.name, display(row.value) if row.resolved else "Inactive / unset", row.source if row.resolved else "")) for row in self.session.cleanup_rows()]
        if self.screen.kind == "cleanup":
            # Rebuilding posts another highlight event; unchanged snapshots must
            # not rebuild again. Preserve the focused setting when rows change.
            current = [tuple(table.get_row_at(i)) for i in range(table.row_count)]
            if current == [cells for _, cells in rows]:
                self.screen.refresh_bindings()
                return
            cursor = table.cursor_row
            selected = current[cursor][1] if current else None
            table.clear()
            for key, cells in rows:
                table.add_row(*(literal_cell(cell) for cell in cells), key=key)
            if rows:
                row_index = next((i for i, (key, _) in enumerate(rows) if key == selected),
                                 min(cursor, len(rows) - 1))
                table.move_cursor(row=row_index)
            self.screen.refresh_bindings()
            return
        for key, cells in rows:
            for column, value in zip(table.columns, cells):
                table.update_cell(key, column, literal_cell(value))
        self.screen.refresh_bindings()

    def delete_assignment(self, name):
        try:
            self.session.delete_keys([name])
            self.operation_status = 0
            self.refresh_rows()
        except Exception as error:
            self.screen.query_one("#error", Static).update(str(error))

    def check_operation_start(self):
        from api_agent.auth.extensions import validate_operation
        try:
            validate_operation(self.context, self.session.result().values)
        except ValueError as error:
            self.unavailable(str(error))
            return False
        return True

    def unavailable(self, text):
        self.operation_status = 3
        self.show("unavailable", "Operation unavailable", text + "\nExit status: 3. No successful execution is reported.", (("menu", "Main menu"), ("exit", "Exit")))

    async def choose(self, key):
        try:
            if key == "stop-containers":
                self.request_container_stop()
            elif key == "refresh-containers":
                self.refresh_container_inventory()
            elif key == "switch-target":
                self.switch_target()
            elif key == "skip":
                await self.start_configuration()
            elif key == 'review-config':
                self.show_configuration()
            elif key == "submit":
                value = self.screen.config_value()
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
                self.show_auth_setup(startup=self.mode is None)
            elif key == "continue":
                if not self.selection.selected_ids:
                    raise ValueError("Select at least one test before continuing")
                if self.mode == "e2e":
                    self.show("acceptance", "E2E human acceptance", f"{len(self.selection.selected_tests)} tests selected to run.\n\n" + E2E_WARNING, (("accept", "Accept for this run"), ("decline", "Decline")))
                elif self.mode == "unit":
                    self.start_unit_run()
                else:
                    if not self.check_operation_start():
                        return
                    self.unavailable("Integration execution is not implemented yet. Your selection was collected but no tests were run.")
            elif key == "accept":
                if not self.check_operation_start():
                    return
                self.unavailable("E2E execution is not implemented yet. Acceptance did not start tests or prerequisites.")
            elif key == "decline":
                self.operation_status = 3
                self.show_selection()
            elif key == "menu":
                if self.test_run_active():
                    await self.close_view()
                else:
                    self.show_menu()
            elif key == "exit":
                self.exit(self.operation_status)
        except Exception as error:
            self.screen.query_one("#error", Static).update(str(error))

    async def confirm_quit(self):
        if self.mode == "unit" and self.unit_rows and self.unit_result is None:
            self.operation_status = 130
        self.collection_cancel.set()
        if self.test_run_active():
            await self.test_run.stop_and_wait()
        self.exit(self.operation_status)

    def on_unmount(self):
        self.collection_cancel.set()

    def test_run_active(self):
        return self.test_run is not None and self.test_run.active

    async def stop_tests_to_menu(self, accepted):
        if not accepted:
            return
        await self.test_run.stop_and_wait()
        self.operation_status = 130
        self.show_menu()

    async def close_view(self):
        if self.test_run_active():
            self.push_screen(QuitDialog("Stop running tests and return to the menu? Enter literal y to confirm; every other response cancels."),
                             self.stop_tests_to_menu)
            return
        if self.screen.kind == "collecting":
            self.collection_cancel.set()
        if self.screen.kind == "suggest" or self.screen.kind == "unavailable" and self.mode is None:
            await self.start_configuration()
        elif self.screen.kind == "input":
            self.show_configuration()
        elif self.screen.kind == "test-failure":
            self.show_unit_results()
        elif self.screen.kind == "test-results" and self.unit_result is None:
            return
        elif self.screen.kind == "cleanup" and self.configuration_pending():
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
