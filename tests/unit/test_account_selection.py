"""Account labels, persisted choice and shared test-picker controls."""
import asyncio
import json
from unittest.mock import Mock

import pytest
from textual.widgets import DataTable, Static

from api_agent.tui.accounts import account_options
from api_agent.testing.collection import TestIdentity
from test_tui import fixture


def descriptors(extra=None):
    return {'password_manager': {'manager': '1password', 'cli_path': 'op'},
            'roles': {'LLM User': [{'vault': 'v', 'user': 'first'}, {'vault': 'v', 'user': 'second'}],
                      'Partner': [{'vault': 'v', 'user': 'partner'}] if extra is None else extra}}


def test_labels_follow_operation_roles_and_pair_limits():
    auth = descriptors()
    assert account_options(auth, ()) == ()
    assert account_options(auth, ('LLM User', 'Partner')) == ((0, 'first / partner'), (1, 'second'))
    assert account_options(auth, ('LLM User', 'Partner'), paired=True) == ((0, 'first / partner'),)
    auth['roles']['Partner'] = []
    assert account_options(auth, ('LLM User', 'Partner'), paired=True) == ()
    assert account_options({}, ('LLM User',)) == ()


@pytest.mark.parametrize('extra', ['bad', [None], [{'user': 2}], [{'user': ''}], [{'user': '   '}], [{}]])
def test_incomplete_optional_role_never_blocks_primary_only_tests(extra):
    assert account_options(descriptors(extra), ('LLM User', 'Partner')) == ((0, 'first'), (1, 'second'))


def make_app(tmp_path, *, argv=(), mode='integration'):
    ids = [TestIdentity(f'base::t.py::{name}', 'base', 't.py', f't.py::{name}') for name in ('a', 'b')]
    app = fixture(tmp_path, mode, fields={
        'UNATTENDED_USER_INDEX': {'type': 'int', 'allow_empty': True, 'validation': 'nonnegative'},
        'UNATTENDED_SECONDARY_USER': {'type': 'str', 'default': 'Partner', 'acknowledge_default': False}},
        argv=argv, collector=lambda *args: ids)
    app.context.credential_role_fields = ('UNATTENDED_SECONDARY_USER',)
    app.session.environ['UNATTENDED_AUTH'] = json.dumps(descriptors())
    return app


def test_bracket_cycle_wraps_and_persists_without_changing_test_selection(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=2)
            selected = set(app.selection.selected_ids)
            assert 'No unattended user set' in str(app.screen.query_one('#account-selection', Static).content)
            assert app.screen.query_one('#account-selection', Static).content.style == 'red'
            for key, index, text in [('right_square_bracket', 0, 'first / partner'),
                                     ('right_square_bracket', 1, 'second'),
                                     ('right_square_bracket', None, 'logins must be done manually'),
                                     ('left_square_bracket', 1, 'second')]:
                await pilot.press(key)
                await pilot.pause()
                assert app.session.result().values['UNATTENDED_USER_INDEX'] == index
                assert text in str(app.screen.query_one('#account-selection', Static).content)
                assert app.screen.query_one('#account-selection', Static).content.style == ('red' if index is None else 'green')
                assert app.screen.query_one(DataTable) is table
                assert table.cursor_row == 2 and table.has_focus
                assert set(app.selection.selected_ids) == selected
                assert app.session.saved_path('UNATTENDED_USER_INDEX') == tmp_path / '.env'
            await pilot.press('right_square_bracket')
            assert app.session.result().values['UNATTENDED_USER_INDEX'] is None
    asyncio.run(scenario())
    restarted = make_app(tmp_path)
    assert restarted.session.result().values['UNATTENDED_USER_INDEX'] is None
    assert restarted.session.result().fields['UNATTENDED_USER_INDEX'].explicit


def test_cycle_saves_to_displayed_target_and_moves_assignment(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        pack = tmp_path / 'pack'
        pack.mkdir()
        app.context.pack_root = pack
        app.context.external_pack = True
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            app.session.switch_target(pack / '.env')
            await pilot.press('right_square_bracket')
            assert app.session.saved_path('UNATTENDED_USER_INDEX') == pack / '.env'
            app.session.switch_target(tmp_path / '.env')
            await pilot.press('right_square_bracket')
            assert app.session.saved_path('UNATTENDED_USER_INDEX') == tmp_path / '.env'
            assert 'UNATTENDED_USER_INDEX' not in (pack / '.env').read_text()
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 1
    asyncio.run(scenario())


def test_higher_cli_selection_remains_effective_with_clear_warning(tmp_path):
    async def scenario():
        app = make_app(tmp_path, argv=['--unattended-user-index=0'])
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await pilot.press('right_square_bracket')
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 0
            assert 'cli overrides the saved choice' in app.screen.query_one('#error', Static).content
            assert 'first / partner' in str(app.screen.query_one('#account-selection', Static).content)
            assert 'UNATTENDED_USER_INDEX="1"' in (tmp_path / '.env').read_text()
    asyncio.run(scenario())


def test_invalid_account_can_cycle_back_to_valid_and_save_errors_are_visible(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        (tmp_path / '.env').write_text('UNATTENDED_USER_INDEX=9\n')
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            assert 'unavailable' in str(app.screen.query_one('#account-selection', Static).content)
            assert app.screen.query_one('#account-selection', Static).content.style == 'red'
            await pilot.press('right_square_bracket')
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 0
            app.session.set_value = Mock(side_effect=OSError('Synthetic save failure'))
            await pilot.press('right_square_bracket')
            assert 'Synthetic save failure' in app.screen.query_one('#error', Static).content
            assert app.session.result().values['UNATTENDED_USER_INDEX'] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['unit', 'e2e'])
def test_account_shortcuts_are_not_yet_enabled_for_other_runners(tmp_path, mode):
    async def scenario():
        app = make_app(tmp_path, mode=mode)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app.screen.check_action('previous_account', ())
            assert not app.screen.check_action('next_account', ())
    asyncio.run(scenario())


def test_no_declaration_or_invalid_credentials_do_not_offer_accounts(tmp_path):
    app = fixture(tmp_path)
    assert app.account_options() == ()
    app = make_app(tmp_path)
    app.session.environ['UNATTENDED_AUTH'] = json.dumps({'roles': {'LLM User': [{'vault': '', 'user': 'bad'}]}})
    assert app.account_options() == ()
    app.mode = 'run-local'
    app.session.environ['UNATTENDED_AUTH'] = json.dumps(descriptors())
    assert app.account_options() == ((0, 'first / partner'),)


def test_stale_index_with_no_eligible_accounts_can_switch_to_manual(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        app.session.environ['UNATTENDED_AUTH'] = '{}'
        (tmp_path / '.env').write_text('UNATTENDED_USER_INDEX=2\n')
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            assert app.screen.check_action('next_account', ())
            assert 'unavailable' in str(app.screen.query_one('#account-selection', Static).content)
            assert app.screen.query_one('#account-selection', Static).content.style == 'red'
            await pilot.press('right_square_bracket')
            assert app.session.result().values['UNATTENDED_USER_INDEX'] is None
            assert 'logins must be done manually' in str(app.screen.query_one('#account-selection', Static).content)
            assert not app.screen.check_action('next_account', ())
    asyncio.run(scenario())
