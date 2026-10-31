"""Global picker rows with cells rendered only when Textual requests them."""
from rich.cells import cell_len
from textual._two_way_dict import TwoWayDict
from textual.widgets import DataTable
from textual.widgets._data_table import CellDoesNotExist, ColumnKey, Row, RowKey, RowDoesNotExist

from .test_results import literal_cell
from .test_table import TestTable


class PickerRows(tuple):
    """Rows paired with the estimator snapshot that produced their display."""
    def __new__(cls, rows, estimator):
        result = super().__new__(cls, rows)
        result.estimator = estimator
        return result


class VirtualPicker(TestTable):
    """Global logical rows with lazy cells for the fixed picker columns.

    The workbench owns four fixed-width base columns and may add or remove
    the final estimated-cost column. Results use the regular TestTable.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._picker_rows = ()
        self._picker_by_key = {}
        self._picker_overrides = {}
        self._label_widths = {}

    def set_picker_rows(self, rows, *, reset_range=False):
        self._picker_estimator = getattr(rows, 'estimator', self.app.cost_estimator)
        rows = tuple(rows)
        keys = [RowKey(row.id) for row in rows]
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate picker row identity')
        if reset_range or tuple(row.id for row in self._picker_rows) != tuple(row.id for row in rows):
            self.action_clear_range()
        # Measure each distinct bounded label once, without generating cells or costs.
        from .app import picker_label
        widths = self._label_widths
        for row in rows:
            if row.label not in widths:
                widths[row.label] = cell_len(picker_label(row.label))
        self.ordered_columns[1].width = max(14, max((widths[row.label] for row in rows), default=0))
        self._picker_overrides.clear()
        self._picker_rows = rows
        self._picker_by_key = {key: row for key, row in zip(keys, rows)}
        self.rows = {key: Row(key, 1) for key in keys}
        self._row_locations = TwoWayDict({key: index for index, key in enumerate(keys)})
        self._data.clear()
        self._new_rows.clear()
        self._updated_cells.clear()
        self._update_count += 1
        self._clear_caches()
        self._require_update_dimensions = True
        self.cursor_coordinate = self.cursor_coordinate
        self.check_idle()
        self.refresh(layout=True)

    def get_row(self, row_key):
        try:
            row = self._picker_by_key[row_key]
        except KeyError:
            raise RowDoesNotExist(f'Row key {row_key!r} is not valid.') from None
        cells = [literal_cell(cell) for cell in self.app.selection_cells(row, self._picker_estimator)]
        return [self._picker_overrides.get((row_key, key), value)
                for key, value in zip(self._column_locations, cells)]

    def get_cell(self, row_key, column_key):
        index = self._column_locations.get(column_key)
        if index is None:
            raise CellDoesNotExist(f'Column key {column_key!r} is not valid.')
        return self.get_row(row_key)[index]

    def _should_highlight(self, cursor, target_cell, type_of_cursor):
        if type_of_cursor == 'row' and self.range_anchor is not None:
            return min(self.range_anchor, self.range_end) <= target_cell.row <= max(self.range_anchor, self.range_end)
        return super()._should_highlight(cursor, target_cell, type_of_cursor)

    def _highlight_row(self, row_index):
        self.refresh_row(row_index)
        if self.is_valid_row_index(row_index):
            self.post_message(DataTable.RowHighlighted(self, row_index, self._row_locations.get_key(row_index)))

    def _post_selected_message(self):
        if not self.row_count:
            return
        coordinate = self.cursor_coordinate
        cell_key = self.coordinate_to_cell_key(coordinate)
        if self.cursor_type == 'cell':
            self.post_message(DataTable.CellSelected(self, self.get_cell_at(coordinate), coordinate=coordinate, cell_key=cell_key))
        elif self.cursor_type == 'row':
            self.post_message(DataTable.RowSelected(self, coordinate.row, cell_key.row_key))
        elif self.cursor_type == 'column':
            self.post_message(DataTable.ColumnSelected(self, coordinate.column, cell_key.column_key))

    def add_column(self, *args, **kwargs):
        # The base implementation fills every existing cell; picker cells are lazy.
        rows, self.rows = self.rows, {}
        try:
            return super().add_column(*args, **kwargs)
        finally:
            self.rows = rows

    def update_cell(self, row_key, column_key, value, *, update_width=False):
        row_key = RowKey(row_key) if isinstance(row_key, str) else row_key
        column_key = ColumnKey(column_key) if isinstance(column_key, str) else column_key
        if row_key not in self._row_locations or column_key not in self._column_locations:
            raise CellDoesNotExist(f'No cell exists for row_key={row_key!r}, column_key={column_key!r}.')
        # Picker columns have fixed widths, so update_width needs no resizing.
        self._picker_overrides[row_key, column_key] = value
        self._update_count += 1
        self.refresh()

    def clear(self, columns=False):
        self._picker_rows = ()
        self._picker_by_key.clear()
        self._picker_overrides.clear()
        self._label_widths.clear()
        return super().clear(columns=columns)
