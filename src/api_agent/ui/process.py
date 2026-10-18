"""Bounded, identity-checked ownership of the included Node HTTP host."""
import asyncio
import hmac
import ipaddress
import subprocess
from secrets import token_urlsafe

import httpx

from api_agent.providers.contracts import SetupPrerequisiteError
from api_agent.providers.control import CleanupPendingError, finish_cleanup
from api_agent.providers.progress import component_timeout
from api_agent.runtime.client_tls import require_verified_context


class OwnedStudioProcess:
    """Own one direct host child; never signal a cached process-group identity."""
    def __init__(self, process, *, timeout=5):
        self.process, self.timeout = process, timeout
        self._closing = None
        self._terminated = False
        self._killed = False
        self._stdin_closed = False

    async def _wait(self, timeout):
        async with asyncio.timeout(timeout):
            while self.process.poll() is None:
                await asyncio.sleep(.02)

    async def _stop(self):
        try:
            async with component_timeout('Studio process cleanup', self.timeout):
                if not self._stdin_closed:
                    writer = getattr(self.process, 'stdin', None)
                    if writer is not None:
                        writer.close()
                    self._stdin_closed = True
                if self.process.poll() is None:
                    if not self._terminated:
                        self.process.terminate()
                        self._terminated = True
                    try:
                        await self._wait(self.timeout / 2)
                    except TimeoutError:
                        if not self._killed:
                            self.process.kill()
                            self._killed = True
                        await self._wait(self.timeout / 2)
        except Exception:
            raise CleanupPendingError((self,), self.aclose) from None

    async def aclose(self):
        if self._closing is None or self._closing.done() and self._closing.exception() is not None:
            self._closing = asyncio.create_task(self._stop())
        await asyncio.shield(self._closing)


class StudioProcesses:
    """A launch adapter; returned owners remain the UI provider's responsibility."""
    def __init__(self, *, ssl_context, timeout=15, cleanup_timeout=5):
        self.ssl_context = require_verified_context(ssl_context)
        if (type(timeout) not in (int, float) or not 0 < timeout <= 60
                or type(cleanup_timeout) not in (int, float) or not 0 < cleanup_timeout <= 10):
            raise ValueError('Invalid Studio process timeout')
        self.timeout, self.cleanup_timeout = timeout, cleanup_timeout

    async def start(self, argv, *, cwd, host, port, scheme, env):
        if host != 'localhost':
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = False
            if not local:
                raise ValueError('Studio process requires loopback')
        if scheme not in ('http', 'https') or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Invalid Studio process listener')
        token = token_urlsafe(32)
        environment = dict(env, API_AGENT_UI_LAUNCH_TOKEN=token,
                           API_AGENT_UI_PARENT_LIVENESS='stdin-v1')
        process = None
        try:
            async with component_timeout('Studio process launch', self.timeout) as budget:
                # Popen completes ownership transfer before the next cancellation point.
                try:
                    child = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             start_new_session=True)
                except OSError:
                    raise SetupPrerequisiteError('Studio could not start Node.js') from None
                process = OwnedStudioProcess(child, timeout=self.cleanup_timeout)
                address = '[' + host + ']' if ':' in host else host
                url = f'{scheme}://{address}:{port}/'
                try:
                    async with httpx.AsyncClient(verify=self.ssl_context, trust_env=False, timeout=.25) as client:
                        while True:
                            if budget.remaining == 0:
                                raise TimeoutError('Studio readiness deadline expired')
                            if child.poll() is not None:
                                raise SetupPrerequisiteError('Studio exited before verified readiness')
                            try:
                                response = await client.head(url)
                            except httpx.HTTPError:
                                response = None
                            # A transport can consume cancellation. The clipped
                            # absolute budget still governs retries and admission.
                            if budget.remaining == 0:
                                raise TimeoutError('Studio readiness deadline expired')
                            if response is not None:
                                identity = response.headers.get('X-UI-Launch-Token', '').encode()
                                if response.status_code == 200 and hmac.compare_digest(identity, token.encode()):
                                    break
                            await asyncio.sleep(.02)
                except asyncio.CancelledError:
                    # Preserve the original interruption; the timeout context
                    # classifies its own cancellation using cancellation counts.
                    raise
                except Exception:
                    if budget.remaining == 0:
                        raise TimeoutError('Studio readiness deadline expired')
                    raise
                if budget.remaining == 0:
                    raise TimeoutError('Studio readiness deadline expired')
                return process
        except BaseException as primary:
            if process is not None:
                try:
                    await finish_cleanup(process.aclose())
                except CleanupPendingError as pending:
                    primary.cleanup_pending = pending
                    if not isinstance(primary, asyncio.CancelledError):
                        raise pending from None
            raise
