"""Process-local credential and browser owners with lazy opaque authentication."""
import asyncio
import os
import inspect
import math
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .contracts import BrowserSession, ContractError, SessionClosedError, LLMCredentialReader
from .control import OperationalError, OperationContext, CleanupPendingError, _cancel_once, finish_cleanup


class ScopedRoleCredentials:
    def __init__(self, roles: Iterable[str], index: int, retrieve: Callable[..., Awaitable[str]]):
        if type(index) is not int or index < 0:
            raise ContractError("Invalid selected account")
        self._roles = frozenset(roles)
        self._index = index
        self._retrieve = retrieve

    async def __call__(self, role: str, index: int, kind: str) -> str:
        if role not in self._roles:
            raise ContractError("Undeclared credential role")
        if type(index) is not int or index != self._index:
            raise ContractError("Credential account is outside binding")
        if kind not in ("username", "password", "totp"):
            raise ContractError("Unsupported credential kind")
        try:
            return await self._retrieve(role, index, kind)
        except (asyncio.CancelledError, OperationalError, ContractError, SessionClosedError, CleanupPendingError):
            raise
        except Exception:
            raise OperationalError("unknown", execution="possibly_executed") from None


class ScopedLLMCredentials:
    def __init__(self, role: str, reader: ScopedRoleCredentials):
        if role not in reader._roles:
            raise ContractError("Undeclared LLM credential role")
        self._role = role
        self._reader = reader

    async def __call__(self, index: int, kind: str) -> str:
        return await self._reader(self._role, index, kind)


def ui_session_policy(mode: str, *, run_ui: bool = False, debug: bool = False) -> bool | None:
    """Return headless policy, or None when no UI browser is allocated."""
    if mode == "e2e":
        return not (run_ui or debug)
    if mode == "run-local" and run_ui:
        return False
    return None


async def _invoke(resource: Any, method: str) -> None:
    await getattr(resource, method)()


async def wait_browser_result(result: asyncio.Future, session: BrowserSession, *, on_wait=None) -> Any:
    """Race completion/closure against one owned asynchronous assistance task.

    Accepted completion wins over closure or a failed guard. The caller still
    owns result; this wait cancels and drains only the assistance it starts.
    """
    assistance = None
    try:
        while not result.done():
            if session.page.is_closed() or not session.browser.is_connected():
                raise SessionClosedError("Browser closed before operation completed")
            if on_wait is not None and assistance is None:
                assistance = asyncio.create_task(on_wait())
            await asyncio.wait({result} if assistance is None else {result, assistance},
                               timeout=.1, return_when=asyncio.FIRST_COMPLETED)
            if assistance is not None and assistance.cancelled():
                assistance.result()
            if result.done():
                break
            if assistance is not None and assistance.done():
                assistance.result()
                assistance = None
                # A cheap guard must not spin; keep completion responsive while
                # retaining the original bounded observation cadence.
                await asyncio.wait({result}, timeout=.1)
        return result.result()
    finally:
        if assistance is not None:
            _cancel_once(assistance, set())
            async def drain():
                await asyncio.gather(assistance, return_exceptions=True)
            await finish_cleanup(drain())


def _consume(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


def _spawn(awaitable: Awaitable[Any]) -> asyncio.Future:
    task = asyncio.ensure_future(awaitable)
    task.add_done_callback(_consume)
    return task


async def _drain(tasks: Iterable[asyncio.Future], timeout: float,
                 cancel_requested: set[asyncio.Future] | None = None,
                 active: Callable[[asyncio.Future], bool] | None = None) -> None:
    if cancel_requested is None:
        cancel_requested = set()
    pending = {task for task in tasks if not task.done()}
    if pending:
        for task in pending:
            _cancel_once(task, cancel_requested)
        _, surviving = await asyncio.wait(pending, timeout=timeout)
        surviving = {task for task in surviving if active is None or active(task)}
        if surviving:
            raise ContractError("Owned work has not stopped; resources remain owned") from None


class _Owner:
    def __init__(self, timeout: float):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ContractError("Invalid cleanup timeout")
        self._timeout = timeout
        self._pid = os.getpid()
        self._loop = None
        self._closed = False
        self._closing = None
        self._resources: list[tuple[Any, str]] = []
        self._resource_task = None
        self._cancel_requested: set[asyncio.Future] = set()

    def _context(self) -> None:
        loop = asyncio.get_running_loop()
        if self._pid != os.getpid() or self._loop is not None and self._loop is not loop:
            raise ContractError("Owner belongs to another execution context") from None
        self._loop = loop

    def _check(self) -> None:
        self._context()
        if self._closed:
            raise SessionClosedError("Session owner is closed") from None

    async def _release(self) -> None:
        while self._resources:
            resource, method = self._resources[-1]
            if self._resource_task is None:
                self._resource_task = _spawn(_invoke(resource, method))
            task = self._resource_task
            done, _ = await asyncio.wait({task}, timeout=self._timeout)
            if not done:
                await _drain([task], self._timeout, self._cancel_requested)
            self._resource_task = None
            try:
                task.result()
            except BaseException:
                # A failed wrapper does not confirm disposal of its nested owner.
                raise ContractError("Resource cleanup remains pending") from None
            self._resources.pop()

    async def aclose(self) -> None:
        self._context()
        self._closed = True
        if self._closing is None or self._closing.done() and (self._closing.cancelled() or self._closing.exception() is not None):
            self._closing = _spawn(self._dispose())
        try:
            await asyncio.shield(self._closing)
        except asyncio.CancelledError:
            raise
        except (CleanupPendingError, SessionClosedError):
            raise
        except ContractError as error:
            if str(error) == "Final usage failed":
                raise error from None
            raise CleanupPendingError((self,), self.aclose) from None
        except Exception:
            raise CleanupPendingError((self,), self.aclose) from None


class InspectionOwner(_Owner):
    """Retains browsers and drivers until explicit bounded inspection cleanup."""
    def __init__(self, *, timeout: float = 5, on_session=None):
        super().__init__(timeout)
        self.sessions: list[BrowserSession] = []
        self._on_session = on_session

    def observe(self, session: BrowserSession) -> None:
        self._check()
        self.sessions.append(session)
        if self._on_session is not None:
            self._on_session(session)

    async def wait_closed(self) -> None:
        """Drive the creating loop while a retained browser still has windows."""
        self._check()
        while any(session.browser.is_connected() and session.context.pages
                  for session in self.sessions):
            await asyncio.sleep(.1)

    def retain(self, resources: list[tuple[Any, str]]) -> None:
        self._check()
        self._resources.extend(resources)

    async def _dispose(self) -> None:
        await self._release()
        self.sessions.clear()


class BrowserOwner(_Owner):
    def __init__(self, driver_factory: Callable[[], Awaitable[Any]], *, headless: bool,
                 inspection: InspectionOwner | None = None, timeout: float = 5):
        super().__init__(timeout)
        self._factory = driver_factory
        self._headless = headless
        self._inspection = inspection
        self.session: BrowserSession | None = None
        self._starting = None
        self._preparing = None

    async def _start(self) -> BrowserSession:
        try:
            driver = await self._factory()
            self._resources.append((driver, "stop"))
            self._check()
            browser = await driver.launch(headless=self._headless)
            self._resources.append((browser, "close"))
            self._check()
            context = await browser.new_context()
            self._resources.append((context, "close"))
            self._check()
            page = await context.new_page()
            self._check()
            self.session = BrowserSession(browser, context, page,
                                          getattr(driver, 'inspection_endpoint', None))
            if self._inspection is not None:
                self._inspection.observe(self.session)
            return self.session
        except (asyncio.CancelledError, OperationalError, ContractError, SessionClosedError, CleanupPendingError):
            raise
        except Exception:
            raise ContractError("Browser setup failed") from None

    async def start(self) -> BrowserSession:
        self._check()
        if self._starting is None:
            self._starting = _spawn(self._start())
        try:
            session = await asyncio.shield(self._starting)
            self._check()
            return session
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if asyncio.current_task() is not self._preparing:
                await self.aclose()
            if isinstance(error, (OperationalError, ContractError, SessionClosedError)):
                raise error from None
            raise ContractError("Browser setup failed") from None

    async def _prepare(self, hook: Any, kwargs: dict) -> BrowserSession:
        session = await self.start()
        try:
            if hook is not None:
                await hook.authenticate(session=session, **kwargs)
            self._check()
            return session
        except (asyncio.CancelledError, OperationalError, ContractError, SessionClosedError, CleanupPendingError):
            raise
        except Exception:
            raise OperationalError("unknown", execution="possibly_executed") from None

    async def prepare(self, hook: Any, *, tui_mode: str, user_index: int | None,
                      credentials: ScopedRoleCredentials) -> BrowserSession:
        self._check()
        if self._preparing is None:
            self._preparing = _spawn(self._prepare(hook, dict(tui_mode=tui_mode,
                                                           user_index=user_index, credentials=credentials)))
        try:
            session = await asyncio.shield(self._preparing)
            self._check()
            return session
        except asyncio.CancelledError:
            raise
        except (OperationalError, ContractError, SessionClosedError):
            await self.aclose()
            raise
        except Exception:
            await self.aclose()
            raise OperationalError("unknown", execution="possibly_executed") from None

    async def _dispose(self) -> None:
        await _drain([t for t in (self._starting, self._preparing) if t is not None],
                     self._timeout, self._cancel_requested)
        if self._inspection is not None:
            try:
                self._inspection.retain(self._resources)
            except SessionClosedError:
                await self._release()
                raise
            self._resources = []
        else:
            await self._release()


class LLMSessionOwner(_Owner):
    """Lazy auth bound to one account; timed-out work retains its dependencies."""
    def __init__(self, provider: Any, browser_factory: Callable[[], BrowserOwner] | None = None, *,
                 user_index: int | None = None, credentials: LLMCredentialReader | None = None,
                 interactive: bool = True,
                 finalizer: Callable[[Any], Awaitable[None]] | None = None, timeout: float = 5):
        super().__init__(timeout)
        if user_index is not None and (
            type(user_index) is not int or user_index < 0
            or isinstance(credentials, ScopedLLMCredentials) and user_index != credentials._reader._index
        ):
            raise ContractError("LLM account is outside credential binding")
        self.provider = provider
        self._browser_factory = browser_factory
        self._index = user_index
        self._credentials = credentials
        self._interactive = interactive
        self._finalizer = finalizer
        self._browser = None
        self._auth = None
        self._initializing = None
        self._calls: dict[asyncio.Task, int] = {}
        self._streams = set()
        self._finalizing = None
        self._disposal_started = False

    async def _release_rejected_auth(self):
        try:
            await self._release()
        except ContractError:
            raise CleanupPendingError((self,), self.aclose) from None

    async def _initialize(self) -> None:
        try:
            await self._release_rejected_auth()
            if self._browser is not None:
                await self._browser.aclose()
                self._browser = None
            setup = getattr(self.provider, "asetup", None)
            if setup is not None:
                auth = await setup(interactive=self._interactive, user_index=self._index,
                                   credentials=self._credentials)
            else:
                if self._browser_factory is None or self._credentials is None:
                    raise ContractError("LLM authentication requires browser and credentials")
                browser = self._browser_factory()
                self._browser = browser
                if not browser._headless:
                    raise ContractError("LLM authentication browser must be headless")
                session = await browser.start()
                auth = await self.provider.authenticate(session=session, user_index=self._index,
                                                        credentials=self._credentials)
            if not callable(auth) or not callable(getattr(auth, "aclose", None)):
                if callable(getattr(auth, "aclose", None)):
                    self._resources.append((auth, "aclose"))
                raise ContractError("Invalid authentication session")
            self._auth = auth
            self._check()
        except (asyncio.CancelledError, CleanupPendingError):
            raise
        except Exception as error:
            await self._release_rejected_auth()
            if not self._closed and self._browser is not None:
                await self._browser.aclose()
                self._browser = None
            if isinstance(error, (OperationalError, ContractError, SessionClosedError)):
                raise error from None
            raise OperationalError("unknown", execution="possibly_executed") from None

    async def _budget(self, awaitable, context):
        if context is None:
            return await awaitable
        if context.remaining() == 0 and inspect.iscoroutine(awaitable):
            awaitable.close()
            raise OperationalError("deadline")
        work = _spawn(awaitable)
        self._calls[work] = 1
        def released(task):
            self._calls.pop(task, None)
            self._cancel_requested.discard(task)
        work.add_done_callback(released)
        try:
            return await context.run(work)
        except (asyncio.CancelledError, OperationalError):
            if not work.done():
                self._cancel_requested.add(work)
            raise

    async def _authenticate(self, context):
        if (getattr(self.provider, "asetup", None) is not None
                or getattr(self.provider, "authenticate", None) is not None and self._index is not None):
            if self._auth is None:
                if self._initializing is None or self._initializing.done():
                    self._initializing = _spawn(self._initialize())
                await self._budget(asyncio.shield(self._initializing), context)
            self._check()
            auth = await self._budget(self._auth(), context)
            self._check()
            return auth
        return None

    async def call(self, operation: Callable[..., Awaitable[Any]], *, context: OperationContext | None = None) -> Any:
        self._check()
        task = asyncio.current_task()
        self._calls[task] = self._calls.get(task, 0) + 1
        try:
            if context is not None and context.remaining() == 0:
                raise OperationalError("deadline")
            auth = await self._authenticate(context)
            self._check()
            return await self._budget(operation(auth=auth), context)
        except (asyncio.CancelledError, OperationalError, ContractError, SessionClosedError, CleanupPendingError):
            raise
        except Exception:
            raise OperationalError("unknown", execution="possibly_executed") from None
        finally:
            self._calls[task] -= 1
            if not self._calls[task]:
                del self._calls[task]
                self._cancel_requested.discard(task)

    @asynccontextmanager
    async def stream(self, operation, *, context: OperationContext | None = None):
        from .streams import ManagedStream
        stream = ManagedStream(self, context)
        self._check()
        self._streams.add(stream)
        try:
            async def open_stream(**kwargs):
                result = operation(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
                stream.bind(result)
            await self.call(open_stream, context=context)
            yield stream
        finally:
            await stream.aclose()

    async def settle_execution(self) -> None:
        """Stop detached calls/setup while retaining auth for final accounting."""
        self._context()
        pending = list(self._calls)
        if self._initializing is not None:
            pending.append(self._initializing)
        await _drain(pending, self._timeout, self._cancel_requested,
                     lambda task: task in self._calls or task is self._initializing)
        for stream in tuple(self._streams):
            await stream.aclose()

    async def _dispose(self) -> None:
        await self.settle_execution()
        if not self._disposal_started:
            await self._release_rejected_auth()
        failed = False
        if not self._disposal_started and self._finalizer is not None and self._auth is not None:
            if self._finalizing is None:
                self._finalizing = _spawn(self._finalizer(self._auth))
            task = self._finalizing
            done, _ = await asyncio.wait({task}, timeout=self._timeout)
            if not done:
                await _drain([task], self._timeout, self._cancel_requested)
            try:
                task.result()
            except BaseException:
                failed = True
        if not self._disposal_started:
            self._disposal_started = True
            if self._browser is not None:
                self._resources.append((self._browser, "aclose"))
            if self._auth is not None:
                self._resources.append((self._auth, "aclose"))
        await self._release()
        self._auth = None
        self._browser = None
        if failed:
            raise ContractError("Final usage failed") from None

    async def rebind(self, provider: Any, *, user_index: int | None,
                     credentials: LLMCredentialReader | None) -> "LLMSessionOwner":
        await self.aclose()
        return LLMSessionOwner(provider, self._browser_factory, user_index=user_index,
                               credentials=credentials, interactive=self._interactive,
                               finalizer=self._finalizer, timeout=self._timeout)
