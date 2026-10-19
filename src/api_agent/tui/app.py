"""Human-reviewable configuration UI; execution adapters are deliberately pending."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import sys
import threading
from rich.text import Text
from api_agent.config.declarations import UNSET
from api_agent.config.resolve import ConfigError, convert_value
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.tui.test_results import (literal_cell, progress_summary, ordered_result_rows,
                                          reconcile_results_table, styled_summary, component_timing, elapsed_time)
from api_agent.tui.select import AdvancingSelect
from api_agent.tui.test_table import TestTable
from api_agent.tui.search import GroupedUserFooter, SearchBar, SearchInput
from api_agent.tui.clipboard import ClipboardError, copy_text
from api_agent.tui.allure import AllureNavigation
from api_agent.tui.container_management import ContainerManagement
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen, ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Select, Static

MODES = (("run-local", "Run local"), ("unit", "Unit tests w/ coverage"),
         ("integration", "Integration tests"), ("e2e", "E2E tests"),
         ("allure", "Allure serve"), ("deployment-logs", "Deployment logs"),
         ("config-cleanup", "Clear .env configuration"),
         ("cleanup-containers", "Container management"),
         ("unattended-auth", "Setup unattended authentication"))
E2E_WARNING = ("Human acceptance is mandatory to continue with E2E tests. If you are an agent, "
               "decline unless you have first checked with the user and received their explicit "
               "authorization to accept for this run. The user may authorize an agent to provide this acceptance.")
NO_TEST_SELECTION_MESSAGE = "Select at least one test before continuing"


def display(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


class ButtonFooter(GroupedUserFooter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._composition_lock = asyncio.Lock()

    async def recompose(self):
        # Concurrent binding updates can otherwise mount two sets of children
        # after both removals finish, including the same tab hint identifier.
        async with self._composition_lock:
            await super().recompose()

    async def _compose(self):
        async with self._composition_lock:
            await super()._compose()

    def compose(self) -> ComposeResult:
        if self._pruning or not self.screen.is_active:
            return
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
        yield Static(self.message, id="quit-message", markup=False)
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


class SelectedTextCopy:
    """Shared platform shortcut and clipboard feedback for read-only selections."""
    COPY_BINDINGS = [Binding('ctrl+c', 'copy_value', 'Copy value', key_display='^c', priority=True),
                     Binding('super+c', 'copy_selected_text', 'Copy value', key_display='⌘c', priority=True)]

    async def _watch_selections(self, old_selections, selections):
        await super()._watch_selections(old_selections, selections)
        self.refresh_bindings()

    @property
    def active_bindings(self):
        bindings = super().active_bindings
        for key in ('ctrl+c', 'super+c'):
            if key in bindings and bindings[key].binding.action in {'copy_value', 'copy_selected_text'}:
                copy = bindings.pop(key)
                bindings[key] = copy
        return bindings

    def check_copy_action(self, action):
        return (self.app.screen is self
                and (sys.platform == 'darwin') == (action == 'copy_selected_text')
                and self.copy_target()[0] is not None)

    def check_action(self, action, parameters):
        if action in {'copy_value', 'copy_selected_text'}:
            return self.check_copy_action(action)
        return super().check_action(action, parameters)

    def copy_target(self):
        return self.get_selected_text(), True

    async def _copy_with_feedback(self, value, on_copied):
        if value is None:
            return
        try:
            native = await copy_text(self.app, value)
            on_copied()
            self.app.notify('Value copied.' if native else 'Value sent to terminal clipboard.', timeout=2)
        except ClipboardError:
            self.app.notify('Could not copy the value.', severity='error', timeout=2)

    async def action_copy_value(self):
        value, _ = self.copy_target()
        await self._copy_with_feedback(value, self.clear_selection)

    async def action_copy_selected_text(self):
        await self.action_copy_value()


class SettingHelpDialog(SelectedTextCopy, ModalScreen):
    BINDINGS = [Binding("escape,c", "close", "Close", key_display="Esc / c", priority=True),
                Binding("up,k", "scroll_description('up')", show=False),
                Binding("down,j", "scroll_description('down')", show=False),
                Binding("left,h", "scroll_description('left')", show=False),
                Binding("right,l", "scroll_description('right')", show=False),
                Binding("pageup", "scroll_description('page_up')", show=False),
                Binding("pagedown", "scroll_description('page_down')", show=False),
                *SelectedTextCopy.COPY_BINDINGS]

    def __init__(self, name, description):
        super().__init__()
        self.setting_name, self.description = name, description

    def compose(self) -> ComposeResult:
        with Vertical(id="setting-help-dialog"):
            yield Static(Text(self.setting_name, style="bold"), id="setting-help-title")
            with VerticalScroll(id="setting-help-scroll") as scroll:
                scroll.can_focus = False
                yield Static(self.description, id="setting-help-text", markup=False)
            yield Button("Close", id="setting-help-close")
        yield Footer()

    def on_mount(self):
        self.set_class(self.size.height < 18, "compact-setting-help")
        self.query_one("#setting-help-close").focus()

    def on_resize(self, event):
        self.set_class(event.size.height < 18, "compact-setting-help")

    def action_close(self):
        self.dismiss()

    def action_scroll_description(self, direction):
        getattr(self.query_one("#setting-help-scroll"), "scroll_" + direction)(animate=False)

    def on_button_pressed(self, event):
        self.action_close()


class WorkbenchScreen(FocusNavigation, SelectedTextCopy, Screen):
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
                Binding("pagedown", "failure_page_down", "Page down"),
                *SelectedTextCopy.COPY_BINDINGS]

    def on_screen_resume(self):
        if self.kind == 'test-results':
            self.call_after_refresh(self.app.update_test_results, self)
        pending, self._pending_cost_refresh = self._pending_cost_refresh, None
        if (pending is not None and pending is self.app.selection
                and self.kind == 'tests' and not self.app._inspection_shutdown):
            self.call_after_refresh(self.app.refresh_picker_costs, self, pending)

    def on_unmount(self):
        self._pending_cost_refresh = None

    @property
    def focus_chain(self):
        chain = super().focus_chain
        if self.kind == 'tests':
            # DOM order is the test list, action buttons, then the search field.
            # The native chain excludes the search field while it is hidden.
            return [widget for widget in chain if isinstance(widget, (TestTable, Button, SearchInput))]
        if self.kind == 'test-results':
            # Summary panes remain mouse-focusable for scrolling and selection,
            # but Tab only visits the actual test list.
            return [widget for widget in chain if isinstance(widget, DataTable)]
        return chain

    def __init__(self, kind, title, text="", actions=(), rows=(), *, selected_row=None):
        super().__init__()
        self.kind, self.heading, self.body = kind, title, text
        self.actions, self.rows = actions, rows
        self.selected_row = selected_row
        self.test_run_complete = False
        self.style_results = None
        self.config_saved = set()
        self.config_defaults = set()
        self.test_search = None
        self._pending_cost_refresh = None

    def compose(self) -> ComposeResult:
        app = self.app
        yield Static(app.identity_text(), id="identity", markup=False)
        with VerticalScroll(id="workbench-content") as viewport:
            # Scroll-only content should not add an empty stop beside real controls.
            viewport.can_focus = not (self.rows or self.actions or self.kind == "input")
            yield Static(self.heading, id="heading")
            if self.kind == "input":
                field = app.schema.fields[app.prompt_name]
                if field.help.strip():
                    yield Static(field.help, id="config-description", markup=False)
            if self.kind == "test-results":
                with VerticalScroll(id="unit-summary"):
                    with Horizontal(id="live-progress"):
                        yield Static(self.body, id="body", markup=False)
                        yield Static('', id='suite-elapsed', markup=False)
            else:
                yield Static(self.body, id="body", markup=False)
            if self.rows:
                table_type = TestTable if self.kind in {'tests', 'test-results'} else DataTable
                if self.kind == 'test-results':
                    yield table_type(id="choices", cursor_type="row", sticky_timing=True)
                else:
                    yield table_type(id="choices", cursor_type="row")
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
                if self.kind == 'tests' and app.mode == 'integration' and 'UNATTENDED_USER_INDEX' in app.schema.fields:
                    yield Static(app.account_summary(), id='account-selection', markup=False)
                yield Static(app.target_summary(), id="dotenv-targets", markup=False)
            with Horizontal(id="actions") as actions:
                if self.kind == "suggest":
                    actions.add_class("auth-suggestion")
                if self.kind == "test-results":
                    actions.display = False
                elif self.kind == "input":
                    actions.styles.dock = "bottom"
                if self.kind == "input" and len(app.session.target_paths) > 1:
                    yield Button("Switch target .env (s outside input)", id="switch-target")
                for key, label in self.actions:
                    if self.kind == "suggest" and key == "skip":
                        with Vertical(id="skip-choice"):
                            yield Button(label, id=key)
                            yield Checkbox("Remember my selection", id="remember-skip")
                    else:
                        yield Button(label, id=key)
            if self.kind not in {"test-results", "defaults"}:
                yield Static("", id="error", markup=False)
        if self.kind == "defaults":
            yield Static(app.configuration_review_blocker(), id="error", markup=False)
        if self.kind in {'test-results', 'test-failure'}:
            status = Static(app._allure_loading, id='allure-loading', markup=False)
            status.display = bool(app._allure_loading)
            yield status
        if self.kind == 'allure':
            with Vertical(id='allure-bottom-bar'):
                status = Static(app._allure_loading, id='allure-loading', markup=False)
                status.display = bool(app._allure_loading)
                yield status
                yield ButtonFooter()
        elif self.kind == "menu" and getattr(app.context, "show_pack_bootstrap", False):
            with Vertical(id="menu-bottom-bar"):
                yield Static("Bootstrap: --resource-pack-dir PATH or RESOURCE_PACK_DIR selects a resource pack.",
                             id="pack-bootstrap", markup=False)
                yield ButtonFooter()
        else:
            yield ButtonFooter()
        if self.kind == 'tests':
            search = SearchBar(marker='/', id='test-search-bar')
            search.display = False
            yield search

    def on_mount(self):
        if self.kind == "defaults":
            self.set_class(self.size.height < 18, "compact-review")
        if self.rows:
            table = self.query_one(DataTable)
            if self.kind == "defaults":
                table.add_columns("Setting", "Effective value", "Source")
            elif self.kind == "test-results":
                table.add_columns("Status", "Test", "Execution time")
                if self.app.mode in {'integration', 'e2e'} and self.app.unit_result is not None:
                    table.add_column('Est. $', width=9)
            elif self.kind == "containers":
                table.add_columns("Selected", "ID", "Name", "Image", "Status")
            elif self.kind == "cleanup":
                table.add_column("", width=1)
                table.add_columns("Setting", "Effective value", "Source")
            else:
                table.add_columns("Selected / present", "Setting / test", "Value / count", "Source")
                if self.kind == "tests" and self.app.cost_estimator is not None:
                    table.add_column("Est. $", width=11)
            for key, cells in self.rows:
                table.add_row(*(literal_cell(cell) for cell in cells), key=key)
            if self.kind == "defaults":
                self.app.refresh_configuration_rows(self)
            table.move_cursor(row=next((i for i, (key, _) in enumerate(self.rows)
                                        if key == self.selected_row), 0))
            if self.kind == "test-results":
                table.results_completed = self.test_run_complete
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
            self._bindings.bind('left_square_bracket', 'previous_account', 'Previous user', key_display='[')
            self._bindings.bind('right_square_bracket', 'next_account', 'Next user', key_display=']')
            self._bindings.bind('slash', 'search_forward', 'Search tests', key_display='/')
            self._bindings.bind('question_mark', 'search_backward', 'Search tests from bottom', key_display='?')
            self._bindings.bind('escape', 'exit_search', 'Exit search', show=False, priority=True)
            for binding in Input.BINDINGS:
                if binding.key.rsplit('+', 1)[-1] in {'left', 'right'}:
                    self._bindings.bind(binding.key, f'edit_search_query({binding.action!r})',
                                        show=False, priority=True)
        if self.kind == "test-results":
            self._bindings.key_to_bindings["a"] = [Binding("a", "view_allure", "View in Allure")]
            self.set_interval(.25, self.refresh_suite_timer)
            self.refresh_suite_timer()
            self.call_after_refresh(self.layout_test_results)
        if self.kind == 'test-failure':
            self._bindings.key_to_bindings['c'] = [Binding('c', 'close_view', 'Close')]
            self._bindings.key_to_bindings['a'] = [Binding('a', 'view_allure', 'View in Allure')]
        if self.kind == "defaults":
            self._bindings.key_to_bindings["c"] = [Binding(
                "c", "close_view", "Close" if self.app.startup_review or self.app.review_destination else "Close / menu")]
            self.call_after_refresh(self.query_one("#workbench-content", VerticalScroll).scroll_home,
                                    animate=False)
        self.refresh_bindings()

    def on_resize(self, event):
        if self.kind == "defaults":
            self.set_class(event.size.height < 18, "compact-review")
            self.body = self.app.configuration_review_text()
            self.query_one("#body", Static).update(self.body)
            self.call_after_refresh(self.query_one("#workbench-content", VerticalScroll).scroll_home,
                                    animate=False)
        if self.kind == "test-results":
            self.call_after_refresh(self.layout_test_results)

    def watch_selections(self):
        # Textual updates highlighted text without refreshing contextual bindings.
        self.refresh_bindings()

    def refresh_suite_timer(self):
        run = self.app.test_run
        timer = self.query_one('#suite-elapsed', Static)
        live = run is not None and run.active and self.app.unit_result is None
        timer.display = live
        if live:
            timer.update(elapsed_time(run.elapsed))

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

    async def action_view_allure(self):
        self.app.run_worker(self.app.open_selected_allure(), group="allure-viewer", exclusive=False)

    async def action_start_selected(self):
        await self.app.choose("continue")

    def action_previous_account(self):
        self.app.cycle_account(-1)

    def action_next_account(self):
        self.app.cycle_account(1)

    def action_search_forward(self):
        self.start_search(1)

    def action_search_backward(self):
        self.start_search(-1)

    async def action_edit_search_query(self, action):
        control = self.query_one(SearchInput)
        control.focus()
        await control.run_action(action)

    def start_search(self, direction):
        from api_agent.testing.search import SearchSelection
        self.test_search = SearchSelection(self.app.selection, direction)
        bar = self.query_one(SearchBar)
        bar.marker = '/' if direction > 0 else '?'
        bar.display = True
        self.query_one(ButtonFooter).display = False
        control = self.query_one(SearchInput)
        control.value = ''
        control.focus()
        self.render_search_rows(reset_cursor=True)
        self.refresh_bindings()

    def selection_rows(self):
        return self.test_search.rows() if self.test_search is not None else self.app.selection.groups()

    def toggle_test_rows(self, keys):
        if self.test_search is not None:
            self.test_search.toggle_many(keys)
            self.render_search_rows()
            self.app.refresh_cost_summary()
        else:
            self.app.selection.toggle_many(keys)
            self.app.refresh_rows()

    def render_search_rows(self, *, reset_cursor=False):
        table = self.query_one(TestTable)
        cursor = table.cursor_row
        previous = table.ordered_rows[cursor].key.value if table.row_count else None
        rows = self.selection_rows()
        stable_rows = not reset_cursor and [row.key.value for row in table.ordered_rows] == [row.id for row in rows]
        if stable_rows:
            for row in rows:
                cells = self.app.selection_cells(row)
                for column, value in zip(table.columns, cells):
                    table.update_cell(row.id, column, literal_cell(value))
        else:
            table.clear()
            for row in rows:
                table.add_row(*(literal_cell(cell) for cell in self.app.selection_cells(row)), key=row.id)
        if rows:
            matches = [index for index, row in enumerate(rows) if row.kind != 'other']
            if reset_cursor:
                index = (matches[0] if self.test_search.direction > 0 else matches[-1]) if matches else 0
            else:
                index = next((i for i, row in enumerate(rows) if row.id == previous), min(cursor, len(rows) - 1))
            table.move_cursor(row=index)
        self.query_one('#error', Static).update(self.test_search.error or ('' if rows else NO_TEST_SELECTION_MESSAGE))
        self.refresh_bindings()

    def on_input_changed(self, event):
        if isinstance(event.input, SearchInput) and self.test_search is not None:
            valid = self.test_search.set_query(event.value)
            self.render_search_rows(reset_cursor=valid)

    def on_search_input_navigate(self, event):
        table = self.query_one(TestTable)
        ranges = {'range_up': (-1, False), 'range_down': (1, False),
                  'range_top': (-1, True), 'range_bottom': (1, True)}
        if event.action in ranges:
            table.action_extend_range(*ranges[event.action])
        elif event.action in {'range_page_up', 'range_page_down'}:
            table.action_extend_page(-1 if event.action == 'range_page_up' else 1)
        else:
            table.action_move_plain({'up': 'cursor_up', 'down': 'cursor_down',
                                     'page_up': 'page_up', 'page_down': 'page_down'}[event.action])

    def on_search_input_toggle(self, event):
        self.toggle_test_rows(self.query_one(TestTable).selected_row_keys)

    def on_search_input_exit(self, event):
        self.action_exit_search()

    def on_key(self, event):
        if self.test_search is not None and isinstance(self.app.focused, TestTable):
            movement = {'j': 'down', 'k': 'up', 'J': 'range_down', 'K': 'range_up',
                        'shift+j': 'range_down', 'shift+k': 'range_up'}
            if event.key in movement:
                self.on_search_input_navigate(SearchInput.Navigate(movement[event.key]))
            elif event.is_printable and event.key != 'space':
                control = self.query_one(SearchInput)
                control.focus()
                control.replace(event.character, *control.selection)
            else:
                return super().on_key(event)
            event.stop()
            event.prevent_default()
            return
        super().on_key(event)

    def action_exit_search(self):
        table = self.query_one(TestTable)
        cursor = table.cursor_row
        selected = table.ordered_rows[cursor].key.value if table.row_count else None
        self.test_search = None
        self.query_one(SearchBar).display = False
        self.query_one(ButtonFooter).display = True
        table.clear()
        rows = self.selection_rows()
        for row in rows:
            table.add_row(*(literal_cell(cell) for cell in self.app.selection_cells(row)), key=row.id)
        table.move_cursor(row=next((i for i, row in enumerate(rows) if row.id == selected), min(cursor, len(rows) - 1)))
        self.query_one('#error', Static).update('')
        table.focus()
        self.refresh_bindings()

    def check_action(self, action, parameters):
        # Textual evaluates underlying screen bindings while a modal is open.
        # Their contextual actions must not inspect the modal as this screen.
        if self.app.screen is not self:
            return False
        if action == 'exit_search':
            return self.test_search is not None
        if action == 'edit_search_query':
            return self.test_search is not None and isinstance(self.app.focused, TestTable)
        if self.test_search is not None and action not in {'copy_value', 'copy_selected_text', 'toggle_selection'}:
            return (action == 'start_selected' and isinstance(self.app.focused, Button)
                    and self.app.focused.id == 'continue')
        if action in {'search_forward', 'search_backward'}:
            return self.kind == 'tests'
        if action == "toggle_selection":
            return isinstance(self.app.focused, DataTable)
        if action == "close_view" and self.kind == "menu":
            return False
        if action == "view_allure":
            return self.app.selected_allure_result(self) is not None
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
        if action in {'copy_value', 'copy_selected_text'}:
            return self.check_copy_action(action)
        if action == "delete_assignment":
            return self.kind == "cleanup" and self.app.focused_cleanup_present()
        if action == "switch_target":
            return (self.kind in {"defaults", "input"}
                    or self.kind == "unavailable" and self.app.mode in {None, "unattended-auth"}) and len(self.app.session.target_paths) > 1
        if action == "start_selected":
            return self.kind == "tests"
        if action in {'previous_account', 'next_account'}:
            return (self.kind == 'tests' and self.app.mode == 'integration'
                    and (bool(self.app.account_options())
                         or self.app.session.result().values.get('UNATTENDED_USER_INDEX') is not None))
        if action == "stop_selected":
            return self.kind == "containers"
        if action == "skip_setup":
            return self.kind == "suggest"
        return True

    def on_data_table_row_highlighted(self, event):
        if isinstance(event.data_table, TestTable):
            event.data_table.reset_moved_range()
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
        elif self.kind == 'tests':
            self.toggle_test_rows(event.data_table.selected_row_keys)
            if self.test_search is not None:
                self.query_one(SearchInput).focus()
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

    async def action_quit_request(self):
        if self.kind == 'menu':
            await self.app.confirm_quit()
            return
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

    def selected_effective_value(self):
        if self.kind not in {"defaults", "cleanup"} or not self.query(DataTable):
            return None
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        cells = table.get_row_at(table.cursor_row)
        if not str(cells[-1]) or str(cells[-1]) == "Not provided":
            return None
        return str(cells[1 if self.kind == "defaults" else 2])

    def copy_target(self):
        """One source for copy availability, full value and deselection policy."""
        if self.test_search is not None:
            selected_search = self.query_one(SearchInput).selected_text
            if selected_search:
                return selected_search, True
        selected = self.selected_result_text()
        if selected is not None:
            return selected, True
        value = self.selected_effective_value()
        if value is not None:
            return value, False
        fallback = (str(self.body) or None) if self.kind == 'error' else self.selected_test_name()
        return fallback, False

    def selected_test_name(self):
        if self.kind == 'tests' and self.test_search is None and not isinstance(self.app.focused, DataTable):
            return None
        if self.kind in {'tests', 'test-results'} and self.query(DataTable):
            table = self.query_one(DataTable)
            if table.row_count:
                return '\n'.join(str(table.get_row_at(index)[1])
                                 for index in table.selected_row_indices)
        if self.kind == 'test-failure' and self.body:
            return (self.body.plain if isinstance(self.body, Text) else self.body).splitlines()[0]
        return None

    async def action_copy_value(self):
        value, selected = self.copy_target()
        if value is None:
            return
        search_control = self.query_one(SearchInput) if selected and self.test_search is not None else None
        search_snapshot = (search_control.value, search_control.selection) if search_control is not None else None
        def copied():
            if selected:
                if search_control is not None:
                    if (search_control.value, search_control.selection) == search_snapshot:
                        search_control.selection = type(search_control.selection).cursor(search_control.cursor_position)
                else:
                    self.clear_selection()
        await self._copy_with_feedback(value, copied)

    def selected_result_text(self):
        return self.get_selected_text()

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
        await self.app.choose("skip")


class ApiAgentApp(AllureNavigation, ContainerManagement, App[int]):
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
    #account-selection { height: auto; margin-top: 1; }
    #identity { height: auto; color: $text-muted; margin-bottom: 1; }
    #heading { text-style: bold; margin-bottom: 1; }
    #body { height: auto; margin-bottom: 1; }
    #config-description { height: auto; margin: 1 0; }
    #unit-summary { height: 1fr; min-height: 6; margin-bottom: 1; }
    #live-progress { height: auto; }
    #live-progress > #body { width: 1fr; }
    #suite-elapsed { width: auto; height: 1; margin-left: 1; text-align: right; }
    #actions { height: auto; layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 4; }
    #actions.auth-suggestion { layout: horizontal; }
    #actions.auth-suggestion > Button { width: 1fr; }
    #skip-choice { width: 1fr; height: auto; }
    #remember-skip { width: 100%; height: auto; }
    Button { width: 100%; height: 3; min-height: 3; margin: 0 1 1 0; }
    Button:focus, Input:focus { border: heavy $accent; }
    DataTable { height: 1fr; min-height: 8; max-height: 100vh; margin-bottom: 1; }
    .configuration-review DataTable { min-height: 3; }
    .compact-review #identity { display: none; }
    .compact-review DataTable { height: 3; }
    .compact-review #heading, .compact-review #body, .compact-review DataTable { margin-bottom: 0; }
    #error { color: $error; height: auto; }
    #allure-loading { color: darkorange; height: auto; }
    #allure-bottom-bar { dock: bottom; height: auto; }
    #allure-bottom-bar Footer { dock: none; }
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
    #test-search-bar SearchInput, #test-search-bar SearchInput:focus { height: 1; border: none; padding: 0; }
    #menu-bottom-bar { dock: bottom; height: auto; }
    #menu-bottom-bar Footer { dock: none; }
    #pack-bootstrap { height: auto; color: red; }
    """

    def __init__(self, context, schema, argv=(), environ=None, *, session=None, collector=None, container_runtime=None, unit_runner=None, integration_runner=None):
        super().__init__()
        from api_agent.config.session import ConfigSession
        self.context, self._initial_schema = context, schema
        self.session = session or ConfigSession(context, schema, argv, environ)
        self.collector = collector
        self.unit_runner = unit_runner
        self.integration_runner = integration_runner
        self.initialize_allure()
        from api_agent.tui.reporting import ReportingSessions
        self.reporting_sessions = ReportingSessions()
        self.pending_shutdown_cleanup = None
        self.integration_unattended = False
        self.integration_persisted = None
        from api_agent.testing.inspection_controls import InspectionGroup
        self.inspections = InspectionGroup()
        self.inspection_failed = False
        self.inspection_lost = False
        self._inspection_generation = object()
        self._inspection_shutdown = False
        self.pending_inspection_cleanup = None
        self.integration_exit_status = None
        self.recording_cleanup = None
        self.unit_result = None
        self.cost_estimator = None
        self.test_costs = {}
        self.suite_usage = {}
        self.starting_quota = None
        self.test_run = None
        self.unit_rows = {}
        self.test_components = {}
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
        self.startup_mode = None
        self.auto_start_pending = False
        self.review_destination = None
        self.startup_review = True
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
            return self.switch_screen(screen)
        return self.push_screen(screen)

    async def confirm_debug_selection(self, mode, selected_tests, cancel_event):
        from api_agent.testing.debug_confirmation import debug_warning
        warning = debug_warning(mode, self.session.result().values.get('DEBUG'), selected_tests,
                                e2e_debug=self.session.result().values.get('E2E_DEBUG'))
        if warning is None:
            return True
        confirmation = asyncio.get_running_loop().create_future()
        def respond(accepted):
            if not confirmation.done():
                confirmation.set_result(accepted)
        dialog = QuitDialog(warning)
        self.push_screen(dialog, respond)
        try:
            while not confirmation.done() and not cancel_event.is_set():
                await asyncio.sleep(.05)
            if cancel_event.is_set():
                if self.screen is dialog:
                    self.pop_screen()
                return False
            return await confirmation
        finally:
            confirmation.cancel()

    async def on_mount(self):
        result = self.session.result()
        from api_agent.runtime.starting import start_configuration_errors, start_requested
        errors = (*result.errors, *start_configuration_errors(result))
        if errors:
            self.show("error", "Configuration error", "\n".join(errors), (("menu", "Main menu"),))
            return
        mode = result.fields.get("TUI_MODE")
        self.auto_start_pending = bool(start_requested(result.values) and not result.values.get('COLLECT_ONLY'))
        self.startup_mode = mode.value if mode and (mode.explicit or self.auto_start_pending) else None
        self.mode = None if self.auto_start_pending else self.startup_mode
        self.review_destination = self.startup_mode
        if self.startup_mode == "unattended-auth" and not result.values.get("COLLECT_ONLY"):
            self.review_destination = None
            self.show_auth_setup(startup=True)
            return
        if self.mode is not None and (result.values.get("COLLECT_ONLY") or self.mode == "unattended-auth"):
            await self.enter_mode(skip_defaults=True)
            return
        if result.values.get("COLLECT_ONLY"):
            self.show_menu()
            return
        if self.mode is not None:
            await self.start_configuration()
            return
        incomplete = self.auth_setup_needed()
        if incomplete and not result.values.get("SKIP_UNATTENDED_SETUP", False):
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
        result = self.session.result(_review=True)
        primary = result.values.get('LLM_CREDENTIAL_ROLE', 'LLM User')
        roles = [primary] if primary else []
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
        from api_agent.auth.profiles import normalize_auth, parse_profiles, validate_role_entries
        profile = self.session.result(_review=True).values.get('UNATTENDED_AUTH')
        try:
            roles = self.credential_roles()
            if not roles:
                return False
            registry = self.session.password_manager_registry
            canonical = normalize_auth(profile, registry=registry)
            for role in roles:
                validate_role_entries(role, canonical['roles'].get(role, []))
            if any(not canonical['roles'].get(role) for role in roles):
                return True
            parse_profiles({'password_manager': canonical['password_manager'],
                            'roles': {role: canonical['roles'][role] for role in roles}}, roles,
                           registry=registry)
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
        self.push_screen(AuthSetupScreen(roles, startup=startup),
                         lambda changed: self.auth_setup_closed(changed, startup=startup))

    async def auth_setup_closed(self, changed, *, startup=False):
        if changed:
            self.operation_status = 0
        if startup:
            if self.mode == "unattended-auth":
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
        rows = self.configuration_rows()
        if "UNATTENDED_AUTH" in self.session.result(_review=True).missing:
            self.show_required_auth()
            return
        if not rows:
            self.finish_configuration_review()
            return
        self.show("defaults", "Review configuration",
                  self.configuration_review_text(),
                  rows=rows, selected_row=selected_row)
        self.screen.add_class("configuration-review")

    def review_operation(self):
        return self.mode or self.review_destination

    def configuration_review_text(self):
        missing = self.session.result().missing
        reason = ("Required settings: " + ", ".join(missing) if missing else
                  "Unsaved defaults are available to review; save or edit values as needed.")
        if missing and len(reason) > max(24, self.size.width - 6):
            reason = f"{len(missing)} required settings; see Required rows."
        if self.size.height < 18:
            if not missing:
                reason = "Unsaved defaults; save or edit as needed."
            return reason + ("\na save; e edit; c close." if self.startup_review or self.review_destination else "\na save; e edit; c close / menu.")
        operation = self.review_operation()
        destination = f"continue {operation}" if self.review_destination else "open the main menu"
        return (reason + f"\na saves a default; e edits; c closes, then {destination}. "
                "Required settings still block execution.")

    def configuration_review_blocker(self):
        """Explain the current execution gate, separately from optional review."""
        operation = self.review_operation()
        if operation == "config-cleanup":
            return ""
        result = self.session.result()
        if result.missing:
            name = result.missing[-1]
            context = f"TUI_MODE={operation}" if operation else "the current configuration"
            return f"{name} required for {context}."
        return ""

    def configuration_rows(self):
        """Rebuild review membership from the current effective declaration set."""
        result = self.session.result(_review=True)
        active = set(result.fields) | set(result.missing) | {
            name for name, field in self.schema.fields.items() if field.review_optional}
        review = self.session.result(_review=True)
        default_names = {name for name, field in self.schema.fields.items()
                         if field.default is not UNSET or field.conditional_defaults}
        self.review_names.intersection_update(self.schema.fields)
        self.review_names.update(self.configuration_pending())
        rows = []
        for name in sorted(self.schema.fields):
            if (not self.schema.fields[name].acknowledge_default or name not in self.review_names
                    or (name not in active and name not in default_names)):
                continue
            resolved = review.fields.get(name) if name in default_names else result.fields.get(name)
            if name in default_names and resolved is None and name not in result.missing and not self.schema.fields[name].review_optional:
                continue
            rows.append((name, (name,
                        display(resolved.value) if resolved else Text("Required", justify="right") if name in result.missing else "",
                        resolved.source if resolved else "Not provided")))
        return rows

    def refresh_configuration_rows(self, screen):
        """Refresh displayed values and contextual actions as one UI snapshot."""
        screen.query_one("#body", Static).update(self.configuration_review_text())
        screen.query_one("#error", Static).update(self.configuration_review_blocker())
        rows = self.configuration_rows()
        if not rows:
            self.finish_configuration_review()
            return
        result = self.session.result(_review=True)
        table = screen.query_one(DataTable)
        names = [row.key.value for row in table.ordered_rows]
        if names != [name for name, _ in rows]:
            cursor = table.cursor_row
            selected = names[cursor] if names else None
            table.clear()
            for name, cells in rows:
                table.add_row(*(literal_cell(cell) for cell in cells), key=name)
            index = next((i for i, (name, _) in enumerate(rows) if name == selected),
                         min(cursor, len(rows) - 1))
            table.move_cursor(row=index)
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
        if self.mode is None and self.auto_start_pending:
            self.mode = self.startup_mode
            skip_defaults = True
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
            self.review_destination = None
            self.startup_review = False
            self.show_cleanup()
        elif self.mode == "cleanup-containers":
            self.review_destination = None
            self.startup_review = False
            self.show_containers()
            self.refresh_container_inventory()
        elif self.mode == "unattended-auth":
            self.show_auth_setup()
        else:
            await self.configure_operation(skip_defaults=skip_defaults)

    def finish_configuration_review(self):
        if self.review_destination:
            self.mode = self.review_destination
            self.run_worker(self.enter_mode(skip_defaults=True), group='startup-mode', exclusive=True)
        else:
            self.show_menu()

    def show_menu(self):
        if self.inspection_cleanup_active():
            return
        self.discard_finished_inspection()
        self.auto_start_pending = False
        self.review_destination = None
        self.startup_review = False
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
        self.refresh_managed_containers()

    async def _refresh_container_inventory(self):
        await self._refresh_managed_containers()

    def update_container_menu(self):
        self.update_managed_container_menu()

    def show_containers(self):
        self.show_container_management()

    def request_container_stop(self):
        selected = tuple(item for item in self.containers if item.running and item.id in self.container_marked)
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
        if result.missing:
            self.review_destination = self.mode
            await self.start_configuration()
            return
        if self.mode in {"unit", "integration", "e2e"}:
            await self.collect_tests()
        elif self.mode == "allure":
            self.review_destination = None
            self.startup_review = False
            self.auto_start_pending = False
            self.run_worker(self.show_allure_history(), group="allure-viewer", exclusive=False)
        else:
            self.review_destination = None
            self.startup_review = False
            self.auto_start_pending = False
            if self.mode == "run-local" and not self.check_operation_start():
                return
            self.unavailable(f"{dict(MODES)[self.mode]} execution is not implemented yet. No services or processes were started.")

    async def discover_tests(self, cancel_event):
        from api_agent.testing.collection import collect_tests
        collector = self.collector or collect_tests
        arguments = (self.context, self.mode, ())
        return await asyncio.to_thread(
            collector, *arguments, **({} if self.collector else {"cancel_event": cancel_event,
                                                               "config": self.session.result().values}))

    async def collect_tests(self):
        self.review_destination = None
        self.startup_review = False
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
            if self.auto_start_pending:
                self.auto_start_pending = False
                await self.choose('continue')
        except Exception as error:
            if cancel_event.is_set() or self.collection_cancel is not cancel_event:
                return
            self.show("error", "Collection failed", str(error), (("menu", "Main menu"),))

    async def back_to_selection(self):
        if self.test_run_active() or self.selection is None:
            return
        if not await self.end_inspection():
            return
        self.clear_allure_snapshots()
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
        if self.inspection_cleanup_active():
            return
        self.discard_finished_inspection()
        self.operation_status = 3
        self.test_run = TestRunLifecycle()
        self.collection_cancel = self.test_run.cancel_event
        self.unit_result = None
        self.test_costs = {}
        self.suite_usage = {}
        self.starting_quota = None
        self.unit_stopped = self.test_run.stopped
        self.unit_rows = {test.id: ("PENDING", test.id, "") for test in self.selection.selected_tests}
        self.show_unit_results(self.unit_progress())
        self.run_worker(self._run_units(), group="unit-run", exclusive=True)

    async def _run_units(self):
        from api_agent.testing.runner import run_unit_tests, unit_parallel_jobs
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
                parallel_jobs=unit_parallel_jobs(self.session.result().values),
                coverage_full=self.session.result().values.get('UNIT_COVERAGE_FULL', False),
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
            self.finish_test_run()

    def finish_test_run(self):
        self.test_run.finish()
        self.refresh_results_leave()
        if (self.unit_result is not None and self.screen_stack and isinstance(self.screen, WorkbenchScreen)
                and self.screen.kind == 'test-results'):
            self.show_unit_results()
        self.refresh_bindings()

    async def _run_integrations(self):
        from api_agent.testing.launched import run_launched_integration
        runner = self.integration_runner or run_launched_integration
        self.discard_finished_inspection()
        generation = self._inspection_generation
        self.integration_persisted = None
        self.integration_exit_status = None
        outcome = {}
        try:
            if self.inspection_cleanup_active():
                raise ValueError('End retained inspection before starting another operation.')
            fresh = await self.discover_tests(self.collection_cancel)
            if self.collection_cancel.is_set():
                return
            self.selection.refresh_for_run(fresh)
            if not self.selection.selected_tests:
                raise ValueError("No selected tests remain after recollection")
            selected = frozenset(test.id for test in self.selection.selected_tests)
            if selected != getattr(self, '_debug_approved_selection', None):
                if not await self.confirm_debug_selection('integration', self.selection.selected_tests,
                                                          self.collection_cancel):
                    self.show_selection()
                    return
                self._debug_approved_selection = selected
            self.unit_rows = {test.id: ("PENDING", test.id, "") for test in self.selection.selected_tests}
            self.show_unit_results(self.unit_progress())
            from api_agent.testing.requirements import plan_integration_requirements
            plan = plan_integration_requirements(self.selection.selected_tests)
            resolved = self.session.result(operation_intents=('ui_session',) if plan.requires_ui_session else ())
            resolved.raise_for_errors()
            if resolved.missing:
                raise ValueError('Missing required configuration: ' + ', '.join(resolved.missing))
            active = dict(resolved.values)
            from api_agent.telemetry.privacy import ContentPolicy
            content_policy = ContentPolicy.from_config(resolved)
            if plan.requires_pack_api and not self.check_operation_start():
                return
            if not self.integration_unattended:
                active['UNATTENDED_USER_INDEX'] = None
            from api_agent.tui.manager_recovery import TUIInteraction
            interaction = TUIInteraction(self, self.collection_cancel)
            def execute():
                try:
                    outcome['recorded'] = runner(self.context, self.selection.selected_tests,
                        active, accepted=True, interaction=interaction,
                        content_policy=content_policy,
                        **self.reporting_runner_options(runner),
                        **self.historical_runner_options(runner),
                        **self.allure_runner_options(runner, generation),
                        usage_output=lambda text: self.call_from_thread(self.receive_usage, text, generation),
                        unattended_index_source=getattr(resolved.fields.get('UNATTENDED_USER_INDEX'), 'source', None),
                        on_event=lambda event: self.call_from_thread(self.integration_event, event, generation),
                        cancel_event=self.collection_cancel)
                    return outcome['recorded']
                except BaseException as error:
                    outcome['error'] = error
                    raise
            from api_agent.runtime.tasks import offload
            recorded = await offload(execute, cancellation=self.collection_cancel)
            from api_agent.testing.inspection_controls import InspectionGroup
            self.inspections = InspectionGroup(getattr(recorded.execution, 'inspections', ()))
            self.inspection_lost |= self.inspections.lost
            self.inspection_failed = self.inspection_lost or self.inspections.pending
            self.unit_result = recorded.execution
            self.suite_usage = getattr(recorded, 'usage', {})
            self.test_costs = self.suite_usage.get('test_costs', {})
            self.integration_persisted = getattr(recorded, 'persisted', True)
            for test in self.unit_result.tests:
                self.unit_rows[test.identity.id] = (test.status.upper(), test.identity.id, f"{test.duration:.3f}s")
            self.integration_exit_status = recorded.exit_code
            self.operation_status = 3 if self.inspection_failed else recorded.exit_code
            if not self.collection_cancel.is_set():
                self.show_unit_results()
        except asyncio.CancelledError as interruption:
            self.collection_cancel.set()
            error = outcome.get('error')
            self.recording_cleanup = getattr(error, 'recording_prerequisites', self.recording_cleanup)
            recorded = outcome.get('recorded', getattr(error, 'recorded_result', None))
            from api_agent.testing.inspection_controls import InspectionGroup
            handles = list(getattr(error, 'inspections', ()))
            if recorded is not None:
                handles.extend(getattr(recorded.execution, 'inspections', ()))
                self.unit_result = recorded.execution
                self.suite_usage = getattr(recorded, 'usage', {})
                self.test_costs = self.suite_usage.get('test_costs', {})
                self.integration_persisted = getattr(recorded, 'persisted', True)
                self.integration_exit_status = recorded.exit_code
            # The cancelled executor await must transfer every late owner before
            # the run lifecycle acknowledges teardown to menu/quit consumers.
            unique = []
            for handle in (*self.inspections.handles, *handles):
                if not any(handle is existing for existing in unique):
                    unique.append(handle)
            self.inspections = InspectionGroup(unique)
            self.inspection_lost |= self.inspections.lost
            self.integration_exit_status = 130
            self.operation_status = 130
            interruption.inspection_group = self.inspections
            interruption.inspections = self.inspections.handles
            if self.inspection_cleanup_active():
                interruption.cleanup_pending = self.inspection_cleanup_receipt()
            raise
        except Exception as error:
            self.operation_status = 3
            self.recording_cleanup = getattr(error, 'recording_prerequisites', self.recording_cleanup)
            from api_agent.testing.inspection_controls import InspectionGroup
            handles = list(self.inspections.handles)
            for handle in getattr(error, 'inspections', ()):
                if not any(handle is existing for existing in handles):
                    handles.append(handle)
            self.inspections = InspectionGroup(handles)
            self.inspection_lost |= self.inspections.lost
            recorded = getattr(error, 'recorded_result', None)
            if recorded is not None:
                for handle in getattr(recorded.execution, 'inspections', ()):
                    if not any(handle is existing for existing in handles):
                        handles.append(handle)
                self.inspections = InspectionGroup(handles)
                self.inspection_lost |= self.inspections.lost
                self.unit_result = recorded.execution
                self.suite_usage = getattr(recorded, 'usage', {})
                self.test_costs = self.suite_usage.get('test_costs', {})
                self.integration_exit_status = recorded.exit_code
                self.integration_persisted = recorded.persisted
                self.inspection_failed = True
                if not self.collection_cancel.is_set():
                    self.show_unit_results()
            elif not self.collection_cancel.is_set():
                from traceback import format_exception
                self.show("error", "Integration execution failed",
                          "Integration setup or recording failed. Check configuration and the selected provider.\n\n"
                          + ''.join(format_exception(error)),
                          (("menu", "Main menu"),))
        finally:
            self.finish_test_run()

    def reporting_runner_options(self, runner):
        import inspect
        parameters = inspect.signature(runner).parameters
        if 'reporting_session' in parameters:
            return {'reporting_session': self.reporting_sessions.acquire}
        return {}

    async def close_reporting_sessions(self):
        try:
            await self.close_container_logs()
            await self.reporting_sessions.aclose()
        except BaseException as error:
            self.operation_status = 3
            self.pending_shutdown_cleanup = self.shutdown_cleanup_receipt()
            if not isinstance(error, Exception):
                error.cleanup_pending = self.pending_shutdown_cleanup
                raise
            return False
        self.pending_shutdown_cleanup = None
        return True

    def shutdown_cleanup_receipt(self):
        from api_agent.providers.control import CleanupPendingError
        inspection = self.pending_inspection_cleanup
        sessions = self.reporting_sessions
        async def retry():
            # Inspection may still publish or access storage; retain session
            # ownership until its complete subtree has acknowledged disposal.
            if inspection is not None:
                await inspection.retry()
            await self.close_container_logs()
            await sessions.aclose()
            self.pending_shutdown_cleanup = None
        logs = getattr(self, "container_log_screen", None)
        owners = ((inspection,) if inspection is not None else ()) + ((logs,) if logs is not None else ()) + (sessions,)
        error = CleanupPendingError(owners, retry)
        error.inspections = getattr(inspection, 'inspections', ())
        error.inspection_group = getattr(inspection, 'inspection_group', self.inspections)
        return error

    def inspection_cleanup_active(self):
        return (self.inspections.active or self.recording_cleanup is not None
                or self.allure_viewer is not None or self.allure_cleanup_active())

    def discard_finished_inspection(self):
        if not self.inspection_cleanup_active():
            from api_agent.testing.inspection_controls import InspectionGroup
            self.inspections = InspectionGroup()
            self.inspection_failed = self.inspection_lost = False
            self.pending_inspection_cleanup = None
            self._inspection_generation = object()

    def integration_event(self, event, generation):
        if generation is self._inspection_generation:
            self.unit_event(event)

    def inspection_cleanup_receipt(self):
        from api_agent.providers.control import CleanupPendingError
        from api_agent.runtime.operation import retry_owned_cleanup
        from api_agent.runtime.tasks import offload
        group = self.inspections
        recording = self.recording_cleanup
        viewer = self.allure_viewer
        allure_cleanup = (self.begin_allure_cleanup() if self.allure_cleanup_active() else None)
        pending = recording.pending_cleanup if recording is not None else None
        generation = self._inspection_generation
        async def retry():
            cleaned = True
            if allure_cleanup is not None:
                cleaned = await self.wait_allure_cleanup(allure_cleanup)
            elif viewer is not None:
                try:
                    await viewer.aclose()
                except Exception:
                    cleaned = False
                else:
                    if self.allure_viewer is viewer:
                        self.allure_viewer = None
            if pending is not None:
                try:
                    await retry_owned_cleanup(recording, pending)
                except Exception:
                    cleaned = False
            closed = await offload(group.close, force=True)
            if not closed or not cleaned:
                raise error from None
            if (generation is self._inspection_generation and self.inspections is group
                    and self.recording_cleanup is recording):
                self.recording_cleanup = None
                self.pending_inspection_cleanup = None
                self.inspection_lost |= group.lost
                self.inspection_failed = self.inspection_lost
                if self.inspection_lost:
                    self.operation_status = 3
                elif self.integration_exit_status is not None:
                    self.operation_status = self.integration_exit_status
        owners = (group,) + ((viewer,) if viewer is not None else ())
        if allure_cleanup is not None:
            owners += (allure_cleanup,)
        if recording is not None:
            owners += (recording,)
        error = CleanupPendingError(owners, retry)
        error.inspection_group = group
        error.inspections = group.handles
        error.recording_prerequisites = recording
        return error

    async def end_inspection(self, *, force=False):
        if not await self.close_allure():
            return False
        self.inspection_lost |= self.inspections.lost
        if self.inspection_lost:
            self.inspection_failed = True
            self.operation_status = 3
        if not self.inspection_cleanup_active():
            return True
        cleaned = True
        if self.recording_cleanup is not None:
            from api_agent.runtime.operation import retry_owned_cleanup
            try:
                await retry_owned_cleanup(self.recording_cleanup, self.recording_cleanup.pending_cleanup)
            except Exception:
                cleaned = False
            else:
                self.recording_cleanup = None
        from api_agent.runtime.tasks import offload
        closed = await offload(self.inspections.close, force=force)
        self.inspection_lost |= self.inspections.lost
        if not closed or not cleaned:
            self.operation_status = 3
            self.inspection_failed = True
        else:
            self.inspection_failed = self.inspection_lost
            if self.inspection_lost:
                self.operation_status = 3
            elif self.integration_exit_status is not None:
                self.operation_status = self.integration_exit_status
        if not self._inspection_shutdown and self.screen_stack and self.unit_result is not None and isinstance(self.screen, WorkbenchScreen) and self.screen.kind == 'test-results':
            self.show_unit_results()
            self.screen.refresh_bindings()
        return closed and cleaned

    def receive_usage(self, text, generation):
        if generation is self._inspection_generation and text and '\n' not in text:
            self.starting_quota = text
            if self.test_run_active() and isinstance(self.screen, WorkbenchScreen) and self.screen.kind == 'test-results':
                self.screen.query_one('#body', Static).update(self.unit_progress())

    def unit_progress(self):
        result = progress_summary(self.mode or "unit", self.unit_rows)
        if self.starting_quota is not None:
            from .test_results import append_usage_line
            result.append('\n')
            append_usage_line(result, self.starting_quota)
        return result

    def unit_event(self, event):
        if event.kind in {'inspection_closed', 'inspection_lost', 'inspection_cleanup_pending'}:
            if event.kind != 'inspection_closed':
                self.inspection_failed = True
                self.inspection_lost |= event.kind == 'inspection_lost'
                self.operation_status = 3
            self.inspection_lost |= self.inspections.lost
            if self.inspection_lost:
                self.inspection_failed = True
                self.operation_status = 3
            self.refresh_results_leave()
            if event.kind == 'inspection_closed' and not self.inspection_cleanup_active() and not self.inspection_lost:
                self.inspection_failed = False
                if self.integration_exit_status is not None:
                    self.operation_status = self.integration_exit_status
            if self.screen_stack and self.unit_result is not None and isinstance(self.screen, WorkbenchScreen) and self.screen.kind == 'test-results':
                self.show_unit_results()
                self.screen.refresh_bindings()
            return
        if self.collection_cancel.is_set():
            return
        if event.identity is not None and event.kind in {"test_started", "test_finished", "component_progress"}:
            key = event.identity.id
            if event.kind == 'component_progress':
                if event.component is None or self.unit_rows.get(key, ('',))[0] != 'RUNNING':
                    return
                components = self.test_components.setdefault(key, {})
                if event.component.active:
                    components[event.component.id] = event.component
                else:
                    components.pop(event.component.id, None)
                self.unit_rows[key] = ('RUNNING', key, component_timing(components.values()))
            else:
                self.test_components.pop(key, None)
                self.unit_rows[key] = ((event.status or "RUNNING").upper(), key,
                                       f"{event.duration:.3f}s" if event.kind == "test_finished" else "")
            if isinstance(self.screen, WorkbenchScreen) and self.screen.kind == "test-results" and self.screen.query(DataTable) and self.screen.query("#body"):
                table = self.screen.query_one(DataTable)
                self.screen.query_one("#body", Static).update(self.unit_progress())
                for column, value in zip(table.columns, self.unit_rows[key]):
                    table.update_cell(key, column, literal_cell(value), update_width=True)
                if event.kind != 'component_progress':
                    self.screen.refresh_bindings()

    def show_unit_results(self, summary=None, *, selected_row=None, scroll_y=None):
        if summary is None:
            if self.mode in {'integration', 'e2e'}:
                from api_agent.testing.contracts import format_test_summary
                summary = format_test_summary(self.mode, self.unit_result.tests,
                                              state='CANCELLED' if self.unit_result.cancelled else None)
                if self.integration_persisted is False:
                    summary += '\nAllure publication: FAIL — could not save the run to all configured history destinations. Check reporting dependencies and storage availability.'
                elif self.integration_persisted is True:
                    summary += '\nAllure publication: PASS'
                runner_status = self.integration_exit_status if self.integration_exit_status is not None else self.operation_status
                if self.integration_persisted is not False and runner_status and not self.unit_result.cancelled and all(test.status in {'passed', 'skipped'} for test in self.unit_result.tests):
                    summary += f'\nTest operation: FAIL — runner or reporting failed outside individual test outcomes (exit status {runner_status}).'
            else:
                from api_agent.testing.runner import format_unit_summary
                summary = format_unit_summary(self.unit_result, include_elapsed=False)
        if self.unit_result is not None and self.suite_usage:
            from api_agent.telemetry.usage_output import final_usage_text
            summary += '\n' + final_usage_text(self.suite_usage)
        if self.unit_result is not None and self.mode == 'integration':
            if self.inspections.active:
                summary += '\nDebug inspection: open — tests and publication have finished. Leaving results closes inspection.'
            if self.inspection_failed or self.recording_cleanup is not None:
                summary += '\nInspection cleanup: FAIL — worker lost or cleanup pending; leave results to retry cleanup.'
        if self.unit_result is not None and self.test_run is not None:
            from .test_results import completed_summary
            summary = completed_summary(summary, self.test_run.elapsed)
        screen = self.screen
        if isinstance(screen, ModalScreen):
            screen = self.screen_stack[-2]
        rows = ordered_result_rows(self.unit_rows, completed=self.unit_result is not None)
        if self.mode in {'integration', 'e2e'} and self.unit_result is not None:
            from .cost_history import cost_cell
            rows = [(key, (*cells, cost_cell(self.test_costs.get(key, {}))))
                    for key, cells in rows]
        if selected_row not in {key for key, _ in rows}:
            scroll_y = None
        if isinstance(screen, WorkbenchScreen) and screen.kind == "test-results":
            if getattr(screen, "results_run", None) is not self.unit_result and screen.query(DataTable):
                screen.query_one(DataTable).results_completed = False
            screen.body, screen.rows = summary, rows
            self.call_after_refresh(self.update_test_results, screen)
        else:
            self.show("test-results", f"{(self.mode or 'unit').capitalize()} test results", summary, rows=rows, selected_row=selected_row)
            screen = self.screen
            if scroll_y is not None:
                self.call_after_refresh(lambda: screen.query_one(DataTable).scroll_to(
                    y=scroll_y, animate=False))
        screen.results_run = self.unit_result
        screen.test_run_complete = self.unit_result is not None
        screen.style_results = self.style_unit_summary
        self.refresh_results_leave()
        if self.unit_result is not None:
            self.call_after_refresh(self.style_unit_summary)

    def update_test_results(self, screen):
        if self.screen is not screen or not screen.query(DataTable):
            return
        screen.query_one("#body", Static).update(screen.body)
        screen.refresh_suite_timer()
        table = screen.query_one(DataTable)
        if screen.test_run_complete and self.mode in {'integration', 'e2e'} and len(table.columns) == 3:
            table.add_column('Est. $', width=9)
        reconcile_results_table(table, screen.rows, completed=screen.test_run_complete)
        if screen.test_run_complete and screen.style_results:
            screen.style_results()
        screen.layout_test_results()

    def style_unit_summary(self):
        if isinstance(self.screen, WorkbenchScreen) and self.screen.kind == "test-results":
            body = self.screen.query_one("#body", Static)
            non_unit = self.mode in {'integration', 'e2e'}
            body.update(styled_summary(
                body.content, failed=(self.unit_result.cancelled or not self.unit_result.tests
                                     or any(test.status not in {'passed', 'skipped'} for test in self.unit_result.tests))
                                     if non_unit else bool(self.unit_result.exit_code),
                skipped=non_unit and any(test.status == 'skipped' for test in self.unit_result.tests),
                neutral_details=non_unit or not self.unit_result.require_full_coverage,
                green_prefixes=("Code coverage: lines 100% · branches 100%.", "Allure publication: PASS"),
                red_prefixes=('Allure publication: FAIL', 'Test operation: FAIL', 'Inspection cleanup: FAIL')))

    def account_options(self):
        if 'UNATTENDED_USER_INDEX' not in self.schema.fields:
            return ()
        from .accounts import account_options
        try:
            roles = self.credential_roles()
            return account_options(self.session.result(_review=True).values.get('UNATTENDED_AUTH') or {},
                                   roles, paired=self.mode == 'run-local',
                                   registry=self.session.password_manager_registry)
        except ValueError:
            return ()

    def account_summary(self):
        index = self.session.result().values.get('UNATTENDED_USER_INDEX')
        if index is None:
            return Text('No unattended user set — logins must be done manually', style='red')
        label = dict(self.account_options()).get(index)
        if label is None:
            return Text(f'Unattended user #{index} is unavailable; configure accounts or choose manual login.', style='red')
        return Text(f'Use unattended user #{index}: {label}', style='green')

    def cycle_account(self, direction):
        try:
            choices = (None,) + tuple(index for index, _ in self.account_options())
            current = self.session.result().values.get('UNATTENDED_USER_INDEX')
            position = choices.index(current) if current in choices else 0
            selected = choices[(position + direction) % len(choices)]
            self.session.set_value('UNATTENDED_USER_INDEX', '' if selected is None else str(selected), persist=True)
            self.screen.query_one('#account-selection', Static).update(self.account_summary())
            self.screen.refresh_bindings()
            actual = self.session.result().fields['UNATTENDED_USER_INDEX']
            message = (f'Saved to {self.session._source(self.session.dotenv_path)}, but {actual.source} overrides the saved choice.'
                       if actual.value != selected else '')
            self.screen.query_one('#error', Static).update(message)
        except (ValueError, OSError) as error:
            self.screen.query_one('#error', Static).update(str(error))

    def historical_runner_options(self, runner):
        import inspect
        parameters = inspect.signature(runner).parameters
        if 'historical_estimator' in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            if getattr(self.cost_estimator, 'picker_configuration', None) is not None:
                # Start freezes a current clock/window even if settings stayed
                # unchanged while this picker was open.
                self.cost_estimator = self.fresh_cost_estimator()
            return {'historical_estimator': self.cost_estimator}
        return {}

    def fresh_cost_estimator(self):
        from api_agent.testing.cost_history import estimator_for
        values = self.session.result().values
        snapshot = estimator_for(self.context, self.selection.tests, values)
        snapshot.picker_configuration = deepcopy(values)
        return snapshot

    def refresh_cost_configuration(self):
        snapshot = self.cost_estimator
        saved = getattr(snapshot, 'picker_configuration', None)
        if saved is not None and saved != self.session.result().values:
            self.cost_estimator = self.fresh_cost_estimator()
            return True
        return False

    def selection_cells(self, row):
        cells = ("✓" if row.selected else "", row.label, f"{row.count}/{row.total}", row.kind)
        if self.cost_estimator is not None:
            from .cost_history import total_cell
            cells += (total_cell(self.cost_estimator, row.members),)
        return cells

    def refresh_cost_summary(self):
        if (self.cost_estimator is not None and isinstance(self.screen, WorkbenchScreen)
                and self.screen.kind == "tests"):
            from .cost_history import selected_summary
            self.screen.query_one('#body', Static).update(
                'Toggle groups or individual tests with Enter. Press s to start selected tests.\n' +
                selected_summary(self.cost_estimator, self.selection.selected_ids))

    def refresh_picker_costs(self, screen, selection):
        # Refresh belongs to the picker which queued it, after its DOM mounts.
        if (selection is not self.selection or not screen.is_attached
                or not screen.is_mounted or not screen.is_running or self.test_run_active()
                or self._inspection_shutdown):
            return
        if screen is self.screen:
            self.refresh_rows()
        elif screen in self.screen_stack:
            # A modal may cover the picker between scheduling and execution.
            screen._pending_cost_refresh = selection

    async def synchronize_cost_history(self, snapshot, selection):
        try:
            replacement = await asyncio.to_thread(snapshot.synchronize)
        except Exception:
            return
        if (self.cost_estimator is snapshot and self.selection is selection and not self.test_run_active()
                and not self._inspection_shutdown
                and any(isinstance(screen, WorkbenchScreen) and screen.kind == 'tests' for screen in self.screen_stack)):
            changed = self.refresh_cost_configuration()
            if not changed:
                self.cost_estimator = replacement or snapshot
                self.cost_estimator.picker_configuration = getattr(snapshot, 'picker_configuration', None)
            if isinstance(self.screen, WorkbenchScreen) and self.screen.kind == 'tests':
                self.screen.call_after_refresh(self.refresh_picker_costs, self.screen, selection)
            else:
                # A modal temporarily covers the same picker. Keep its result
                # and redraw once the original picker becomes active again.
                picker = next(screen for screen in reversed(self.screen_stack)
                              if isinstance(screen, WorkbenchScreen) and screen.kind == 'tests')
                picker._pending_cost_refresh = selection
            if changed:
                self.run_worker(self.synchronize_cost_history(self.cost_estimator, selection),
                                group='cost-history', exclusive=True)

    def show_selection(self):
        self.cost_estimator = None
        if self.mode in {'integration', 'e2e'}:
            try:
                self.cost_estimator = self.fresh_cost_estimator()
            except Exception:
                pass
        rows = [(row.id, self.selection_cells(row)) for row in self.selection.groups()]
        actions = (("continue", "(s) Start selected tests"),)
        self.show("tests", f"Select {self.mode} tests", "Toggle groups or individual tests with Enter. Press s to start selected tests.", actions, rows)
        if self.cost_estimator is not None:
            self.call_after_refresh(self.refresh_cost_summary)
            self.run_worker(self.synchronize_cost_history(self.cost_estimator, self.selection),
                            group='cost-history', exclusive=True)

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
            result = next((test for test in self.unit_result.tests if test.identity.id == key), None)
            from api_agent.telemetry.usage_output import has_tokens
            historical = (self.cost_estimator.predictions.get(key, {}).get('has_token_history', False)
                          if self.cost_estimator is not None else False)
            if result and (self.mode in {'integration', 'e2e'} or result.status != 'passed' or getattr(result, 'local_diagnostic', '')
                           or has_tokens(self.suite_usage, key) or historical):
                mode = (self.mode or 'unit').capitalize()
                title = (f"{mode} test details" if result.status == 'passed' else
                         f"{mode} test skipped" if result.status == 'skipped' else f"{mode} test failure")
                from .test_results import test_details
                table = self.screen.query_one(DataTable)
                return_scroll_y = table.scroll_y
                self.show("test-failure", title, test_details(result, self.suite_usage,
                    self.test_costs.get(key, {}), historical_tokens=historical))
                self.screen.result_return = (self.unit_result, key, return_scroll_y)
                self.call_after_refresh(lambda: self.screen.query_one(VerticalScroll).focus())
        elif self.screen.kind == "tests":
            self.selection.toggle(key)
            self.refresh_rows()
        elif self.screen.kind == "containers":
            self.toggle_managed_container(key)

    def refresh_rows(self):
        if self.screen.kind == "tests" and self.selection is None:
            return
        table = self.screen.query_one(DataTable)
        if self.screen.kind == 'tests' and self.screen.test_search is not None:
            self.screen.render_search_rows()
            self.refresh_cost_summary()
            return
        if self.screen.kind == "tests":
            rows = [(row.id, self.selection_cells(row)) for row in self.selection.groups()]
        else:
            rows = [(row.name, ("✓" if row.present and str(row.source).endswith("/.env") else "", row.name, display(row.value) if row.resolved else "Inactive / unset", row.source if row.resolved else "")) for row in self.session.cleanup_rows()]
        if self.screen.kind == "cleanup":
            # Rebuilding posts another highlight event; unchanged snapshots must
            # not rebuild again. Preserve the focused setting when rows change.
            current = [tuple(table.get_row_at(i)) for i in range(table.row_count)]
            if current == [tuple(literal_cell(cell) for cell in cells) for _, cells in rows]:
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
        self.refresh_cost_summary()

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
            elif key == "container-logs":
                self.request_container_logs()
            elif key == "allure-restore-secondary":
                if (not self._allure_opening and
                        (self._allure_restore_worker is None or self._allure_restore_worker.is_finished)):
                    self._allure_restore_worker = self.run_worker(
                        self.restore_allure_secondary_history(), group="allure-restore", exclusive=False)
            elif key == "refresh-allure":
                if not self._allure_opening:
                    self.run_worker(self.show_allure_history(), group="allure-viewer", exclusive=False)
            elif key == "switch-target":
                self.switch_target()
            elif key == "skip":
                if self.screen.query_one("#remember-skip", Checkbox).value:
                    self.session.set_value("SKIP_UNATTENDED_SETUP", "True", persist=True)
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
                self.show_auth_setup(startup=self.startup_review)
            elif key == "continue":
                if self.inspection_cleanup_active():
                    await self.close_view()
                    return
                if not self.selection.selected_ids:
                    raise ValueError(NO_TEST_SELECTION_MESSAGE)
                from api_agent.testing.debug_confirmation import debug_warning
                warning = debug_warning(self.mode, self.session.result().values.get('DEBUG'),
                                        self.selection.selected_tests,
                                        e2e_debug=self.session.result().values.get('E2E_DEBUG'))
                if warning:
                    selected = frozenset(test.id for test in self.selection.selected_tests)
                    def after_debug_confirmation(accepted):
                        if accepted:
                            self._debug_approved_selection = selected
                            self.run_worker(self.choose('confirmed-continue'),
                                            group='debug-confirmation', exclusive=True)
                        else:
                            self.show_selection()
                    self.push_screen(QuitDialog(warning), after_debug_confirmation)
                    return
                await self.choose('confirmed-continue')
            elif key == 'confirmed-continue':
                if self.mode == "e2e":
                    from api_agent.runtime.starting import e2e_accepted
                    if e2e_accepted(self.session.result()):
                        await self.choose("accept")
                        return
                    self.show("acceptance", "E2E human acceptance", f"{len(self.selection.selected_tests)} tests selected to run.\n\n" + E2E_WARNING, (("accept", "Accept for this run"), ("decline", "Decline")))
                elif self.mode == "unit":
                    self.start_unit_run()
                else:
                    if not self.session.result().values.get('DEBUG'):
                        self._debug_approved_selection = None
                    index = self.session.result().values.get('UNATTENDED_USER_INDEX')
                    if index is not None and index not in dict(self.account_options()):
                        raise ValueError('UNATTENDED_USER_INDEX has no eligible configured account; choose a user or manual login.')
                    self.integration_unattended = index is not None
                    self.operation_status = 3
                    self.test_run = TestRunLifecycle()
                    self.collection_cancel = self.test_run.cancel_event
                    self.unit_result = None
                    self.clear_allure_snapshots()
                    self.unit_rows = {test.id: ("PENDING", test.id, "") for test in self.selection.selected_tests}
                    self.show_unit_results(self.unit_progress())
                    self.run_worker(self._run_integrations(), group='integration-run', exclusive=True)
            elif key == "accept":
                if not self.check_operation_start():
                    return
                self.unavailable("E2E execution is not implemented yet. Acceptance did not start tests or prerequisites.")
            elif key == "decline":
                self.operation_status = 3
                self.show_selection()
            elif key == "menu":
                if self.test_run_active() or self.inspection_cleanup_active() or self.allure_viewer is not None:
                    await self.close_view()
                else:
                    self.show_menu()
            elif key == "exit":
                await self.confirm_quit()
        except Exception as error:
            self.screen.query_one("#error", Static).update(str(error))

    async def action_quit(self):
        from .logs import LogScreen
        if isinstance(self.screen, LogScreen):
            await self.screen.action_quit_request()
        else:
            await super().action_quit()

    async def confirm_quit(self):
        self.stop_allure_admission(shutdown=True)
        if self.mode in {"unit", "integration"} and self.unit_rows and self.unit_result is None:
            self.operation_status = 130
        self.collection_cancel.set()
        if self.test_run_active():
            await self.test_run.stop_and_wait()
        if not await self.end_inspection(force=True):
            return
        if not await self.close_reporting_sessions():
            self.notify("Reporting cleanup incomplete; retry Quit to finish releasing the session.", severity="error", timeout=10)
            return
        self.exit(self.operation_status)

    async def on_unmount(self):
        self.stop_allure_admission(shutdown=True)
        self._inspection_shutdown = True
        for screen in self.screen_stack:
            if isinstance(screen, WorkbenchScreen):
                screen._pending_cost_refresh = None
        self.collection_cancel.set()
        if self.test_run_active():
            await self.test_run.stop_and_wait()
        try:
            await self.end_inspection(force=True)
        finally:
            if self.inspection_cleanup_active():
                self.pending_inspection_cleanup = self.inspection_cleanup_receipt()
                self.pending_shutdown_cleanup = self.shutdown_cleanup_receipt()
            else:
                await self.close_reporting_sessions()

    def test_run_active(self):
        return self.test_run is not None and self.test_run.active

    async def stop_tests_to_menu(self, accepted):
        if not accepted:
            return
        if self.test_run_active():
            await self.test_run.stop_and_wait()
            self.operation_status = 130
        if await self.end_inspection(force=True):
            self.show_menu()

    async def end_inspection_to_menu(self, accepted):
        if accepted and await self.end_inspection():
            self.show_menu()

    def results_leave_prompt(self):
        active = self.test_run_active()
        duties = ["stop running tests"] if active else []
        if self.inspections.active:
            duties.append("end browser inspection")
        if self.recording_cleanup is not None:
            duties.append("finish pending cleanup")
        if self.allure_viewer is not None:
            duties.append("close Allure")
        prefix = ", ".join(duties)
        message = (prefix + " and " if prefix else "") + "return to the menu"
        message = message[0].upper() + message[1:]
        return active, bool(duties), message + "? Enter literal y to confirm; every other response cancels."

    def refresh_results_leave(self):
        if not self.screen_stack:
            return
        dialog = self.screen
        if isinstance(dialog, QuitDialog) and getattr(dialog, '_results_leave', False):
            _, _, dialog.message = self.results_leave_prompt()
            for body in dialog.query('#quit-message'):
                body.update(dialog.message)

    async def close_view(self):
        if self.screen.kind == 'test-failure':
            result, key, scroll_y = getattr(self.screen, 'result_return', (None, None, None))
            self.show_unit_results(selected_row=key if result is self.unit_result else None,
                                   scroll_y=scroll_y if result is self.unit_result else None)
            return
        if self.screen.kind == 'allure':
            if await self.close_allure():
                self.show_menu()
            return
        active, needs_confirmation, message = self.results_leave_prompt()
        if needs_confirmation:
            dialog = QuitDialog(message)
            dialog._results_leave = True
            self.push_screen(dialog, self.stop_tests_to_menu if active else self.end_inspection_to_menu)
            return
        if self.screen.kind == "collecting":
            self.collection_cancel.set()
        if self.screen.kind == "suggest" or self.screen.kind == "unavailable" and self.mode is None:
            await self.start_configuration()
        elif self.screen.kind == "input":
            self.show_configuration()
        elif self.screen.kind == "defaults" and self.review_destination:
            self.mode = self.review_destination
            await self.enter_mode(skip_defaults=True)
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
    try:
        result = app.run()
    except BaseException as error:
        pending = getattr(app, 'pending_shutdown_cleanup', None) or getattr(app, 'pending_inspection_cleanup', None)
        if pending is not None:
            error.cleanup_pending = pending
            error.inspections = pending.inspections
            error.inspection_group = pending.inspection_group
        raise
    pending = getattr(app, 'pending_shutdown_cleanup', None) or getattr(app, 'pending_inspection_cleanup', None)
    if pending is not None:
        raise pending from None
    if getattr(app, "console_error", None):
        import sys
        print(app.console_error, file=sys.stderr)
    if getattr(app, "collected_output", None):
        print(app.collected_output)
    return result if isinstance(result, int) else 3
