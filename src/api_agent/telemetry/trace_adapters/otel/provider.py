"""Explicit compatibility adapter for the ordinary OTLP HTTP configuration."""
from api_agent.telemetry.tracing_providers import ExportHandles


class Adapter:
    version = 1

    def __init__(self, services):
        self.own = services['own_exporter']
        self.environment = services.get('export_environment')

    def create_exporter(self, plan):
        from api_agent.telemetry.tracing import _sdk_factory
        from api_agent.telemetry.genai_metrics import sdk_meter
        meter_provider = meter = None
        if plan.metric_endpoint is not None:
            meter_provider, meter = sdk_meter(plan, own=self.own)
            self.own(meter_provider)
        provider, tracer = _sdk_factory(plan.trace_endpoint, dict(plan.headers.get('traces', {})), dict(plan.resource), own=self.own, environ=self.environment)
        return ExportHandles(provider, tracer, meter_provider, meter)

    async def aclose(self):
        pass


def create_tracing_provider(config, services):
    return Adapter(services)
