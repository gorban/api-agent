"""Actual provider declarations remain discoverable in configuration review."""
import asyncio
from pathlib import Path
import tomllib

from textual.widgets import DataTable, Input, Static

from api_agent.llm_providers.bedrock import mantle
from test_tui import fixture, setting_row


def declarations():
    path = Path(mantle.__file__).parent / 'cfg' / 'config.toml'
    return tomllib.loads(path.read_text())['fields']


def test_session_provider_review_editor_and_optional_sso_fields(tmp_path):
    async def scenario():
        fields = declarations()
        assert 'BEDROCK_LOGIN_METHOD' not in fields
        names = [name for name in fields if name.startswith('BEDROCK_LOGIN_')]
        names += ['BEDROCK_AUTH_MODE', 'BEDROCK_SESSION_PROVIDER']
        app = fixture(tmp_path, fields={name: fields[name] for name in names})
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.move_cursor(row=setting_row(table, 'BEDROCK_SESSION_PROVIDER'))
            await pilot.press('e')
            await pilot.pause()
            description = str(app.screen.query_one('#config-description', Static).content)
            assert 'aws_login.py:factory' in description
            assert 'aws_sso.py:factory' in description
            app.screen.query_one('#config-value', Input).value = 'src/api_agent/llm_providers/bedrock/aws_sso.py:factory'
            await pilot.click('#submit')
            await pilot.pause()
            result = app.session.result()
            assert result.values['BEDROCK_SESSION_PROVIDER'].endswith('/aws_sso.py:factory')
            assert not any(name in result.missing for name in names if name.startswith('BEDROCK_LOGIN_'))
            app.session.set_value('BEDROCK_AUTH_MODE', 'access_key', persist=True)
            assert not any(name in app.session.result().fields for name in names if name.startswith('BEDROCK_LOGIN_'))
    asyncio.run(scenario())


def test_attended_key_help_describes_prompt_and_index_binding():
    fields = declarations()
    assert 'prompt' in fields['UNATTENDED_USER_INDEX']['help']
    assert 'no fallback' in fields['UNATTENDED_USER_INDEX']['help']
    assert 'prompt' in fields['BEDROCK_AUTH_MODE']['help']
