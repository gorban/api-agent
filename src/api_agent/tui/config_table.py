"""Stable marked rows and keyboard ranges for configuration actions."""
from textual.binding import Binding
from textual.message import Message

from .test_table import TestTable


class ConfigTable(TestTable):
    BINDINGS = [Binding('space', 'mark_cursor', show=False),
                Binding('escape', 'clear_marks', show=False)]

    class SelectionChanged(Message):
        pass

    def __init__(self, *args, **kwargs):
        self._marked_keys = set()
        super().__init__(*args, **kwargs)

    @property
    def marked_keys(self):
        ranged = super().selected_row_keys if self.range_anchor is not None else ()
        return self._marked_keys | set(ranged)

    @property
    def selection_explicit(self):
        return bool(self._marked_keys) or self.range_anchor is not None

    @property
    def selected_row_keys(self):
        if self.selection_explicit:
            marked = self.marked_keys
            visible = tuple(row.key.value for row in self.ordered_rows if row.key.value in marked)
            return visible + tuple(sorted(marked - set(visible)))
        return super().selected_row_keys

    def _changed(self):
        self._clear_caches()
        self.refresh()
        self.post_message(self.SelectionChanged())

    def set_marked_keys(self, keys):
        self._marked_keys = set(keys)
        self.range_anchor = self.range_end = None
        self._changed()

    def action_mark_cursor(self):
        keys = set(super().selected_row_keys)
        if keys <= self._marked_keys:
            self._marked_keys.difference_update(keys)
        else:
            self._marked_keys.update(keys)
        self.range_anchor = self.range_end = None
        self._changed()

    def action_clear_marks(self):
        self._marked_keys.clear()
        self.action_clear_range()

    def action_clear_range(self):
        super().action_clear_range()
        self._changed()

    def action_extend_range(self, direction, to_edge=False):
        super().action_extend_range(direction, to_edge)
        self._changed()

    def clear(self, columns=False):
        self._marked_keys.clear()
        return super().clear(columns=columns)

    def _should_highlight(self, cursor, target_cell, type_of_cursor):
        if (type_of_cursor == 'row' and 0 <= target_cell.row < self.row_count
                and self.ordered_rows[target_cell.row].key.value in self._marked_keys):
            return True
        return super()._should_highlight(cursor, target_cell, type_of_cursor)

    async def _on_click(self, event):
        row = event.style.meta.get('row')
        if (event.ctrl or event.shift) and type(row) is int and 0 <= row < self.row_count:
            if event.shift:
                if self.range_anchor is None:
                    self.range_anchor = self.cursor_row
                self.range_end = row
                self.move_cursor(row=row)
                self._changed()
            else:
                self.move_cursor(row=row)
                self.range_anchor = self.range_end = None
                self.action_mark_cursor()
            event.stop()
            return
        self.action_clear_range()
        await super()._on_click(event)
