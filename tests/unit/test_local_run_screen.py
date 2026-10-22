import asyncio
from types import SimpleNamespace as NS

import pytest

from textual.app import App

from api_agent.tui.local_run import LocalRunScreen
from api_agent.runtime.local_contracts import LocalSnapshot, LocalDestination, LocalEvent


class Harness(App):
    def __init__(self, screen):
        super().__init__()
        self.local = screen
        self.operation_status = None
        self.menu_shown = False
        self.opened = []
    def on_mount(self): self.push_screen(self.local)
    def show_menu(self): self.menu_shown = True
    def open_url(self, url): self.opened.append(url)
    async def confirm_quit(self): self.menu_shown = await self.local.close_owned()


def test_live_screen_keeps_capture_across_menus_and_retries_cleanup(monkeypatch):
    async def exercise():
        closed, callbacks = [], []
        opened = []
        async def open_url(url): opened.append(url)
        monkeypatch.setattr('api_agent.runtime.local_terminal.open_host_url', open_url)
        stop = asyncio.Event()
        snapshot = LocalSnapshot('ready', log_sources=('agent', 'ui'),
                      destinations=(LocalDestination('Agent', 'https://localhost/'),))
        class Owner:
            pending_cleanup = RuntimeError('pending')
            def diagnostic(self, error): return str(error)
            def __init__(self, *args, on_event, **kwargs): callbacks.append(on_event)
            async def start(self): return snapshot
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self):
                closed.append(True)
                if len(closed) == 1:
                    raise self.pending_cleanup
                self.pending_cleanup = None
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            callbacks[0](LocalEvent('log', source='agent', text='one\n'))
            screen.action_sources()
            callbacks[0](LocalEvent('log', source='ui', text='two\n'))
            await pilot.press('2', 'enter')
            assert screen.source == 'ui'
            assert screen.query_one('#local-output').text == 'two\n'
            screen.action_all_logs()
            assert screen.query_one('#local-output').text == '[agent] one\n[ui] two\n'
            screen.action_browsers()
            await pilot.press('1', 'enter')
            assert opened == ['https://localhost/']
            screen.action_view()
            assert not await screen.close_owned()
            assert 'Cleanup pending' in screen.failure
            assert await screen.close_owned()
            assert app.operation_status == 0
    asyncio.run(exercise())


def test_unmounted_screen_ignores_events_and_close_without_owner():
    async def exercise():
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
        screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=())))
        screen.receive(NS(kind='other'))
        assert screen.sources() == ()
        assert screen.destinations() == ()
        assert await screen.close_owned()
        screen.run_task = asyncio.create_task(asyncio.sleep(30))
        assert await screen.close_owned()
        assert screen.run_task.cancelled()
    asyncio.run(exercise())


def test_screen_runtime_failures_and_default_factory(monkeypatch):
    import sys
    async def exercise(failure):
        class Owner:
            pending_cleanup = None
            def diagnostic(self, error): return str(error)
            def __init__(self, *args, **kwargs): pass
            async def start(self): raise failure
            async def wait(self): raise AssertionError('unreachable')
            def request_stop(self): pass
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
        await screen.run()
        assert screen.finished
        assert screen.status == (130 if isinstance(failure, asyncio.CancelledError) else 1)
        return screen
    asyncio.run(exercise(ValueError('failed')))
    asyncio.run(exercise(asyncio.CancelledError()))


def test_live_controls_stop_quit_and_cleanup_rejection():
    async def exercise():
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
        app = Harness(screen)
        pending = RuntimeError('pending')
        class Owner:
            pending_cleanup = pending
            def diagnostic(self, error): return str(error)
            reject = True
            def request_stop(self): pass
            async def aclose(self):
                if self.reject:
                    raise pending
        owner = Owner()
        async with app.run_test() as pilot:
            await pilot.pause()
            # Replace the startup controller only after its task has settled.
            await screen.run_task
            screen.owner = owner
            screen.run_task = None
            await screen.on_key(NS(character=None, key='x'))
            screen.action_sources()
            await screen.on_key(NS(character='0', key='0', stop=lambda: None))
            await screen.on_key(NS(character=None, key='enter'))
            await screen.action_stop()
            assert not app.menu_shown
            try:
                await screen.on_unmount()
            except RuntimeError as error:
                assert error is pending
            else:
                raise AssertionError('cleanup must reject unmount')
            owner.reject = False
            await screen.action_stop()
            assert app.menu_shown
            await screen.action_quit()
    # Default runtime is intentionally replaced by a deterministic failure.
    import sys
    class FailingOwner:
        pending_cleanup = None
        def diagnostic(self, error): return str(error)
        def __init__(self, *args, **kwargs): pass
        async def start(self): raise ValueError('unavailable')
        def request_stop(self): pass
        async def aclose(self): pass
    from unittest.mock import patch
    with patch.dict(sys.modules, {'api_agent.runtime.local_run': NS(LocalRunOwner=FailingOwner)}):
        asyncio.run(exercise())


def test_app_run_local_dispatch_and_confirmed_quit_use_controller(tmp_path, monkeypatch):
    import sys
    from test_tui import fixture
    callbacks, closed = [], []
    async def exercise():
        stop = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def diagnostic(self, error): return str(error)
            def __init__(self, *args, on_event, **kwargs): callbacks.append(on_event)
            async def start(self): return LocalSnapshot('ready')
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): closed.append(True)
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
        app = fixture(tmp_path, 'run-local', fields={'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, LocalRunScreen)
            assert len(callbacks) == 1
            from api_agent.tui.app import QuitDialog
            await pilot.press('q')
            assert isinstance(app.screen, QuitDialog)
            assert closed == []
            await pilot.press('escape')
            await app.confirm_quit()
            assert closed
            assert app.operation_status == 0
    asyncio.run(exercise())


def test_menus_support_tenth_log_and_destination_and_opener_failure(monkeypatch):
    async def exercise():
        opened = []
        async def open_url(url):
            opened.append(url)
            raise OSError('unavailable')
        monkeypatch.setattr('api_agent.runtime.local_terminal.open_host_url', open_url)
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
        screen.snapshot = LocalSnapshot('ready', log_sources=tuple(f'service-{i}' for i in range(1, 11)),
            destinations=tuple(LocalDestination(f'Service {i}', f'https://localhost/{i}') for i in range(1, 11)))
        screen.action_sources()
        await screen.on_key(NS(character=None, key='x'))
        for digit in '10':
            await screen.on_key(NS(character=digit, key=digit, stop=lambda: None))
        await screen.on_key(NS(character=None, key='enter', stop=lambda: None))
        assert screen.source == 'service-10'
        screen.action_browsers()
        for digit in '10':
            await screen.on_key(NS(character=digit, key=digit, stop=lambda: None))
        await screen.on_key(NS(character=None, key='enter', stop=lambda: None))
        assert opened == ['https://localhost/10']
        assert 'Could not open the host browser' in screen.failure
    asyncio.run(exercise())


def test_retired_screen_cannot_replace_another_operations_status():
    async def exercise():
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
        app = Harness(screen)
        class Owner:
            pending_cleanup = None
            def diagnostic(self, error): return str(error)
            def request_stop(self): pass
            async def aclose(self): pass
        async with app.run_test() as pilot:
            await pilot.pause()
            await screen.run_task
            screen.owner = Owner()
            screen.run_task = None
            app.local_run_screen = object()
            app.operation_status = 9
            assert await screen.close_owned()
            assert await screen.close_owned()
            assert app.operation_status == 9
    import sys
    class FailingOwner:
        pending_cleanup = None
        def diagnostic(self, error): return str(error)
        def __init__(self, *args, **kwargs): pass
        async def start(self): raise ValueError('not ready')
        def request_stop(self): pass
        async def aclose(self): pass
    from unittest.mock import patch
    with patch.dict(sys.modules, {'api_agent.runtime.local_run': NS(LocalRunOwner=FailingOwner)}):
        asyncio.run(exercise())


def test_main_menu_run_local_selection_dispatches_once(tmp_path, monkeypatch):
    import sys
    from test_tui import fixture
    calls = []
    async def exercise():
        stop = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self):
                calls.append('start')
                return LocalSnapshot('ready')
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): calls.append('close')
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
        app = fixture(tmp_path, fields={'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.kind == 'menu'
            await app.choose('mode-run-local')
            await pilot.pause()
            assert isinstance(app.screen, LocalRunScreen)
            assert calls == ['start']
            await app.screen.action_stop()
            await pilot.pause()
            assert app.screen.kind == 'menu'
            assert calls == ['start', 'close']
    asyncio.run(exercise())


@pytest.mark.parametrize('failure,status', [(RuntimeError('synthetic-secret'), 1), (asyncio.CancelledError(), 130)])
def test_screen_factory_failure_has_safe_fallback(failure, status):
    async def exercise():
        def failed_factory(*args, **kwargs): raise failure
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=failed_factory)
        await screen.run()
        assert screen.status == status
        assert screen.owner is None
        assert screen.failure == ('Local operation could not start.' if status == 1 else '')
    asyncio.run(exercise())


def test_app_factory_failure_disposes_without_reporting_success(tmp_path, monkeypatch):
    import sys
    from test_tui import fixture
    def failed_factory(*args, **kwargs): raise RuntimeError('synthetic-secret')
    monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=failed_factory))
    async def exercise():
        app = fixture(tmp_path, 'run-local', fields={'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.failure == 'Local operation could not start.'
            await app.confirm_quit()
            assert app.return_value == 1
            assert app.local_run_screen is None
    asyncio.run(exercise())
