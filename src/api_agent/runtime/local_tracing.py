"""Readiness and dashboard authentication owned by one local operation."""
import asyncio
import math
from collections.abc import Mapping
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from api_agent.providers.progress import component_timeout
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.contracts import ContractError
from api_agent.runtime.client_tls import verified_client_context
from api_agent.runtime.local_status import safe_destination


class DashboardError(RuntimeError):
    """Fixed actionable errors never contain browser or server response bodies."""


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise URLError('Dashboard readiness may not redirect.')


def _probe(url, ca_file):
    opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=verified_client_context(ca_file)))
    try:
        with opener.open(Request(url), timeout=10) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


class LocalTracing:
    """No infrastructure or browser allocation; credentials never enter snapshots."""
    def __init__(self, config, *, emissions, probe=None, cleanup_timeout=5, binding=None, owner_root=None):
        if type(cleanup_timeout) not in (int, float) or not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0:
            raise ContractError('Invalid cleanup timeout')
        self.cleanup_timeout = cleanup_timeout
        self._probe_task = None
        self._closed = False
        self.config = config if isinstance(config, Mapping) else config.values
        resolved = config if hasattr(config, 'fields') else self.config
        self.emissions = emissions
        self.probe = probe or _probe
        self.viewer_url = None
        self.state = 'disabled'
        self.authentication = 'not_attempted'
        self.diagnostic = None
        self._credentials = None
        self._binding = binding
        self._admission_lock = asyncio.Lock()
        self._provider_selected = bool(self.config.get('TRACE_PROVIDER'))
        self._resolved, self._owner_root = resolved, owner_root
        if self._binding is not None:
            self.config = self._binding.values

    @staticmethod
    def _probe_finished(task):
        if not task.cancelled():
            task.exception()

    async def start(self):
        if self._closed:
            raise DashboardError('Tracing dashboard owner is closed.')
        async with self._admission_lock:
            if self._binding is None and self._provider_selected:
                from api_agent.telemetry.tracing_providers import effective_selection, admit_binding
                selected = effective_selection(self._resolved, owner_root=self._owner_root, require_export=False)
                if selected is not None:
                    from api_agent.telemetry.privacy import register_secret_async
                    for name in selected.declaration.secret_fields:
                        value = selected.values.get(name)
                        if type(value) is str and value:
                            await register_secret_async(self.emissions, value)
                    self._binding = admit_binding(self._resolved, owner_root=self._owner_root, boundary=self.emissions,
                                                  require_export=False)
                    self.config = self._binding.values
        if self._binding is not None:
            return await self._start_provider()
        if self._provider_selected:
            return self
        return self

    async def _start_provider(self):
        declaration = self._binding.declaration.dashboard
        if declaration is None:
            self.state = 'unavailable'
            self.diagnostic = 'The selected tracing provider has no dashboard capability.'
            return self
        url = self.config.get(declaration.url_field)
        readiness = (self.config.get(declaration.readiness_field) if declaration.readiness_field
                     else url.rstrip('/') + declaration.readiness_path if type(url) is str else url)
        if safe_destination(url) is None or safe_destination(readiness) is None or _origin(url) != _origin(readiness):
            raise DashboardError('Tracing dashboard requires admitted same-origin HTTP(S) destinations.')
        if declaration.username_field is not None:
            values = tuple(self.config.get(field) for field in (declaration.username_field, declaration.password_field))
            if any(type(value) is not str or not value.strip() or '\x00' in value for value in values):
                raise DashboardError('Configure the explicitly declared dashboard human login fields.')
            self._credentials = values
            from api_agent.telemetry.privacy import register_secret_async
            for value in values:
                await register_secret_async(self.emissions, value)
        if declaration.login == 'provider':
            async with component_timeout('Tracing dashboard capability admission', 30):
                await self._binding.dashboard_adapter()
        self.state = 'unavailable'
        if self._probe_task is None:
            self._probe_task = asyncio.create_task(asyncio.to_thread(self.probe, readiness, self.config.get('CLIENT_CA_FILE') or None))
            self._probe_task.add_done_callback(self._probe_finished)
        async with component_timeout('Tracing dashboard readiness', 15):
            try:
                ready = await asyncio.shield(self._probe_task)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise DashboardError('Tracing dashboard readiness failed.') from None
        if ready and not self._closed:
            self.viewer_url, self.state = url.rstrip('/'), 'ready'
        elif not self._closed:
            self.diagnostic = 'The selected tracing dashboard is not ready.'
        return self

    def interactive_credentials(self):
        """For literal persistent interactive status only; never diagnostics/events."""
        return self._credentials

    async def login(self, page):
        if self.viewer_url is None:
            raise DashboardError('Tracing viewer is unavailable; check its configured address.')
        allowed = _origin(self.viewer_url)
        async def guard(route):
            if _origin(route.request.url) != allowed:
                await route.abort()
            else:
                await route.continue_()
        await page.route('**/*', guard)
        try:
            async with component_timeout('Tracing dashboard login', 45):
                response = await page.goto(self.viewer_url, wait_until='domcontentloaded')
                if response is None or response.status != 200 or _origin(page.url) != allowed:
                    raise DashboardError('Tracing dashboard did not load at its admitted origin.')
                declaration = self._binding.declaration.dashboard
                if declaration.login == 'none':
                    return
                self.authentication = 'failed'
                adapter = await self._binding.dashboard_adapter()
                username, password = self._credentials
                await adapter.prepare_dashboard(page, url=self.viewer_url, username=username, password=password)
                if _origin(page.url) != allowed:
                    raise DashboardError('Tracing dashboard login left its admitted origin.')
                identity = await adapter.dashboard_identity(page, url=self.viewer_url)
                if identity != username or _origin(page.url) != allowed:
                    raise DashboardError('Tracing dashboard identity could not be verified.')
                self.authentication = 'authenticated'
                return
        except asyncio.CancelledError:
            raise
        except DashboardError:
            raise
        except Exception:
            raise DashboardError('Tracing dashboard login failed; verify the configured account and dashboard readiness.') from None
        # Retain the origin guard while this dashboard page is alive.

    async def aclose(self):
        self._closed = True
        task = self._probe_task
        if task is not None and not task.done():
            done, _ = await asyncio.wait({task}, timeout=self.cleanup_timeout)
            if not done:
                raise CleanupPendingError((self,), self.aclose)
        self._probe_task = None
        if self._binding is not None:
            await self._binding.aclose()
        self._credentials = None
        self.viewer_url = None
