"""Private operation-owned native sign-in interaction; never owns the caller."""
import asyncio

from api_agent.providers.contracts import InteractionRequiredError, ProviderAccessError
from api_agent.providers.control import CleanupPendingError, finish_cleanup
from api_agent.providers.progress import component_timeout, independent_timeout_scope, report_timeout

_LIMIT = 65536
_SECONDS = 2
_MESSAGE = 'AWS sign-in interaction is unavailable.'
_CODE_WHITESPACE = str.maketrans('', '', ' \t\r\n\v\f')
_CODE_ALPHABET = frozenset('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=')


class LoginInteraction:
    _label = 'AWS sign-in'
    _message = _MESSAGE

    def __init__(self, request):
        self._borrowed = request.interaction if request.interactive else None
        self._owner = None
        self._channel = None
        self._tasks = set()
        self._closed = False
        self._closing = None

    def __repr__(self):
        return '<LoginInteraction>'

    async def _call(self, function):
        with independent_timeout_scope():
            task = asyncio.create_task(asyncio.to_thread(function))
        self._tasks.add(task)
        try:
            async with component_timeout('AWS sign-in interaction', _SECONDS):
                return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise InteractionRequiredError(self._message) from None
        finally:
            if task.done():
                self._tasks.discard(task)

    def _open(self):
        borrowed = self._borrowed
        self._check_owner(borrowed)
        fork = getattr(borrowed, 'fork', None)
        self._owner = fork() if callable(fork) else None
        self._channel = (self._owner or borrowed).open(self._label)
        self._check()

    def _check_owner(self, owner):
        event = getattr(owner, 'cancel_event', None)
        if event is not None and event.is_set():
            raise InteractionRequiredError(self._message)

    def _check(self):
        self._check_owner(self._borrowed)
        self._check_owner(self._owner)
        if (self._channel is None or getattr(self._channel, 'closed', False)
                or not all(callable(getattr(self._channel, name, None)) for name in ('write', 'read', 'close'))):
            raise InteractionRequiredError(self._message)
        check = getattr(self._channel, 'check_open', None)
        if check is not None:
            check()

    async def check_ready(self):
        self._live()
        await self._call(self._check)

    async def open(self):
        if self._closed or self._borrowed is None or self._channel is not None or self._tasks:
            raise InteractionRequiredError(self._message)
        await self._call(self._open)

    def _live(self):
        if self._closed or self._channel is None:
            raise InteractionRequiredError(self._message)

    async def write(self, text):
        self._live()
        if type(text) is not str or len(text) > _LIMIT:
            raise InteractionRequiredError(self._message)
        await self._call(lambda: self._channel.write(text))

    async def read(self):
        self._live()
        reply = await self._call(lambda: self._channel.read(0.1))
        if reply is None:
            return None
        if (getattr(reply, 'action', None) != 'input'
                or type(getattr(reply, 'value', None)) is not str or len(reply.value) > _LIMIT):
            raise InteractionRequiredError(self._message)
        value = reply.value.translate(_CODE_WHITESPACE)
        if not _CODE_ALPHABET.issuperset(value):
            error = ProviderAccessError(
                'invalid_request',
                'AWS sign-in code contains unexpected characters. Copy the complete code from the current browser sign-in.',
                code='invalid_code')
            error.execution = 'not_started'
            raise error
        return value

    async def _dispose(self):
        async with report_timeout('AWS sign-in interaction cleanup', _SECONDS) as budget:
            if self._tasks:
                done, pending = await asyncio.wait(tuple(self._tasks), timeout=budget.remaining)
                await asyncio.gather(*done, return_exceptions=True)
                self._tasks.difference_update(done)
                if pending:
                    raise CleanupPendingError((self,), self.aclose)
            if self._channel is not None:
                await self._call(self._channel.close)
                self._channel = None
            if self._owner is not None:
                await self._call(self._owner.close)
                self._owner = None
            self._borrowed = None

    async def aclose(self):
        self._closed = True
        if self._closing is None or self._closing.done():
            with independent_timeout_scope():
                self._closing = asyncio.create_task(self._dispose())
        async def settle():
            try:
                await asyncio.shield(self._closing)
            except Exception:
                raise CleanupPendingError((self,), self.aclose) from None
        await finish_cleanup(settle())
