"""A changing read-only widget must not break screen selection copying."""

import pytest
from textual.app import App, ComposeResult
from textual.geometry import Offset
from textual.screen import Screen
from textual.selection import Selection
from textual.containers import Vertical
from textual.widgets import Static

from api_agent.tui.app import SelectedTextCopy


class SelectionScreen(SelectedTextCopy, Screen):
    def compose(self) -> ComposeResult:
        yield Static("first\nsecond\nthird", id="changing")
        yield Static("neighbor", id="neighbor")
        yield Vertical(id="control")


class SelectionApp(App):
    def on_mount(self):
        self.push_screen(SelectionScreen())


@pytest.mark.asyncio
async def test_stale_open_selection_preserves_adjacent_text():
    app = SelectionApp()
    async with app.run_test() as pilot:
        screen = app.screen
        changing = screen.query_one("#changing", Static)
        neighbor = screen.query_one("#neighbor", Static)
        changing.update("first\nsecond")
        screen.selections = {
            changing: Selection(Offset(0, 2), None),
            neighbor: Selection(Offset(0, 0), None),
        }
        await pilot.pause()

        with pytest.raises(IndexError):
            Screen.get_selected_text(screen)
        assert screen.get_selected_text() == "neighbor"
        assert screen.check_copy_action()


@pytest.mark.asyncio
async def test_live_open_selection_still_extracts_text():
    app = SelectionApp()
    async with app.run_test() as pilot:
        screen = app.screen
        changing = screen.query_one("#changing", Static)
        screen.selections = {changing: Selection(Offset(0, 2), None)}
        await pilot.pause()

        assert screen.get_selected_text() == "third"


@pytest.mark.asyncio
async def test_empty_detached_and_nontext_selections():
    app = SelectionApp()
    async with app.run_test() as pilot:
        screen = app.screen
        neighbor = screen.query_one("#neighbor", Static)
        control = screen.query_one("#control", Vertical)
        assert screen.get_selected_text() is None

        screen.selections = {
            Static("detached"): Selection(None, None),
            control: Selection(None, None),
            neighbor: Selection(Offset(0, 0), Offset(4, 0)),
        }
        await pilot.pause()

        assert screen.get_selected_text() == "neig"


@pytest.mark.asyncio
async def test_stale_only_selection_does_not_copy_empty_value():
    app = SelectionApp()
    async with app.run_test() as pilot:
        screen = app.screen
        changing = screen.query_one("#changing", Static)
        changing.update("first\nsecond")
        screen.selections = {changing: Selection(Offset(0, 2), None)}
        await pilot.pause()

        assert screen.get_selected_text() is None
        assert not screen.check_copy_action()
