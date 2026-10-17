"""Configuration descriptions come from declarations, without mutating values."""
import asyncio
from dataclasses import replace
import pytest
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from api_agent.config import load_shared_schema
from api_agent.config.declarations import UNSET
from api_agent.tui.app import SettingHelpDialog, WorkbenchScreen
from test_tui import fixture, setting_row


@pytest.mark.parametrize('mode', [None, 'config-cleanup'])
@pytest.mark.parametrize('close', ['escape', 'c', 'button', 'enter'])
def test_setting_description_popup_preserves_selected_row_and_config(tmp_path, mode, close):
    async def scenario():
        description = '[red]Literal declaration text[/red]\n' + '\n'.join(f'Explanation line {i}' for i in range(60))
        fields = {f'SETTING_{i}': {'type': 'str', 'default': 'value',
                  'help': description if i == 10 else ''} for i in range(20)}
        app = fixture(tmp_path, mode, fields)
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one(DataTable)
            row = setting_row(table, "SETTING_10")
            table.move_cursor(row=row)
            await pilot.pause()
            original_values = app.session.result(_review=True).values.copy()
            assert screen.check_action('setting_help', ())
            await pilot.press('?')
            await pilot.pause()
            assert isinstance(app.screen, SettingHelpDialog)
            assert str(app.screen.query_one('#setting-help-title', Static).content) == 'SETTING_10'
            assert str(app.screen.query_one('#setting-help-text', Static).content) == description
            assert not screen.check_action('setting_help', ())
            scroll = app.screen.query_one(VerticalScroll)
            close_button = app.screen.query_one('#setting-help-close')
            assert close_button.has_focus
            assert app.screen.focus_chain == [close_button]
            assert not app.screen.query('#tab-select-hint')
            for key in ('tab', 'tab', 'shift+tab'):
                await pilot.press(key)
                assert close_button.has_focus
            await pilot.press('j', 'down')
            await pilot.pause()
            assert scroll.scroll_y > 0
            assert close_button.has_focus
            await pilot.press('pagedown', 'pageup', 'k', 'up', 'h', 'left', 'l', 'right')
            await pilot.pause()
            assert close_button.has_focus
            await pilot.resize_terminal(65, 18)
            await pilot.pause()
            dialog = app.screen.query_one('#setting-help-dialog')
            assert dialog.region.bottom <= app.screen.size.height
            await pilot.resize_terminal(65, 12)
            await pilot.pause()
            close_button = app.screen.query_one('#setting-help-close')
            assert close_button.region.bottom <= dialog.content_region.bottom
            if close == 'button':
                assert await pilot.click('#setting-help-close')
            else:
                await pilot.press(close)
            await pilot.pause()
            assert app.screen is screen
            assert table.cursor_row == row and table.has_focus
            assert app.session.result(_review=True).values == original_values
            assert not (tmp_path / '.env').exists()
            table.move_cursor(row=row + 1)
            await pilot.pause()
            assert screen.selected_setting_help() is None
            assert not screen.check_action('setting_help', ())
            await pilot.press('?')
            screen.action_setting_help()
            await pilot.pause()
            assert app.screen is screen
            table.clear()
            assert screen.selected_setting_help() is None
    asyncio.run(scenario())


def test_setting_description_unavailable_without_table_or_outside_config(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert app.screen.selected_setting_help() is None
            assert not app.screen.check_action('setting_help', ())
            # An unmounted configuration screen has no selected table yet.
            assert WorkbenchScreen('defaults', 'Empty configuration').selected_setting_help() is None
    asyncio.run(scenario())


def test_optional_recording_destinations_are_reviewable_without_becoming_required(tmp_path):
    async def scenario():
        app = fixture(tmp_path)
        schema = load_shared_schema()
        names = ('ALLURE_SECONDARY_DIR', 'TOKEN_USAGE_CSV')
        for name in names:
            field = schema.fields[name]
            assert field.review_optional and field.acknowledge_default
            assert not field.required and field.default is UNSET
            assert 'When unset' in field.help
        # Exercise the real metadata through the TUI without unrelated settings.
        app.schema = replace(app.schema, fields={**app.schema.fields,
                            **{name: schema.fields[name] for name in names},
                            'COLLECT_ONLY': schema.fields['COLLECT_ONLY'],
                            'ALLURE_VIEW_SECONDARY': replace(schema.fields['ALLURE_VIEW_SECONDARY'],
                                                             acknowledge_default=False),
                            'TOKEN_USAGE_CSV_ENABLED': replace(schema.fields['TOKEN_USAGE_CSV_ENABLED'],
                                                               acknowledge_default=False)})
        app.session.schema = app.schema
        app.context.anchors['owner_root'] = tmp_path
        async with app.run_test() as pilot:
            await pilot.pause()
            assert set(app.configuration_pending()) == set(names)
            table = app.screen.query_one(DataTable)
            assert [table.get_row_at(i)[0] for i in range(table.row_count)] == list(names)
            for name in names:
                assert name not in app.session.result(_review=True).missing
                assert name not in app.session.result(_review=True).fields
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert not (tmp_path / '.env').exists()
    asyncio.run(scenario())
