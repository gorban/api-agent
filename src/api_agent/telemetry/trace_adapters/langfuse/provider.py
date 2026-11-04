"""Explicit trace-only compatibility adapter for the configured LangFuse route."""
from base64 import b64encode
from api_agent.telemetry.tracing_providers import ExportHandles


class Adapter:
    version = 1

    def __init__(self, config, services):
        self.config, self.boundary = config, services['emissions']
        self.own = services['own_exporter']
        self.environment = services.get('export_environment')

    def create_exporter(self, plan):
        from api_agent.telemetry.tracing import _sdk_factory
        public, secret = self.config.get('LANGFUSE_PUBLIC_KEY'), self.config.get('LANGFUSE_SECRET_KEY')
        if any(type(value) is not str or not value for value in (public, secret)):
            raise ValueError('LangFuse tracing requires configured project API keys')
        auth = 'Basic ' + b64encode(f'{public}:{secret}'.encode()).decode()
        self.boundary.register_secret(auth)
        self.boundary.register_secret(auth[6:])
        endpoint = plan.trace_endpoint
        provider, tracer = _sdk_factory(endpoint, {**dict(plan.headers.get('traces', {})), 'Authorization': auth}, dict(plan.resource), own=self.own, environ=self.environment)
        return ExportHandles(provider, tracer)

    async def aclose(self):
        pass


def create_tracing_provider(config, services):
    return Adapter(config, services)
