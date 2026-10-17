"""First-party sign-in before app authorization in the original managed page."""
import asyncio
import re
import time
from urllib.parse import urlsplit

from api_agent.providers.progress import component_timeout
from api_agent.providers.sessions import wait_browser_result
from api_agent.runtime.managed_browser import _dispose
from .login import LoginWorkflow


FIRST_PARTY = 'https://chatgpt.com'
_SESSION_COOKIE = re.compile(r'__Secure-next-auth\.session-token(?:\.[0-9]+)?\Z')


class FirstPartyWorkflow(LoginWorkflow):
    """Treat session hints as handoff signals, never verified OAuth identity."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ready = asyncio.get_running_loop().create_future()
        self.submitted_email = False
        self.authorizing = False

    async def advance(self):
        if self.authorizing:
            await super().advance()
            return
        location = urlsplit(self.page.url)
        origin = f'{location.scheme}://{location.netloc}'
        if origin not in (FIRST_PARTY, 'https://auth.openai.com'):
            await super().advance()
            return
        cookies = await self.session.context.cookies(FIRST_PARTY + '/')
        cookie_hint = any(_SESSION_COOKIE.fullmatch(cookie['name']) and cookie['value']
                          and (cookie['expires'] == -1 or cookie['expires'] > time.time())
                          for cookie in cookies)
        del cookies
        home = origin == FIRST_PARTY
        login = self.page.get_by_role('button', name='Log in', exact=True).first
        # Initial hydration can temporarily omit Log in. This UI alternative is
        # eligible only after this workflow submitted the account's email.
        returned = home and self.submitted_email and not await login.is_visible()
        if cookie_hint or returned:
            self.ready.set_result(None)
            return
        if not home:
            await super().advance()
            return
        if await self.guard.check():
            self.disabled = True
            return
        field = self.page.locator('input[type="email"][name="login_hint"]')
        if await field.is_visible():
            await self._submit(self.page.get_by_role('button', name='Continue', exact=True), field,
                               expected_url=location.geturl())
        elif await login.is_visible():
            await self._submit(login, expected_url=location.geturl())

    async def aclose(self):
        self.ready.cancel()
        await super().aclose()

    async def _submit(self, button, field=None, *, expected_url):
        if self.page.url != expected_url or not await button.is_visible():
            return
        key = (expected_url, 'email' if field is not None else 'open')
        if key == self.last_action and await self._same_document():
            return
        handles = []
        try:
            bound_button = await button.element_handle(timeout=1000)
            handles.append(bound_button)
            bound_field = await field.element_handle(timeout=1000) if field is not None else None
            handles.append(bound_field)
            if bound_button is None or (field is not None and bound_field is None):
                return
            document = await bound_button.evaluate_handle('(element) => element.ownerDocument')
            self.documents.append(document)
            if field is not None:
                if not await self._bound(bound_field, bound_button, key[0]):
                    return
                await bound_field.fill(self.username, timeout=1000)
            if not await self._bound(bound_button, bound_button, key[0]):
                return
            await bound_button.click(timeout=1000)
            self.submitted_email = field is not None
            self.last_document, self.last_action = document, key
            await self._close_documents(keep=document)
        finally:
            async def release():
                await asyncio.gather(*(handle.dispose() for handle in handles if handle is not None),
                                     return_exceptions=True)
            await _dispose(release())


async def authenticate_first_party(workflow, session, *, callback=None):
    """Reuse the caller's browser/key owners and inherited overall login budget."""
    async with component_timeout('GPT first-party sign-in', 180):
        result = asyncio.get_running_loop().create_future()
        def wake(_):
            if not result.done():
                result.set_result(None)
        futures = (workflow.ready,) if callback is None else (workflow.ready, callback)
        for future in futures:
            future.add_done_callback(wake)
            if future.done():
                wake(future)
        async def advance():
            if callback is None or not callback.done():
                await workflow.advance()
        try:
            if callback is None or not callback.done():
                await session.page.goto(FIRST_PARTY + '/', wait_until='domcontentloaded')
            await wait_browser_result(result, session, on_wait=advance)
        finally:
            for future in futures:
                future.remove_done_callback(wake)
            result.cancel()
