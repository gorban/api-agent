"""Acceptance of false-valued defaults uses declaration presence, not truthiness."""
import asyncio

import pytest


@pytest.mark.parametrize(('field', 'value', 'text'), [
    ({'type': 'str', 'default': ''}, '', ''),
    ({'type': 'bool', 'default': False}, False, 'False'),
    ({'type': 'int', 'default': 0}, 0, '0'),
])
def test_keyboard_accepts_and_rereads_false_valued_default(tmp_path, field, value, text):
    from api_agent.config.dotenv import read_dotenv
    from test_tui import fixture

    async def scenario():
        app = fixture(tmp_path, fields={'OPTION': field})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert app.screen.check_action('accept_default', ())
            await pilot.press('a')
            await pilot.pause()
            assert read_dotenv(tmp_path / '.env')['OPTION'] == text
            resolved = app.session.result().fields['OPTION']
            assert type(resolved.value) is type(value)
            assert resolved.value == value and resolved.explicit
            assert not app.screen.check_action('accept_default', ())
    asyncio.run(scenario())


def test_missing_default_cannot_be_accepted(tmp_path):
    from test_tui import fixture

    async def scenario():
        app = fixture(tmp_path, fields={'OPTION': {'type': 'str', 'review_optional': True}})
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert app.focused_setting() == 'OPTION'
            assert not app.screen.check_action('accept_default', ())
            await pilot.press('a')
            assert not (tmp_path / '.env').exists()
            assert 'OPTION' not in app.session.result().fields
    asyncio.run(scenario())


def test_explicit_environment_value_overrides_empty_default(tmp_path):
    from test_tui import fixture

    async def scenario():
        app = fixture(tmp_path, fields={'OPTION': {'type': 'str', 'default': '', 'review_optional': True}})
        app.session.save_defaults(['OPTION'])
        assert (tmp_path / '.env').exists()
        app.session.environ['OPTION'] = 'chosen'
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            resolved = app.session.result().fields['OPTION']
            assert resolved.value == 'chosen' and resolved.explicit
            from api_agent.config.dotenv import read_dotenv
            assert read_dotenv(tmp_path / '.env')['OPTION'] == ''
    asyncio.run(scenario())
