"""Preserve a configuration table's visual anchor across row rebuilds."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Viewport:
    keys: tuple[str, ...]
    index: int
    screen_y: float
    scroll_x: float
    marked_keys: tuple[str, ...] = ()


def capture_viewport(table):
    """Capture the highlighted row's identity and position, in table lines."""
    rows = table.ordered_rows
    if not rows:
        return None
    index = min(table.cursor_row, len(rows) - 1)
    header = table.header_height if table.show_header else 0
    return Viewport(tuple(row.key.value for row in rows), index,
                    table.scrollable_content_region.y + header
                    + sum(row.height for row in rows[:index]) - table.scroll_y,
                    table.scroll_x, tuple(getattr(table, 'marked_keys', ())))


def restore_viewport(table, viewport):
    """Restore after layout, preferring the next survivor if the anchor vanished."""
    if viewport is None or not table.row_count:
        return
    keys = [row.key.value for row in table.ordered_rows]
    candidates = (viewport.keys[viewport.index:] +
                  tuple(reversed(viewport.keys[:viewport.index])))
    anchor = next((key for key in candidates if key in keys), None)
    index = keys.index(anchor) if anchor is not None else min(viewport.index, len(keys) - 1)
    table.move_cursor(row=index, scroll=False)
    if hasattr(table, 'set_marked_keys'):
        table.set_marked_keys(viewport.marked_keys)
    token = object()
    table._configuration_viewport_token = token

    def apply():
        if table._configuration_viewport_token is not token or not table.row_count:
            return
        rows = table.ordered_rows
        # A smaller terminal may no longer have room for the original offset.
        region = table.scrollable_content_region
        header = table.header_height if table.show_header else 0
        height = region.height - header
        offset = min(max(viewport.screen_y - region.y - header, 0), max(height - 1, 0))
        top = sum(row.height for row in rows[:table.cursor_row])
        table.scroll_to(x=viewport.scroll_x, y=top - offset, animate=False, immediate=True)

    table.call_after_refresh(apply)
