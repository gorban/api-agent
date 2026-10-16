"""Ephemeral managed software authenticators; never evidence or persisted state."""
import asyncio
import base64
import os
from urllib.parse import urlsplit

from api_agent.providers.control import CleanupPendingError
from api_agent.providers.progress import component_timeout, independent_timeout_scope, report_timeout
from .contracts import AuthError


def require_private_transport():
    """Protocol/channel debug output bypasses application credential scrubbing."""
    debug = os.environ.get('DEBUG', '').strip().lower()
    if (debug not in ('', 'true', 'false', '0', '1')
            or 'DEBUGP' in os.environ or os.environ.get('PWDEBUG')
            or os.environ.get('DEBUG_FILE')):
        raise AuthError('passkey_protocol_logging')


class SoftwarePasskey:
    """Own import and removal on the original page, including uncertain replies."""
    def __init__(self, session, credential, *, trusted_origin='https://auth.openai.com', callback_url=None):
        self.session = session
        self.credential = credential
        self.channel = None
        self.authenticator = None
        self.installing = None
        self.closing = None
        self.closed = False
        self.trusted_origin = trusted_origin
        self.callback_url = callback_url
        self.intercepting = False
        self.navigation_tasks = set()
        self.navigation_failed = False

    def _paused(self, event):
        task = asyncio.create_task(self._navigation(event))
        self.navigation_tasks.add(task)
        task.add_done_callback(self.navigation_tasks.discard)
        task.add_done_callback(self._consume)

    async def _navigation(self, event):
        url = event['request']['url']
        location = urlsplit(url)
        origin = f'{location.scheme}://{location.netloc}'
        callback = self.callback_url is not None and url.split('?', 1)[0] == self.callback_url
        allowed = origin == self.trusted_origin or callback
        try:
            method = 'Fetch.continueRequest' if allowed else 'Fetch.failRequest'
            parameters = {'requestId': event['requestId']}
            if not allowed:
                parameters['errorReason'] = 'BlockedByClient'
            await self.channel.send(method, parameters)
        except Exception:
            self.navigation_failed = True

    async def prepare(self):
        require_private_transport()
        if self.credential.sign_count != 0:
            raise AuthError('passkey_counter_unsupported')
        self.installing = asyncio.create_task(self._install())
        self.installing.add_done_callback(self._consume)
        try:
            await asyncio.shield(self.installing)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise AuthError('passkey_browser_unsupported') from None

    @staticmethod
    def _consume(task):
        if not task.cancelled():
            task.exception()

    async def _install(self):
        async with component_timeout('GPT software passkey preparation', 15):
            self.channel = await self.session.context.new_cdp_session(self.session.page)
            self.channel.on('Fetch.requestPaused', self._paused)
            self.intercepting = True
            await self.channel.send('Fetch.enable', {'patterns': [
                {'resourceType': 'Document', 'requestStage': 'Request'}]})
            await self.channel.send('WebAuthn.enable', {'enableUI': False})
            result = await self.channel.send('WebAuthn.addVirtualAuthenticator', {'options': {
                'protocol': 'ctap2', 'transport': 'internal', 'hasResidentKey': True,
                'hasUserVerification': True, 'isUserVerified': True,
                'automaticPresenceSimulation': True, 'hasPrf': False,
                'defaultBackupEligibility': False, 'defaultBackupState': False}})
            self.authenticator = result['authenticatorId']
            key = self.credential
            await self.channel.send('WebAuthn.addCredential', {
                'authenticatorId': self.authenticator, 'credential': {
                    'credentialId': base64.b64encode(base64.urlsafe_b64decode(
                        key.credential_id + '=' * (-len(key.credential_id) % 4))).decode(),
                    'isResidentCredential': True, 'rpId': key.relying_party_id,
                    'privateKey': key.pkcs8_base64(),
                    'userHandle': base64.b64encode(base64.urlsafe_b64decode(
                        key.user_handle + '=' * (-len(key.user_handle) % 4))).decode(),
                    'userName': key.username, 'signCount': -1,
                    'backupEligibility': False, 'backupState': False}})
            self.credential = None

    async def _release(self):
        if self.installing is not None:
            await asyncio.gather(self.installing, return_exceptions=True)
        if not self.session.browser.is_connected():
            self.credential = None
            self.closed = True
            return
        if self.channel is not None:
            if self.authenticator is not None:
                await self.channel.send('WebAuthn.removeVirtualAuthenticator', {
                    'authenticatorId': self.authenticator})
                self.authenticator = None
            await self.channel.send('WebAuthn.disable')
            if self.intercepting:
                await self.channel.send('Fetch.disable')
                self.intercepting = False
                self.channel.remove_listener('Fetch.requestPaused', self._paused)
            await asyncio.gather(*tuple(self.navigation_tasks), return_exceptions=True)
            await self.channel.detach()
            self.channel = None
        self.credential = None
        self.closed = True

    async def aclose(self):
        if self.closed:
            return
        if self.closing is None or self.closing.done():
            self.closing = asyncio.create_task(self._release())
            self.closing.add_done_callback(self._consume)
        cancelled = False
        with independent_timeout_scope():
            async with report_timeout('GPT software passkey removal', 5) as budget:
                while not self.closing.done() and budget.remaining > 0:
                    try:
                        await asyncio.wait((self.closing,), timeout=budget.remaining)
                    except asyncio.CancelledError:
                        cancelled = True
        failed = not self.closing.done() or self.closing.cancelled() or self.closing.exception() is not None
        pending = CleanupPendingError((self,), self.aclose) if failed else None
        if cancelled:
            error = asyncio.CancelledError()
            if pending is not None:
                error.cleanup_error, error.retained_owners = pending, pending.owners
            raise error
        if pending is not None:
            raise pending from None
