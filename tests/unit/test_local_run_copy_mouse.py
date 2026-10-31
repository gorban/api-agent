import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from textual.widgets import TextArea
from textual.widgets.text_area import Selection

from api_agent.runtime.local_contracts import LocalEvent, LocalSnapshot
from api_agent.tui.local_run import LocalRunScreen
from api_agent.tui.clipboard import ClipboardError
from test_tui import fixture


@pytest.mark.parametrize('target', ['header', 'error', 'log'])
def test_double_click_copies_selected_text_without_quit(tmp_path, monkeypatch, target):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready', log_sources=('agent',))
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        notices = []
        app.notify = lambda message, **options: notices.append((message, options))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, LocalRunScreen)
            screen.failure = 'listener_start_failed diagnostic'
            screen.receive(LocalEvent('log', source='agent', text='listener_start_failed log'))
            screen.refresh_view()
            await pilot.pause()
            if target != 'header':
                await pilot.double_click('#local-status', offset=(2, 0))
                assert screen.get_selected_text().startswith('Run local: ready')
            selector = {'header': '#local-status', 'error': '#local-error', 'log': '#local-output'}[target]
            assert await pilot.double_click(selector, offset=(2, 0))
            await pilot.pause()
            selected = screen.get_selected_text() if target == 'header' else screen.query_one(selector, TextArea).selected_text
            if target == 'header':
                assert selected.startswith('Run local: ready')
            else:
                assert selected == 'listener_start_failed'
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, selected)
            assert notices == [('Value copied.', {'timeout': 2})]
            assert app.screen is screen and not stopped.is_set()
            await screen.close_owned()
    asyncio.run(scenario())


def test_no_selection_ctrl_c_uses_the_normal_app_fallback(tmp_path, monkeypatch):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready')
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        notices = []
        app.notify = lambda message, **options: notices.append((message, options))
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert not screen.check_action('copy_choice', ())
            await pilot.press('ctrl+c')
            copy.assert_not_awaited()
            assert notices and notices[-1][1].get('title') == 'Do you want to quit?'
            assert app.screen is screen and not stopped.is_set()
            await screen.close_owned()
    asyncio.run(scenario())


@pytest.mark.parametrize('target', ['error', 'log'])
def test_drag_selection_survives_refresh_and_copies_exact_text(tmp_path, monkeypatch, target):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready', log_sources=('agent',))
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.failure = 'listener_start_failed diagnostic'
            screen.receive(LocalEvent('log', source='agent', text='listener_start_failed log'))
            screen.refresh_view()
            await pilot.pause()
            selector = '#local-error' if target == 'error' else '#local-output'
            await pilot.mouse_down(selector, offset=(1, 0))
            await pilot.hover(selector, offset=(9, 0))
            await pilot.mouse_up(selector, offset=(9, 0))
            area = screen.query_one(selector, TextArea)
            selected = area.selected_text
            assert selected and selected in 'listener_start_failed'
            screen.refresh_view()
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, selected)
            assert app.screen is screen and not stopped.is_set()
            await screen.close_owned()
    asyncio.run(scenario())


@pytest.mark.parametrize('fails', [False, True])
def test_header_selection_precedes_menu_copy_and_survives_failure(tmp_path, monkeypatch, fails):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready', log_sources=('agent',))
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(side_effect=ClipboardError('unavailable') if fails else None, return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            await pilot.double_click('#local-status', offset=(2, 0))
            selected = screen.get_selected_text()
            assert selected.startswith('Run local: ready')
            screen.action_sources()
            await pilot.pause()
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, selected)
            assert screen.menu == 'logs' and app.screen is screen and not stopped.is_set()
            assert screen.get_selected_text() == (selected if fails else None)
            await screen.close_owned()
    asyncio.run(scenario())


def test_double_click_whitespace_or_secondary_button_does_not_select_word(tmp_path, monkeypatch):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready')
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.failure = 'alpha   omega'
            screen.refresh_view()
            await pilot.pause()
            area = screen.query_one('#local-error', TextArea)
            await pilot.double_click(area, offset=(8, 0))
            assert area.selected_text == ''
            await pilot.double_click(area, offset=(2, 0), button=3)
            assert area.selected_text == ''
            await screen.close_owned()
    asyncio.run(scenario())


def test_focused_keyboard_selection_precedes_old_header_selection(tmp_path, monkeypatch):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready')
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.failure = 'Current diagnostic'
            screen.refresh_view()
            await pilot.pause()
            await pilot.double_click('#local-status', offset=(2, 0))
            assert screen.get_selected_text().startswith('Run local: ready')
            error = screen.query_one('#local-error', TextArea)
            error.focus()
            error.action_select_all()
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, 'Current diagnostic')
            await screen.close_owned()
    asyncio.run(scenario())


@pytest.mark.parametrize('target', ['error', 'log'])
@pytest.mark.parametrize('method', ['mouse', 'keyboard'])
@pytest.mark.parametrize('append', [False, True])
def test_new_header_selection_wins_over_old_output_selection(tmp_path, monkeypatch, target, method, append):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready', log_sources=('agent',))
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.failure = 'old diagnostic'
            screen.receive(LocalEvent('log', source='agent', text='old log'))
            screen.refresh_view()
            await pilot.pause()
            area = screen.query_one('#local-error' if target == 'error' else '#local-output', TextArea)
            if method == 'mouse':
                await pilot.double_click(area, offset=(2, 0))
            else:
                area.focus()
                area.action_select_all()
            assert area.selected_text
            await pilot.double_click('#local-status', offset=(2, 0))
            selected = screen.get_selected_text()
            assert selected.startswith('Run local: ready')
            if append:
                screen.receive(LocalEvent('log', source='agent', text='new log'))
                await pilot.pause()
                assert screen.get_selected_text() == selected
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, selected)
            await screen.close_owned()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['status', 'hidden'])
def test_changed_or_hidden_header_selection_does_not_copy_stale_output(tmp_path, monkeypatch, change):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready', log_sources=('agent',))
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.receive(LocalEvent('log', source='agent', text='old log'))
            await pilot.pause()
            area = screen.query_one('#local-output', TextArea)
            area.focus()
            area.action_select_all()
            await pilot.double_click('#local-status', offset=(2, 0))
            assert screen.get_selected_text()
            if change == 'status':
                screen.snapshot = LocalSnapshot('stopping', log_sources=('agent',))
                screen.refresh_view()
            else:
                screen.query_one('#local-status').display = False
            await pilot.pause()
            await pilot.press('ctrl+c')
            copy.assert_not_awaited()
            await screen.action_copy_choice()
            copy.assert_not_awaited()
            area.focus()
            area.selection = Selection.cursor((0, 0))
            area.action_select_all()
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, 'old log')
            await screen.close_owned()
    asyncio.run(scenario())


@pytest.mark.parametrize('target', ['error', 'log'])
@pytest.mark.parametrize('method', ['mouse', 'keyboard'])
def test_clearing_new_header_selection_does_not_revive_older_output(tmp_path, monkeypatch, target, method):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready', log_sources=('agent',))
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.failure = 'old error'
            screen.receive(LocalEvent('log', source='agent', text='old log'))
            await pilot.pause()
            area = screen.query_one('#local-error' if target == 'error' else '#local-output', TextArea)
            if method == 'mouse':
                await pilot.double_click(area, offset=(2, 0))
            else:
                area.focus()
                area.action_select_all()
            assert area.selected_text
            await pilot.double_click('#local-status', offset=(2, 0))
            await pilot.pause()
            assert screen.get_selected_text()
            await pilot.click('#local-status', offset=(2, 0))
            await pilot.pause()
            assert not screen.get_selected_text()
            await pilot.press('ctrl+c')
            copy.assert_not_awaited()
            area.focus()
            await pilot.press('f7')
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, 'old error' if target == 'error' else 'old log')
            await screen.close_owned()
    asyncio.run(scenario())


def test_reselect_same_output_range_after_header(tmp_path, monkeypatch):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready', log_sources=('agent',))
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.receive(LocalEvent('log', source='agent', text='old log'))
            await pilot.pause()
            area = screen.query_one('#local-output', TextArea)
            area.focus()
            await pilot.press('f7')
            assert area.selected_text == 'old log'
            await pilot.double_click('#local-status', offset=(2, 0))
            await pilot.pause()
            assert screen.get_selected_text()
            await pilot.press('f7')
            await pilot.press('ctrl+c')
            copy.assert_awaited_once_with(app, 'old log')
            await screen.close_owned()
    asyncio.run(scenario())


def test_unfocused_output_can_reselect_same_range_after_header(tmp_path, monkeypatch):
    async def scenario():
        stopped = asyncio.Event()
        class Owner:
            pending_cleanup = None
            def __init__(self, *args, **kwargs): pass
            async def start(self): return LocalSnapshot('ready', log_sources=('agent',))
            async def wait(self): await stopped.wait(); return 0
            def request_stop(self): stopped.set()
            async def aclose(self): pass
        monkeypatch.setitem(sys.modules, 'api_agent.runtime.local_run', SimpleNamespace(LocalRunOwner=Owner))
        copy = AsyncMock(return_value=True)
        monkeypatch.setattr('api_agent.tui.local_run.copy_text', copy)
        app = fixture(tmp_path, 'run-local', fields={
            'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False}})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            screen = app.screen
            screen.failure = 'old error'
            screen.receive(LocalEvent('log', source='agent', text='old log'))
            await pilot.pause()
            error = screen.query_one('#local-error', TextArea)
            output = screen.query_one('#local-output', TextArea)
            error.focus()
            await pilot.press('f7')
            output.focus()
            await pilot.press('f7')
            await pilot.double_click('#local-status', offset=(2, 0))
            await pilot.pause()
            error.focus()
            await pilot.press('f7', 'ctrl+c')
            copy.assert_awaited_once_with(app, 'old error')
            await screen.close_owned()
    asyncio.run(scenario())
