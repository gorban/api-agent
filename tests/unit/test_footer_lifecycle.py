"""Footer refreshes serialize removal/mounting and release cancelled work."""
import asyncio

import pytest
from textual._context import active_message_pump
from textual.app import App
from textual.binding import Binding
from textual.widgets import Button, Footer, Static
from textual.widgets._footer import FooterKey

from api_agent.tui.app import ButtonFooter


class FooterHarness(App):
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding('x', 'example', 'Initial')]

    def compose(self):
        yield Button('Action')
        yield ButtonFooter()


@pytest.mark.parametrize('cancel_first', [False, True])
def test_overlapping_footer_refreshes_preserve_latest_bindings(monkeypatch, cancel_first):
    async def scenario():
        app = FooterHarness()
        async with app.run_test() as pilot:
            await pilot.pause()
            footer = app.query_one(ButtonFooter)
            original = Footer.mount_all
            entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
            active, calls = 0, 0

            async def controlled_recompose(widget, *args, **kwargs):
                nonlocal active, calls
                calls += 1
                active += 1
                assert active == 1, 'Footer removal/mounting must not overlap'
                try:
                    if calls == 1:
                        entered.set()
                        await release.wait()
                    token = active_message_pump.set(widget)
                    try:
                        await original(widget, *args, **kwargs)
                    finally:
                        active_message_pump.reset(token)
                finally:
                    active -= 1

            monkeypatch.setattr(Footer, 'mount_all', controlled_recompose)
            async def first_refresh():
                token = active_message_pump.set(footer)
                try:
                    await footer.recompose()
                finally:
                    active_message_pump.reset(token)

            first = asyncio.create_task(first_refresh())
            await entered.wait()
            app._bindings.key_to_bindings['x'] = [Binding('x', 'example', 'Latest')]

            async def refresh_again():
                second_started.set()
                token = active_message_pump.set(footer)
                try:
                    await footer.recompose()
                finally:
                    active_message_pump.reset(token)

            second = asyncio.create_task(refresh_again())
            await second_started.wait()
            assert calls == 1
            if cancel_first:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            else:
                release.set()
                await first
            await second
            assert calls == 2
            assert len(footer.query('#tab-select-hint')) == 1
            assert any(key.description == 'Latest' for key in footer.query(FooterKey))
    asyncio.run(scenario())


def test_detached_footer_refresh_returns_without_mounting():
    async def scenario():
        app = FooterHarness()
        async with app.run_test() as pilot:
            await pilot.pause()
            footer = app.query_one(ButtonFooter)
            await footer.remove()
        await footer.recompose()
        assert not footer.is_attached
    asyncio.run(scenario())



def test_attached_footer_waits_for_initial_mount_before_binding_refresh(monkeypatch):
    async def scenario():
        app = FooterHarness()
        async with app.run_test() as pilot:
            await pilot.pause()
            footer = ButtonFooter()
            footer._bindings_ready = True
            original = footer.mount_all
            mounts = []

            def record_mount(nodes):
                mounts.append(nodes)
                return original(nodes)

            monkeypatch.setattr(footer, 'mount_all', record_mount)
            pending_mount = app.mount(footer)
            token = active_message_pump.set(footer)
            try:
                assert footer.is_attached and not footer.is_mounted
                app._bindings.key_to_bindings['x'] = [Binding('x', 'example', 'Latest')]
                await footer.recompose()
                assert mounts == []
                await pending_mount
                footer._bindings_ready = True
                await footer.recompose()
                assert len(footer.query('#tab-select-hint')) == 1
                assert any(key.description == 'Latest' for key in footer.query(FooterKey))
            finally:
                active_message_pump.reset(token)
    asyncio.run(scenario())



@pytest.mark.parametrize('during_removal', [False, True])
def test_footer_ignores_refresh_after_app_shutdown_starts(monkeypatch, during_removal):
    async def scenario():
        app = FooterHarness()
        async with app.run_test() as pilot:
            await pilot.pause()
            footer = app.query_one(ButtonFooter)
            mounts = []
            monkeypatch.setattr(footer, 'mount_all', lambda nodes: mounts.append(nodes))
            entered, release = asyncio.Event(), asyncio.Event()
            original_remove = footer.remove_children
            original_children = tuple(footer.children)

            async def paused_remove(children):
                entered.set()
                await release.wait()
                await original_remove(children)

            if during_removal:
                monkeypatch.setattr(footer, 'remove_children', paused_remove)

            async def refresh():
                token = active_message_pump.set(footer)
                try:
                    await footer.recompose()
                finally:
                    active_message_pump.reset(token)

            try:
                if during_removal:
                    task = asyncio.create_task(refresh())
                    await entered.wait()
                    app._running = False
                    release.set()
                    await task
                else:
                    # App._shutdown clears this before draining queued callbacks.
                    app._running = False
                    await refresh()
                assert mounts == []
                if not during_removal:
                    assert tuple(footer.children) == original_children
            finally:
                app._running = True
    asyncio.run(scenario())


def test_footer_waiting_for_batch_lock_ignores_shutdown():
    async def scenario():
        app = FooterHarness()
        async with app.run_test() as pilot:
            await pilot.pause()
            footer = app.query_one(ButtonFooter)
            original_children = tuple(footer.children)
            started = asyncio.Event()

            async def refresh():
                started.set()
                await footer.recompose()

            try:
                async with footer.lock:
                    task = asyncio.create_task(refresh())
                    await started.wait()
                    app._running = False
                await task
                assert tuple(footer.children) == original_children
            finally:
                app._running = True
    asyncio.run(scenario())


def test_footer_refresh_preserves_system_children():
    async def scenario():
        app = FooterHarness()
        async with app.run_test() as pilot:
            await pilot.pause()
            footer = app.query_one(ButtonFooter)
            system = Static('System', id='footer-system', classes='-textual-system')
            await footer.mount(system)
            token = active_message_pump.set(footer)
            try:
                await footer.recompose()
                assert footer.query_one('#footer-system') is system
                assert len(footer.query('#tab-select-hint')) == 1
            finally:
                active_message_pump.reset(token)
    asyncio.run(scenario())
