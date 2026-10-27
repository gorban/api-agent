"""Synthetic fresh-code input through the masked screen and native worker."""
import asyncio
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from textual import events
from textual.app import App
from textual.widgets import Input

from api_agent.auth.interaction_transport import InteractionClient, InteractionServer
from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
from api_agent.llm_providers.bedrock.login_transport import LoginTransport
from api_agent.tui.manager_recovery import TUIInteraction
from test_bedrock_code_paste import _WORKER, copy_selected_url


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['clipboard', 'terminal'])
async def test_one_line_code_reaches_native_worker(monkeypatch, route):
    assert await handoff(monkeypatch, 'c3ludGhldGlj', route) == {'accepted': 'c3ludGhldGlj'}


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['\nc3ludGhldGlj\n', '\r\nc3ludGhldGlj\r\n', ' \tc3lu\r\ndGhl\vdGlj\f '])
async def test_ascii_whitespace_crosses_shared_interaction(monkeypatch, value):
    assert await handoff(monkeypatch, value, 'clipboard', remote=True) == {'accepted': 'c3ludGhldGlj'}


@pytest.mark.asyncio
async def test_terminal_paste_preserves_complete_code(monkeypatch):
    assert await handoff(monkeypatch, 'c3lu\ndGhldGlj', 'terminal') == {'accepted': 'c3ludGhldGlj'}


async def handoff(monkeypatch, value, route, *, remote=False):
    launch = asyncio.create_subprocess_exec

    async def synthetic_worker(*command, **kwargs):
        assert command[0] == sys.executable
        return await launch(sys.executable, '-c', _WORKER, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', synthetic_worker)
    app = App()
    tui = TUIInteraction(app, threading.Event())
    server = InteractionServer(tui) if remote else None
    owner = InteractionClient(server.descriptor) if remote else tui
    interaction = LoginInteraction(SimpleNamespace(interactive=True, interaction=owner))
    transport = LoginTransport(timeout=5)
    async with app.run_test(size=(85, 30)) as pilot:
        try:
            await interaction.open()
            await pilot.pause()
            url = await copy_selected_url(monkeypatch, app.screen, pilot)
            field = app.screen.query_one('#manager-input', Input)
            field.focus()
            if route == 'clipboard':
                async def read_text():
                    return value
                monkeypatch.setattr('api_agent.tui.clipboard.read_text', read_text)
                await pilot.press('ctrl+v')
                assert field.value == value
                assert app.clipboard == ''
            else:
                field.post_message(events.Paste(value))
                await pilot.pause()
                assert field.value == value
            await pilot.press('enter')
            return await transport.acquire(
                {'method': 'direct', 'login': True},
                {'PATH': os.defpath, 'PYTHONPATH': os.environ.get('PYTHONPATH', '')}, interaction)
        finally:
            await transport.aclose()
            await interaction.aclose()
            if server is not None:
                await asyncio.to_thread(server.close)


@pytest.mark.asyncio
async def test_base64_punctuation_crosses_native_worker(monkeypatch):
    assert await handoff(monkeypatch, ' \t+/8=\r\n', 'clipboard', remote=True) == {'accepted': '+/8='}
