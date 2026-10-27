"""Native login uses the existing ephemeral masked TUI channel."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Input, Static

from api_agent.auth.interaction_transport import OwnedInteraction
from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
from api_agent.providers.control import CleanupPendingError
from api_agent.tui.manager_recovery import TUIInteraction


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_dismiss', [False, True])
async def test_aws_private_screen_title_masked_reply_and_owned_cleanup(monkeypatch, failed_dismiss):
    app = App()
    async with app.run_test() as pilot:
        tui = TUIInteraction(app, threading.Event())
        root = OwnedInteraction(tui)
        login = LoginInteraction(SimpleNamespace(interactive=True, interaction=root))
        await login.open()
        await pilot.pause()
        channel = tui.active
        assert str(channel.screen.query_one('#manager-title', Static).render()) == 'AWS sign-in'
        field = channel.screen.query_one('#manager-input', Input)
        assert field.password
        await login.write('Synthetic authorization prompt')
        await pilot.pause()
        assert channel.screen.transcript == 'Synthetic authorization prompt'
        field.value = 'synthetic private authorization code'
        channel.screen.send_value()
        assert await login.read() == 'syntheticprivateauthorizationcode'
        if failed_dismiss:
            original = app.call_from_thread
            def unavailable(callback, *args):
                if callback == tui.dismiss:
                    raise RuntimeError('synthetic private failure')
                return original(callback, *args)
            monkeypatch.setattr(app, 'call_from_thread', unavailable)
            with pytest.raises(CleanupPendingError):
                await login.aclose()
            assert not channel.closed and tui.active is channel
            monkeypatch.setattr(app, 'call_from_thread', original)
        await login.aclose()
        await pilot.pause()
        assert tui.active is None
        assert channel.screen.transcript == '' and field.value == ''
        assert not root.cancelled and not root.requests
