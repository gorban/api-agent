"""Real headless key dispatch for shared single-line editing."""
import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Footer
from textual._xterm_parser import XTermParser

from api_agent.tui.editable_input import EditableInput
from api_agent.tui.auth_setup import AuthInput
from api_agent.tui.search import SearchInput
from api_agent.tui.manager_recovery import _PrivateInput


class Editor(App):
    def compose(self) -> ComposeResult:
        yield EditableInput('alpha beta gamma', select_on_focus=False)
        yield Footer()


@pytest.mark.parametrize('key,position,selected', [
    ('alt+shift+left', 16, 'gamma'),
    ('alt+shift+b', 16, 'gamma'),
    ('alt+shift+right', 0, 'alpha '),
    ('alt+shift+f', 0, 'alpha '),
    ('super+shift+left', 16, 'alpha beta gamma'),
    ('alt+shift+home', 16, 'alpha beta gamma'),
    ('super+shift+right', 0, 'alpha beta gamma'),
    ('alt+shift+end', 0, 'alpha beta gamma'),
    ('shift+home', 16, 'alpha beta gamma'),
    ('shift+end', 0, 'alpha beta gamma'),
    ('ctrl+g', 7, 'alpha beta gamma'),
    ('super+a', 7, 'alpha beta gamma'),
])
def test_selection_replaces_exact_range(key, position, selected):
    async def run():
        app = Editor()
        async with app.run_test() as pilot:
            field = app.query_one(EditableInput)
            field.cursor_position = position
            await pilot.press(key)
            left, right = sorted(field.selection)
            assert field.value[left:right] == selected
            original = field.value
            await pilot.press('x')
            assert field.value == original[:left] + 'x' + original[right:]
    asyncio.run(run())


@pytest.mark.parametrize('key,start,end', [
    ('alt+left', 16, 11), ('alt+b', 16, 11),
    ('alt+right', 0, 6), ('alt+f', 0, 6),
    ('super+left', 16, 0), ('super+right', 0, 16),
])
def test_unshifted_movement_does_not_select_or_edit(key, start, end):
    async def run():
        app = Editor()
        async with app.run_test() as pilot:
            field = app.query_one(EditableInput)
            field.cursor_position = start
            await pilot.press(key)
            assert field.cursor_position == end and field.selection.is_empty
            assert field.value == 'alpha beta gamma'
    asyncio.run(run())


@pytest.mark.parametrize('sequence,key', [
    ('\x1b[1;4D', 'alt+shift+left'),
    ('\x1b[1;4C', 'alt+shift+right'),
    ('\x1b[1;2H', 'shift+home'),
    ('\x1b[1;2F', 'shift+end'),
])
def test_terminal_modified_key_sequence_preserves_selection_modifier(sequence, key):
    parser = XTermParser(debug=False)
    events = list(parser._sequence_to_key_events(sequence))
    assert [event.key for event in events] == [key]


@pytest.mark.parametrize('field_type', [EditableInput, AuthInput, SearchInput, _PrivateInput])
def test_all_editors_advertise_portable_select_all(field_type):
    class Specialized(Editor):
        def compose(self):
            yield field_type(value='long value', id='value')
            yield Footer()
    async def run():
        app = Specialized()
        async with app.run_test() as pilot:
            field = app.query_one('#value')
            await pilot.press('ctrl+g')
            assert sorted(field.selection) == [0, len(field.value)]
            binding = app.active_bindings['ctrl+g'].binding
            assert binding.show and binding.description == 'Select all'
    asyncio.run(run())


@pytest.mark.parametrize('password', [False, True])
@pytest.mark.parametrize('key', ['super+c', 'ctrl+x'])
def test_selection_copy_and_cut_do_not_export_masked_values(password, key):
    class PrivateEditor(Editor):
        def compose(self):
            yield EditableInput('field value', password=password)
    async def run():
        app = PrivateEditor()
        async with app.run_test() as pilot:
            app.copy_to_clipboard('existing clipboard')
            await pilot.press('ctrl+g', key)
            assert app.clipboard == ('existing clipboard' if password else 'field value')
            assert app.query_one(EditableInput).value == ('' if key == 'ctrl+x' else 'field value')
    asyncio.run(run())
