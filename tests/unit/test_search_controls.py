"""Mounted search editing and clickable footer behavior."""
import asyncio

import pytest
from textual.app import App
from textual.binding import Binding
from textual.events import Paste
from textual.widgets import Static
from textual.widgets._footer import FooterKey

from api_agent.tui import search


class Harness(App):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding('left_square_bracket', 'previous_account', 'Previous user'),
                Binding('right_square_bracket', 'next_account', 'Next user'),
                Binding('f2', 'other', 'Other')]

    def __init__(self, *, footer=True, marker='/'):
        super().__init__()
        self.footer, self.marker = footer, marker
        self.actions = []

    def compose(self):
        yield search.SearchBar(marker=self.marker)
        if self.footer:
            yield search.GroupedUserFooter()

    def on_mount(self):
        self.query_one(search.SearchInput).focus()

    def on_search_input_navigate(self, event):
        self.actions.append(event.action)

    def on_search_input_toggle(self):
        self.actions.append('toggle')

    def on_search_input_exit(self):
        self.actions.append('exit')

    def action_previous_account(self):
        self.actions.append('previous')

    def action_next_account(self):
        self.actions.append('next')

    def action_other(self):
        self.actions.append('other')


def test_native_edit_selection_replacement_and_paste():
    async def scenario():
        app = Harness(footer=False)
        async with app.run_test() as pilot:
            field = app.query_one(search.SearchInput)
            await pilot.press('h', 'l', 'space', 'a', 'b', 'c')
            assert field.value == 'hl abc'
            await pilot.press('shift+left', 'shift+left')
            assert field.selected_text == 'bc'
            await pilot.press('x')
            assert field.value == 'hl ax'
            await pilot.press('home', 'shift+end')
            assert field.selected_text == 'hl ax'
            field.post_message(Paste('regex.*'))
            await pilot.pause()
            assert field.value == 'regex.*'
            await pilot.press('left', 'shift+home')
            assert field.selected_text == 'regex.'
            await pilot.press('ctrl+a')
            assert field.selected_text == field.value
            await pilot.press('super+a')
            assert field.selected_text == field.value
            assert app.actions == []
    asyncio.run(scenario())


@pytest.mark.parametrize('key,action', [
    ('up', 'up'), ('k', 'up'), ('down', 'down'), ('j', 'down'),
    ('pageup', 'page_up'), ('pagedown', 'page_down'),
    ('shift+up', 'range_up'), ('shift+k', 'range_up'),
    ('shift+down', 'range_down'), ('shift+j', 'range_down'),
    ('shift+pageup', 'range_page_up'), ('shift+pagedown', 'range_page_down'),
    ('ctrl+shift+up', 'range_top'), ('super+shift+up', 'range_top'),
    ('ctrl+shift+down', 'range_bottom'), ('super+shift+down', 'range_bottom'),
    ('enter', 'toggle'), ('escape', 'exit')])
def test_navigation_messages(key, action):
    async def scenario():
        app = Harness(footer=False)
        async with app.run_test() as pilot:
            await pilot.press(key)
            assert app.actions == [action]
            assert app.query_one(search.SearchInput).value == ''
    asyncio.run(scenario())


@pytest.mark.parametrize('platform,key,wrong', [('linux', 'ctrl+x', 'super+x'),
                                             ('darwin', 'super+x', 'ctrl+x')])
def test_cut_requires_selection_success_and_platform(monkeypatch, platform, key, wrong):
    copied = []
    async def copy(app, text):
        copied.append(text)
        return False
    monkeypatch.setattr(search, 'copy_text', copy)
    monkeypatch.setattr(search.sys, 'platform', platform)
    async def scenario():
        app = Harness(footer=False)
        async with app.run_test() as pilot:
            field = app.query_one(search.SearchInput)
            field.value = 'abc'
            field.cursor_position = 3
            await pilot.press(key)
            assert not copied and field.value == 'abc'
            await pilot.press('shift+left', 'shift+left', wrong)
            assert not copied and field.value == 'abc'
            await pilot.press(key)
            assert copied == ['bc'] and field.value == 'a'
    asyncio.run(scenario())


def test_failed_cut_preserves_selection(monkeypatch):
    async def copy(app, text):
        raise search.ClipboardError()
    monkeypatch.setattr(search, 'copy_text', copy)
    async def scenario():
        app = Harness(footer=False)
        async with app.run_test() as pilot:
            field = app.query_one(search.SearchInput)
            field.value = 'abc'
            await pilot.press('ctrl+a')
            await field.action_search_cut()
            assert field.value == 'abc' and field.selected_text == 'abc'
    asyncio.run(scenario())


def test_search_bar_layout_and_rich_esc():
    async def scenario():
        app = Harness(footer=False, marker='?')
        async with app.run_test(size=(70, 10)) as pilot:
            await pilot.pause()
            bar = app.query_one(search.SearchBar)
            field = bar.query_one(search.SearchInput)
            assert bar.region.width == 70 and bar.region.height == 1
            assert field.region.height == 1 and field.styles.border.top[0] == ''
            decorations = bar.query(Static)
            assert all(not decoration.can_focus for decoration in decorations)
            hint = decorations.last().render()
            assert hint.plain == ' Esc exit'
            assert [(span.start, span.end) for span in hint.spans] == [(1, 4)]
    asyncio.run(scenario())


def test_grouped_brackets_color_and_clicks_preserve_other_footer():
    async def scenario():
        app = Harness()
        async with app.run_test() as pilot:
            app.screen.set_focus(None)
            await pilot.pause()
            footer = app.query_one(search.GroupedUserFooter)
            brackets = list(footer.query(search._ShortcutKey))
            assert [widget.render().plain for widget in brackets] == ['[', ']']
            stock_style = footer.query_one(FooterKey).get_component_rich_style('footer-key--key')
            assert all(widget.rich_style.color == stock_style.color
                       and widget.rich_style.bgcolor == stock_style.bgcolor
                       and widget.rich_style.bold for widget in brackets)
            group = footer.query_one('.user-cycle')
            assert brackets[0].region.x == group.region.x + 1
            assert ''.join(widget.render().plain for widget in group.query(Static)) == '[ / ] Loop through users'
            assert any(getattr(widget, 'action', None) == 'other' for widget in footer.children)
            assert footer.styles.grid_size_columns == 2
            await pilot.click(brackets[0])
            await pilot.click(brackets[1])
            assert app.actions == ['previous', 'next']
            await pilot.press('f2')
            assert app.actions[-1] == 'other'
    asyncio.run(scenario())


def test_cut_does_not_delete_content_edited_during_copy(monkeypatch):
    async def copy(app, text):
        field = app.query_one(search.SearchInput)
        field.value = 'new text'
    monkeypatch.setattr(search, 'copy_text', copy)
    async def scenario():
        app = Harness(footer=False)
        async with app.run_test() as pilot:
            field = app.query_one(search.SearchInput)
            field.value = 'old text'
            await pilot.press('ctrl+a')
            await field.action_cut()
            assert field.value == 'new text'
            assert field.check_action('copy', ()) is False
    asyncio.run(scenario())


def test_disabled_bracket_does_not_dispatch():
    class Disabled(Harness):
        def check_action(self, action, parameters):
            if action == 'previous_account':
                return None
            return True
    async def scenario():
        app = Disabled()
        async with app.run_test() as pilot:
            app.screen.set_focus(None)
            await pilot.pause()
            brackets = list(app.query(search._ShortcutKey))
            assert brackets[0].enabled is False and brackets[1].enabled is True
            assert brackets[0].rich_style.bold and brackets[0].rich_style.dim
            assert not brackets[1].rich_style.dim
            await pilot.click(brackets[0])
            assert app.actions == []
            await pilot.click(brackets[1])
            assert app.actions == ['next']
    asyncio.run(scenario())



def test_search_marker_updates_after_mount():
    async def scenario():
        app = Harness(footer=False)
        async with app.run_test() as pilot:
            bar = app.query_one(search.SearchBar)
            assert bar.query_one('.search-marker', Static).render().plain == '/ '
            bar.marker = '?'
            await pilot.pause()
            assert bar.query_one('.search-marker', Static).render().plain == '? '
            bar.marker = '/'
            await pilot.pause()
            assert bar.query_one('.search-marker', Static).render().plain == '/ '
    asyncio.run(scenario())


@pytest.mark.parametrize('reverse_order', [False, True])
def test_search_shortcuts_share_label_and_preserve_both_actions(reverse_order):
    class SearchHarness(Harness):
        BINDINGS = ([Binding('question_mark', 'search_backward', 'Search tests from bottom'),
                     Binding('slash', 'search_forward', 'Search tests')]
                    if reverse_order else
                    [Binding('slash', 'search_forward', 'Search tests'),
                     Binding('question_mark', 'search_backward', 'Search tests from bottom')])

        def action_search_forward(self):
            self.actions.append('forward')

        def action_search_backward(self):
            self.actions.append('backward')

    async def scenario():
        app = SearchHarness()
        async with app.run_test(size=(120, 12)) as pilot:
            app.screen.set_focus(None)
            await pilot.pause()
            footer = app.query_one(search.GroupedUserFooter)
            group = footer.query_one('.test-search-shortcuts')
            assert ''.join(widget.render().plain for widget in group.query(Static)) == '/ / ? Search tests (? backwards)'
            keys = list(group.query(search._ShortcutKey))
            assert [key.render().plain for key in keys] == ['/', '?']
            colors = []
            for theme in ('textual-dark', 'textual-light', 'nord'):
                app.theme = theme
                await pilot.pause()
                stock_style = footer.query_one(FooterKey).get_component_rich_style('footer-key--key')
                colors.append(stock_style.color)
                assert all(key.rich_style.color == stock_style.color
                           and key.rich_style.bgcolor == stock_style.bgcolor
                           and key.rich_style.bold for key in footer.query(search._ShortcutKey))
            assert len(set(colors)) > 1
            assert footer.styles.grid_size_columns == 3  # User pair, search pair, Other.
            await pilot.click(keys[0])
            await pilot.click(keys[1])
            assert app.actions == ['forward', 'backward']
    asyncio.run(scenario())
