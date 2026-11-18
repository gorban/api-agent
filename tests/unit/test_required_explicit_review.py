"""Settings that block execution are reviewable even when they can never be a saved default."""
import asyncio
from unittest.mock import Mock

import pytest
from rich.text import Text
from textual.widgets import DataTable, Input, Static

from api_agent.tui.config_bulk import plan_review
from test_tui import fixture

HELP = 'Path to a file holding the example credential.'
REQUIRED_MODES = ['run-local', 'integration']
FIELDS = {
    'VISIBLE': {'type': 'int', 'default': 8},
    'EXAMPLE_SECRET_FILE': {'type': 'path', 'anchor': 'owner_root', 'acknowledge_default': False, 'help': HELP,
                            'required_when': {'TUI_MODE': REQUIRED_MODES}},
    'EXAMPLE_OPTIONAL_FILE': {'type': 'path', 'anchor': 'owner_root', 'acknowledge_default': False},
}


@pytest.fixture(autouse=True)
def restore_private_input_filters(monkeypatch):
    from textual import Logger
    from textual._xterm_parser import XTermParser
    monkeypatch.setattr(Logger, '__call__', Logger.__call__)
    monkeypatch.setattr(XTermParser, 'debug_log', XTermParser.debug_log)


def row_names(app):
    return [row.key.value for row in app.screen.query_one(DataTable).ordered_rows]


async def open_review(app, pilot):
    await pilot.pause()
    if app.screen.kind == 'suggest':
        await app.choose('skip')
        await pilot.pause()
    assert app.screen.kind == 'defaults'


async def edit_row(app, pilot, name, value):
    table = app.screen.query_one(DataTable)
    table.move_cursor(row=row_names(app).index(name))
    await pilot.press('e')
    await pilot.pause()
    assert app.screen.kind == 'input'
    app.screen.query_one(Input).value = value
    await pilot.click('#submit')
    await pilot.pause()


def test_blocking_explicit_only_setting_is_listed_as_required_and_not_provided(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'run-local', fields=FIELDS)
        async with app.run_test() as pilot:
            await open_review(app, pilot)
            table = app.screen.query_one(DataTable)
            assert row_names(app) == ['EXAMPLE_SECRET_FILE', 'VISIBLE']
            assert table.get_row('EXAMPLE_SECRET_FILE')[1] == Text('Required', justify='right')
            assert table.get_row('EXAMPLE_SECRET_FILE')[2] == 'Not provided'
            assert 'Required settings: EXAMPLE_SECRET_FILE' in str(app.screen.query_one('#body', Static).content)
            assert str(app.screen.query_one('#error', Static).content) == (
                'EXAMPLE_SECRET_FILE required for TUI_MODE=run-local.')
    asyncio.run(scenario())


def test_explicit_only_setting_that_is_not_blocking_stays_out_of_review(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'unit', fields=FIELDS)
        async with app.run_test() as pilot:
            await open_review(app, pilot)
            assert row_names(app) == ['VISIBLE']
            assert app.configuration_pending() == {'VISIBLE'}
    asyncio.run(scenario())


def test_explicit_only_setting_required_for_another_operation_stays_out_of_review(tmp_path):
    async def scenario():
        fields = FIELDS | {'EXAMPLE_OTHER_FILE': {
            'type': 'path', 'anchor': 'owner_root', 'acknowledge_default': False, 'required': True,
            'applies_when': {'TUI_MODE': 'e2e'}}}
        app = fixture(tmp_path, 'unit', fields=fields)
        assert 'EXAMPLE_OTHER_FILE' in app.session.result(_review=True).missing
        assert 'EXAMPLE_OTHER_FILE' not in app.session.result().missing
        async with app.run_test() as pilot:
            await open_review(app, pilot)
            assert row_names(app) == ['VISIBLE']
            assert app.configuration_pending() == {'VISIBLE'}
    asyncio.run(scenario())


def test_editing_the_required_row_saves_an_explicit_value_and_clears_the_blocker(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'run-local', fields=FIELDS)
        value = str(tmp_path / 'credential.txt')
        async with app.run_test() as pilot:
            await open_review(app, pilot)
            await edit_row(app, pilot, 'EXAMPLE_SECRET_FILE', value)
            assert app.screen.kind == 'defaults'
            assert row_names(app) == ['VISIBLE']
            assert 'Required settings:' not in str(app.screen.query_one('#body', Static).content)
            assert str(app.screen.query_one('#error', Static).content) == ''
            result = app.session.result()
            assert result.missing == ()
            assert str(result.values['EXAMPLE_SECRET_FILE']) == value
            assert result.fields['EXAMPLE_SECRET_FILE'].explicit
            assert 'EXAMPLE_SECRET_FILE' in (tmp_path / '.env').read_text()
            assert 'EXAMPLE_SECRET_FILE' not in {item.name for item in app.session.default_candidates()}
    asyncio.run(scenario())


def test_accept_key_never_saves_the_required_explicit_only_setting(tmp_path):
    async def scenario():
        app = fixture(tmp_path, 'run-local', fields=FIELDS)
        async with app.run_test() as pilot:
            await open_review(app, pilot)
            app.screen.query_one(DataTable).move_cursor(row=row_names(app).index('EXAMPLE_SECRET_FILE'))
            await pilot.press('a')
            await pilot.pause()
            assert app.session.result().missing == ('EXAMPLE_SECRET_FILE',)
            assert not (tmp_path / '.env').exists()
    asyncio.run(scenario())


def test_bulk_accept_skips_the_required_explicit_only_setting_but_saves_defaults(tmp_path):
    app = fixture(tmp_path, 'run-local', fields=FIELDS)
    snapshot = app.session.review_snapshot()

    plan = plan_review(snapshot, ('EXAMPLE_SECRET_FILE', 'VISIBLE'), 'accept')

    assert plan.names == ('VISIBLE',)
    assert plan.skipped == ('EXAMPLE_SECRET_FILE',)


def test_bulk_accept_skips_an_explicit_only_setting_once_it_has_a_value(tmp_path):
    app = fixture(tmp_path, 'run-local', fields=FIELDS)
    app.session.set_value('EXAMPLE_SECRET_FILE', str(tmp_path / 'credential.txt'), persist=True)

    plan = plan_review(app.session.review_snapshot(), ('EXAMPLE_SECRET_FILE',), 'accept')

    assert plan.names == ()
    assert plan.skipped == ('EXAMPLE_SECRET_FILE',)


def test_bulk_accept_skips_explicit_only_settings_that_have_a_declared_default(tmp_path):
    fields = FIELDS | {'EXAMPLE_PINNED': {'type': 'int', 'default': 3, 'acknowledge_default': False}}
    app = fixture(tmp_path, 'run-local', fields=fields)

    plan = plan_review(app.session.review_snapshot(), ('EXAMPLE_PINNED', 'VISIBLE'), 'accept')

    assert plan.names == ('VISIBLE',)
    assert plan.skipped == ('EXAMPLE_PINNED',)


def test_bulk_accept_skips_a_default_that_is_already_explicit(tmp_path):
    app = fixture(tmp_path, 'run-local', fields=FIELDS)
    app.session.set_value('VISIBLE', '5', persist=True)

    plan = plan_review(app.session.review_snapshot(), ('VISIBLE',), 'accept')

    assert plan.names == ()
    assert plan.skipped == ('VISIBLE',)


def test_error_screen_names_the_help_and_offers_review_configuration(tmp_path):
    async def scenario():
        collector = Mock(return_value=())
        app = fixture(tmp_path, fields={'EXAMPLE_SECRET_FILE': FIELDS['EXAMPLE_SECRET_FILE']},
                      collector=collector)
        value = str(tmp_path / 'credential.txt')
        async with app.run_test() as pilot:
            await pilot.pause()
            if app.screen.kind == 'suggest':
                await app.choose('skip')
                await pilot.pause()
            await app.choose('mode-integration')
            await pilot.pause()
            assert app.screen.kind == 'error'
            assert app.screen.heading == 'Missing configuration'
            body = str(app.screen.query_one('#body', Static).content)
            assert 'EXAMPLE_SECRET_FILE: .env / environment EXAMPLE_SECRET_FILE or --example-secret-file VALUE' in body
            assert HELP in body
            assert 'Review configuration' in body
            assert 'review-config' in dict(app.screen.actions)
            collector.assert_not_called()

            await app.choose('review-config')
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            assert row_names(app) == ['EXAMPLE_SECRET_FILE']
            await edit_row(app, pilot, 'EXAMPLE_SECRET_FILE', value)
            await pilot.pause()
            assert app.mode == 'integration'
            assert app.screen.heading != 'Missing configuration'
            collector.assert_called_once()
    asyncio.run(scenario())


def test_error_screen_without_help_text_lists_only_the_guidance(tmp_path):
    async def scenario():
        field = {key: value for key, value in FIELDS['EXAMPLE_SECRET_FILE'].items() if key != 'help'}
        app = fixture(tmp_path, fields={'EXAMPLE_SECRET_FILE': field})
        async with app.run_test() as pilot:
            await pilot.pause()
            if app.screen.kind == 'suggest':
                await app.choose('skip')
                await pilot.pause()
            await app.choose('mode-integration')
            await pilot.pause()
            assert app.screen.kind == 'error'
            lines = str(app.screen.query_one('#body', Static).content).splitlines()
            assert lines[1] == (
                'EXAMPLE_SECRET_FILE: .env / environment EXAMPLE_SECRET_FILE or --example-secret-file VALUE')
            assert HELP not in lines
    asyncio.run(scenario())
