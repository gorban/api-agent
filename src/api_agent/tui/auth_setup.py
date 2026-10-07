"""Interactive account descriptors and shared password-manager settings."""
from __future__ import annotations

from collections.abc import Mapping
from rich.text import Text
from api_agent.auth.profiles import normalize_auth, DEFAULT_PASSWORD_MANAGER
from api_agent.tui.app import ButtonFooter, FocusNavigation, QuitDialog
from api_agent.tui.test_results import literal_cell
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Select, Static


class AuthSetupScreen(FocusNavigation, Screen[bool]):
    DEFAULT_CSS = """
    AuthSetupScreen #auth-identity { height: auto; color: $text-muted; margin-bottom: 1; }
    AuthSetupScreen #auth-title-row { height: auto; margin-bottom: 1; }
    AuthSetupScreen #auth-heading { width: 1fr; height: auto; text-style: bold; margin-bottom: 0; }
    AuthSetupScreen #auth-storage { width: 2fr; height: auto; text-align: right; }
    AuthSetupScreen #auth-workflow { margin-bottom: 1; }
    AuthSetupScreen #auth-content { height: 1fr; }
    AuthSetupScreen #auth-content Static { height: auto; }
    AuthSetupScreen #auth-form-actions { height: auto; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 4; }
    AuthSetupScreen #auth-form-actions Button { width: 100%; }
        AuthSetupScreen #auth-error { color: $error; }
    AuthSetupScreen #auth-targets { margin-top: 1; }
    """
    BINDINGS = [Binding("a", "add_entry", "Add"), Binding("e", "edit_entry", "Edit"),
                Binding("r", "remove_entry", "Remove"), Binding("s", "switch_target", "Switch .env"),
                Binding("c", "close_view", "Close"), Binding("q", "quit_request", "Quit"),
                Binding("escape", "cancel_form", "Back")]

    def __init__(self, roles: tuple[str, ...]):
        super().__init__()
        self.roles = tuple(roles)
        self.entries = {role: [] for role in self.roles}
        self.manager = dict(DEFAULT_PASSWORD_MANAGER)
        self.editing = None
        self.changed = False
        self.load_error = ""
        self.shared_target = None

    def compose(self) -> ComposeResult:
        identity = getattr(self.app, "identity_text", None)
        yield Static(identity() if identity else f"Base: {self.app.context.base_root}\nPack: {self.app.context.pack_root}", id="auth-identity", markup=False)
        with Horizontal(id="auth-title-row"):
            yield Static("Setup unattended authentication", id="auth-heading", markup=False)
            yield Static("* Only a pointer to your password-manager entry is saved.", id="auth-storage", markup=False)
        with VerticalScroll(id="auth-content"):
            yield Static("", id="auth-override", markup=False)
            yield Static("Create a Keeper/1Password login with password and optional TOTP.\n"
                         "Enter its exact vault and login/item names here.\n"
                         "Duplicates of each role allowed; pair like users on same index to select them together.", id="auth-profile-help", markup=False)
            yield Static("", id="auth-workflow", markup=False)
            yield DataTable(id="auth-table", cursor_type="row")
            yield DataTable(id="auth-manager-table", cursor_type="row")
            yield Static("Select an account, or the password-manager row to edit its settings.", id="auth-selection-help", markup=False)
            yield Static("", id="auth-form-title", markup=False)
            yield Static("Password manager", classes="auth-manager-field")
            yield Select([("Keeper", "keeper"), ("1Password", "1password")], value="keeper", id="auth-manager")
            yield Static("CLI executable path", classes="auth-manager-field")
            yield Input(id="auth-path")
            yield Static("Vault", classes="auth-user-field")
            yield Input(id="auth-vault")
            yield Static("Login / item name", classes="auth-user-field")
            yield Input(id="auth-user")
            with Horizontal(id="auth-form-actions"):
                yield Button("Save", id="auth-save")
                yield Button("Cancel", id="auth-cancel")
            yield Static("", id="auth-error", markup=False)
            yield Static("", id="auth-targets", markup=False)
        yield ButtonFooter()

    def on_mount(self):
        self.shared_target = self.app.session.dotenv_path
        self.query_one('#auth-table', DataTable).add_columns('Role', 'Index', 'Vault', 'Login / item')
        manager_table = self.query_one('#auth-manager-table', DataTable)
        manager_table.add_columns('Password manager', 'CLI path')
        manager_table.styles.margin = (1, 0, 0, 0)
        self.query_one('#auth-table', DataTable).styles.margin = 0
        self._show_form(False)
        self._load()
        self._refresh()
        self.query_one('#auth-table', DataTable).focus()

    def _load(self):
        self.load_error = ''
        try:
            value = self.app.session.result(_review=True).values.get('UNATTENDED_AUTH') or {}
            normalized = normalize_auth(value)
            self.manager = dict(normalized['password_manager'])
            for role in self.roles:
                entries = normalized['roles'].get(role, [])
                if not isinstance(entries, list) or any(not isinstance(item, Mapping) or set(item) != {'vault', 'user'}
                    or any(not isinstance(item[k], str) or not item[k].strip() for k in ('vault', 'user')) for item in entries):
                    raise ValueError(f"Invalid account entries for {role!r}.")
                self.entries[role] = [dict(item) for item in entries]
        except (TypeError, ValueError) as exc:
            self.load_error = f"Invalid unattended authentication configuration: {exc} Fix its source before editing accounts."

    def _manager_selected(self):
        return self.editing == 'manager' or self.editing is None and self.app.focused is self.query_one('#auth-manager-table')

    def _selected(self):
        table = self.query_one('#auth-table', DataTable)
        if not table.row_count:
            return None
        key = str(table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value)
        role, index = key.split(':')
        return self.roles[int(role)], None if index == '-' else int(index)

    def _active_role(self):
        if self._manager_selected():
            return 'LLM User'
        selected = self.editing if isinstance(self.editing, tuple) else self._selected()
        return selected[0] if selected else self.roles[0]

    def _target_paths(self):
        if self._active_role() != 'LLM User' and self.app.context.external_pack:
            return (self.app.context.pack_root / '.env',)
        return self.app.session.target_paths

    def _refresh_target(self):
        paths = self._target_paths()
        self.app.session.switch_target(paths[0] if len(paths) == 1 else self.shared_target)
        self.query_one('#auth-targets', Static).update('Target .env files:\n' + '\n'.join(
            f"{'✓' if path == self.app.session.dotenv_path else ' '} {path}" for path in paths))
        field = self.app.session.result(_review=True).fields.get('UNATTENDED_AUTH')
        source = field.source if field else 'absent'
        override = self.query_one('#auth-override', Static)
        override.display = source in {'environment', 'environ', 'cli'}
        override.update(f"The {source} value overrides saved .env settings until removed from that source." if override.display else '')
        self.query_one('#auth-workflow', Static).update(self._workflow_text())
        self.refresh_bindings()
        self.call_after_refresh(self._fit_tables)

    def _workflow_text(self):
        steps = [(None, 'Select a role'), ('a', 'Add account'), (None, 'Enter vault'),
                 (None, 'Enter login/item name'), ('Enter', 'Save'),
                 (None, 'Repeat for more accounts'), ('e', 'Edit password manager'), ('c', 'Close setup')]
        if len(self._target_paths()) > 1:
            steps.insert(0, ('s', 'Choose .env target'))
        style = f"bold {self.app.get_css_variables()['footer-key-foreground']}"
        numbered = []
        for index, (key, label) in enumerate(steps, 1):
            line = Text(f'{index}. ')
            if key:
                line.append('(').append(key, style=style).append(') ')
            line.append(label)
            numbered.append(line)
        left, right = numbered[:5], numbered[5:]
        width = max(map(len, left)) + 4
        if self.size.width - 4 < width + max(map(len, right)):
            return Text('\n').join(numbered)
        rows = []
        for index, line in enumerate(left):
            row = line.copy()
            if index < len(right):
                row.append(' ' * (width - len(line))).append_text(right[index])
            rows.append(row)
        return Text('\n').join(rows)

    def _refresh(self):
        table = self.query_one('#auth-table', DataTable)
        selected = self.editing if isinstance(self.editing, tuple) else self._selected()
        table.clear()
        cursor = 0
        for role in self.roles:
            entries = self.entries[role]
            if not entries:
                table.add_row(*(literal_cell(x) for x in (role, '—', '', 'Not configured')), key=f'{self.roles.index(role)}:-')
                if selected and selected[0] == role:
                    cursor = table.row_count - 1
            for index, item in enumerate(entries):
                table.add_row(*(literal_cell(x) for x in (role, str(index), item['vault'], item['user'])), key=f'{self.roles.index(role)}:{index}')
                if selected and selected[0] == role and index == min(selected[1] if selected[1] is not None else len(entries) - 1, len(entries) - 1):
                    cursor = table.row_count - 1
        table.move_cursor(row=cursor)
        manager = self.query_one('#auth-manager-table', DataTable)
        manager.clear()
        manager.add_row(*(literal_cell(x) for x in (self.manager['manager'], self.manager['cli_path'])), key='manager')
        self._error(self.load_error)
        self._refresh_target()

    def _fit_tables(self):
        if not self.is_mounted:
            return
        viewport = self.query_one('#auth-content', VerticalScroll)
        table = self.query_one('#auth-table', DataTable)
        manager = self.query_one('#auth-manager-table', DataTable)
        manager_height = 2 + int(manager.virtual_size.width > manager.content_size.width)
        manager.styles.min_height = manager_height
        manager.styles.height = manager_height
        horizontal = int(table.virtual_size.width > table.content_size.width)
        minimum = 5 + horizontal
        table.styles.min_height = minimum
        others = sum(child.outer_size.height + child.styles.margin.top + child.styles.margin.bottom for child in viewport.children if child is not table and child.display)
        table.styles.height = min(max(minimum, table.row_count + 1 + horizontal), max(minimum, viewport.content_size.height - others))

    def on_resize(self):
        self.query_one('#auth-workflow', Static).update(self._workflow_text())
        self.call_after_refresh(self._fit_tables)

    def on_descendant_focus(self, event):
        if self.is_mounted and self.shared_target is not None and self.app.screen is self:
            self._refresh_target()

    def on_data_table_row_highlighted(self, event):
        if self.shared_target is not None and self.app.screen is self:
            self._refresh_target()

    def _show_form(self, show):
        manager = show and self.editing == 'manager'
        for widget in self.query('#auth-manager, #auth-path, .auth-manager-field'):
            widget.display = manager
        for widget in self.query('#auth-vault, #auth-user, .auth-user-field'):
            widget.display = show and not manager
        for widget in self.query('#auth-form-title, #auth-form-actions'):
            widget.display = show
        self.query_one(ButtonFooter).refresh(recompose=True)
        self.refresh_bindings()
        self.call_after_refresh(self._fit_tables)

    def check_action(self, action, parameters):
        if self.app.screen is not self:
            return False
        if action == 'switch_target':
            return self.editing is None and len(self._target_paths()) > 1
        if action == 'add_entry':
            return self.editing is None and not self.load_error and not self._manager_selected()
        if action == 'edit_entry':
            selected = self._selected()
            return self.editing is None and (self._manager_selected() or not self.load_error and selected is not None and selected[1] is not None)
        if action == 'remove_entry':
            selected = self._selected()
            return self.editing is None and (self._manager_selected() or not self.load_error and selected is not None and selected[1] is not None)
        return True

    def action_add_entry(self):
        if not self.check_action('add_entry', ()):
            return
        role, _ = self._selected()
        self.editing = role, None
        self.query_one('#auth-form-title', Static).update(f'Add account for {role}')
        self.query_one('#auth-vault', Input).value = ''
        self.query_one('#auth-user', Input).value = role
        self._show_form(True)
        self.query_one('#auth-vault', Input).focus()

    def action_edit_entry(self):
        if not self.check_action('edit_entry', ()):
            return
        if self._manager_selected():
            self.editing = 'manager'
            self.query_one('#auth-form-title', Static).update('Edit shared password manager')
            self.query_one('#auth-manager', Select).value = self.manager['manager']
            self.query_one('#auth-path', Input).value = self.manager['cli_path']
            self._show_form(True)
            self.query_one('#auth-path', Input).focus()
        else:
            role, index = self._selected()
            self.editing = role, index
            self.query_one('#auth-form-title', Static).update(f'Edit account for {role}')
            self.query_one('#auth-vault', Input).value = self.entries[role][index]['vault']
            self.query_one('#auth-user', Input).value = self.entries[role][index]['user']
            self._show_form(True)
            self.query_one('#auth-vault', Input).focus()

    def save_entry(self):
        if self.editing is None:
            return
        try:
            if self.editing == 'manager':
                manager = {'manager': self.query_one('#auth-manager', Select).value, 'cli_path': self.query_one('#auth-path', Input).value}
                self.app.session.save_auth_manager(manager)
                self._load()
            else:
                role, index = self.editing
                item = {'vault': self.query_one('#auth-vault', Input).value, 'user': self.query_one('#auth-user', Input).value}
                entries = [dict(value) for value in self.entries[role]]
                if index is None:
                    entries.append(item)
                else:
                    entries[index] = item
                self.app.session.save_auth_role(role, entries)
                self.entries[role] = entries
            self.changed = True
            self._refresh()
            self.action_cancel_form()
        except Exception as exc:
            self._error(f'Unable to save: {exc}')

    def action_remove_entry(self):
        if not self.check_action('remove_entry', ()):
            return
        try:
            if self._manager_selected():
                self.app.session.reset_auth_manager()
                self._load()
            else:
                role, index = self._selected()
                entries = [dict(item) for item in self.entries[role]]
                entries.pop(index)
                self.app.session.save_auth_role(role, entries)
                self.entries[role] = entries
            self.changed = True
            self._refresh()
        except Exception as exc:
            self._error(f'Unable to remove: {exc}')

    def _error(self, text):
        error = self.query_one('#auth-error', Static)
        error.update(text)
        error.display = bool(text)
        self.call_after_refresh(self._fit_tables)

    def action_cancel_form(self):
        if self.editing is not None:
            target = '#auth-manager-table' if self.editing == 'manager' else '#auth-table'
            self.editing = None
            self._show_form(False)
            self.query_one(target, DataTable).focus()
            self._refresh_target()

    def action_switch_target(self):
        if not self.check_action('switch_target', ()):
            return
        self.app.session.switch_target()
        self.shared_target = self.app.session.dotenv_path
        self._refresh_target()

    def on_select_changed(self, event):
        if event.select.id == 'auth-manager' and self.editing == 'manager':
            path = self.query_one('#auth-path', Input)
            if path.value in {'keeper', 'op'}:
                path.value = 'op' if event.value == '1password' else 'keeper'

    def action_close_view(self):
        if self.editing is not None:
            self.action_cancel_form()
        else:
            self.dismiss(self.changed)

    def action_quit_request(self):
        self.app.push_screen(QuitDialog(), lambda yes: self.app.confirm_quit() if yes else None)

    def on_button_pressed(self, event):
        if event.button.id == 'auth-save':
            self.save_entry()
        elif event.button.id == 'auth-cancel':
            self.action_cancel_form()

    def on_input_submitted(self, event):
        self.save_entry()
