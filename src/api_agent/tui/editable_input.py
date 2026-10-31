"""Shared text selection without depending on terminal-specific Home/End keys."""
from textual.binding import Binding
from textual.widgets import Input


class EditableInput(Input):
    """Retain native editing and add Mac modifiers and a portable select-all key."""

    BINDINGS = [
        Binding('ctrl+g', 'select_all', 'Select all', key_display='^g'),
        Binding('super+a', 'select_all', show=False),
        Binding('alt+left,alt+b', 'cursor_left_word', show=False),
        Binding('alt+right,alt+f', 'cursor_right_word', show=False),
        Binding('alt+shift+left,alt+shift+b', 'cursor_left_word(True)', show=False),
        Binding('alt+shift+right,alt+shift+f', 'cursor_right_word(True)', show=False),
        Binding('super+left', 'home', show=False),
        Binding('super+right', 'end', show=False),
        Binding('super+shift+left,alt+shift+home', 'home(True)', show=False),
        Binding('super+shift+right,alt+shift+end', 'end(True)', show=False),
    ]

    def action_copy(self):
        if not self.password:
            super().action_copy()

    def action_cut(self):
        if self.password:
            self.delete_selection()
        else:
            super().action_cut()
