"""Observed-page subscription login; credentials remain scoped and in memory."""
import asyncio
import re
from urllib.parse import urlsplit

from api_agent.providers.contracts import UnattendedCredentialError
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.progress import independent_timeout_scope, report_timeout
from api_agent.providers.sessions import wait_browser_result
from api_agent.runtime.managed_browser import _dispose
from .contracts import AuthError


class LoginWorkflow:
    def __init__(self, session, username, credentials, app_name, guard, *, allow_interaction):
        self.session = session
        self.page = session.page
        self.username = username
        self.credentials = credentials
        self.guard = guard
        self.allow_interaction = allow_interaction
        self.disabled = False
        self.last_action = None
        self.last_document = None
        self.documents = []
        self.closing = None
        self.consent = re.compile(r'^Connect(?: your)? ' + re.escape(app_name) + r' to ChatGPT$')

    def _interaction(self, message):
        if not self.allow_interaction:
            raise AuthError('login_interaction_required')
        if not self.disabled:
            print(message, flush=True)
        self.disabled = True

    async def _credential(self, kind):
        task = asyncio.create_task(self.credentials(kind))
        try:
            return await wait_browser_result(task, self.session, on_wait=self.guard.check)
        finally:
            task.cancel()
            async def drain():
                await asyncio.gather(task, return_exceptions=True)
            await _dispose(drain())

    async def advance(self):
        if await self.guard.check() is True:
            self.disabled = True
            return
        location = urlsplit(self.page.url)
        if location.scheme == 'about':
            return
        if (location.scheme != 'https' or location.netloc != 'auth.openai.com'):
            self._interaction('GPT login needs attention in the visible browser; automatic credential entry is paused.')
            return
        # Prefer the furthest recognized page. An email field may remain visible
        # as an account label on later pages, so its heading must match too.
        consent = self.page.get_by_role('heading', name=self.consent)
        totp = self.page.get_by_role('heading', name='Check your authenticator app', exact=True)
        password = self.page.get_by_role('heading', name='Enter your password', exact=True)
        username = self.page.get_by_role('heading', name='Welcome back', exact=True)
        if await consent.is_visible():
            stage, field, kind = 'consent', None, None
        elif await totp.is_visible():
            stage, field, kind = 'totp', self.page.locator('input[name=code][autocomplete=one-time-code]'), 'totp'
        elif await password.is_visible():
            stage, field, kind = 'password', self.page.locator('input[type=password]'), 'password'
        elif await username.is_visible():
            stage, field, kind = 'username', self.page.get_by_role('textbox', name='Email address', exact=True), 'username'
        else:
            return
        if self.credentials is None and stage != "username":
            return
        if self.disabled:
            self.disabled = False
        # Include the heading: the observed flow has two consent pages. Never
        # retry the same submission while waiting for navigation or human help.
        heading = consent if stage == 'consent' else {'totp': totp, 'password': password, 'username': username}[stage]
        key = (location.path, stage, await heading.text_content())
        if field is not None and not await field.is_visible():
            return
        button = self.page.get_by_role('button', name='Continue', exact=True)
        if not await button.is_visible():
            return
        # Pin actual elements before retrieving secrets. Unlike Locators, these
        # handles cannot resolve to matching elements in a replacement document.
        handles = []
        try:
            bound_button = await button.element_handle(timeout=1000)
            handles.append(bound_button)
            if key == self.last_action and await self._same_document():
                return
            bound_field = await field.element_handle(timeout=1000) if field is not None else None
            handles.append(bound_field)
            if bound_button is None or (field is not None and bound_field is None):
                return
            document = await bound_button.evaluate_handle('(element) => element.ownerDocument')
            self.documents.append(document)
            if field is not None:
                value = self.username if kind == 'username' else await self._credential(kind)
                if not isinstance(value, str) or not value.strip():
                    raise UnattendedCredentialError('The selected login does not provide the credential requested by GPT. Check its password-manager entry.')
                if not await self._current(heading, key[2], location.geturl()):
                    return
                if not await self._bound(bound_field, bound_button, location.geturl()):
                    return
                await bound_field.fill(value, timeout=1000)
                del value
            if not await self._current(heading, key[2], location.geturl()):
                return
            if not await self._bound(bound_button, bound_button, location.geturl()):
                return
            await bound_button.click(timeout=1000)
            self.last_document = document
            self.last_action = key
            await self._close_documents(keep=document)
        finally:
            async def release():
                await asyncio.gather(*(handle.dispose() for handle in handles if handle is not None),
                                     return_exceptions=True)
            await _dispose(release())

    async def aclose(self):
        """Release owned documents, retaining a safe retry receipt on failure."""
        await self._close_documents()

    async def _close_documents(self, *, keep=None):
        async def release():
            for document in tuple(self.documents):
                if document is keep:
                    continue
                await document.dispose()
                self.documents.remove(document)
                if self.last_document is document:
                    self.last_document = None
        while any(document is not keep for document in self.documents):
            if self.closing is None or self.closing.done():
                self.closing = asyncio.create_task(release())
                self.closing.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())
            task = self.closing
            cancelled = False
            try:
                with independent_timeout_scope():
                    async with report_timeout('GPT login document cleanup', 5) as budget:
                        while not task.done() and budget.remaining > 0:
                            try:
                                await asyncio.wait((task,), timeout=budget.remaining)
                            except asyncio.CancelledError:
                                cancelled = True
            except asyncio.CancelledError:
                cancelled = True
            failed = not task.done() or task.cancelled() or task.exception() is not None
            pending = CleanupPendingError((self,), self.aclose) if failed else None
            if cancelled:
                error = asyncio.CancelledError()
                if pending is not None:
                    error.retained_owners = pending.owners
                    error.cleanup_error = pending
                raise error from None
            if pending is not None:
                raise pending from None

    async def _same_document(self):
        try:
            return await self.last_document.evaluate('(previous) => previous === document')
        except Exception:
            # Full navigation destroys the previous document's execution context.
            return False

    async def _current(self, heading, expected_heading, expected_url):
        """Recheck the allowed stage after every credential or page action wait."""
        if await self.guard.check() is True:
            self.disabled = True
            return False
        return (self.page.url == expected_url and await heading.is_visible()
                and await heading.text_content() == expected_heading)

    async def _bound(self, element, button, expected_url):
        try:
            return await element.evaluate(
                "(element, binding) => element.ownerDocument === document && "
                "binding.button.ownerDocument === document && location.href === binding.url",
                {'button': button, 'url': expected_url})
        except Exception:
            # Navigation may destroy the bound execution context. Never resolve
            # a replacement element, and never forward its identity-bearing error.
            return False


async def finish_login(workflow, primary=None):
    """Keep the operation outcome primary while exposing pending document cleanup."""
    try:
        await workflow.aclose()
    except (CleanupPendingError, asyncio.CancelledError) as error:
        if primary is None:
            raise
        cleanup = getattr(error, 'cleanup_error', error)
        primary.cleanup_error = cleanup
        if isinstance(cleanup, CleanupPendingError):
            primary.retained_owners = cleanup.owners
