"""Interactive account descriptors and shared password-manager settings."""
from __future__ import annotations

from collections.abc import Mapping
import json
from rich.text import Text
from api_agent.auth.profiles import _canonical_shape, normalize_auth, DEFAULT_PASSWORD_MANAGER
from api_agent.config.dotenv import read_dotenv
from api_agent.tui.app import ButtonFooter, FocusNavigation, QuitDialog, SelectedTextCopy
from api_agent.tui.select import AdvancingSelect
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Input, Select, Static


def replace_auth_rows(table, headings, rows):
    """Size literal cells before drawing, avoiding deferred-width stale rows."""
    rows = [(key, [Text(str(value)) for value in values]) for key, values in rows]
    table.clear(columns=True)
    for index, heading in enumerate(headings):
        table.add_column(heading, width=max([Text(heading).cell_len,
                                            *(cells[index].cell_len for _, cells in rows)]))
    for key, cells in rows:
        table.add_row(*cells, key=key)


def _manager_draft(value):
    """Extract a display-only manager draft without resolving implementations."""
    if not isinstance(value, dict):
        return None
    if _canonical_shape(value):
        saved = value.get('password_manager')
        if (set(value) <= {'password_manager', 'roles'} and
                isinstance(saved, dict) and set(saved) == {'manager', 'cli_path'} and
                all(isinstance(saved[key], str) for key in ('manager', 'cli_path'))):
            return dict(saved)
        return None
    found = None
    for entries in value.values():
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if (not isinstance(entry, dict) or
                    set(entry) != {'manager', 'cli_path', 'vault', 'user'} or
                    any(not isinstance(entry[key], str) for key in entry) or
                    any(not entry[key].strip() for key in ('vault', 'user'))):
                return None
            candidate = {'manager': entry['manager'], 'cli_path': entry['cli_path']}
            if found is not None and candidate != found:
                return None
            found = candidate
    return found


class AuthTable(DataTable):
    """Continue row navigation between the account and manager tables."""

    def action_cursor_down(self):
        if (self.id == 'auth-table' and self.row_count
                and self.cursor_row == self.row_count - 1 and self.screen.editing is None):
            self.screen.query_one('#auth-manager-table', DataTable).focus()
        else:
            super().action_cursor_down()

    def action_cursor_up(self):
        if self.id == 'auth-manager-table' and self.screen.editing is None:
            users = self.screen.query_one('#auth-table', DataTable)
            users.move_cursor(row=max(0, users.row_count - 1))
            users.focus()
        else:
            super().action_cursor_up()


class AuthInput(Input):
    """Keep masked replies private even when a screen shortcut is unavailable."""

    def action_copy(self):
        if not self.password:
            super().action_copy()


class ExecutableInput(AuthInput):
    """Preserve deliberate same-text replacements as explicit edits."""

    def __init__(self, value, *, on_edit, id):
        self._on_edit = on_edit
        super().__init__(value=value, id=id)

    def replace(self, text, start, end):
        previous = self.value
        super().replace(text, start, end)
        if text and self.value == previous:
            self._on_edit(self.value)


class AuthEditScreen(FocusNavigation, SelectedTextCopy, Screen[bool]):
    """Edit account descriptors on a separate page without resizing the list."""

    DEFAULT_CSS = """
    AuthEditScreen #auth-edit-identity { height: auto; color: $text-muted; margin-bottom: 1; }
    AuthEditScreen #auth-edit-content { height: 1fr; }
    AuthEditScreen #auth-edit-content Static { height: auto; }
    AuthEditScreen #auth-form-title { text-style: bold; margin-bottom: 1; }
    AuthEditScreen .auth-next-field { margin-top: 1; }
    AuthEditScreen #auth-targets { margin-top: 1; }
    AuthEditScreen #auth-error { color: $error; }
    AuthEditScreen #auth-form-actions { height: 4; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 4; }
    AuthEditScreen #auth-form-actions Button { width: 100%; }
    """
    BINDINGS = [Binding('escape', 'cancel', 'Cancel'),
                Binding('s', 'switch_target', 'Switch .env'),
                Binding('c', 'cancel', 'Cancel'), Binding('q', 'quit_request', 'Quit'),
                *SelectedTextCopy.COPY_BINDINGS]

    def __init__(self, owner, title, values):
        super().__init__()
        self.owner, self.title, self.values = owner, title, dict(values)
        self.manager_form = owner.editing == 'manager'
        if self.manager_form:
            available = {identifier for _, identifier in owner.app.session.password_manager_registry.choices}
            if self.values['manager'] not in available:
                # A removed pack choice cannot be represented by the picker.
                # Require an explicit new selection while retaining the CLI draft.
                self.values['manager'] = ''
            self._draft_manager = self.values['manager']
            self._cli_draft_value = self.values['cli_path']
            definition = owner.app.session.password_manager_registry.get(self._draft_manager) if self._draft_manager else None
            self._cli_explicit = bool(self._cli_draft_value) and (
                definition is None or self._cli_draft_value != definition.cli_default)

    def _selected_definition(self, identifier):
        if not identifier:
            return None
        return self.app.session.password_manager_registry.get(identifier)

    def _implementation_text(self, identifier):
        definition = self._selected_definition(identifier)
        return str(definition.implementation) if definition else '-'

    def compose(self) -> ComposeResult:
        identity = getattr(self.app, 'identity_text', None)
        yield Static(identity() if identity else f"Base: {self.app.context.base_root}\nPack: {self.app.context.pack_root}",
                     id='auth-edit-identity', markup=False)
        with VerticalScroll(id='auth-edit-content'):
            yield Static(self.title, id='auth-form-title', markup=False)
            if self.manager_form:
                yield Static('Password manager', id='auth-first-label')
                yield AdvancingSelect([('-', ''), *self.app.session.password_manager_registry.choices],
                             value=self.values['manager'], allow_blank=False, id='auth-manager')
                yield Static('Password manager implementation path', classes='auth-next-field')
                yield Static(self._implementation_text(self.values['manager']), id='auth-implementation', markup=False)
                yield Static('CLI executable path', classes='auth-next-field')
                yield ExecutableInput(value=self.values['cli_path'], on_edit=self._record_cli_edit, id='auth-path')
            else:
                yield Static('Vault name', id='auth-first-label')
                yield AuthInput(value=self.values['vault'], id='auth-vault')
                yield Static('Login / item name', id='auth-login-label', classes='auth-next-field')
                yield AuthInput(value=self.values['user'], id='auth-user')
            yield Static('', id='auth-error', markup=False)
            yield Static(self.owner.target_summary(), id='auth-targets', markup=False)
        with Horizontal(id='auth-form-actions') as actions:
            if len(self.owner._target_paths()) > 1:
                actions.styles.grid_size_columns = 3
                yield Button('Switch .env (s)', id='auth-switch-target')
            yield Button('Save value (Enter)', id='auth-save')
            yield Button('Cancel', id='auth-cancel')
        yield ButtonFooter()

    def on_mount(self):
        self.query_one('#auth-edit-content', VerticalScroll).can_focus = False
        self.query_one('#auth-error').display = False
        if self.manager_form:
            self.watch(self.query_one('#auth-path', Input), 'value', self._remember_cli_edit, init=False)
        self.query_one('#auth-manager' if self.manager_form else '#auth-vault').focus()

    def save_entry(self):
        if self.manager_form:
            values = {'manager': self.query_one('#auth-manager', Select).value,
                      'cli_path': self.query_one('#auth-path', Input).value}
        else:
            values = {'vault': self.query_one('#auth-vault', Input).value,
                      'user': self.query_one('#auth-user', Input).value}
        try:
            self.owner._save_entry(values)
        except Exception as exc:
            error = self.query_one('#auth-error', Static)
            error.update(f'Unable to save: {exc}')
            error.display = True
        else:
            self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)

    def on_descendant_focus(self, event):
        self.refresh_bindings()

    def copy_target(self):
        selected, _ = super().copy_target()
        if selected is not None:
            return selected, True
        focused = self.app.focused
        if isinstance(focused, Input) and focused.screen is self and not focused.password:
            return focused.selected_text or focused.value, bool(focused.selected_text)
        return None, False

    async def action_copy_value(self):
        value, selected = self.copy_target()
        focused = self.app.focused
        input_selection = (focused if selected and self.get_selected_text() is None
                           and isinstance(focused, Input) and not focused.password else None)
        snapshot = (input_selection.value, input_selection.selection) if input_selection is not None else None
        readonly_snapshot = dict(self.selections)

        def copied():
            if input_selection is not None:
                if (input_selection.value, input_selection.selection) == snapshot:
                    input_selection.selection = type(input_selection.selection).cursor(input_selection.cursor_position)
            elif (selected and self.selections == readonly_snapshot
                  and self.get_selected_text() == value):
                self.clear_selection()

        await self._copy_with_feedback(value, copied)

    def check_action(self, action, parameters):
        if self.app.screen is not self:
            return False
        if action == 'switch_target':
            return len(self.owner._target_paths()) > 1
        return super().check_action(action, parameters)

    def action_switch_target(self):
        if self.check_action('switch_target', ()):
            self.owner.switch_env_target()
            self.query_one('#auth-targets', Static).update(self.owner.target_summary())

    def action_quit_request(self):
        self.app.push_screen(QuitDialog(), lambda yes: self.app.confirm_quit() if yes else None)

    def on_button_pressed(self, event):
        if event.button.id == 'auth-save':
            self.save_entry()
        elif event.button.id == 'auth-cancel':
            self.action_cancel()
        elif event.button.id == 'auth-switch-target':
            self.action_switch_target()

    def on_input_submitted(self, event):
        self.save_entry()

    def on_advancing_select_submitted(self, event):
        if self.manager_form:
            self.query_one('#auth-path', Input).focus()

    def _remember_cli_edit(self, value):
        if value != self._cli_draft_value:
            self._record_cli_edit(value)

    def _record_cli_edit(self, value):
        self._cli_explicit = bool(value)
        self._cli_draft_value = value

    def on_select_changed(self, event):
        if event.select.id == 'auth-manager':
            self.query_one('#auth-implementation', Static).update(self._implementation_text(event.value))
            path = self.query_one('#auth-path', Input)
            if event.value != self._draft_manager and not self._cli_explicit:
                definition = self._selected_definition(event.value)
                # Record generated values before the synchronous input watcher.
                self._cli_draft_value = definition.cli_default if definition else ''
                with path.prevent(Input.Changed):
                    path.value = self._cli_draft_value
            self._draft_manager = event.value


class AuthSetupScreen(FocusNavigation, Screen[bool]):
    DEFAULT_CSS = """
    AuthSetupScreen #auth-identity { height: auto; color: $text-muted; margin-bottom: 1; }
    AuthSetupScreen #auth-title-row { height: auto; margin-bottom: 1; }
    AuthSetupScreen #auth-heading { width: 1fr; height: auto; text-style: bold; margin-bottom: 0; }
    AuthSetupScreen #auth-storage { width: 2fr; height: auto; text-align: right; }
    AuthSetupScreen #auth-workflow { margin-bottom: 1; }
    AuthSetupScreen #auth-content { height: 1fr; }
    AuthSetupScreen #auth-content Static { height: auto; }
    AuthSetupScreen #auth-error { color: $error; }
    AuthSetupScreen #auth-targets { margin-top: 1; }
    """
    BINDINGS = [Binding("a", "add_entry", "Add"), Binding("e", "edit_entry", "Edit"),
                Binding("r", "remove_entry", "Remove"),
                Binding("[", "move_entry_up", "Move up"), Binding("]", "move_entry_down", "Move down"), Binding("s", "switch_target", "Switch .env"),
                Binding("c", "close_view", "Close"), Binding("q", "quit_request", "Quit")]

    def __init__(self, roles: tuple[str, ...], *, startup=False):
        super().__init__()
        self.startup = startup
        self._bindings.key_to_bindings["c"] = [Binding(
            "c", "close_view", "Close" if startup else "Close / menu")]
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
            yield Static("Create a 1Password/Keeper login with password and optional TOTP.\n"
                         "Duplicates of each role allowed; pair like users on same index to select them together.\n", id="auth-profile-help", markup=False)
            yield Static("", id="auth-workflow", markup=False)
            yield AuthTable(id="auth-table", cursor_type="row")
            yield AuthTable(id="auth-manager-table", cursor_type="row")
            yield Static("Select an account, or the password-manager row to edit its settings.", id="auth-selection-help", markup=False)
            yield Static("", id="auth-error", markup=False)
            yield Static("", id="auth-targets", markup=False)
        yield ButtonFooter()

    def on_mount(self):
        self.shared_target = self.app.session.dotenv_path
        self.query_one('#auth-content', VerticalScroll).can_focus = False
        manager_table = self.query_one('#auth-manager-table', DataTable)
        manager_table.styles.margin = (1, 0, 0, 0)
        self.query_one('#auth-table', DataTable).styles.margin = 0
        self._refresh()
        self.query_one('#auth-table', DataTable).focus()

    def _load(self):
        self.load_error = ''
        try:
            value = self.app.session.result(_review=True).values.get('UNATTENDED_AUTH') or {}
            normalized = normalize_auth(value, registry=self.app.session.password_manager_registry)
            self.manager = dict(normalized['password_manager'])
            for role in self.roles:
                entries = normalized['roles'].get(role, [])
                if not isinstance(entries, list) or any(not isinstance(item, Mapping) or set(item) != {'vault', 'user'}
                    or any(not isinstance(item[k], str) or not item[k].strip() for k in ('vault', 'user')) for item in entries):
                    raise ValueError(f"Invalid account entries for {role!r}.")
                self.entries[role] = [dict(item) for item in entries]
        except (TypeError, ValueError) as exc:
            self.load_error = f"Invalid unattended authentication configuration: {exc} Fix its source before editing accounts."
            if 'password manager is unavailable' in str(exc):
                # The resolved error marker deliberately hides old descriptors.
                # An environment or CLI override still carries its own draft
                # and must take precedence over a saved .env manager.
                candidates = [value]
                if isinstance(value, dict) and value.get('_auth_error') == 'manager_unavailable':
                    candidates = []
                    for path in reversed(self.app.session.target_paths):
                        try:
                            raw = read_dotenv(path).get('UNATTENDED_AUTH')
                            candidates.append(json.loads(raw) if raw is not None else None)
                        except (OSError, ValueError):
                            continue
                for candidate in candidates:
                    saved = _manager_draft(candidate)
                    if saved is not None:
                        self.manager = saved
                        break

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

    def target_summary(self):
        return 'Target .env files:\n' + '\n'.join(
            f"{'✓' if path == self.app.session.dotenv_path else ' '} {path}"
            for path in self._target_paths())

    def _refresh_target(self):
        paths = self._target_paths()
        self.app.session.switch_target(paths[0] if len(paths) == 1 else self.shared_target)
        self.query_one('#auth-targets', Static).update(self.target_summary())
        field = self.app.session.result(_review=True).fields.get('UNATTENDED_AUTH')
        source = field.source if field else 'absent'
        override = self.query_one('#auth-override', Static)
        override.display = source in {'environment', 'environ', 'cli'}
        override.update(f"The {source} value overrides saved .env settings until removed from that source." if override.display else '')
        self.query_one('#auth-workflow', Static).update(self._workflow_text())
        self.refresh_bindings()
        self.call_after_refresh(self._fit_tables)

    def _workflow_text(self):
        steps = [('s', 'Choose .env target'), (None, 'Select a role'), ('a', 'Add account'), (None, 'Enter vault name'),
                 (None, 'Enter login/item name'), ('Enter', 'Save'),
                 (None, 'Repeat for more accounts'), ('e', 'Edit password manager'), ('c', 'Close' if self.startup else 'Close / menu')]
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
        self._load()
        table = self.query_one('#auth-table', DataTable)
        manager = self.query_one('#auth-manager-table', DataTable)
        scroll = ((table.scroll_x, table.scroll_y), (manager.scroll_x, manager.scroll_y))
        sources = self.app.session.auth_sources()
        selected = self.editing if isinstance(self.editing, tuple) else self._selected()
        rows = []
        cursor = 0
        for role in self.roles:
            entries = self.entries[role]
            source = sources['roles'].get(role, 'Not provided')
            if not entries:
                rows.append((f'{self.roles.index(role)}:-', (role, '—', '', 'Not configured', source)))
                if selected and selected[0] == role:
                    cursor = len(rows) - 1
            for index, item in enumerate(entries):
                rows.append((f'{self.roles.index(role)}:{index}',
                             (role, str(index), item['vault'], item['user'], source)))
                if selected and selected[0] == role and index == min(selected[1] if selected[1] is not None else len(entries) - 1, len(entries) - 1):
                    cursor = len(rows) - 1
        replace_auth_rows(table, ('Role', 'Index', 'Vault', 'Login / item', 'Source'), rows)
        table.move_cursor(row=cursor, scroll=False)
        replace_auth_rows(manager, ('Password manager', 'CLI path', 'Source'), [
            ('manager', (self.manager['manager'] or '-', self.manager['cli_path'] or '-', sources['password_manager']))])
        self.call_after_refresh(lambda: self._restore_table_scroll(scroll))
        self._error(self.load_error)
        self._refresh_target()

    def _restore_table_scroll(self, scroll):
        for table, (x, y) in zip((self.query_one('#auth-table', DataTable),
                                  self.query_one('#auth-manager-table', DataTable)), scroll):
            table.scroll_to(x=min(x, table.max_scroll_x), y=min(y, table.max_scroll_y),
                            animate=False, force=True)

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

    def on_data_table_row_selected(self, event):
        if self.app.screen is self:
            event.data_table.focus()
            self.action_edit_entry()

    def check_action(self, action, parameters):
        if self.app.screen is not self:
            return False
        if action == 'switch_target':
            return self.editing is None
        if action == 'add_entry':
            return self.editing is None and not self.load_error and not self._manager_selected()
        if action == 'edit_entry':
            selected = self._selected()
            return self.editing is None and (self._manager_selected() or not self.load_error and selected is not None)
        if action in {'move_entry_up', 'move_entry_down'}:
            selected = self._selected()
            return (self.editing is None and not self.load_error and not self._manager_selected()
                    and selected is not None and selected[1] is not None
                    and len(self.entries[selected[0]]) > 1)
        if action == 'remove_entry':
            selected = self._selected()
            return self.editing is None and (self._manager_selected() or not self.load_error and selected is not None and selected[1] is not None)
        return True

    def _open_editor(self, editing, title, values):
        self.editing = editing
        self._refresh_target()
        self.app.push_screen(AuthEditScreen(self, title, values), self._edit_closed)

    def action_add_entry(self):
        if not self.check_action('add_entry', ()):
            return
        role, _ = self._selected()
        self._open_editor((role, None), f'Add account for {role}', {'vault': '', 'user': role})

    def action_edit_entry(self):
        if not self.check_action('edit_entry', ()):
            return
        if self._manager_selected():
            self._open_editor('manager', 'Edit shared password manager', self.manager)
        else:
            role, index = self._selected()
            values = self.entries[role][index] if index is not None else {'vault': '', 'user': role}
            self._open_editor((role, index), f'Edit account for {role}', values)

    def _save_entry(self, values):
        if self.editing == 'manager':
            self.app.session.save_auth_manager(values)
        else:
            role, index = self.editing
            entries = [dict(value) for value in self.entries[role]]
            if index is None:
                entries.append(values)
            else:
                entries[index] = values
            self.app.session.save_auth_role(role, entries)
            self.entries[role] = entries
        self.changed = True

    def _edit_closed(self, saved):
        target = '#auth-manager-table' if self.editing == 'manager' else '#auth-table'
        if saved:
            self._refresh()
        self.editing = None
        self.query_one(target, DataTable).focus(scroll_visible=False)
        self._refresh_target()

    def _move_entry(self, step):
        action = 'move_entry_up' if step < 0 else 'move_entry_down'
        if not self.check_action(action, ()):
            return
        role, index = self._selected()
        destination = index + step
        if not 0 <= destination < len(self.entries[role]):
            return
        entries = [dict(item) for item in self.entries[role]]
        moved = entries[index]
        occurrence = sum(item == moved for item in entries[:index])
        entries[index], entries[destination] = entries[destination], entries[index]
        try:
            self.app.session.save_auth_role(role, entries)
        except Exception as exc:
            self._error(f'Unable to move account: {exc}')
            return
        self.entries[role] = entries
        self.changed = True
        self._refresh()
        table = self.query_one('#auth-table', DataTable)
        matches = [position for position, item in enumerate(self.entries[role]) if item == moved]
        source = self.app.session.auth_sources()['roles'].get(role)
        # Identical neighbors still move by index when saved ordering is effective.
        if source not in {'environment', 'environ', 'cli', 'interactive'} and self.entries[role] == entries:
            selected_index = destination
        else:
            selected_index = matches[occurrence] if occurrence < len(matches) else min(destination, len(self.entries[role]) - 1)
        row = sum(max(1, len(self.entries[earlier])) for earlier in self.roles[:self.roles.index(role)]) + selected_index
        table.move_cursor(row=row)
        table.focus()

    def action_move_entry_up(self):
        self._move_entry(-1)

    def action_move_entry_down(self):
        self._move_entry(1)

    def action_remove_entry(self):
        if not self.check_action('remove_entry', ()):
            return
        try:
            if self._manager_selected():
                self.app.session.reset_auth_manager()
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

    def action_switch_target(self):
        if not self.check_action('switch_target', ()):
            return
        self.switch_env_target()
        self._refresh_target()

    def switch_env_target(self):
        paths = self._target_paths()
        if len(paths) == 1:
            self.app.session.switch_target(paths[0])
        else:
            self.app.session.switch_target()
            self.shared_target = self.app.session.dotenv_path

    def action_close_view(self):
        self.dismiss(self.changed)

    def action_quit_request(self):
        self.app.push_screen(QuitDialog(), lambda yes: self.app.confirm_quit() if yes else None)
