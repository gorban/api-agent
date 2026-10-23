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
    def shutdown_cleanup_receipt(self): return self.local.pending_cleanup


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
            assert app.focused.id.endswith('-1')
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
            await pilot.press('down')
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
            await pilot.press('down')
            assert app.focused.id.endswith('-all')
            callbacks[0](LocalEvent('status', snapshot=LocalSnapshot('ready', log_sources=('new', 'next'))))
            await pilot.pause()
            assert app.focused.id.endswith('-0')
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
