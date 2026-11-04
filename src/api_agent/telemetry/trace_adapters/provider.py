"""Bundled HTTP exporters and their provider-owned dashboard authentication."""
from base64 import b64encode
from urllib.parse import urlsplit
from api_agent.telemetry.tracing_providers import ExportHandles


class Adapter:
    version = 1

    def __init__(self, config, services):
        self.config = config
        self.boundary = services['emissions']
        self.own = services['own_exporter']
        self.factory = services.get('sdk_factory')
        self.environment = services.get('export_environment')

    def create_exporter(self, plan):
        from api_agent.telemetry.tracing import _sdk_factory
        from api_agent.telemetry.genai_metrics import sdk_meter
        headers = dict(plan.headers.get('traces', {}))
        if self.config['TRACE_MODE'] == 'LangFuse':
            public, secret = self.config.get('LANGFUSE_PUBLIC_KEY'), self.config.get('LANGFUSE_SECRET_KEY')
            if any(type(value) is not str or not value for value in (public, secret)):
                raise ValueError('LangFuse tracing requires configured project API keys')
            auth = 'Basic ' + b64encode(f'{public}:{secret}'.encode()).decode()
            self.boundary.register_secret(auth)
            self.boundary.register_secret(auth[6:])
            headers['Authorization'] = auth
        meter_provider = meter = None
        if plan.metric_endpoint is not None:
            meter_provider, meter = sdk_meter(plan, own=self.own)
            self.own(meter_provider)
        if self.factory is None:
            provider, tracer = _sdk_factory(plan.trace_endpoint, headers, dict(plan.resource), own=self.own, environ=self.environment)
        else:
            provider, tracer = self.factory(plan.trace_endpoint, headers, dict(plan.resource))
        return ExportHandles(provider, tracer, meter_provider, meter)

    async def prepare_dashboard(self, page, *, url, username, password):
        from api_agent.runtime.local_tracing import DashboardError, _origin
        allowed = _origin(url)
        await page.goto(url.rstrip('/') + '/auth/sign-in', wait_until='domcontentloaded')
        if _origin(page.url) != allowed:
            raise DashboardError('Tracing dashboard login left its admitted origin.')
        await page.get_by_label('Email', exact=True).fill(username)
        button = page.get_by_role('button', name='Continue', exact=True)
        if await button.is_visible():
            await button.click()
        if _origin(page.url) != allowed:
            raise DashboardError('Tracing dashboard login left its admitted origin.')
        await page.get_by_label('Password', exact=False).fill(password)
        await page.get_by_role('button', name='Sign in', exact=True).click()
        await page.wait_for_url(lambda value: _origin(str(value)) == allowed and '/auth/' not in urlsplit(str(value)).path)
        await page.get_by_role('link', name='Go to project', exact=True).first.wait_for(state='visible')

    async def dashboard_identity(self, page, *, url):
        response = await page.request.get(url.rstrip('/') + '/api/auth/session', max_redirects=0)
        if response.status != 200:
            return None
        session = await response.json()
        return session.get('user', {}).get('email')

    async def aclose(self):
        pass


def create_tracing_provider(config, services):
    return Adapter(config, services)
