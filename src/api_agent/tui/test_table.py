"""Shared keyboard ranges for test pickers and result lists."""
from textual.binding import Binding
from textual.widgets import DataTable
from textual.strip import Strip
from rich.segment import Segment


class TestTable(DataTable):
    __test__ = False
    BINDINGS = [
        Binding('pageup', "move_plain('page_up')", show=False),
        Binding('pagedown', "move_plain('page_down')", show=False),
        Binding('ctrl+home', "move_plain('scroll_top')", show=False),
        Binding('ctrl+end', "move_plain('scroll_bottom')", show=False),
        Binding('home', "move_plain('scroll_home')", show=False),
        Binding('end', "move_plain('scroll_end')", show=False),
        Binding('shift+up', 'extend_range(-1)', show=False),
        Binding('shift+down', 'extend_range(1)', show=False),
        Binding('shift+pageup', 'extend_page(-1)', show=False),
        Binding('shift+pagedown', 'extend_page(1)', show=False),
        Binding('ctrl+shift+up,super+shift+up,shift+home', 'extend_range(-1, True)', show=False),
        Binding('ctrl+shift+down,super+shift+down,shift+end', 'extend_range(1, True)', show=False),
        Binding('ctrl+a,super+a', 'select_all_rows', show=False),
        Binding('escape', 'clear_range', show=False),
    ]

    def __init__(self, *args, sticky_timing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.sticky_timing = sticky_timing
        self.range_anchor = None
        self.range_end = None

    def _render_line(self, y, x1, x2, base_style):
        """Preview the final timing column without changing scroll geometry.

        Render through DataTable to preserve row/cell styles and mouse metadata.
        Once scrolling exposes its start, the normal column (including its long
        tail) takes over. Only the hidden right edge of preceding cells is marked.
        """
        normal = super()._render_line(y, x1, x2, base_style)
        if not self.sticky_timing or len(self.columns) != 3 or not normal.text.strip():
            return normal
        width = self.scrollable_content_region.width
        preview_width = len('Execution time') + 2 * self.cell_padding
        timing_start = sum(column.get_render_width(self) for column in self.ordered_columns[:-1])
        if width <= preview_width or timing_start - x1 <= width - preview_width:
            return normal
        boundary = width - preview_width
        edge = normal.crop(boundary - 1, boundary)
        ellipsis = Strip([Segment('…', next(iter(edge)).style)])
        preview = super()._render_line(y, timing_start, timing_start + preview_width,
                                       base_style).crop(0, preview_width)
        return Strip.join((normal.crop(0, boundary - 1), ellipsis, preview,
                           normal.crop(width)))

    @property
    def selected_row_indices(self):
        if not self.row_count:
            return ()
        if self.range_anchor is None:
            return (self.cursor_row,)
        return tuple(range(min(self.range_anchor, self.range_end),
                           max(self.range_anchor, self.range_end) + 1))

    @property
    def selected_row_keys(self):
        return tuple(self.ordered_rows[index].key.value for index in self.selected_row_indices)

    def _should_highlight(self, cursor, target_cell, type_of_cursor):
        if type_of_cursor == 'row' and self.range_anchor is not None:
            return target_cell.row in self.selected_row_indices
        return super()._should_highlight(cursor, target_cell, type_of_cursor)

    def action_extend_range(self, direction, to_edge=False):
        if not self.row_count:
            return
        if self.range_anchor is None:
            self.range_anchor = self.cursor_row
        destination = (0 if direction < 0 else self.row_count - 1) if to_edge else (
            max(0, min(self.row_count - 1, self.cursor_row + direction)))
        self.range_end = destination
        self.move_cursor(row=destination)
        self._clear_caches()
        self.refresh()

    def action_select_all_rows(self):
        if self.row_count:
            self.range_anchor = 0
            self.action_extend_range(1, True)

    def action_extend_page(self, direction):
        visible_rows = max(1, self.scrollable_content_region.height -
                           (self.header_height if self.show_header else 0))
        self.action_extend_range(direction * visible_rows)

    def action_clear_range(self):
        self.range_anchor = self.range_end = None
        self._clear_caches()
        self.refresh()

    def reset_moved_range(self):
        if self.range_end is not None and self.cursor_row != self.range_end:
            self.action_clear_range()

    def action_cursor_up(self):
        self.action_move_plain("cursor_up")

    def action_cursor_down(self):
        self.action_move_plain("cursor_down")

    def action_move_plain(self, action):
        # Bind before DataTable consumes the key, including clamped movement.
        self.action_clear_range()
        getattr(super(), 'action_' + action)()

    def clear(self, columns=False):
        self.action_clear_range()
        return super().clear(columns=columns)
