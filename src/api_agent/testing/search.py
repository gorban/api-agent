"""Regex projection of test selection without changing the underlying selection."""
import re

from .collection import CollectionError
from .selection import SelectionRow


class SearchSelection:
    def __init__(self, selection, direction=1):
        self.selection = selection
        self.direction = direction
        self.query = ""
        self.error = None
        self._pattern = re.compile("")

    def set_query(self, value):
        """Retain the last valid pattern when the new expression is invalid."""
        self.query = value
        try:
            pattern = re.compile(value)
        except re.error:
            self.error = "Invalid regular expression"
            return False
        self._pattern = pattern
        self.error = None
        return True

    def rows(self):
        groups = tuple(row for row in self.selection.groups() if row.kind != "all")
        direct = {row.id for row in groups if self._pattern.search(row.label)}
        visible = frozenset().union(*(row.members for row in groups if row.id in direct))
        rows = []
        for row in groups:
            members = row.members & visible
            if row.id not in direct and row.kind != "test":
                continue
            if not members:
                continue
            selected = members & self.selection.selected_ids
            rows.append(SelectionRow(row.id, row.label, row.kind, members,
                                     members <= self.selection.selected_ids,
                                     len(selected), len(members)))
        other = frozenset(self.selection.selected_ids) - visible
        if other:
            rows.append(SelectionRow("search:other", f"{len(other)} other tests", "other",
                                     other, True, len(other), len(other)))
        return tuple(rows)

    def toggle_many(self, keys):
        """Toggle each member of the projected row union exactly once."""
        by_key = {row.id: row for row in self.rows()}
        rows = []
        for key in keys:
            key = key.id if isinstance(key, SelectionRow) else key
            if key not in by_key:
                raise CollectionError(f"Unknown selection group or test: {key}")
            rows.append(by_key[key])
        if rows:
            members = frozenset().union(*(row.members for row in rows))
            self.selection.toggle_members(members, (row.id for row in rows
                                                    if row.kind in {"tag", "file"}))
