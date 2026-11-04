"""Run local summaries remain reachable after actual recovery-screen push/pop."""
import asyncio
import sys
from types import SimpleNamespace as NS

import pytest
from textual.widgets import TextArea

from api_agent.runtime.local_contracts import LocalComponent, LocalDestination, LocalEvent, LocalSnapshot
from api_agent.tui.local_run import LocalRunScreen
from api_agent.tui.manager_recovery import ManagerRecoveryScreen, transcript_text
from test_tui import fixture


@pytest.mark.parametrize('size', [(80, 24), (100, 35), (85, 18), (50, 14)])
def test_ready_summary_survives_recovery_and_log_focus(tmp_path, monkeypatch, size):
    async def scenario():
        release, stopped = asyncio.Event(), asyncio.Event()
        destinations = tuple(LocalDestination(f'Service {index}',
            f'http://localhost:{8100 + index}/service-dashboard/ready') for index in range(12))
        ready = LocalSnapshot('ready',
            components=tuple(LocalComponent(f'service-{index}', 'ready') for index in range(8)),
            destinations=destinations, inspection_endpoint='http://127.0.0.1:39999')
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, on_event, interaction_owner, **kwargs):
                self.emit, self.interaction = on_event, interaction_owner
            async def start(self):
                channel = await asyncio.to_thread(self.interaction.open, 'Synthetic manager')
                await asyncio.to_thread(channel.write, 'Touch your security key')
                self.emit(LocalEvent('status', snapshot=ready))
                await release.wait()
                await asyncio.to_thread(channel.close)
                return ready
            def interactive_dashboard_credentials(self):
                return ('admin@example.test', 'synthetic-dashboard-password')
            async def wait(self):
                await stopped.wait()
                return 0
            def request_stop(self): stopped.set()
            async def aclose(self): await asyncio.to_thread(self.interaction.close)
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=size) as pilot:
            for _ in range(40):
                if isinstance(app.screen, ManagerRecoveryScreen) and app.local_run_screen.snapshot.phase == 'ready':
                    break
                await pilot.pause(.02)
            assert isinstance(app.screen, ManagerRecoveryScreen)
            screen = app.local_run_screen
            release.set()
            for _ in range(40):
                if app.screen is screen and screen._dashboard_credentials:
                    break
                await pilot.pause(.02)
            assert app.screen is screen and screen.snapshot is ready
            output = screen.query_one('#local-output', TextArea)
            output.focus()
            screen.receive(LocalEvent('log', source='agent', text='Local application is ready\n'))
            await pilot.pause()
            assert screen.scroll_y == 0
            assert screen.query_one('#local-status').region.y >= screen.content_region.y
            frame = transcript_text(screen._compositor.render_full_update().render_segments(app.console))
            assert 'Run local: ready' in frame, (screen.scroll_y, screen.query_one('#local-status').region, output.region, frame)
            assert 'Local application is ready' in frame
            summary = screen.query_one('#local-summary')
            assert summary.region.bottom <= output.region.y
            assert output.region.height >= 3
            await pilot.press('shift+tab')
            assert app.focused is summary
            await pilot.press('end')
            await pilot.pause()
            tail = transcript_text(screen._compositor.render_full_update().render_segments(app.console))
            assert 'synthetic-dashboard-password' in tail
            assert screen.scroll_y == 0
            output.focus()
            screen.receive(LocalEvent('log', source='agent', text='agent still running\n'))
            await pilot.pause()
            assert 'agent still running' in transcript_text(screen._compositor.render_full_update().render_segments(app.console))
            await pilot.press('b')
            await pilot.pause()
            assert screen.menu == 'browsers'
            await pilot.press('escape')
            await pilot.pause()
            assert screen.menu is None
            screen.failure = '\n'.join(f'Safe diagnostic line {index}' for index in range(40))
            screen.refresh_view()
            error = screen.query_one('#local-error', TextArea)
            error.focus()
            await pilot.press(*(['pagedown'] * 40))
            await pilot.pause()
            diagnostic_frame = transcript_text(screen._compositor.render_full_update().render_segments(app.console))
            assert 'Safe diagnostic line 39' in diagnostic_frame
            assert 'agent still running' in diagnostic_frame
            assert output.content_region.height >= 2
            assert error.region.bottom <= screen.content_region.bottom
            assert output.region.height >= 3
            assert screen.query_one('#local-hint').region.bottom <= screen.content_region.bottom
            assert screen.scroll_y == 0
            assert not any('synthetic-dashboard-password' in event.text for event in screen.events)
            assert 'synthetic-dashboard-password' not in repr(screen.snapshot)
            await screen.close_owned()
    asyncio.run(scenario())
