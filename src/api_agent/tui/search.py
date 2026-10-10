"""Editable search field and compact, clickable user navigation hints."""
import sys

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Footer, Input, Static

from api_agent.tui.clipboard import ClipboardError, copy_text


class SearchInput(Input):
    """Native text editing with table navigation on vertical keys."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('select_on_focus', False)
        super().__init__(*args, **kwargs)

    BINDINGS = [
        Binding('up,k', "navigate('up')", show=False),
        Binding('down,j', "navigate('down')", show=False),
        Binding('pageup', "navigate('page_up')", show=False),
        Binding('pagedown', "navigate('page_down')", show=False),
        Binding('shift+up,shift+k,K', "navigate('range_up')", show=False),
        Binding('shift+down,shift+j,J', "navigate('range_down')", show=False),
        Binding('shift+pageup', "navigate('range_page_up')", show=False),
        Binding('shift+pagedown', "navigate('range_page_down')", show=False),
        Binding('ctrl+shift+up,super+shift+up', "navigate('range_top')", show=False),
        Binding('ctrl+shift+down,super+shift+down', "navigate('range_bottom')", show=False),
        Binding('ctrl+a,super+a', 'select_all', show=False),
        Binding('ctrl+x', "search_cut('ctrl+x')", show=False),
        Binding('super+x', "search_cut('super+x')", show=False),
        Binding('enter', 'toggle', show=False),
        Binding('escape', 'exit_search', show=False),
    ]

    class Navigate(Message):
        def __init__(self, action: str):
            super().__init__()
            self.action = action

    class Toggle(Message):
        pass

    class Exit(Message):
        pass

    def action_navigate(self, action):
        self.post_message(self.Navigate(action))

    def action_toggle(self):
        self.post_message(self.Toggle())

    def action_exit_search(self):
        self.post_message(self.Exit())

    def check_action(self, action, parameters):
        if action == 'search_cut' and parameters:
            expected = 'super+x' if sys.platform == 'darwin' else 'ctrl+x'
            return parameters[0] == expected
        if action == 'copy':
            # Screen owns copying, including table rows when text is unselected.
            return False
        return super().check_action(action, parameters)

    async def action_search_cut(self, key=None):
        if self.selection.is_empty:
            return
        selection, value = self.selection, self.value
        try:
            await copy_text(self.app, value[min(selection.start, selection.end):max(selection.start, selection.end)])
        except ClipboardError:
            self.notify('Could not copy selected text.', severity='error')
            return
        # Clipboard tools run asynchronously; never delete newly edited content.
        if self.value == value and self.selection == selection:
            self.delete_selection()

    async def _on_key(self, event: events.Key):
        movement = {'j': 'down', 'k': 'up', 'J': 'range_down', 'K': 'range_up',
                    'shift+j': 'range_down', 'shift+k': 'range_up'}
        if event.key in movement:
            self.action_navigate(movement[event.key])
            event.stop()
            event.prevent_default()
            return
        # The other platform's hidden cut shortcut must never reach native cut.
        if event.key in ('ctrl+x', 'super+x'):
            expected = 'super+x' if sys.platform == 'darwin' else 'ctrl+x'
            if event.key != expected:
                event.stop()
                event.prevent_default()
                return
        await super()._on_key(event)

    async def action_cut(self):
        # Native cut is never used, even if invoked by an inherited binding.
        await self.action_search_cut()


class SearchBar(Horizontal):
    marker = reactive('/')

    DEFAULT_CSS = """
    SearchBar { dock: bottom; width: 100%; height: 1; }
    SearchBar > .search-marker { width: 2; height: 1; }
    SearchBar > SearchInput, SearchBar > SearchInput:focus { width: 1fr; height: 1; border: none; padding: 0; }
    SearchBar > .search-exit { width: 9; height: 1; text-align: right; }
    """

    def __init__(self, marker='/', **kwargs):
        super().__init__(**kwargs)
        self.marker = marker

    def watch_marker(self, marker):
        if self.is_mounted:
            self.query_one('.search-marker', Static).update(Text(marker + ' '))

    def compose(self):
        yield Static(Text(self.marker + ' '), classes='search-marker')
        yield SearchInput(id='test-search-input')
        yield Static(Text.assemble(' ', ('Esc', 'yellow'), ' exit'), classes='search-exit')


class _ShortcutKey(Static):
    DEFAULT_CSS = '_ShortcutKey { width: 1; height: 1; }'

    def __init__(self, bracket, key, enabled):
        super().__init__(Text(bracket, style='bold yellow' if enabled else 'bold yellow dim'))
        self.key = key
        self.enabled = enabled

    def on_click(self, event):
        event.stop()
        if self.enabled:
            self.app.simulate_key(self.key)
        else:
            self.app.bell()


class GroupedUserFooter(Footer):
    """Preserve stock footer actions while combining related shortcut hints."""

    DEFAULT_CSS = """
    GroupedUserFooter .shortcut-pair { width: auto; height: 1; }
    GroupedUserFooter .shortcut-pair > Static { width: auto; height: 1; }
    GroupedUserFooter .user-cycle { padding-left: 1; }
    """

    def compose(self):
        widgets = list(super().compose())
        pairs = {getattr(widget, 'key', None): widget for widget in widgets}
        groups, hidden = {}, set()
        for keys, labels, description, css_class in (
            (('left_square_bracket', 'right_square_bracket'), ('[', ']'),
             ' Loop through users', 'user-cycle'),
            (('slash', 'question_mark'), ('/', '?'),
             ' Search tests (? backwards)', 'test-search-shortcuts'),
        ):
            left, right = (pairs.get(key) for key in keys)
            if left is None or right is None:
                continue
            anchor = next(widget for widget in widgets if widget in (left, right))
            groups[anchor] = (left, right, labels, description, css_class)
            hidden.update((left, right))
        self.styles.grid_size_columns -= len(groups)
        for widget in widgets:
            if widget in groups:
                left, right, labels, description, css_class = groups[widget]
                with Horizontal(classes='shortcut-pair ' + css_class):
                    yield _ShortcutKey(labels[0], left.key, not left.has_class('-disabled'))
                    yield Static(' / ')
                    yield _ShortcutKey(labels[1], right.key, not right.has_class('-disabled'))
                    yield Static(description)
            elif widget not in hidden:
                yield widget
