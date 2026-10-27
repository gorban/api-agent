"""Native code handoff uses the real recovery screen and isolated pipe worker."""
import asyncio
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Input, TextArea
from textual.document._document import Selection

from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
from api_agent.llm_providers.bedrock.login_transport import LoginTransport
from api_agent.tui.manager_recovery import ManagerRecoveryScreen, TUIInteraction


_WORKER = '''import os, sys
from api_agent.llm_providers.bedrock import login_worker as worker
def run(config):
    command = [sys.executable, '-c', "import sys; print('Enter code:', flush=True); value=sys.stdin.buffer.readline(); sys.stdout.write('accepted:'+value.decode())"]
    output = worker.diagnosed('sign_in', worker.cli, command, dict(os.environ), interactive=True)
    worker.emit({'auth_done': True})
    worker.emit({'result': {'accepted': output.decode().split('accepted:', 1)[1].strip()}})
worker.run = run
worker.main()
'''


class CodePasteApp(App):
    def __init__(self):
        super().__init__()
        self.interaction = TUIInteraction(self, threading.Event())


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix', ['\n', '\r\n'])
async def test_native_code_ctrl_v_trailing_line_ending_is_accepted(monkeypatch, suffix):
    result = await paste_into_native_worker(monkeypatch, 'c3ludGhldGlj' + suffix)
    assert result == {'accepted': 'c3ludGhldGlj'}


@pytest.mark.asyncio
async def test_native_code_ctrl_v_embedded_line_break_is_normalized(monkeypatch):
    assert await paste_into_native_worker(monkeypatch, 'c3lu\ndGhldGlj') == {'accepted': 'c3ludGhldGlj'}


async def paste_into_native_worker(monkeypatch, clipboard):
    launch = asyncio.create_subprocess_exec

    async def launch_synthetic_worker(*command, **kwargs):
        # Replace only the native command boundary; preserve real worker IPC.
        assert command[0] == sys.executable
        return await launch(sys.executable, '-c', _WORKER, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', launch_synthetic_worker)
    app = CodePasteApp()
    interaction = LoginInteraction(SimpleNamespace(interactive=True, interaction=app.interaction))
    transport = LoginTransport(timeout=5)
    async with app.run_test(size=(85, 30)) as pilot:
        try:
            await interaction.open()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ManagerRecoveryScreen)
            url = await copy_selected_url(monkeypatch, screen, pilot)
            async def read_text():
                return clipboard
            monkeypatch.setattr('api_agent.tui.clipboard.read_text', read_text)
            field = screen.query_one('#manager-input', Input)
            field.focus()
            await pilot.press('ctrl+v')
            await pilot.pause()
            # Browser copy changes only the native clipboard; the app cache stays empty.
            assert field.value == clipboard
            assert app.clipboard == ''
            await pilot.press('enter')
            return await transport.acquire({'method': 'direct', 'login': True}, {'PATH': os.defpath, 'PYTHONPATH': os.environ.get('PYTHONPATH', '')}, interaction)
        finally:
            await transport.aclose()
            await interaction.aclose()


async def copy_selected_url(monkeypatch, screen, pilot):
    app = screen.app
    output = screen.query_one('#manager-output', TextArea)
    url = 'https://login.example.test/authorize'
    screen.append_output(url)
    output.selection = Selection((0, 0), (0, len(url)))
    writes = []
    def native_write(command, **kwargs):
        writes.append(kwargs['input'])
    monkeypatch.setattr('api_agent.tui.clipboard.native_command', lambda: ['synthetic-copy'])
    monkeypatch.setattr('api_agent.tui.clipboard.subprocess.run', native_write)
    await screen.action_copy_output()
    await pilot.pause()
    assert app.clipboard == ''
    assert writes == [url.encode()]
    return url
