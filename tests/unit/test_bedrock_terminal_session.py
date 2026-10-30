"""Visible terminal session login joins the normal shared integration owner."""
import io
import os
import time
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.usefixtures('isolated_native_login_gate')

from api_agent.auth.interaction_transport import InteractionClient, InteractionServer
from api_agent.auth.terminal_interaction import TerminalInteraction
from api_agent.auth.terminal_output import TerminalOutput
from api_agent.llm_providers.bedrock.ambient import AmbientSession
from api_agent.llm_providers.bedrock.login_transport import LoginTransport
from api_agent.providers.contracts import InteractionRequiredError
from api_agent.testing import provider_setup
from test_bedrock_login_integration import MODEL, ROOT, native_config


@pytest.mark.parametrize('reply', ['code', 'eof'])
def test_no_stdin_session_login_relays_url_and_code_through_shared_integration(
        tmp_path, monkeypatch, reply):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(AmbientSession, 'acquire', AsyncMock(return_value=(None, {})))
    reader, writer = os.pipe()
    output = io.StringIO()
    presentation = TerminalOutput(output, output)
    if reply == 'code':
        os.write(writer, b'c3ludGhldGlj\n')
    os.close(writer)
    calls = []
    async def acquire(transport, settings, environment, interaction):
        calls.append(settings)
        assert interaction is not None and settings['login']
        await interaction.write('Open https://example.test/sign-in\nAuthorization code: ')
        presentation.write('Concurrent test progress')
        assert output.getvalue().endswith('Authorization code: ')
        assert await interaction.read() == 'c3ludGhldGlj'
        return dict(AccessKeyId='synthetic-key', SecretAccessKey='synthetic-secret',
            SessionToken='synthetic-token', expires_at=time.time() + 300,
            identity=dict(Account='123456789012', Arn='arn:aws:iam::123456789012:user/example'))
    monkeypatch.setattr(LoginTransport, 'acquire', acquire)
    load = provider_setup.aload_provider
    async def load_catalog(*args, **kwargs):
        loaded = await load(*args, **kwargs)
        loaded.implementation.transport.call = AsyncMock(return_value={'data': [{'id': MODEL}]})
        return loaded
    monkeypatch.setattr(provider_setup, 'aload_provider', load_catalog)
    config = dict(native_config('direct'), NO_STD_IN=True, UNATTENDED_USER_INDEX=None)
    with os.fdopen(reader) as stream:
        server = InteractionServer(TerminalInteraction(stream, output, presentation=presentation))
        owner = provider_setup.IntegrationLLM(config, ROOT,
            interaction=InteractionClient(server.descriptor), skip_unavailable=False)
        try:
            if reply == 'code':
                assert [choice.selected_llm for choice in owner.describe()] == [MODEL]
                assert owner.owner._interactive is True
            else:
                with pytest.raises(InteractionRequiredError):
                    owner.describe()
            assert len(calls) == 1
            assert 'Authentication: AWS sign-in\nOpen https://example.test/sign-in' in output.getvalue()
            assert output.getvalue().endswith('Authorization code: \nConcurrent test progress\n')
            assert 'c3ludGhldGlj' not in output.getvalue()
        finally:
            owner.close()
            server.close()
            presentation.close()
    assert not presentation.active and not presentation.pending


@pytest.mark.asyncio
async def test_owned_terminal_session_never_replaces_selected_index_with_attended_identity(monkeypatch):
    from api_agent.llm_providers.bedrock.auth import create_session
    forbidden = AsyncMock(side_effect=AssertionError('Indexed login cannot fall back to browser identity'))
    monkeypatch.setattr(AmbientSession, 'acquire', forbidden)
    monkeypatch.setattr(LoginTransport, 'acquire', forbidden)
    interaction = type('Interaction', (), {'open': forbidden})()
    config = dict(native_config('direct'), NO_STD_IN=True)

    with pytest.raises(InteractionRequiredError):
        await create_session(config, interactive=True, user_index=2,
            credentials=None, services={'owner_root': ROOT, 'interaction': interaction})

    forbidden.assert_not_called()
