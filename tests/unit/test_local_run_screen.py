import asyncio
import time
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
    def shutdown_cleanup_receipt(self): return self.local.pending_cleanup


def test_local_controller_starts_after_first_refresh_and_skips_late_start_after_close():
    async def exercise():
        created = []

        class DeferredScreen(LocalRunScreen):
            def call_after_refresh(self, callback, *args, **kwargs):
                if self.run_task is None:
                    self.deferred_start = callback
                    return
                return super().call_after_refresh(callback, *args, **kwargs)

        def owner_factory(*args, **kwargs):
            created.append(True)
            raise AssertionError('late controller start')

        screen = DeferredScreen(None,
            lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=owner_factory)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert screen.run_task is None
            assert created == []
            screen._startup_timer.stop()
            screen._startup_timer = None
            await screen.on_unmount()
            assert screen.disposed
            screen.deferred_start()
            await pilot.pause()
            assert screen.run_task is None
            assert created == []

    asyncio.run(exercise())


def test_actual_app_paints_starting_screen_before_owner_starts(tmp_path, monkeypatch):
    import sys
    from textual.widgets import Static
    from test_tui import fixture
    import api_agent.tui.local_run as local_run_module

    async def exercise():
        created = []
        stop = asyncio.Event()

        class DeferredScreen(LocalRunScreen):
            def call_after_refresh(self, callback, *args, **kwargs):
                if getattr(callback, '__name__', '') == 'start_after_paint':
                    self.deferred_start = callback
                    return
                return super().call_after_refresh(callback, *args, **kwargs)

        class Owner:
            pending_cleanup = None

            def __init__(self, *args, **kwargs):
                created.append(True)

            async def start(self):
                return LocalSnapshot('ready')

            async def wait(self):
                await stop.wait()
                return 0

            def request_stop(self):
                stop.set()

            async def aclose(self):
                pass

        monkeypatch.setattr(local_run_module, 'LocalRunScreen', DeferredScreen)
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(50, 14)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, DeferredScreen)
            assert screen.run_task is None and created == []
            assert 'Starting local operation' in str(screen.query_one('#local-status', Static).render())
            assert screen.query_one('#local-error').region.height == 0
            assert screen.query_one('#local-output').region.height >= 9
            screen.deferred_start()
            await pilot.pause()
            assert created == [True]
            assert screen.run_task is not None
            assert await screen.close_owned()

    asyncio.run(exercise())


def test_startup_activity_and_elapsed_remain_visible_with_filtered_logs():
    from textual.widgets import Static

    async def exercise():
        entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Owner:
            pending_cleanup = None

            def __init__(self, *args, on_event, **kwargs):
                self.emit = on_event

            async def start(self):
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('starting',
                    log_sources=('agent', 'api'), activity='Checking local services')))
                self.emit(LocalEvent('log', source='api', text='private api log\n'))
                entered.set()
                await release.wait()
                return LocalSnapshot('ready')

            async def wait(self):
                await stop.wait()
                return 0

            def request_stop(self):
                stop.set()

            async def aclose(self):
                release.set()

        screen = LocalRunScreen(None,
            lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await entered.wait()
            screen._startup_started = time.monotonic() - 65
            screen._refresh_startup_status()
            rendered = str(screen.query_one('#local-status', Static).render())
            assert 'Checking local services — 65s elapsed' in rendered
            assert screen.query_one('#local-output').text == ''
            await pilot.press('asterisk')
            assert 'Checking local services' in str(screen.query_one('#local-status', Static).render())
            assert 'private api log' in screen.query_one('#local-output').text
            release.set()
            await pilot.pause()
            assert screen.snapshot.phase == 'ready'
            assert 'Checking local services' not in str(screen.query_one('#local-status', Static).render())
            assert await screen.close_owned()

    asyncio.run(exercise())


def test_settled_startup_failure_renders_failed_with_safe_reason():
    from textual.widgets import Static, TextArea

    async def exercise():
        class Owner:
            pending_cleanup = None

            def __init__(self, *args, on_event, **kwargs):
                self.emit = on_event

            async def start(self):
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('stopped')))
                raise RuntimeError('raw secret')

            def diagnostic(self, error):
                return 'Local service did not become ready.'

            def request_stop(self):
                pass

            async def aclose(self):
                pass

        screen = LocalRunScreen(None,
            lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await screen.run_task
            await pilot.pause()
            assert screen.finished and screen.status == 1
            assert 'Run local: failed' in str(screen.query_one('#local-status', Static).render())
            assert screen.query_one('#local-error', TextArea).text == 'Local service did not become ready.'
            assert 'raw secret' not in screen.query_one('#local-error', TextArea).text
            assert await screen.close_owned()

    asyncio.run(exercise())


def test_unexpected_listener_exit_renders_failure_after_cleanup(tmp_path, monkeypatch):
    import sys
    from textual.widgets import Static, TextArea
    from test_tui import fixture

    async def exercise():
        stop_requests = []

        class Owner:
            pending_cleanup = None

            def __init__(self, *args, on_event, **kwargs):
                self.emit = on_event

            async def start(self):
                return LocalSnapshot('ready')

            async def wait(self):
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('stopping')))
                await asyncio.sleep(0)
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('stopped')))
                return 3

            def request_stop(self):
                stop_requests.append(True)

            async def aclose(self):
                pass

        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            await screen.run_task
            assert screen.status == 3 and screen.finished
            assert screen.snapshot.phase == 'stopped'
            assert 'Run local: failed' in str(screen.query_one('#local-status', Static).render())
            assert screen.query_one('#local-error', TextArea).text == 'Local operation ended unexpectedly.'
            assert stop_requests == []
            assert await screen.close_owned()

    asyncio.run(exercise())


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
            await pilot.press('enter')
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
        screen.refresh_status()
        screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=())))
        screen.receive(NS(kind='other'))
        assert screen.check_action('copy_choice', ()) is False
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
                from api_agent.providers.control import CleanupPendingError
                assert isinstance(error, CleanupPendingError)
                assert owner.pending_cleanup is pending
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
        await screen.on_key(NS(character='0', key='0', stop=lambda: None))
        assert screen.source == 'service-10'
        screen.action_browsers()
        await screen.on_key(NS(character='0', key='0', stop=lambda: None))
        assert opened == ['https://localhost/10']
        assert 'Could not open the host browser' in screen.failure
    asyncio.run(exercise())


def test_live_menu_buttons_navigation_hotkeys_and_copy(monkeypatch):
    async def exercise():
        opened, copied, callbacks = [], [], []
        async def open_url(url): opened.append(url)
        async def copy_text(app, value):
            copied.append(value)
            return True
        monkeypatch.setattr('api_agent.runtime.local_terminal.open_host_url', open_url)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy_text)
        stop = asyncio.Event()
        names = tuple(f'Source {i}' for i in range(12)) + ('Ω [bold]',)
        destinations = tuple(LocalDestination(name, f'https://localhost/{i}')
                             for i, name in enumerate(names))
        class Owner:
            pending_cleanup = None
            def diagnostic(self, error): return str(error)
            def __init__(self, *args, on_event, **kwargs): callbacks.append(on_event)
            async def start(self): return LocalSnapshot('ready', log_sources=names,
                                                        destinations=destinations)
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': names[0]}), owner_factory=Owner)
        app = Harness(screen)
        native_copies = []
        app.copy_to_clipboard = native_copies.append
        async with app.run_test() as pilot:
            await pilot.pause()
            callbacks[0](LocalEvent('log', source=names[0], text='selected log text'))
            area = screen.query_one('#local-output')
            area.focus()
            area.action_select_all()
            assert area.selected_text == 'selected log text'
            await pilot.press('ctrl+c')
            assert copied == ['selected log text']
            assert native_copies == []
            screen.action_browsers()
            await pilot.pause()
            first = app.focused
            assert first.id.endswith('-0')
            assert screen.query_one('#local-output').display is False
            callbacks[0](LocalEvent('log', source=names[0], text='still live\n'))
            assert app.focused is first
            assert screen.query_one('#local-output').text == 'selected log textstill live\n'
            await pilot.press('j')
            assert app.focused.id.endswith('-2')
            await pilot.press('k')
            assert app.focused is first
            await pilot.press('l')
            assert app.focused.id.endswith('-1')
            await pilot.press('h')
            assert app.focused is first
            await pilot.press('ctrl+c')
            assert copied == ['selected log text', 'https://localhost/0']
            await pilot.press('enter')
            assert opened == ['https://localhost/0']
            await pilot.press('0')
            assert opened[-1] == 'https://localhost/9'
            assert len(opened) == 2
            screen.action_sources()
            await pilot.pause()
            await pilot.press('ctrl+c')
            assert copied[-1] == names[0]
            buttons = list(screen.query('#local-choices Button'))
            assert len(buttons) == 14
            buttons[12].focus()
            await pilot.press('enter')
            assert screen.source == names[12]
            assert screen.menu is None
            assert screen.query_one('#local-output').display is True
            assert await screen.close_owned()
    asyncio.run(exercise())


def test_menu_copy_guards_empty_choices_and_all_logs(monkeypatch):
    from textual.widgets import Button
    from api_agent.tui.clipboard import ClipboardError

    async def exercise():
        async def failed_copy(app, text): raise ClipboardError('unavailable')
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', failed_copy)
        stop = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready')
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'first'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await screen.action_copy_choice()
            screen.open_menu('browsers', ())
            await pilot.pause()
            assert list(screen.query('#local-choices Button')) == []
            assert screen.check_action('copy_choice', ()) is False
            await screen.on_key(NS(key='down', prevent_default=lambda: None, stop=lambda: None))
            await screen.activate_choice(0)
            screen.open_menu('logs', ('first',))
            await pilot.pause()
            assert screen.check_action('sources', ()) is False
            first = app.focused
            await screen.action_copy_choice()
            assert app.focused is first
            all_logs = list(screen.query('#local-choices Button'))[-1]
            all_logs.focus()
            await screen.action_copy_choice()
            await pilot.press('enter')
            assert screen.source == 'all'
            assert screen.menu is None
            screen.open_menu('logs', ('first',))
            await pilot.pause()
            old = app.focused
            screen.open_menu('logs', ('second',))
            await pilot.pause()
            await screen.on_button_pressed(NS(button=old, stop=lambda: None))
            assert screen.menu_choices == ('second',)
            await screen.on_button_pressed(NS(button=Button('Other'), stop=lambda: None))
            assert screen.menu == 'logs'
            screen.query_one('#local-output').focus()
            await screen.action_copy_choice()
            pane = screen.query_one('#local-choices')
            stale = Button('Stale', id='local-choice-0-0')
            invalid = Button('Missing', id=f'local-choice-{screen.menu_generation}-99')
            other = Button('Other', id='other-choice')
            await pane.mount(stale, invalid, other)
            other.focus()
            await pilot.pause()
            assert screen.check_action('copy_choice', ()) is False
            await screen.action_copy_choice()
            stale.focus()
            await pilot.pause()
            await screen.action_copy_choice()
            invalid.focus()
            await pilot.pause()
            await screen.action_copy_choice()
            assert await screen.close_owned()
    asyncio.run(exercise())


def test_status_reconciles_open_menu_and_preserves_focused_destination(monkeypatch):
    async def exercise():
        opened, callbacks = [], []
        async def open_url(url): opened.append(url)
        monkeypatch.setattr('api_agent.runtime.local_terminal.open_host_url', open_url)
        stop = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, on_event, **kwargs): callbacks.append(on_event)
            async def start(self): return LocalSnapshot('starting')
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        a = LocalDestination('A', 'https://localhost/a')
        b = LocalDestination('B', 'https://localhost/b')
        c = LocalDestination('C', 'https://localhost/c')
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('b')
            assert screen.menu_choices == ()
            assert 'Enter Open' in str(screen.query_one('#local-hint').render())
            callbacks[0](LocalEvent('status', snapshot=LocalSnapshot('ready', destinations=(a, b))))
            await pilot.pause()
            assert len(list(screen.query('#local-choices Button'))) == 2
            assert app.focused.id.endswith('-0')
            await pilot.press('right')
            focused = app.focused
            assert focused.id.endswith('-1')
            callbacks[0](LocalEvent('log', source='agent', text='event\n'))
            callbacks[0](LocalEvent('status', snapshot=LocalSnapshot('ready', destinations=(a, b))))
            assert app.focused is focused
            callbacks[0](LocalEvent('status', snapshot=LocalSnapshot('ready', destinations=(c, a, b))))
            await pilot.pause()
            assert screen.menu_choices == (c, a, b)
            assert app.focused.id.endswith('-2')
            await screen.on_button_pressed(NS(button=focused, stop=lambda: None))
            assert opened == []
            await pilot.press('enter')
            assert opened == [b.url]
            callbacks[0](LocalEvent('status', snapshot=LocalSnapshot('ready', destinations=(c, a))))
            await pilot.pause()
            assert app.focused.id.endswith('-1')
            await pilot.press('enter')
            assert opened[-1] == a.url
            screen.action_sources()
            await pilot.pause()
            callbacks[0](LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=('new',))))
            await pilot.pause()
            assert screen.menu_choices == ('new',)
            await pilot.press('right')
            assert app.focused.id.endswith('-all')
            callbacks[0](LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=('new', 'next'))))
            await pilot.pause()
            assert app.focused.id.endswith('-all')
            await pilot.press('escape')
            assert screen.menu is None
            assert 'l Log sources' in str(screen.query_one('#local-hint').render())
            assert await screen.close_owned()
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


def test_unsettled_consumer_task_retains_bounded_retry_without_an_owner(monkeypatch):
    from contextlib import asynccontextmanager
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.progress import component_timeout
    from api_agent.tui import local_run
    @asynccontextmanager
    async def short_settlement(name, timeout):
        async with component_timeout(name, .02) as budget:
            yield budget
    monkeypatch.setattr(local_run, 'component_timeout', short_settlement)
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def late_task():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
        screen.run_task = asyncio.create_task(late_task())
        await entered.wait()
        try:
            assert not await screen.close_owned()
            assert not screen.disposed
            assert 'Local presentation shutdown remains pending' in screen.failure
            receipt = screen.pending_cleanup
            with pytest.raises(CleanupPendingError):
                await receipt.retry()
            assert screen.pending_cleanup is not None
        finally:
            release.set()
            await screen.run_task
        assert await screen.close_owned()
        assert screen.pending_cleanup is None
    asyncio.run(exercise())


def test_interrupted_owner_disposal_retains_presentation_receipt():
    from unittest.mock import AsyncMock
    from api_agent.providers.control import CleanupPendingError
    async def exercise():
        interruption = asyncio.CancelledError()
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
        screen.owner = NS(request_stop=lambda: None, aclose=AsyncMock(side_effect=interruption))
        with pytest.raises(asyncio.CancelledError) as caught:
            await screen.close_owned()
        assert caught.value is interruption
        assert isinstance(screen.pending_cleanup, CleanupPendingError)
        assert caught.value.cleanup_pending is screen.pending_cleanup
        assert not screen.disposed
    asyncio.run(exercise())


def test_consumer_wait_timeout_does_not_cancel_the_owned_task(monkeypatch):
    from api_agent.tui import local_run
    async def exercise():
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
        screen.run_task = task
        async def elapsed_wait(tasks, *, timeout):
            assert tasks == {task}
            assert 0 < timeout <= 15
            return set(), tasks
        monkeypatch.setattr(local_run.asyncio, 'wait', elapsed_wait)
        try:
            with pytest.raises(TimeoutError, match='presentation shutdown remains pending'):
                await screen.settle_run_task()
            assert not task.done()
        finally:
            release.set()
            await task
    asyncio.run(exercise())


@pytest.mark.parametrize('native', [True, False])
def test_selected_source_log_copy_uses_shared_clipboard_and_preserves_refresh(monkeypatch, native):
    from textual.widgets import TextArea

    async def exercise():
        copied, notices, callbacks = [], [], []
        async def copy(app, value):
            copied.append(value)
            return native
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        stop = asyncio.Event()
        snapshot = LocalSnapshot('ready', log_sources=('agent', 'ui'))
        class Owner:
            def __init__(self, *args, on_event, **kwargs): callbacks.append(on_event)
            async def start(self): return snapshot
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        app.notify = lambda message, **kwargs: notices.append((message, kwargs))
        async with app.run_test() as pilot:
            await pilot.pause()
            callbacks[0](LocalEvent('log', source='ui', text='visible selected text\n'))
            await pilot.press('l')
            await pilot.pause()
            await pilot.press('2')
            area = screen.query_one('#local-output', TextArea)
            assert app.focused is area
            area.action_select_all()
            selected = area.selected_text
            selection = area.selection
            callbacks[0](LocalEvent('status', snapshot=snapshot))
            callbacks[0](LocalEvent('log', source='agent', text='filtered out\n'))
            assert area.selection == selection
            assert area.selected_text == selected == 'visible selected text\n'
            callbacks[0](LocalEvent('log', source='ui', text='new tail\n'))
            assert area.selected_text == selected
            assert app.focused is area
            assert copied == []
            await pilot.press('ctrl+c')
            assert copied == [selected]
            assert notices[-1][0] == ('Value copied.' if native else 'Value sent to terminal clipboard.')
            assert not stop.is_set() and not screen.finished
            assert area.selected_text == selected
            from collections import deque
            screen.events = deque(screen.events, maxlen=2)
            callbacks[0](LocalEvent('log', source='ui', text='replacement one\n'))
            callbacks[0](LocalEvent('log', source='ui', text='replacement two\n'))
            assert area.text == 'replacement one\nreplacement two\n'
            assert area.selected_text == ''
            screen.events = deque(screen.events, maxlen=3)
            callbacks[0](LocalEvent('log', source='ui', text='last tail\n'))
            assert area.text == 'replacement one\nreplacement two\nlast tail\n'
            assert area.selected_text == ''
            assert app.focused is area
            assert copied == [selected]
            await screen.close_owned()
    asyncio.run(exercise())


def test_selected_log_copy_failure_keeps_operation_and_selection(monkeypatch):
    from api_agent.tui.clipboard import ClipboardError

    async def exercise():
        notices, copied = [], []
        async def copy(app, value):
            copied.append(value)
            raise ClipboardError('unavailable')
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        stop = asyncio.Event()
        class Owner:
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready')
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        app.notify = lambda message, **kwargs: notices.append((message, kwargs))
        async with app.run_test() as pilot:
            await pilot.pause()
            screen.receive(LocalEvent('log', source='agent', text='copy this'))
            area = screen.query_one('#local-output')
            area.focus()
            assert not screen.check_action('copy_choice', ())
            area.action_select_all()
            await pilot.press('ctrl+c')
            assert copied == ['copy this']
            assert notices[-1][1]['severity'] == 'error'
            assert area.selected_text == 'copy this' and app.focused is area
            assert not stop.is_set() and not screen.finished
            await screen.close_owned()
    asyncio.run(exercise())


def test_visible_error_selection_copies_only_current_diagnostic(monkeypatch):
    from textual.widgets import TextArea

    async def exercise():
        copied = []

        async def copy(app, value):
            copied.append(value)
            return True

        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        stop = asyncio.Event()

        class Owner:
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready')
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass

        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test(size=(50, 14)) as pilot:
            await pilot.pause()
            error = screen.query_one('#local-error', TextArea)
            output = screen.query_one('#local-output', TextArea)
            assert error.display is False
            assert error.region.height == 0
            assert output.region.height >= 9
            screen.failure = 'First diagnostic\nSecond diagnostic'
            screen.refresh_view()
            await pilot.pause()
            assert error.display is True
            assert error.region.y > screen.query_one('#local-hint').region.y
            error.focus()
            error.action_select_all()
            screen.refresh_status()
            assert error.selected_text == 'First diagnostic\nSecond diagnostic'
            await pilot.press('ctrl+c')
            assert copied == ['First diagnostic\nSecond diagnostic']

            screen.failure = 'Current diagnostic'
            screen.refresh_view()
            assert error.selected_text == ''
            assert not screen.check_action('copy_choice', ())
            await screen.action_copy_choice()
            assert copied == ['First diagnostic\nSecond diagnostic']
            error.action_select_all()
            await pilot.press('ctrl+c')
            assert copied[-1] == 'Current diagnostic'

            screen.failure = ''
            screen.refresh_view()
            assert error.text == ''
            assert error.display is False
            assert not screen.check_action('copy_choice', ())
            await screen.action_copy_choice()
            assert copied[-1] == 'Current diagnostic'

            screen.receive(LocalEvent('log', source='agent', text='log text'))
            output.focus()
            output.action_select_all()
            await pilot.press('ctrl+c')
            assert copied[-1] == 'log text'
            assert await screen.close_owned()

    asyncio.run(exercise())


def test_error_selection_copies_while_menu_is_open(monkeypatch):
    from textual.widgets import TextArea

    async def exercise():
        copied = []

        async def copy(app, value):
            copied.append(value)
            return True

        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        stop = asyncio.Event()

        class Owner:
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready', log_sources=('agent',))
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass

        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test(size=(50, 14)) as pilot:
            await pilot.pause()
            screen.failure = 'Selectable diagnostic'
            screen.refresh_view()
            screen.action_sources()
            await pilot.pause()
            error = screen.query_one('#local-error', TextArea)
            assert error.display is True
            error.focus()
            error.action_select_all()
            assert screen.check_action('copy_choice', ())
            await pilot.press('ctrl+c')
            assert copied == ['Selectable diagnostic']
            assert screen.menu == 'logs'
            assert screen.query_one('#local-output', TextArea).display is False
            screen.query_one('#local-choices Button').focus()
            await pilot.press('ctrl+c')
            assert copied == ['Selectable diagnostic', 'agent']
            assert await screen.close_owned()

    asyncio.run(exercise())


def test_diagnostic_display_limits_are_independent_of_full_output():
    screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
    for index in range(1030):
        screen.receive(LocalEvent('diagnostic', source='agent', text=f'event {index}\n',
                                  diagnostic={'transient': 'not retained'}))
    assert len(screen.events) == 1000
    assert all(event.diagnostic is None for event in screen.events)
    assert screen.events[0].text == 'event 30\n'
    for _ in range(40):
        screen.receive(LocalEvent('diagnostic', source='agent', text='😀' * 20000))
    assert screen._event_bytes <= 1048576
    assert len(screen.events) < 40
    assert screen.events[-1].text.endswith('[TUI display truncated]\n')
    assert screen._event_bytes == sum(len(event.text.encode()) for event in screen.events)


def test_dashboard_credentials_are_never_requested_or_shown_in_interactive_status():
    from unittest.mock import Mock

    async def exercise():
        stop = asyncio.Event()
        class Owner:
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready')
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass
            interactive_dashboard_credentials = Mock(return_value=('admin@example.test', '[red]literal-password[/red]'))
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = screen.query_one('#local-status').render().plain
            Owner.interactive_dashboard_credentials.assert_not_called()
            assert screen.failure == ''
            assert 'dashboard account' not in text
            assert 'dashboard password' not in text
            assert 'admin@example.test' not in text
            assert 'literal-password' not in text
            assert not screen.events
            assert 'literal-password' not in repr(screen.snapshot)
            await screen.close_owned()
    asyncio.run(exercise())


@pytest.mark.parametrize('key', ['*', 'asterisk'])
@pytest.mark.parametrize('location', ['source-menu', 'all-button', 'source-view', 'browser-menu'])
def test_all_logs_hotkey_respects_active_menu(monkeypatch, key, location):
    from textual.widgets import TextArea

    async def exercise():
        opened = []
        async def open_url(url): opened.append(url)
        monkeypatch.setattr('api_agent.runtime.local_terminal.open_host_url', open_url)
        stop = asyncio.Event()
        class Owner:
            def __init__(self, *args, **kwargs): pass
            async def start(self):
                return LocalSnapshot('ready', log_sources=('agent', 'ui'),
                                     destinations=(LocalDestination('Studio', 'http://localhost/'),))
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass

        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen.receive(LocalEvent('log', source='agent', text='agent event\n'))
            screen.receive(LocalEvent('log', source='ui', text='ui event\n'))
            await pilot.press('l')
            await pilot.pause()
            if location == 'all-button':
                await pilot.press('down', 'down')
                assert app.focused.id.endswith('-all')
            elif location == 'source-view':
                await pilot.press('2')
                assert screen.source == 'ui'
                assert isinstance(app.focused, TextArea)
            elif location == 'browser-menu':
                await pilot.press('escape', 'b')
                await pilot.pause()
                assert screen.menu == 'browsers'
            else:
                assert app.focused.id.endswith('-0')
            focused = app.focused

            await pilot.press(key)

            assert opened == []
            if location == 'browser-menu':
                assert screen.menu == 'browsers'
                assert screen.source == 'agent'
                assert app.focused is focused
            else:
                assert screen.menu is None
                assert screen.source == 'all'
                assert app.focused is screen.query_one('#local-output', TextArea)
                assert screen.query_one('#local-output', TextArea).text == '[agent] agent event\n[ui] ui event\n'
            assert await screen.close_owned()
    asyncio.run(exercise())


@pytest.mark.parametrize('location', ['text-area', 'logs-menu'])
def test_all_logs_during_blocked_startup_does_not_stop_owned_run(location):
    from textual.widgets import TextArea

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        stop_requests, phases = [], []

        class Owner:
            pending_cleanup = None

            def __init__(self, *args, on_event, **kwargs):
                self.emit = on_event

            async def start(self):
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('starting',
                    log_sources=('agent', 'api'))))
                phases.append('starting')
                entered.set()
                await release.wait()
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('ready',
                    log_sources=('agent', 'api'))))
                phases.append('ready')
                return LocalSnapshot('ready', log_sources=('agent', 'api'))

            async def wait(self):
                await asyncio.Event().wait()

            def request_stop(self):
                stop_requests.append(True)

            async def aclose(self):
                pass

        screen = LocalRunScreen(None,
            lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await entered.wait()
            await pilot.pause()
            area = screen.query_one('#local-output', TextArea)
            area.focus()
            if location == 'logs-menu':
                await pilot.press('l')
                await pilot.pause()
                assert screen.menu == 'logs'

            before = screen.run_task
            await pilot.press('asterisk')
            await pilot.pause()

            assert screen.source == 'all'
            assert screen.menu is None
            assert app.focused is area
            assert screen.run_task is before and not before.done()
            assert stop_requests == []
            assert phases == ['starting']
            assert screen.snapshot.phase == 'starting'
            release.set()
            await pilot.pause()
            assert phases == ['starting', 'ready']
            assert screen.snapshot.phase == 'ready'
            before.cancel()
            await before

    asyncio.run(exercise())


@pytest.mark.parametrize('location', ['text-area', 'logs-menu'])
def test_actual_app_all_logs_during_blocked_startup_keeps_run_active(tmp_path, monkeypatch, location):
    import sys
    from textual.widgets import TextArea, Static
    from test_tui import fixture

    async def exercise():
        entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        stop_requests, phases = [], []

        class Owner:
            pending_cleanup = None

            def __init__(self, *args, on_event, **kwargs):
                self.emit = on_event

            async def start(self):
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('starting',
                    log_sources=('agent', 'api'), activity='Checking local services')))
                self.emit(LocalEvent('log', source='api', text='api startup event\n'))
                phases.append('starting')
                entered.set()
                await release.wait()
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('ready',
                    log_sources=('agent', 'api'))))
                phases.append('ready')
                return LocalSnapshot('ready', log_sources=('agent', 'api'))

            async def wait(self):
                await stop.wait()
                return 0

            def request_stop(self):
                stop_requests.append(True)
                stop.set()

            async def aclose(self):
                release.set()

        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test() as pilot:
            await entered.wait()
            await pilot.pause()
            screen = app.screen
            area = screen.query_one('#local-output', TextArea)
            area.focus()
            if location == 'logs-menu':
                await pilot.press('l')
                await pilot.pause()
                assert screen.menu == 'logs'

            before = screen.run_task
            await pilot.press('asterisk')
            await pilot.pause()

            assert screen.source == 'all'
            assert screen.menu is None
            assert app.focused is area
            assert before is screen.run_task and not before.done()
            assert stop_requests == []
            assert phases == ['starting']
            assert screen.snapshot.phase == 'starting'
            assert 'Checking local services' in str(screen.query_one('#local-status', Static).render())
            assert 'api startup event' in area.text
            release.set()
            await pilot.pause()
            assert phases == ['starting', 'ready']
            assert screen.snapshot.phase == 'ready'
            assert await screen.close_owned()

    asyncio.run(exercise())


def test_all_logs_can_reveal_a_startup_failure_that_precedes_keypress(tmp_path, monkeypatch):
    import sys
    from test_tui import fixture

    async def exercise():
        entered, fail = asyncio.Event(), asyncio.Event()
        stop_requests, phases = [], []

        class Owner:
            pending_cleanup = None

            def __init__(self, *args, on_event, **kwargs):
                self.emit = on_event

            async def start(self):
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('starting',
                    log_sources=('agent', 'api'))))
                phases.append('starting')
                entered.set()
                await fail.wait()
                self.emit(LocalEvent('status', snapshot=LocalSnapshot('stopped',
                    log_sources=('agent', 'api'))))
                phases.append('stopped')
                raise RuntimeError('Synthetic startup failure')

            def diagnostic(self, error):
                return str(error)

            def request_stop(self):
                stop_requests.append(True)

            async def aclose(self):
                pass

        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', NS(LocalRunOwner=Owner))
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test() as pilot:
            await entered.wait()
            screen = app.screen
            fail.set()
            await screen.run_task
            assert screen.snapshot.phase == 'stopped'
            assert screen.status == 1
            assert stop_requests == []
            assert phases == ['starting', 'stopped']

            await pilot.press('asterisk')
            await pilot.pause()

            assert screen.source == 'all'
            assert screen.snapshot.phase == 'stopped'
            assert screen.status == 1
            assert stop_requests == []
            assert phases == ['starting', 'stopped']
            assert screen.failure == 'Synthetic startup failure'
            from textual.widgets import Static
            assert 'Run local: failed' in str(screen.query_one('#local-status', Static).render())
            assert await screen.close_owned()

    asyncio.run(exercise())


@pytest.mark.parametrize('updated_sources', [('agent', 'ui', 'worker'), ('ui',), ()])
def test_all_logs_focus_survives_source_reconciliation(updated_sources):
    async def exercise():
        stop = asyncio.Event()
        class Owner:
            def __init__(self, *args, **kwargs): pass
            async def start(self):
                return LocalSnapshot('ready', log_sources=('agent', 'ui'))
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): stop.set()
            async def aclose(self): pass

        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('l', 'down', 'down')
            previous = app.focused
            assert previous.id.endswith('-all')

            screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=updated_sources)))
            await pilot.pause()

            assert screen.menu_choices == updated_sources
            assert app.focused is not previous
            assert app.focused.id == f'local-choice-{screen.menu_generation}-all'
            assert screen.check_action('copy_choice', ()) is False
            await pilot.press('enter')
            assert screen.source == 'all'
            assert screen.menu is None
            assert app.focused is screen.query_one('#local-output')
            assert await screen.close_owned()
    asyncio.run(exercise())


@pytest.fixture
def reconciling_screen():
    stop = asyncio.Event()
    class Owner:
        def __init__(self, *args, **kwargs): pass
        async def start(self):
            return LocalSnapshot('ready', log_sources=('agent', 'ui'), destinations=(
                LocalDestination('First', 'http://localhost/first'),
                LocalDestination('Second', 'http://localhost/second')))
        async def wait(self):
            await stop.wait()
            return 0
        def request_stop(self): stop.set()
        async def aclose(self): pass
    return LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)


@pytest.mark.parametrize('updates', [
    (('agent', 'ui', 'worker'), ('ui', 'worker')),
    (('ui',), (), ('worker', 'ui')),
    (('ui', 'worker'), ()),
])
def test_all_logs_focus_survives_rapid_source_updates(reconciling_screen, updates):
    async def exercise():
        screen = reconciling_screen
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('l', 'down', 'down')
            stale = app.focused
            for sources in updates:
                screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=sources)))
            await pilot.pause()
            assert app.focused.id == f'local-choice-{screen.menu_generation}-all'
            assert screen._pending_menu_focus is None
            await screen.on_button_pressed(NS(button=stale, stop=lambda: None))
            assert screen.menu == 'logs'
            assert screen.source == 'agent'
            await pilot.press('enter')
            assert screen.source == 'all'
            assert screen.menu is None
            assert await screen.close_owned()
    asyncio.run(exercise())


def test_named_source_focus_survives_rapid_reordering(reconciling_screen):
    async def exercise():
        screen = reconciling_screen
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('l', 'right')
            for sources in (('ui', 'agent'), ('worker', 'agent', 'ui'), ('worker', 'ui')):
                screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=sources)))
            await pilot.pause()
            assert app.focused.id == f'local-choice-{screen.menu_generation}-1'
            await pilot.press('enter')
            assert screen.source == 'ui'
            assert await screen.close_owned()
    asyncio.run(exercise())


def test_browser_focus_survives_rapid_reordering_without_activation(monkeypatch, reconciling_screen):
    async def exercise():
        opened = []
        async def open_url(url): opened.append(url)
        monkeypatch.setattr('api_agent.runtime.local_terminal.open_host_url', open_url)
        screen = reconciling_screen
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('b', 'right')
            first, second = screen.menu_choices
            stale = app.focused
            for destinations in ((second, first), (second,), (first, second)):
                screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', destinations=destinations)))
            await pilot.pause()
            assert app.focused.id == f'local-choice-{screen.menu_generation}-1'
            assert opened == []
            await screen.on_button_pressed(NS(button=stale, stop=lambda: None))
            assert opened == []
            await pilot.press('enter')
            assert opened == [second.url]
            assert screen.menu == 'browsers'
            assert await screen.close_owned()
    asyncio.run(exercise())


def test_user_focus_overrides_pending_menu_focus(monkeypatch, reconciling_screen):
    async def exercise():
        screen = reconciling_screen
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('l', 'down', 'down')
            callbacks = []
            monkeypatch.setattr(screen, 'call_after_refresh', lambda callback: callbacks.append(callback))
            screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=('ui', 'agent'))))
            await pilot.pause()
            assert screen._pending_menu_focus is not None
            await pilot.press('right')
            assert app.focused.id.endswith('-1')
            screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=('worker', 'ui', 'agent'))))
            await pilot.pause()
            for callback in callbacks:
                callback()
            await pilot.pause()
            assert app.focused.id == f'local-choice-{screen.menu_generation}-2'
            assert screen._pending_menu_focus is None
            await pilot.press('enter')
            assert screen.source == 'agent'
            assert await screen.close_owned()
    asyncio.run(exercise())


def test_pending_focus_callback_respects_user_navigation_and_closed_menu(monkeypatch, reconciling_screen):
    async def exercise():
        screen = reconciling_screen
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press('l', 'down', 'down')
            callbacks = []
            monkeypatch.setattr(screen, 'call_after_refresh', lambda callback: callbacks.append(callback))
            screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=('ui', 'agent'))))
            await pilot.pause()
            await pilot.press('right')
            focused = app.focused
            assert focused.id.endswith('-1')

            for callback in callbacks:
                callback()
            callbacks.clear()

            assert app.focused is focused
            assert screen._pending_menu_focus is None
            screen.receive(LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=('agent', 'ui'))))
            await pilot.press('escape')
            for callback in callbacks:
                callback()
            callbacks.clear()
            assert screen.menu is None
            assert screen._pending_menu_focus is None
            assert app.focused is screen.query_one('#local-output')
            assert await screen.close_owned()
    asyncio.run(exercise())


@pytest.mark.parametrize('action', ['stop', 'quit'])
def test_interactive_closing_renders_owned_component_progress(action):
    from textual.widgets import Static
    from api_agent.runtime.local_contracts import LocalComponent
    async def exercise():
        entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, on_event, **kwargs): self.event = on_event
            async def start(self): return LocalSnapshot('ready')
            async def wait(self):
                await stop.wait()
                return 0
            def request_stop(self): pass
            async def aclose(self):
                self.event(LocalEvent('status', LocalSnapshot('stopping', components=(
                    LocalComponent('agent', 'cleanup scope'),
                    LocalComponent('profile:extension-profile', 'cleanup scope')))))
                entered.set()
                await release.wait()
                stop.set()
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            if action == 'stop':
                close = asyncio.create_task(pilot.press('s'))
            else:
                await pilot.press('q')
                await pilot.pause()
                from textual.widgets import Input
                app.screen.query_one('#quit-answer', Input).focus()
                close = asyncio.create_task(pilot.press('y', 'enter'))
            await entered.wait()
            try:
                assert screen.closing
                rendered = str(screen.query_one('#local-status', Static).render())
                assert 'Run local: stopping' in rendered
                assert 'profile:extension-profile: cleanup scope' in rendered
            finally:
                release.set()
                if close is not None:
                    await close
                else:
                    await stop.wait()
                    await pilot.pause()
    asyncio.run(exercise())


@pytest.mark.parametrize('action', ['s', 'q'])
def test_real_controller_stop_keeps_screen_and_retries_visible_pending(action):
    from api_agent.runtime.local_run import LocalRunOwner
    from textual.widgets import Static, Input
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def held():
            calls.append('cleanup')
            entered.set()
            await release.wait()
        class Owner(LocalRunOwner):
            CLEANUP_ATTEMPT_SECONDS = .01
            CLEANUP_RETRY_SECONDS = .01
            async def start(self):
                self._started = True
                self._planned_components = ('agent', 'profile:extension-profile')
                self._stack.push(held)
                self._publish('ready')
                return self.snapshot
        screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
        app = Harness(screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            if action == 'q':
                await pilot.press('q')
                app.screen.query_one('#quit-answer', Input).focus()
                close = asyncio.create_task(pilot.press('y', 'enter'))
            else:
                close = asyncio.create_task(pilot.press('s'))
            try:
                await entered.wait()
                while screen.snapshot.phase != 'cleanup-pending':
                    await asyncio.sleep(.001)
                assert not screen.disposed
                assert screen.closing
                assert not screen.run_task.done()
                assert screen.owner.pending_cleanup is not None
                assert calls == ['cleanup']
                rendered = str(screen.query_one('#local-status', Static).render())
                assert 'cleanup-pending' in rendered
                assert 'profile:extension-profile: cleanup scope' in rendered
            finally:
                release.set()
                await close
                await screen.run_task
            assert screen.disposed
            assert screen.owner.pending_cleanup is None
            assert screen.snapshot.phase == 'stopped'
            assert calls == ['cleanup']
    asyncio.run(exercise())


@pytest.mark.parametrize('automatic', [False, True])
def test_start_failure_keeps_owned_cleanup_in_automatic_retry(automatic):
    from unittest.mock import AsyncMock
    class Owner:
        pending_cleanup = object()
        def __init__(self, *args, **kwargs):
            if automatic:
                self.finish_cleanup = AsyncMock()
        async def start(self): raise RuntimeError('startup failed')
        def diagnostic(self, error): return 'safe startup failure'
    screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}), owner_factory=Owner)
    asyncio.run(screen.run())
    if automatic:
        screen.owner.finish_cleanup.assert_awaited_once()
    assert screen.finished and screen.status == 1
    assert screen.failure == 'safe startup failure'


@pytest.mark.parametrize('kind,height', [('logs', 3), ('browsers', 4)])
@pytest.mark.parametrize('size', [(100, 30), (44, 12)])
def test_two_column_menus_render_and_scroll_unbounded_choices(reconciling_screen, kind, height, size):
    async def exercise():
        screen = reconciling_screen
        app = Harness(screen)
        names = tuple(f'Pack {index}' for index in range(25))
        destinations = tuple(LocalDestination(name, f'http://localhost/{index}')
                             for index, name in enumerate(names))
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            screen.open_menu(kind, names if kind == 'logs' else destinations)
            await pilot.pause()
            buttons = list(screen.query('#local-choices Button'))
            assert len(buttons) == 25 + (kind == 'logs')
            assert buttons[0].region.y == buttons[1].region.y
            assert buttons[0].region.right <= buttons[1].region.x
            assert buttons[2].region.x == buttons[0].region.x
            assert buttons[2].region.y > buttons[0].region.y
            assert buttons[0].region.height == height
            if kind == 'browsers':
                lines = buttons[0].render_lines(buttons[0].content_region.translate(
                    (-buttons[0].region.x, -buttons[0].region.y)))
                assert 'Pack 0' in lines[0].text
                assert 'http://local' in lines[1].text
                if size[0] == 100:
                    assert destinations[0].url in lines[1].text
                assert str(buttons[0].label).splitlines() == ['1  Pack 0', '   http://localhost/0']
            await pilot.press('right', 'right', 'up')
            assert app.focused is buttons[1]
            await pilot.press('down')
            assert app.focused is buttons[3]
            await pilot.press('left', 'up')
            assert app.focused is buttons[0]
            await pilot.press('left', 'up')
            assert app.focused is buttons[0]
            buttons[-1].focus()
            await pilot.pause(0.5)
            pane = screen.query_one('#local-choices')
            assert pane.scroll_y > 0
            assert buttons[-1].region.y >= pane.content_region.y
            assert buttons[-1].region.bottom <= pane.content_region.bottom
            await pilot.press('down', 'right')
            assert app.focused is buttons[-1]
            buttons[23].focus()
            await pilot.press('down')
            assert app.focused is buttons[-1]
            assert await screen.close_owned()
    asyncio.run(exercise())


def test_browser_long_name_keeps_url_on_second_content_line(monkeypatch, reconciling_screen):
    async def exercise():
        copied = []
        async def copy_text(app, value):
            copied.append(value)
            return True
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy_text)
        screen = reconciling_screen
        destination = LocalDestination('Long browser destination name ' * 4,
                                       'http://localhost/' + 'long-path/' * 12)
        app = Harness(screen)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen.open_menu('browsers', (destination,))
            await pilot.pause()
            button = app.focused
            lines = button.render_lines(button.content_region.translate(
                (-button.region.x, -button.region.y)))
            assert 'Long browser' in lines[0].text
            assert 'http://localhost/' in lines[1].text
            await pilot.press('ctrl+c')
            assert copied == [destination.url]
            assert await screen.close_owned()
    asyncio.run(exercise())
