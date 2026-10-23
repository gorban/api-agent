"""Readiness and dashboard authentication owned by one local operation."""
import asyncio
import math
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from api_agent.providers.progress import component_timeout
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.contracts import ContractError
from api_agent.runtime.client_tls import verified_client_context
from api_agent.runtime.compose import ComposeError, selected_profiles
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
    def __init__(self, config, *, emissions, probe=None, cleanup_timeout=5):
        if type(cleanup_timeout) not in (int, float) or not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0:
            raise ContractError('Invalid cleanup timeout')
        self.cleanup_timeout = cleanup_timeout
        self._probe_task = None
        self._closed = False
        self.config, self.emissions = config, emissions
        self.probe = probe or _probe
        self.viewer_url = None
        self.state = 'disabled'
        self.authentication = 'not_attempted'
        self.diagnostic = None
        self._credentials = None

    @staticmethod
    def _probe_finished(task):
        if not task.cancelled():
            task.exception()

    async def start(self):
        if self._closed:
            raise DashboardError('Tracing dashboard owner is closed.')
        mode = self.config.get('TRACE_MODE', '')
        if not mode:
            return self
        if mode not in {'LangFuse', 'OTEL'}:
            raise DashboardError('Unsupported tracing dashboard mode.')
        try:
            local = selected_profiles(self.config)
        except ComposeError:
            raise DashboardError('Invalid tracing destination configuration.') from None
        if mode == 'LangFuse':
            url = self.config['TRACE_TARGET'].rstrip('/')
            email, password = (self.config.get(key) for key in ('LANGFUSE_ADMIN_EMAIL', 'LANGFUSE_ADMIN_PASSWORD'))
            if not all(isinstance(value, str) and value.strip() and '\x00' not in value for value in (email, password)):
                raise DashboardError('Configure the LangFuse dashboard admin email and password.')
            self._credentials = email, password
            for value in self._credentials:
                self.emissions.register_secret(value)
            path = '/auth/sign-in'
        else:
            url = self.config.get('TRACE_VIEWER_URL')
            if not url and 'otel' in local:
                url = 'http://localhost:' + str(self.config.get('JAEGER_VIEWER_PORT', 16686))
            path = '/'
        self.state = 'unavailable'
        if not url:
            self.diagnostic = ('Tracing viewer is unavailable: set TRACE_VIEWER_URL for the custom OTEL collector, '
                               'or use the managed local OTLP traces endpoint on port 4318.')
            return self
        if safe_destination(url) is None:
            raise DashboardError('Tracing viewer requires an HTTP(S) URL without credentials, query or fragment.')
        if self._probe_task is None:
            self._probe_task = asyncio.create_task(asyncio.to_thread(self.probe,
                url.rstrip('/') + path, self.config.get('CLIENT_CA_FILE') or None))
            self._probe_task.add_done_callback(self._probe_finished)
        async with component_timeout('Tracing dashboard readiness', 15):
            try:
                ready = await asyncio.shield(self._probe_task)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise DashboardError('Tracing dashboard readiness failed; check its configured address.') from None
        if ready and not self._closed:
            self.viewer_url, self.state = url.rstrip('/'), 'ready'
        elif not self._closed:
            self.diagnostic = (
                'Tracing viewer readiness failed: check TRACE_VIEWER_URL or the managed local '
                'Jaeger service and JAEGER_VIEWER_PORT; no dashboard tab was opened.' if mode == 'OTEL' else
                'Tracing viewer readiness failed: check TRACE_TARGET and the LangFuse service; '
                'no dashboard tab was opened.')
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
                if self.config['TRACE_MODE'] == 'OTEL':
                    response = await page.goto(self.viewer_url, wait_until='domcontentloaded')
                    if response is None or response.status != 200 or _origin(page.url) != allowed:
                        raise DashboardError('Tracing viewer did not load at its configured origin.')
                    return
                self.authentication = 'failed'
                await page.goto(self.viewer_url + '/auth/sign-in', wait_until='domcontentloaded')
                if _origin(page.url) != allowed:
                    raise DashboardError('LangFuse dashboard redirected outside its configured origin.')
                email, password = self._credentials
                await page.get_by_label('Email', exact=True).fill(email)
                continue_button = page.get_by_role('button', name='Continue', exact=True)
                if await continue_button.is_visible():
                    await continue_button.click()
                if _origin(page.url) != allowed:
                    raise DashboardError('LangFuse dashboard redirected outside its configured origin.')
                await page.get_by_label('Password', exact=False).fill(password)
                await page.get_by_role('button', name='Sign in', exact=True).click()
                await page.wait_for_url(lambda url: _origin(str(url)) == allowed and '/auth/' not in urlsplit(str(url)).path)
                # Verify the authenticated identity independently of navigation.
                response = await page.request.get(self.viewer_url + '/api/auth/session', max_redirects=0)
                if response.status != 200:
                    raise DashboardError('LangFuse dashboard account could not be verified.')
                session = await response.json()
                if session.get('user', {}).get('email') != email:
                    raise DashboardError('LangFuse dashboard account could not be verified.')
                await page.get_by_role('link', name='Go to project', exact=True).first.wait_for(state='visible')
                self.authentication = 'authenticated'
        except asyncio.CancelledError:
            raise
        except DashboardError:
            raise
        except Exception:
            raise DashboardError('LangFuse dashboard login failed; verify the configured account and dashboard readiness.') from None
        # Retain the origin guard while this dashboard page is alive.

    async def aclose(self):
        self._closed = True
        task = self._probe_task
        if task is not None and not task.done():
            done, _ = await asyncio.wait({task}, timeout=self.cleanup_timeout)
            if not done:
                raise CleanupPendingError((self,), self.aclose)
        self._probe_task = None
        self._credentials = None
        self.viewer_url = None
