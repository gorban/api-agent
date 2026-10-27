"""Operation-owned local authentication uses the shared interactive transport."""
import asyncio
import threading
import ssl
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.auth.interaction_transport import OwnedInteraction
from api_agent.runtime.local_run import LocalRunOwner
from test_local_run import local


@pytest.mark.asyncio
async def test_owned_fork_stop_preserves_sibling_and_waits_for_close(local):
    closed = []
    class Channel:
        def close(self): closed.append(threading.get_ident())
    root = OwnedInteraction(NS(open=lambda label: Channel()))
    operation, sibling = root.fork(), root.fork()
    operation.open('operation')
    sibling.open('sibling')
    local.owner.interaction_owner = operation
    local.owner._stack.push(local.owner._close_interaction)
    loop_thread = threading.get_ident()

    local.owner.request_stop()
    await local.owner.aclose()

    assert operation.cancelled and not root.cancelled and not sibling.cancelled
    assert not operation.channels and len(sibling.channels) == 1
    assert closed and loop_thread not in closed
    assert local.owner.snapshot.phase == 'stopped'
    sibling.close()


@pytest.mark.asyncio
async def test_failed_interaction_close_is_retained_for_explicit_retry(local):
    interaction = NS(cancel=Mock(), close=Mock(side_effect=[RuntimeError('retry'), None]))
    local.owner.interaction_owner = interaction
    local.owner._stack.push(local.owner._close_interaction)

    with pytest.raises(Exception):
        await local.owner.aclose()
    assert local.owner.pending_cleanup is not None
    assert local.owner.snapshot.phase != 'stopped'
    await local.owner.aclose()

    assert interaction.close.call_count == 2
    assert local.owner.pending_cleanup is None
    assert local.owner.snapshot.phase == 'stopped'


@pytest.mark.asyncio
@pytest.mark.parametrize('finish,native', [('reply', False), ('reply', True),
    ('stop', False), ('quit', False), ('unmount', False), ('prompt-cancel', False), ('teardown', True)])
async def test_shared_prompt_through_normal_app_real_owner_and_llm(tmp_path, monkeypatch, finish, native):
    from textual.widgets import Input, TextArea
    from api_agent.runtime import local_run, local_llm, client_tls
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
    from api_agent.tui.local_run import LocalRunScreen
    from api_agent.tui.manager_recovery import ManagerRecoveryScreen
    from test_tui import fixture

    replies, services_seen = [], []
    loop = asyncio.get_running_loop()
    prompt_written = loop.create_future()

    def settle_prompt(error=None):
        if not prompt_written.done():
            if error is None:
                prompt_written.set_result(None)
            else:
                prompt_written.set_exception(error)
    class Auth:
        async def __call__(self): return 'synthetic-session'
        async def aclose(self): pass
    class Provider:
        capabilities = ProviderCapabilities(kind='llm')
        async def asetup(self, **options):
            try:
                if native:
                    from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
                    login = LoginInteraction(NS(interaction=services_seen[0]['interaction'], interactive=True))
                    try:
                        await login.open()
                        await login.write('Open https://example.test/sign-in and enter a value.\n')
                        settle_prompt()
                        value = None
                        while value is None:
                            value = await login.read()
                        from api_agent.auth.interaction import Reply
                        replies.append(Reply('input', value))
                    finally:
                        await login.aclose()
                    return Auth()
                def converse():
                    channel = services_seen[0]['interaction'].open('Synthetic sign-in')
                    try:
                        channel.write('Open https://example.test/sign-in and enter a value.\n')
                        loop.call_soon_threadsafe(settle_prompt)
                        while True:
                            reply = channel.read(.02)
                            if reply is not None:
                                replies.append(reply)
                                return
                    finally:
                        channel.close()
                await asyncio.to_thread(converse)
                return Auth()
            except BaseException as error:
                settle_prompt(error)
                raise
    async def loader(location, config, services, **options):
        services_seen.append(services)
        return NS(implementation=Provider(), astart=AsyncMock(), aclose=AsyncMock())
    llm_init = local_llm.LocalLLM.__init__
    def init_llm(self, *args, **kwargs):
        llm_init(self, *args, loader=loader, **kwargs)
    monkeypatch.setattr(local_llm.LocalLLM, '__init__', init_llm)
    monkeypatch.setattr(client_tls, 'verified_client_context', lambda value: ssl.create_default_context())
    monkeypatch.setattr(local_run, 'compose_runtime', lambda *args, **kwargs:
        NS(services={}, emissions=EmissionBoundary(ContentPolicy()), tracing=None))
    owner_init = LocalRunOwner.__init__
    def init_owner(self, *args, **kwargs):
        def operation_factory(*args, **options):
            @asynccontextmanager
            async def opened(**ignored):
                runtime = options['runtime_factory'](self.resolve(), 'https://localhost:8443', NS(ca_file='synthetic'))
                try:
                    await runtime.services['ensure_llm_ready']()
                    yield NS(runtime=runtime, callback=NS(origin='https://localhost:8443'), listener=None, infrastructure=None)
                finally:
                    await self._llm.aclose()
            return NS(open=opened, pending_cleanup=None)
        browser = NS(session=None, flow_url='https://example.test/flow', start=AsyncMock(), aclose=AsyncMock())
        owner_init(self, *args, **kwargs, operation_factory=operation_factory,
            browser_factory=lambda *a, **k: browser,
            application_factory=lambda *a, **k: NS(),
            prepare_view=lambda *a, **k: (NS(definition_id='synthetic'), None, False, None, {}),
            plan_factory=lambda *a, **k: NS(conditional=(), services=()))
    monkeypatch.setattr(LocalRunOwner, '__init__', init_owner)
    provider_path = tmp_path / 'synthetic.py'
    provider_path.write_text('# Synthetic provider declaration; loader injected below.\n')
    app = fixture(tmp_path, fields={
        'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False},
        'LLM_PROVIDER': {'type': 'str', 'default': str(provider_path) + ':create', 'acknowledge_default': False}})
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        screen = LocalRunScreen(app.context, app.session.result)
        app.local_run_screen = screen
        normal_run = screen.run

        async def run_until_prompt():
            try:
                await normal_run()
            finally:
                # Failures before provider entry must also wake the write waiter.
                settle_prompt(AssertionError(screen.failure or 'Local startup ended before writing its prompt'))

        monkeypatch.setattr(screen, 'run', run_until_prompt)
        app.push_screen(screen)
        # Presentation precedes native write dispatch; wait for the real transport
        # write to complete instead of repeatedly draining the UI while it runs.
        await prompt_written
        await pilot.pause()
        assert isinstance(app.screen, ManagerRecoveryScreen), screen.failure
        prompt = app.screen
        assert 'https://example.test/sign-in' in prompt.query_one('#manager-output', TextArea).text
        field = prompt.query_one('#manager-input', Input)
        assert field.password
        if finish == 'teardown':
            return
        if finish != 'reply':
            if finish == 'stop':
                await screen.action_stop()
                await pilot.pause()
            elif finish == 'quit':
                await app.confirm_quit()
            elif finish == 'unmount':
                await screen.on_unmount()
            else:
                await pilot.click('#manager-cancel')
                await pilot.press('y', 'enter')
                for _ in range(100):
                    await pilot.pause(.02)
                    if replies:
                        break
                assert replies[0].action == 'cancel'
                assert await screen.close_owned()
            assert screen.owner.interaction_owner.cancelled
            assert not screen.owner.interaction_owner.channels
            assert screen.owner.snapshot.phase == 'stopped'
            return
        from textual.widgets.text_area import Selection
        from api_agent.tui import manager_recovery
        clipboard = AsyncMock(return_value=True)
        monkeypatch.setattr(manager_recovery, 'copy_text', clipboard)
        url = 'https://example.test/sign-in'
        output = prompt.query_one('#manager-output', TextArea)
        output.selection = Selection((0, 5), (0, 5 + len(url)))
        reply_value = 'c3ludGhldGlj' if native else 'synthetic-reply'
        field.value = reply_value
        await pilot.press('ctrl+c')
        await pilot.pause()
        clipboard.assert_awaited_once_with(app, url)
        assert app.screen is prompt and not prompt.pending
        assert field.password and field.value == reply_value
        assert not replies and not screen.owner._start_done.is_set()
        assert reply_value not in output.text
        field.focus()
        await pilot.press('enter')
        for _ in range(100):
            await pilot.pause(.02)
            if screen.snapshot.phase == 'ready':
                break
        assert screen.snapshot.phase == 'ready', screen.failure
        assert replies[0].value == reply_value
        assert services_seen[0]['interaction'] is screen.owner.interaction_owner
        assert screen.owner._llm.interaction_owner is screen.owner.interaction_owner
        assert await screen.close_owned()
        assert screen.owner.interaction_owner.cancelled
        assert not screen.owner.interaction_owner.channels


@pytest.mark.asyncio
async def test_cancel_failure_retries_and_completed_cancel_is_not_repeated(local):
    interaction = NS(cancel=Mock(side_effect=[RuntimeError('retry'), None]), close=Mock())
    local.owner.interaction_owner = interaction
    local.owner._stack.push(local.owner._close_interaction)
    with pytest.raises(Exception):
        await local.owner.aclose()
    await local.owner.aclose()
    local.owner.request_stop()
    await local.owner._close_interaction()
    assert interaction.cancel.call_count == 2
    assert interaction.close.call_count == 1


@pytest.mark.asyncio
async def test_interaction_close_without_stop_is_off_loop_and_survives_cancellation(local):
    entered, release = threading.Event(), threading.Event()
    def close():
        entered.set()
        assert release.wait(5)
    interaction = NS(cancel=Mock(), close=Mock(side_effect=close))
    local.owner.interaction_owner = interaction
    closing = asyncio.create_task(local.owner._close_interaction())
    while not entered.is_set():
        await asyncio.sleep(.001)
    closing.cancel()
    await asyncio.sleep(.01)
    assert not closing.done()
    assert not local.owner._interaction_close.done()
    release.set()
    await closing
    await local.owner._close_interaction()
    assert interaction.close.call_count == 1


@pytest.mark.asyncio
async def test_rejected_task_allocation_keeps_interaction_retryable(local, monkeypatch):
    import api_agent.runtime.local_llm as module
    interaction = NS(cancel=Mock(), close=Mock())
    local.owner.interaction_owner = interaction
    local.owner._stack.push(local.owner._close_interaction)
    with monkeypatch.context() as patch:
        patch.setattr(module.asyncio, 'create_task', Mock(side_effect=RuntimeError('scheduler')))
        with pytest.raises(RuntimeError, match='scheduler'):
            local.owner.request_stop()
        assert local.owner._interaction_cancel is None
    await local.owner.aclose()
    assert interaction.cancel.call_count == 1
    assert interaction.close.call_count == 1


@pytest.mark.asyncio
async def test_cancelled_helpers_are_replaced(local):
    interaction = NS(cancel=Mock(), close=Mock())
    local.owner.interaction_owner = interaction
    task = asyncio.create_task(asyncio.sleep(5))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    local.owner._interaction_cancel = task
    local.owner._interaction_close = task
    local.owner.request_stop()
    await local.owner._close_interaction()
    assert interaction.cancel.call_count == 1
    assert interaction.close.call_count == 1


@pytest.mark.asyncio
async def test_constructor_failure_reclaims_presentation_interaction(tmp_path, monkeypatch):
    from api_agent.tui.local_run import LocalRunScreen
    from test_local_run_screen import Harness
    monkeypatch.setattr(LocalRunOwner, '__init__', Mock(side_effect=RuntimeError('constructor')))
    screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
    app = Harness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen.finished and screen.status == 1
        assert screen._interaction_close_task.done()
        assert screen._unclaimed_interaction is None
        assert await screen.close_owned()


@pytest.mark.asyncio
async def test_failed_unclaimed_cleanup_retries_after_constructor_failure(tmp_path, monkeypatch):
    from api_agent.tui.local_run import LocalRunScreen
    from test_local_run_screen import Harness
    monkeypatch.setattr(LocalRunOwner, '__init__', Mock(side_effect=RuntimeError('constructor')))
    close = Mock(side_effect=[RuntimeError('close'), None])
    monkeypatch.setattr(OwnedInteraction, 'close', close)
    screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
    app = Harness(screen)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert screen.finished and screen.pending_cleanup is not None
        assert screen._unclaimed_interaction is not None
        assert await screen.close_owned()
        assert close.call_count == 2
        assert screen._unclaimed_interaction is None
        assert screen.pending_cleanup is None


@pytest.mark.asyncio
async def test_unclaimed_close_reuses_pending_and_successful_task():
    from api_agent.tui.local_run import LocalRunScreen
    screen = LocalRunScreen(None, lambda: NS(values={'RUN_LOG_SOURCE': 'agent'}))
    entered, release = threading.Event(), threading.Event()
    def close():
        entered.set()
        assert release.wait(5)
    interaction = NS(close=Mock(side_effect=close))
    screen._unclaimed_interaction = interaction
    first = asyncio.create_task(screen._close_unclaimed_interaction())
    while not entered.is_set():
        await asyncio.sleep(.001)
    second = asyncio.create_task(screen._close_unclaimed_interaction())
    await asyncio.sleep(.01)
    assert not first.done() and not second.done()
    release.set()
    await asyncio.gather(first, second)
    await screen._close_unclaimed_interaction()
    assert interaction.close.call_count == 1


def test_live_ui_dismiss_failure_retains_channel_and_siblings():
    from api_agent.tui.manager_recovery import TUIInteraction, _Channel
    app = NS(is_running=True, call_from_thread=Mock(side_effect=RuntimeError('live failure')))
    transport = TUIInteraction(app, threading.Event())
    channel, sibling = _Channel(transport, 'operation'), _Channel(transport, 'sibling')
    transport.active = channel
    transport.waiting.append(sibling)
    channel.screen.transcript = 'private prompt'
    with pytest.raises(RuntimeError, match='live failure'):
        channel.close()
    assert not channel.closed and transport.active is channel
    assert channel.screen.transcript == 'private prompt'
    app.is_running = False
    channel.close()
    assert channel.closed and transport.active is None
    assert channel.screen.transcript == ''
    assert transport.waiting == [sibling] and not sibling.closed


def test_ui_shutdown_race_disposes_only_owned_channel():
    from api_agent.tui.manager_recovery import TUIInteraction, _Channel
    app = NS(is_running=True)
    def closing(*args):
        app.is_running = False
        raise RuntimeError('loop stopped')
    app.call_from_thread = closing
    transport = TUIInteraction(app, threading.Event())
    channel = _Channel(transport, 'operation')
    channel.screen.transcript = 'private prompt'
    transport.active = channel
    channel.close()
    assert channel.closed and channel.screen.transcript == ''
    assert transport.active is None
