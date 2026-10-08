import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ContractError, SessionClosedError, OperationalError, CleanupPendingError
from api_agent.providers.sessions import (
    BrowserOwner, InspectionOwner, LLMSessionOwner, ScopedLLMCredentials,
    ScopedRoleCredentials, ui_session_policy, wait_browser_result,
)


def run(coroutine):
    return asyncio.run(coroutine)


def browser(**kwargs):
    context = SimpleNamespace(new_page=AsyncMock(return_value=object()), close=AsyncMock())
    instance = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    driver = SimpleNamespace(launch=AsyncMock(return_value=instance), stop=AsyncMock())
    owner = BrowserOwner(AsyncMock(return_value=driver), headless=True, **kwargs)
    return owner, driver, instance, context


def credentials():
    return ScopedLLMCredentials("llm-user", ScopedRoleCredentials(["llm-user"], 0, AsyncMock(return_value="opaque")))


@pytest.mark.parametrize('closed', ['page', 'browser'])
def test_browser_wait_stops_on_closure_without_canceling_caller_work(closed):
    async def scenario():
        result = asyncio.get_running_loop().create_future()
        session = SimpleNamespace(page=SimpleNamespace(is_closed=lambda: closed == 'page'),
                                  browser=SimpleNamespace(is_connected=lambda: closed != 'browser'))
        with pytest.raises(SessionClosedError, match='^Browser closed before operation completed$'):
            await wait_browser_result(result, session)
        assert not result.done()
        result.cancel()  # The caller owns and disposes its pending result.
    run(scenario())


def test_browser_wait_completed_result_wins_over_later_closure():
    async def scenario():
        result = asyncio.get_running_loop().create_future()
        result.set_result('accepted')
        session = SimpleNamespace(page=SimpleNamespace(is_closed=lambda: True))
        assert await wait_browser_result(result, session) == 'accepted'
    run(scenario())


def test_browser_wait_cancellation_leaves_result_owned_and_no_monitor_task():
    async def scenario():
        result = asyncio.get_running_loop().create_future()
        session = SimpleNamespace(page=SimpleNamespace(is_closed=lambda: False),
                                  browser=SimpleNamespace(is_connected=lambda: True))
        waiting = asyncio.create_task(wait_browser_result(result, session))
        await asyncio.sleep(0)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not result.done()
        result.cancel()
        assert not [task for task in asyncio.all_tasks()
                    if task is not asyncio.current_task() and not task.done()]
    run(scenario())


def test_credentials():
    async def scenario():
        retrieve = AsyncMock(return_value="opaque")
        reader = ScopedRoleCredentials(["llm-user"], 0, retrieve)
        llm = ScopedLLMCredentials("llm-user", reader)
        retrieve.assert_not_called()
        assert await llm(0, "password") == "opaque"
        assert await llm(0, "totp") == "opaque"
        for role, index, kind in [("other", 0, "password"), ("llm-user", 1, "password"),
                                  ("llm-user", False, "password"), ("llm-user", 0, "other")]:
            with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
                await reader(role, index, kind)
        assert retrieve.await_count == 2
    run(scenario())
    for index in [-1, False]:
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            ScopedRoleCredentials([], index, AsyncMock())
    with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
        ScopedLLMCredentials("other", ScopedRoleCredentials([], 0, AsyncMock()))


@pytest.mark.parametrize("mode", ["unit", "integration", "help", "collection", "run-local", "e2e"])
@pytest.mark.parametrize("ui", [False, True])
@pytest.mark.parametrize("debug", [False, True])
def test_policy(mode, ui, debug):
    expected = not (ui or debug) if mode == "e2e" else False if mode == "run-local" and ui else None
    assert ui_session_policy(mode, run_ui=ui, debug=debug) is expected


def test_browser_identity_retention_and_partial_failure():
    async def scenario():
        inspection = InspectionOwner()
        owner, driver, instance, context = browser(inspection=inspection)
        async def prepare(**kwargs):
            kwargs["session"].page = "updated"
        session = await owner.prepare(SimpleNamespace(authenticate=prepare), tui_mode="e2e",
                                      user_index=None, credentials=None)
        assert await owner.start() is session
        assert session.page == "updated" and session.context is context
        await owner.aclose()
        assert not driver.stop.called
        await inspection.aclose()
        await inspection.aclose()
        for resource in [driver.stop, instance.close, context.close]:
            resource.assert_awaited_once()
        with pytest.raises(SessionClosedError):
            await owner.start()
        with pytest.raises(SessionClosedError):
            inspection.retain([])
        owner, driver, instance, context = browser(inspection=inspection)
        with pytest.raises(SessionClosedError):
            await owner.start()
        driver.stop.assert_awaited_once()
        owner, driver, instance, context = browser()
        context.new_page.side_effect = RuntimeError("setup")
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await owner.start()
        driver.stop.assert_awaited_once()
        instance.close.assert_awaited_once()
        context.close.assert_awaited_once()
        owner, driver, instance, context = browser()
        hook = SimpleNamespace(authenticate=AsyncMock(side_effect=RuntimeError("hook")))
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await owner.prepare(hook, tui_mode="e2e", user_index=0, credentials=None)
        owner, driver, instance, context = browser()
        await owner.prepare(None, tui_mode="run-local", user_index=None, credentials=None)
        context.close.side_effect = RuntimeError("close")
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await owner.aclose()
        driver.stop.assert_not_called()
        context.close.side_effect = None
        await owner.aclose()
        driver.stop.assert_awaited_once()
    run(scenario())


def test_local_execution_context():
    async def scenario():
        owner, *_ = browser()
        owner._pid = -1
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await owner.start()
        owner._pid = __import__('os').getpid()
        owner._loop = object()
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await owner.start()
        llm = LLMSessionOwner(object(), lambda: owner, user_index=None, credentials=credentials())
        llm._pid = -1
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.call(AsyncMock())
        llm._pid = __import__('os').getpid()
        llm._loop = object()
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.call(AsyncMock())
    run(scenario())


class Auth:
    def __init__(self):
        self.values = []
        self.closed = False
    async def __call__(self):
        value = object()
        self.values.append(value)
        return value
    async def aclose(self):
        self.closed = True


def test_lazy_concurrent_fresh_and_rebind():
    async def scenario():
        auth = Auth()
        owner, driver, *_ = browser()
        provider = SimpleNamespace(authenticate=AsyncMock(return_value=auth))
        final = AsyncMock()
        llm = LLMSessionOwner(provider, lambda: owner, user_index=0, credentials=credentials(), finalizer=final)
        provider.authenticate.assert_not_called()
        op = AsyncMock(side_effect=lambda **kwargs: kwargs["auth"])
        values = await asyncio.gather(llm.call(op), llm.call(op))
        assert values == auth.values and values[0] is not values[1]
        await llm.call(op)
        provider.authenticate.assert_awaited_once()
        driver.launch.assert_awaited_once_with(headless=True)
        async def finalize(getter):
            assert not getter.closed
            await getter()
        llm._finalizer = finalize
        replacement = await llm.rebind(object(), user_index=None, credentials=credentials())
        assert auth.closed
        with pytest.raises(SessionClosedError):
            await llm.call(op)
        assert await replacement.call(op) is None
        await replacement.aclose()
        await llm.aclose()
    run(scenario())


def test_attended_absent_and_headed_rejection():
    async def scenario():
        provider = SimpleNamespace(authenticate=AsyncMock())
        owner, *_ = browser()
        llm = LLMSessionOwner(provider, lambda: owner, user_index=None, credentials=credentials())
        await llm.call(AsyncMock())
        provider.authenticate.assert_not_called()
        await llm.aclose()
        owner._headless = False
        llm = LLMSessionOwner(provider, lambda: owner, user_index=0, credentials=credentials())
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.call(AsyncMock())
        await llm.aclose()
    run(scenario())


def test_retry_and_cancelled_waiter():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        auth = Auth()
        owners = []
        def allocate():
            owner, *_ = browser()
            owners.append(owner)
            return owner
        async def authenticate(**kwargs):
            entered.set()
            await release.wait()
            return auth
        provider = SimpleNamespace(authenticate=AsyncMock(side_effect=RuntimeError("failure")))
        llm = LLMSessionOwner(provider, allocate, user_index=0, credentials=credentials())
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.call(AsyncMock())
        assert owners[0]._closed
        provider.authenticate.side_effect = authenticate
        waiter = asyncio.create_task(llm.call(AsyncMock()))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await llm.call(AsyncMock())
        await llm.aclose()
        assert auth.closed
    run(scenario())


def test_shutdown_during_initialization_and_active_call():
    async def scenario():
        entered = asyncio.Event()
        async def block(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        owner, driver, *_ = browser()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=block), lambda: owner,
                              user_index=0, credentials=credentials(), timeout=.01)
        call = asyncio.create_task(llm.call(AsyncMock()))
        await entered.wait()
        await llm.aclose()
        with pytest.raises(asyncio.CancelledError):
            await call
        driver.stop.assert_awaited_once()
        auth = Auth()
        owner, *_ = browser()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=auth)), lambda: owner,
                              user_index=0, credentials=credentials())
        entered.clear()
        call = asyncio.create_task(llm.call(block))
        await entered.wait()
        await llm.aclose()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert auth.closed
    run(scenario())


def test_late_auth_is_disposed_after_shutdown():
    async def scenario():
        entered = asyncio.Event()
        auth = Auth()
        async def authenticate(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return auth
        owner, driver, *_ = browser()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=authenticate), lambda: owner,
                              user_index=0, credentials=credentials())
        call = asyncio.create_task(llm.call(AsyncMock()))
        await entered.wait()
        await llm.aclose()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert auth.closed
        assert isinstance(llm._initializing.exception(), SessionClosedError)
        driver.stop.assert_awaited_once()
    run(scenario())


def test_each_ui_attempt_retains_distinct_partial_resources():
    async def scenario():
        inspection = InspectionOwner()
        first, driver1, browser1, context1 = browser(inspection=inspection)
        second, driver2, browser2, context2 = browser(inspection=inspection)
        failed_hook = SimpleNamespace(authenticate=AsyncMock(side_effect=RuntimeError("setup")))
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await first.prepare(failed_hook, tui_mode="e2e", user_index=0, credentials=None)
        session = await second.prepare(None, tui_mode="e2e", user_index=0, credentials=None)
        assert first.session is not session
        assert first.session.context is not session.context
        await second.aclose()
        assert not driver1.stop.called and not driver2.stop.called
        await inspection.aclose()
        for resource in [driver1.stop, driver2.stop, browser1.close, browser2.close,
                         context1.close, context2.close]:
            resource.assert_awaited_once()
    run(scenario())


def test_auth_cleanup_failure_retains_browser_until_retry_and_finalizer_failure():
    async def scenario():
        owner, driver, *_ = browser()
        auth = Auth()
        auth.aclose = AsyncMock(side_effect=RuntimeError("dispose"))
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=auth)), lambda: owner,
                              user_index=0, credentials=credentials())
        await llm.call(AsyncMock())
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.aclose()
        driver.stop.assert_not_called()
        assert llm._resources[-1] == (auth, 'aclose')
        auth.aclose.assert_awaited_once()
        async def close_auth():
            auth.closed = True
        async def close_browser():
            assert auth.closed
        driver.stop.side_effect = close_browser
        auth.aclose.side_effect = close_auth
        await llm.aclose()
        assert auth.aclose.await_count == 2 and auth.closed
        driver.stop.assert_awaited_once()
        owner, driver, *_ = browser()
        auth = Auth()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=auth)), lambda: owner,
                              user_index=0, credentials=credentials(),
                              finalizer=AsyncMock(side_effect=RuntimeError("usage")))
        await llm.call(AsyncMock())
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.aclose()
        assert auth.closed
        driver.stop.assert_awaited_once()
    run(scenario())


def test_coalesced_start_prepare_and_concurrent_close():
    async def scenario():
        owner, driver, _, context = browser()
        entered, release = asyncio.Event(), asyncio.Event()
        async def factory():
            entered.set()
            await release.wait()
            return driver
        owner._factory = factory
        first = asyncio.create_task(owner.start())
        await entered.wait()
        second = asyncio.create_task(owner.start())
        release.set()
        sessions = await asyncio.gather(first, second)
        assert sessions[0] is sessions[1]
        driver.launch.assert_awaited_once()
        hook = SimpleNamespace(authenticate=AsyncMock())
        prepared = await asyncio.gather(*[owner.prepare(hook, tui_mode="e2e", user_index=None,
                                                        credentials=None) for _ in range(2)])
        assert prepared[0] is sessions[0] is prepared[1]
        hook.authenticate.assert_awaited_once()
        entered.clear()
        release.clear()
        async def close():
            entered.set()
            await release.wait()
        context.close = close
        first = asyncio.create_task(owner.aclose())
        await entered.wait()
        second = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        await asyncio.gather(first, second)
        driver.stop.assert_awaited_once()
    run(scenario())


def test_shutdown_resistant_startup_cannot_allocate_more_or_handoff():
    async def scenario():
        owner, driver, *_ = browser(timeout=.02)
        entered, release = asyncio.Event(), asyncio.Event()
        async def factory():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return driver
        owner._factory = factory
        starting = asyncio.create_task(owner.start())
        await entered.wait()
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await owner.aclose()
        assert not driver.stop.called and not driver.launch.called
        release.set()
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await starting
        await owner.aclose()
        driver.stop.assert_awaited_once()
        driver.launch.assert_not_called()
    run(scenario())


def test_nested_calls_and_resistant_operation_keep_dependencies_owned():
    async def scenario():
        owner, driver, *_ = browser()
        auth = Auth()
        entered, release = asyncio.Event(), asyncio.Event()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=auth)), lambda: owner,
                              user_index=0, credentials=credentials(), timeout=.02)
        async def outer(**kwargs):
            await llm.call(AsyncMock())
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            assert not auth.closed
        call = asyncio.create_task(llm.call(outer))
        await entered.wait()
        tracked = llm._calls.get(call) == 1
        if not tracked:
            release.set()
        assert tracked
        try:
            await llm.aclose()
        except CleanupPendingError:
            refused = True
        else:
            refused = False
            release.set()
        assert refused
        assert not auth.closed and not driver.stop.called
        release.set()
        await call
        await llm.aclose()
        assert auth.closed
        driver.stop.assert_awaited_once()
    run(scenario())


def test_resistant_finalizer_and_resource_cleanup_are_retryable():
    async def scenario():
        owner, driver, *_ = browser()
        auth = Auth()
        entered, release = asyncio.Event(), asyncio.Event()
        async def finalizer(getter):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            assert not auth.closed
            await getter()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=auth)), lambda: owner,
                              user_index=0, credentials=credentials(), timeout=.02, finalizer=finalizer)
        await llm.call(AsyncMock())
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.aclose()
        assert not auth.closed and not driver.stop.called
        release.set()
        await llm._finalizing
        await llm.aclose()
        assert auth.closed
        inspection = InspectionOwner(timeout=.02)
        entered.clear()
        release.clear()
        async def close():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
        resource = SimpleNamespace(close=close)
        inspection.retain([(resource, "close")])
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await inspection.aclose()
        assert inspection._resources and inspection._resource_task is not None
        release.set()
        await inspection._resource_task
        await inspection.aclose()
        assert not inspection._resources
    run(scenario())


def test_inspection_close_coalesces_and_context_checks_include_close():
    async def scenario():
        inspection = InspectionOwner()
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        inspection.retain([(SimpleNamespace(close=close), "close")])
        first = asyncio.create_task(inspection.aclose())
        await entered.wait()
        second = asyncio.create_task(inspection.aclose())
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        await asyncio.gather(first, second)
        owner, *_ = browser()
        llm = LLMSessionOwner(object(), lambda: owner, user_index=None, credentials=credentials())
        for item in [owner, llm, InspectionOwner()]:
            item._pid = -1
            with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
                await item.aclose()
            item._pid = __import__('os').getpid()
            item._loop = object()
            with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
                await item.aclose()
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            inspection._pid = -1
            inspection.retain([])
    run(scenario())


def test_private_failures_are_neutral_and_shared_failures_consumed():
    import traceback
    async def scenario():
        marker = "synthetic-credential-marker"
        async def check(awaitable):
            with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)) as error:
                await awaitable
            assert marker not in ''.join(traceback.format_exception(error.value))
        reader = ScopedRoleCredentials(["llm-user"], 0, AsyncMock(side_effect=RuntimeError(marker)))
        await check(reader("llm-user", 0, "password"))
        owner, _, _, context = browser()
        await owner.start()
        context.close.side_effect = RuntimeError(marker)
        await check(owner.aclose())
        owner, *_ = browser()
        owner._factory.side_effect = RuntimeError(marker)
        await check(owner.prepare(None, tui_mode="e2e", user_index=None, credentials=None))
        owner, *_ = browser()
        await check(owner.prepare(SimpleNamespace(authenticate=AsyncMock(side_effect=RuntimeError(marker))),
                                  tui_mode="e2e", user_index=None, credentials=None))
        owner, *_ = browser()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(side_effect=RuntimeError(marker))),
                              lambda: owner, user_index=0, credentials=credentials())
        await check(llm.call(AsyncMock()))
        await llm.aclose()
        reader = ScopedRoleCredentials(["llm-user"], 0, AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await reader("llm-user", 0, "password")
    run(scenario())


def test_pending_setup_preparation_and_close_cancellation():
    async def scenario():
        owner, driver, *_ = browser()
        entered = asyncio.Event()
        async def blocked_factory():
            entered.set()
            await asyncio.Event().wait()
        owner._factory = blocked_factory
        starting = asyncio.create_task(owner.start())
        await entered.wait()
        await owner.aclose()
        with pytest.raises(asyncio.CancelledError):
            await starting
        driver.launch.assert_not_called()
        owner, _, _, context = browser()
        entered.clear()
        async def hook(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        preparing = asyncio.create_task(owner.prepare(SimpleNamespace(authenticate=hook), tui_mode="e2e",
                                                     user_index=None, credentials=None))
        await entered.wait()
        await owner.aclose()
        with pytest.raises(asyncio.CancelledError):
            await preparing
        context.close.assert_awaited_once()
        owner, _, _, context = browser()
        await owner.start()
        entered.clear()
        release = asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        context.close = close
        closing = asyncio.create_task(owner.aclose())
        await entered.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not owner._closing.done()
        release.set()
        await owner.aclose()
    run(scenario())


def test_binding_and_resistant_auth_disposal_retry():
    async def scenario():
        for index in [False, -1, 1]:
            with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
                LLMSessionOwner(object(), lambda: None, user_index=index, credentials=credentials())
        owner, driver, *_ = browser()
        entered, release = asyncio.Event(), asyncio.Event()
        auth = Auth()
        async def close_auth():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            auth.closed = True
        auth.aclose = close_auth
        finalizer = AsyncMock()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=auth)), lambda: owner,
                              user_index=0, credentials=credentials(), finalizer=finalizer, timeout=.02)
        await llm.call(AsyncMock())
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.aclose()
        assert not driver.stop.called
        release.set()
        await llm._resource_task
        await llm.aclose()
        finalizer.assert_awaited_once()
        assert auth.closed
        driver.stop.assert_awaited_once()
        owner, *_ = browser()
        await owner.start()
        def bad_close():
            raise RuntimeError("synthetic-credential-marker")
        owner._resources[-1][0].close = bad_close
        import traceback
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)) as error:
            await owner.aclose()
        assert "synthetic-credential-marker" not in ''.join(traceback.format_exception(error.value))
        inspection = InspectionOwner()
        inspection._dispose = AsyncMock(side_effect=RuntimeError("synthetic-credential-marker"))
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)) as error:
            await inspection.aclose()
        assert "synthetic-credential-marker" not in ''.join(traceback.format_exception(error.value))
    run(scenario())


def test_cancelled_auth_waiter_consumes_failure_and_preserves_failed_cleanup():
    import gc
    async def scenario():
        failures = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: failures.append(context))
        entered, release = asyncio.Event(), asyncio.Event()
        owner, *_ = browser()
        async def authenticate(**kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("synthetic-credential-marker")
        llm = LLMSessionOwner(SimpleNamespace(authenticate=authenticate), lambda: owner,
                              user_index=0, credentials=credentials())
        waiter = asyncio.create_task(llm.call(AsyncMock()))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        await asyncio.sleep(.01)
        await llm.aclose()
        del waiter, llm
        gc.collect()
        assert failures == []
        owner, *_ = browser()
        owner.aclose = AsyncMock(side_effect=[ContractError("pending cleanup"), None])
        replacement, *_ = browser()
        factory = __import__('unittest.mock', fromlist=['Mock']).Mock(side_effect=[owner, replacement])
        provider = SimpleNamespace(authenticate=AsyncMock(side_effect=[RuntimeError("auth"), Auth()]))
        llm = LLMSessionOwner(provider, factory, user_index=0, credentials=credentials())
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.call(AsyncMock())
        assert llm._browser is owner
        await llm.call(AsyncMock())
        assert owner.aclose.await_count == 2
        await llm.aclose()
    run(scenario())


def test_nested_cleanup_timeout_requires_confirmed_close_and_getter_cannot_launch_after_close():
    async def scenario():
        owner, driver, _, context = browser(timeout=1)
        entered, release = asyncio.Event(), asyncio.Event()
        async def close_context():
            entered.set()
            await release.wait()
        context.close = close_context
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=Auth())), lambda: owner,
                              user_index=0, credentials=credentials(), timeout=.02)
        await llm.call(AsyncMock())
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await llm.aclose()
        assert llm._resources and not driver.stop.called
        release.set()
        await owner._closing
        await llm.aclose()
        assert not llm._resources
        driver.stop.assert_awaited_once()
        owner, *_ = browser()
        entered.clear()
        class Getter(Auth):
            async def __call__(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    return object()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=Getter())), lambda: owner,
                              user_index=0, credentials=credentials())
        operation = AsyncMock()
        call = asyncio.create_task(llm.call(operation))
        await entered.wait()
        await llm.aclose()
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await call
        operation.assert_not_called()
    run(scenario())


def test_repeated_close_never_recancels_ongoing_operation_cleanup():
    async def scenario():
        owner, driver, *_ = browser()
        auth = Auth()
        entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def operation(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                assert not auth.closed
                cleaned.set()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=auth)), lambda: owner,
                              user_index=0, credentials=credentials(), timeout=.02)
        call = asyncio.create_task(llm.call(operation))
        await entered.wait()
        for _ in range(2):
            with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
                await llm.aclose()
            assert not call.done() and not auth.closed and not driver.stop.called
        release.set()
        await call
        assert cleaned.is_set()
        await llm.aclose()
        assert auth.closed
        driver.stop.assert_awaited_once()
        from api_agent.providers.sessions import _drain
        entered.clear()
        release.clear()
        task = asyncio.create_task(operation())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises((ContractError, OperationalError, SessionClosedError, CleanupPendingError)):
            await _drain([task], .02)
        assert not task.done() and task.cancelling() == 1
        auth.closed = False
        release.set()
        await task
    run(scenario())


def test_reused_caller_task_gets_new_owner_cancellation_scope():
    async def scenario():
        first_browser, *_ = browser()
        second_browser, *_ = browser()
        first = LLMSessionOwner(object(), lambda: first_browser, user_index=None, credentials=credentials(), timeout=.02)
        second = LLMSessionOwner(object(), lambda: second_browser, user_index=None, credentials=credentials(), timeout=.02)
        entered_first, entered_second = asyncio.Event(), asyncio.Event()
        cancelled = []
        async def operation(event):
            event.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(event)
                asyncio.current_task().uncancel()
        async def caller():
            await first.call(lambda **kwargs: operation(entered_first))
            await second.call(lambda **kwargs: operation(entered_second))
        task = asyncio.create_task(caller())
        await entered_first.wait()
        await first.aclose()
        await entered_second.wait()
        await second.aclose()
        await task
        assert cancelled == [entered_first, entered_second]
        assert task not in first._cancel_requested and task not in second._cancel_requested
    run(scenario())


def test_plain_future_finalizer_is_cancelled_and_retry_disposes_safely():
    async def scenario():
        owner, driver, *_ = browser()
        auth = Auth()
        future = asyncio.get_running_loop().create_future()
        llm = LLMSessionOwner(SimpleNamespace(authenticate=AsyncMock(return_value=auth)), lambda: owner,
                              user_index=0, credentials=credentials(), timeout=.02,
                              finalizer=lambda getter: future)
        await llm.call(AsyncMock())
        with pytest.raises(ContractError, match='Final usage failed'):
            await llm.aclose()
        assert future.cancelled() and auth.closed
        driver.stop.assert_awaited_once()
        await llm.aclose()
        driver.stop.assert_awaited_once()
    run(scenario())


def test_inspection_observation_waits_for_windows_without_disposing_driver():
    async def scenario():
        observed = []
        inspection = InspectionOwner(on_session=observed.append)
        owner, driver, instance, context = browser(inspection=inspection)
        context.pages = [object()]
        instance.is_connected = lambda: True
        driver.inspection_endpoint = 'http://127.0.0.1:12345'
        session = await owner.start()
        assert observed == [session] and session.inspection_endpoint == driver.inspection_endpoint
        await owner.aclose()
        waiting = asyncio.create_task(inspection.wait_closed())
        await asyncio.sleep(.02)
        assert not waiting.done()
        driver.stop.assert_not_awaited()
        context.pages.clear()
        await waiting
        await inspection.aclose()
        assert not inspection.sessions
        driver.stop.assert_awaited_once()
        with pytest.raises(SessionClosedError):
            inspection.observe(session)
    run(scenario())


def test_inspection_disconnect_and_empty_sessions_finish_without_wait():
    async def scenario():
        inspection = InspectionOwner()
        await inspection.wait_closed()
        from api_agent.providers.contracts import BrowserSession
        inspection.observe(BrowserSession(SimpleNamespace(is_connected=lambda: False), SimpleNamespace(pages=[object()]), object()))
        await inspection.wait_closed()
        await inspection.aclose()
    run(scenario())
