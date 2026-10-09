"""Observed-page subscription login; credentials remain scoped and in memory."""
import asyncio
import re
from urllib.parse import urlsplit

from api_agent.providers.contracts import UnattendedCredentialError
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
        self.consent = re.compile(r'^Connect(?: your)? ' + re.escape(app_name) + r' to ChatGPT$')

    def _interaction(self, message):
        if not self.allow_interaction:
            raise AuthError('login_interaction_required')
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
        await self.guard.check()
        if self.disabled:
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
        # Include the heading: the observed flow has two consent pages. Never
        # retry the same submission while waiting for navigation or human help.
        heading = consent if stage == 'consent' else {'totp': totp, 'password': password, 'username': username}[stage]
        key = (location.path, stage, await heading.text_content())
        if key == self.last_action:
            return
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
            bound_field = await field.element_handle(timeout=1000) if field is not None else None
            handles.append(bound_field)
            if bound_button is None or (field is not None and bound_field is None):
                return
            if field is not None:
                value = self.username if kind == 'username' else await self._credential(kind)
                if not isinstance(value, str) or not value.strip():
                    raise UnattendedCredentialError('The selected login does not provide the credential requested by GPT. Check its password-manager entry.')
                if not await self._bound(bound_field, bound_button, location.geturl()):
                    return
                await bound_field.fill(value, timeout=1000)
                del value
            if not await self._bound(bound_button, bound_button, location.geturl()):
                return
            await bound_button.click(timeout=1000)
            self.last_action = key
        finally:
            async def release():
                await asyncio.gather(*(handle.dispose() for handle in handles if handle is not None),
                                     return_exceptions=True)
            await _dispose(release())

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
