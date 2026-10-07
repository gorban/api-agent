"""Interactive editor for unattended credential descriptors."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict

from api_agent.auth.profiles import parse_profiles
from api_agent.config.editor import DotenvEditor
from api_agent.tui.app import ButtonFooter, FocusNavigation, QuitDialog
from api_agent.tui.test_results import literal_cell
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Select, Static


class AuthSetupScreen(FocusNavigation, Screen[bool]):
    """Edit caller-declared roles; dismissal reports whether configuration changed."""

    DEFAULT_CSS = """
    AuthSetupScreen #auth-identity { height: auto; color: $text-muted; margin-bottom: 1; }
    AuthSetupScreen #auth-heading { height: auto; text-style: bold; margin-bottom: 1; }
    AuthSetupScreen #auth-workflow { margin-bottom: 1; }
    AuthSetupScreen #auth-content { height: 1fr; }
    AuthSetupScreen #auth-content Static { height: auto; }
    AuthSetupScreen #auth-table { height: 8; min-height: 6; margin-bottom: 1; }
    AuthSetupScreen #auth-form-actions { height: auto; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 4; }
    AuthSetupScreen #auth-form-actions Button { width: 100%; }
    AuthSetupScreen .auth-form-field { margin-top: 1; }
    AuthSetupScreen #auth-error { color: $error; }
    AuthSetupScreen #auth-targets { margin-top: 1; }
    """

    BINDINGS = [Binding("a", "add_entry", "Add"), Binding("e", "edit_entry", "Edit"),
                Binding("r", "remove_entry", "Remove"), Binding("x", "clear_config", "Clear"),
                Binding("t", "attended_role", "Use attended"),
                Binding("s", "switch_target", "Switch .env"), Binding("c", "close_view", "Close"),
                Binding("q", "quit_request", "Quit"), Binding("escape", "cancel_form", "Back")]

    def __init__(self, roles: tuple[str, ...]):
        super().__init__()
        self.roles = tuple(roles)
        self.entries: dict[str, list[dict[str, str]]] = {role: [] for role in self.roles}
        self.other_roles: dict[str, object] = {}
        self.selection: tuple[str, int | None] | None = None
        self.editing: tuple[str, int | None] | None = None
        self.changed = False
        self.load_error = ""

    def compose(self) -> ComposeResult:
        identity = getattr(self.app, "identity_text", None)
        if identity is not None:
            yield Static(identity(), id="auth-identity", markup=False)
        else:
            yield Static(f"Base: {self.app.context.base_root}\nPack: {self.app.context.pack_root}", id="auth-identity", markup=False)
        yield Static("Setup unattended authentication", id="auth-heading")
        with VerticalScroll(id="auth-content"):
            yield Static("", id="auth-source", markup=False)
            yield Static("Create a Keeper/1Password login with password and optional TOTP.\n"
                         "Enter its exact vault and login/item names here.\n"
                         "Multiple accounts per role; matching indexes pair needed roles.",
                         id="auth-profile-help", markup=False)
            yield Static("", id="auth-workflow", markup=False)
            yield DataTable(id="auth-table", cursor_type="row")
            yield Static("Select a role or account. Empty role arrays use attended login.", markup=False)
            yield Static("", id="auth-form-title", markup=False)
            yield Static("Password manager", classes="auth-form-field")
            yield Select([("Keeper", "keeper"), ("1Password", "1password")], value="keeper", id="auth-manager")
            yield Static("CLI executable path", classes="auth-form-field")
            yield Input(placeholder="CLI executable path", id="auth-path")
            yield Static("Vault", classes="auth-form-field")
            yield Input(placeholder="Vault", id="auth-vault")
            yield Static("Item / user name", classes="auth-form-field")
            yield Input(placeholder="Item / user name", id="auth-user")
            with Horizontal(id="auth-form-actions"):
                yield Button("Save account", id="auth-save")
                yield Button("Cancel", id="auth-cancel")
            yield Static("", id="auth-error", markup=False)
            yield Static("", id="auth-targets", markup=False)
        yield ButtonFooter()

    def on_mount(self) -> None:
        self.query_one("#auth-table", DataTable).add_columns("Role", "Index", "Manager", "CLI path", "Vault", "User")
        self._show_form(False)
        self._load()
        self._refresh()
        self.query_one("#auth-table", DataTable).focus()

    def _workflow_text(self) -> str:
        steps = ["Select a role", "a Add account", "Pick password manager", "Set CLI path", "Enter vault",
                 "Enter item / user", "Enter to save", "Repeat for more accounts", "c Close setup"]
        if len(self.app.session.target_paths) > 1:
            steps.insert(0, "s Choose .env target")
        numbered = [f"{index}. {step}" for index, step in enumerate(steps, 1)]
        left, right = numbered[:5], numbered[5:]
        column_width = max(map(len, left)) + 4
        if self.size.width - 4 < column_width + max(map(len, right)):
            return "\n".join(numbered)
        return "\n".join(f"{line:<{column_width}}{right[index] if index < len(right) else ''}".rstrip()
                         for index, line in enumerate(left))

    def on_resize(self) -> None:
        self.query_one("#auth-workflow", Static).update(self._workflow_text())

    def _show_form(self, show: bool) -> None:
        for widget in self.query("#auth-form-title, #auth-manager, #auth-path, #auth-vault, #auth-user, #auth-form-actions, .auth-form-field"):
            widget.display = show
        self.query_one(ButtonFooter).refresh(recompose=True)
        self.refresh_bindings()

    def _load(self) -> None:
        try:
            resolved = self.app.session.result(_review=True).fields.get("UNATTENDED_AUTH")
            value = resolved.value if resolved else None
            if value is None:
                return
            if not isinstance(value, Mapping):
                raise ValueError("UNATTENDED_AUTH must be a JSON object of role arrays.")
            self.other_roles = {role: entries for role, entries in value.items() if role not in self.roles}
            profiles = parse_profiles({role: value[role] for role in self.roles if role in value}, self.roles)
            self.entries = {role: [asdict(source) for source in profiles.for_role(role)] for role in self.roles}
        except (ValueError, TypeError) as exc:
            # Parser diagnostics contain role/index only; never echo malformed input.
            self.load_error = f"Existing UNATTENDED_AUTH is invalid: {exc} Clear it or fix its source before editing."

    def _source_text(self) -> str:
        field = self.app.session.result(_review=True).fields.get("UNATTENDED_AUTH")
        source = field.source if field else "absent"
        message = f"Effective source: {source}. Saved descriptors contain no passwords or codes."
        if source in {"environment", "environ", "cli"}:
            message += " This external value overrides saved .env settings until removed from that source."
        return message

    def _refresh(self) -> None:
        self.query_one("#auth-workflow", Static).update(self._workflow_text())
        table = self.query_one("#auth-table", DataTable)
        table.clear()
        for role in self.roles:
            sources = self.entries[role]
            if not sources:
                table.add_row(*(literal_cell(x) for x in (role, "—", "attended", "", "", "")), key=f"{self.roles.index(role)}:-")
            for index, source in enumerate(sources):
                table.add_row(*(literal_cell(x) for x in (role, str(index), source["manager"], source["cli_path"], source["vault"], source["user"])), key=f"{self.roles.index(role)}:{index}")
        self.query_one("#auth-source", Static).update(self._source_text())
        self.query_one("#auth-targets", Static).update(self.app.target_summary())
        self.query_one("#auth-error", Static).update(self.load_error)

    def _selected(self) -> tuple[str, int | None] | None:
        table = self.query_one("#auth-table", DataTable)
        if table.row_count == 0:
            return None
        key = str(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)
        role_index, entry_index = key.split(":")
        return self.roles[int(role_index)], None if entry_index == "-" else int(entry_index)

    def _error(self, message: str) -> None:
        self.query_one("#auth-error", Static).update(message)

    def check_action(self, action, parameters):
        if self.app.screen is not self:
            return False
        if action == "switch_target":
            return len(self.app.session.target_paths) > 1 and self.editing is None
        if action == "clear_config":
            return self.editing is None
        if action in {"add_entry", "edit_entry", "remove_entry", "attended_role"}:
            return self.editing is None and not self.load_error
        return True

    def action_add_entry(self) -> None:
        if self.editing is not None:
            return
        selected = self._selected()
        if selected is None or self.load_error:
            self._error(self.load_error or "Choose a role first.")
            return
        role, _ = selected
        self.editing = role, None
        self.query_one("#auth-form-title", Static).update(f"Add account for {role}")
        self.query_one("#auth-manager", Select).value = "keeper"
        self.query_one("#auth-path", Input).value = "keeper"
        self.query_one("#auth-vault", Input).value = ""
        self.query_one("#auth-user", Input).value = role
        self._show_form(True)
        self.query_one("#auth-path", Input).focus()

    def action_edit_entry(self) -> None:
        if self.editing is not None:
            return
        selected = self._selected()
        if selected is None or selected[1] is None or self.load_error:
            self._error("Select an account to edit.")
            return
        role, index = selected
        source = self.entries[role][index]
        self.editing = selected
        self.query_one("#auth-form-title", Static).update(f"Edit {role} account {index}")
        self.query_one("#auth-manager", Select).value = source["manager"]
        for key, selector in (("cli_path", "#auth-path"), ("vault", "#auth-vault"), ("user", "#auth-user")):
            self.query_one(selector, Input).value = source[key]
        self._show_form(True)
        self.query_one("#auth-path", Input).focus()

    def action_cancel_form(self) -> None:
        if self.editing is not None:
            self.editing = None
            self._show_form(False)
            self.query_one("#auth-table", DataTable).focus()

    def _persist(self, entries: dict[str, list[dict[str, str]]]) -> bool:
        try:
            parse_profiles(entries, self.roles)
            encoded = json.dumps({**self.other_roles, **entries}, separators=(",", ":"), ensure_ascii=False)
            self.app.session.set_value("UNATTENDED_AUTH", encoded, persist=True)
        except Exception as exc:
            self._error(f"Unable to save credential descriptors: {exc}")
            return False
        self.entries = entries
        self.changed = True
        self.load_error = ""
        self._refresh()
        return True

    def save_entry(self) -> None:
        if self.editing is None:
            return
        role, index = self.editing
        manager = self.query_one("#auth-manager", Select).value
        source = {"manager": manager, "cli_path": self.query_one("#auth-path", Input).value,
                  "vault": self.query_one("#auth-vault", Input).value,
                  "user": self.query_one("#auth-user", Input).value}
        entries = {label: [dict(item) for item in values] for label, values in self.entries.items()}
        if index is None:
            entries[role].append(source)
        else:
            entries[role][index] = source
        if self._persist(entries):
            self.action_cancel_form()

    def action_remove_entry(self) -> None:
        if self.editing is not None:
            return
        selected = self._selected()
        if selected is None or selected[1] is None or self.load_error:
            self._error("Select an account to remove.")
            return
        role, index = selected
        entries = {label: [dict(item) for item in values] for label, values in self.entries.items()}
        entries[role].pop(index)
        self._persist(entries)

    def action_attended_role(self) -> None:
        if self.editing is not None or self.load_error:
            return
        selected = self._selected()
        if selected is None:
            self._error("Choose a role first.")
            return
        role, _ = selected
        if self.entries[role]:
            self.app.push_screen(QuitDialog(f"Remove all saved accounts for {role} and use attended login? Enter literal y to confirm; every other response cancels."),
                                 lambda yes: self._set_attended(role) if yes else None)
        else:
            self._set_attended(role)

    def _set_attended(self, role: str) -> None:
        entries = {label: [dict(item) for item in values] for label, values in self.entries.items()}
        entries[role] = []
        self._persist(entries)

    def action_clear_config(self) -> None:
        if self.editing is not None:
            return
        self.app.push_screen(QuitDialog("Clear unattended authentication from the selected .env? Enter literal y to confirm; every other response cancels."), self._clear_if_confirmed)

    def _clear_if_confirmed(self, yes: bool) -> None:
        if not yes:
            return
        try:
            prior = self.app.session.result(_review=True).fields.get("UNATTENDED_AUTH")
            prior_source = prior.source if prior else "absent"
            other_saved = any("UNATTENDED_AUTH" in DotenvEditor(path).keys()
                              for path in self.app.session.target_paths if path != self.app.session.dotenv_path)
            self.app.session.clear_auth_config()
        except Exception as exc:
            self._error(f"Unable to clear unattended authentication: {exc}")
            return
        self.entries = {role: [] for role in self.roles}
        self.other_roles = {}
        self.changed = True
        self.load_error = ""
        self._refresh()
        message = "Unattended authentication cleared for this invocation; attended login will be used."
        if prior_source in {"environment", "environ", "cli"}:
            message += f" The {prior_source} override remains externally supplied for future launches."
        if other_saved:
            message += " Another .env target still has UNATTENDED_AUTH for future launches."
        self._error(message)

    def action_switch_target(self) -> None:
        if self.editing is not None:
            return
        self.app.session.switch_target()
        self._refresh()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "auth-manager" or self.editing is None:
            return
        path = self.query_one("#auth-path", Input)
        if path.value in {"keeper", "op"}:
            path.value = "op" if event.value == "1password" else "keeper"

    def action_close_view(self) -> None:
        if self.editing is not None:
            self.action_cancel_form()
        else:
            self.dismiss(self.changed)

    def action_quit_request(self) -> None:
        self.app.push_screen(QuitDialog(), lambda yes: self.app.confirm_quit() if yes else None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {"auth-save": self.save_entry, "auth-cancel": self.action_cancel_form}
        action = actions.get(event.button.id)
        if action:
            action()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.editing is not None:
            self.save_entry()
