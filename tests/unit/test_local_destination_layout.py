"""Destination layout must survive the production app's button stylesheet."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button
from textual.geometry import Region

from api_agent.runtime.local_contracts import LocalDestination, LocalSnapshot
from test_tui import fixture


@pytest.mark.parametrize('size', [(100, 30), (80, 24), (44, 12)])
def test_actual_app_destination_padding_url_row_and_copy(tmp_path, monkeypatch, size):
    async def scenario():
        stop = asyncio.Event()
        destinations = (LocalDestination('Agent', 'https://localhost:8443/'),
                        LocalDestination('Studio', 'https://localhost:9443/'))

        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self):
                return LocalSnapshot('ready', destinations=destinations,
                                     inspection_endpoint='http://127.0.0.1:12345')
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass

        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run',
                            SimpleNamespace(LocalRunOwner=Owner))
        copied = AsyncMock()
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copied)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent',
                               'acknowledge_default': False}})
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.press('b')
            buttons = list(screen.query('#local-choices Button'))
            assert len(buttons) == 3
            assert buttons[0].region.y == buttons[1].region.y
            assert buttons[0].region.right <= buttons[1].region.x
            for index, (button, destination) in enumerate(zip(buttons, screen.menu_choices)):
                button.focus()
                await pilot.pause()
                await pilot.wait_for_scheduled_animations()
                assert app.focused is button
                assert button.region.height == 4
                assert button.styles.padding.left >= 1
                assert button.content_region.x - button.region.x >= 2
                outer = button.render_lines(Region(0, 0, button.region.width, button.region.height))
                assert outer[1].text[1] == ' '
                lines = button.render_lines(button.content_region.translate(
                    (-button.region.x, -button.region.y)))
                assert f'{index + 1}  ' in lines[0].text
                assert destination.url[:8] in lines[1].text
                if size[0] >= 80:
                    assert destination.url in lines[1].text
                pane = screen.query_one('#local-choices')
                assert pane.content_region.y <= button.content_region.y
                assert button.content_region.bottom <= pane.content_region.bottom
                await pilot.press('ctrl+c')
                copied.assert_awaited_with(app, destination.url)
            assert await screen.close_owned()
    asyncio.run(scenario())
