"""Pure effective tracing selection shared by execution and captured planning."""
from dataclasses import dataclass, replace
from copy import deepcopy
from hashlib import sha256
import json
import os
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path

from .tracing_providers import read_declaration, select_declaration, TracingProviderError

OTEL_KEYS = frozenset({
    'OTEL_SDK_DISABLED', 'OTEL_EXPORTER_OTLP_ENDPOINT',
    'OTEL_EXPORTER_OTLP_PROTOCOL', 'OTEL_EXPORTER_OTLP_HEADERS',
    'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', 'OTEL_EXPORTER_OTLP_TRACES_PROTOCOL',
    'OTEL_EXPORTER_OTLP_TRACES_HEADERS', 'OTEL_EXPORTER_OTLP_METRICS_ENDPOINT',
    'OTEL_EXPORTER_OTLP_METRICS_PROTOCOL', 'OTEL_EXPORTER_OTLP_METRICS_HEADERS',
    'OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE',
    'OTEL_RESOURCE_ATTRIBUTES', 'OTEL_SERVICE_NAME',
})


@dataclass(frozen=True)
class EffectiveSelection:
    declaration: object
    values: object
    explicit_fields: frozenset
    environment: object
    location: str
    path: object
    identity: str


def effective_selection(config, *, owner_root=None, environ=None, declaration=None, require_export=True, explicit_fields=None):
    """Resolve actual signal destinations before any factory or infrastructure work."""
    from api_agent.providers.loading import provider_location
    from .genai_metrics import export_plan, signal_endpoint
    resolved = hasattr(config, 'fields') and hasattr(config, 'values')
    values = deepcopy(dict(config.values if resolved else config))
    explicit = (frozenset(explicit_fields) if explicit_fields is not None else
                frozenset(name for name, value in config.fields.items() if value.explicit) if resolved else frozenset(values))
    location = values.get('TRACE_PROVIDER')
    if not location:
        return None
    if declaration is None:
        path, _ = provider_location(location, owner_root=owner_root)
    else:
        path_text, separator, factory = location.rpartition(':')
        if not separator or not factory.isidentifier():
            raise TracingProviderError('Invalid captured tracing provider location')
        path = Path(path_text)
        if not path.is_absolute():
            path = Path(owner_root or Path.cwd()) / path
        path = path.absolute()
    selected = select_declaration(declaration or read_declaration(path.parent), values)
    if selected is None:
        return None
    if require_export and values.get('TRACE_METRICS') and 'metrics' not in selected.signals:
        raise TracingProviderError('Selected tracing provider does not support metrics')
    environment = {key: value for key, value in (os.environ if environ is None else environ).items() if key in OTEL_KEYS}
    if require_export:
        plan = export_plan(values, selected, {}, environ=environment, explicit_fields=explicit)
        trace_endpoint = plan.trace_endpoint
        if plan.metric_endpoint is not None:
            values['TRACE_METRICS_ENDPOINT'] = plan.metric_endpoint
    else:
        plan = None
        trace_endpoint = (signal_endpoint(values, selected, 'traces', environ=environment,
                          explicit_fields=explicit, required=False) if 'traces' in selected.signals else None)
    if trace_endpoint is not None:
        values[selected.endpoint_field] = trace_endpoint
    # A profile's local dashboard default has no authority over remote ingestion.
    if selected.managed_endpoints:
        managed = dict(selected.managed_endpoints)
        local = trace_endpoint in (managed['trace_endpoint'], *managed.get('trace_endpoint_aliases', ()))
        field = selected.dashboard.url_field
        if local and 'dashboard_port_field' in managed:
            port = values.get(managed['dashboard_port_field'])
            if type(port) is not int or not 1 <= port <= 65535:
                if require_export or field not in explicit or not values.get(field):
                    raise TracingProviderError('Managed dashboard requires a valid configured port')
                # An explicit dashboard can be viewed independently, but a bad
                # local port never grants authority over managed resources.
                local = False
            else:
                parsed = urlsplit(managed['dashboard_url'])
                managed['dashboard_url'] = urlunsplit(parsed._replace(netloc=('[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname) + ':' + str(port)))
        if local and field not in explicit:
            values[field] = values.get(field) or managed['dashboard_url']
        if not local and field not in explicit:
            values[field] = ''
            selected = replace(selected, dashboard=None)
        if not values.get(field):
            selected = replace(selected, dashboard=None)
        if (not local
                or values.get(field) != managed['dashboard_url']):
            selected = replace(selected, infrastructure=(), managed_compose_profile=None)
    proof = {'descriptor': selected.source.sha256, 'profile': selected.profile,
             'dashboard': values.get(selected.dashboard.url_field) if selected.dashboard else None}
    if require_export:
        proof.update(trace=plan.trace_endpoint, metrics=plan.metric_endpoint,
                     protocol=plan.protocol, temporality=plan.temporality,
                     headers={key: dict(value) for key, value in plan.headers.items()},
                     resource=_resource_identity(values, selected, plan, environment))
    else:
        proof.update(purpose='dashboard', infrastructure=selected.infrastructure,
                     managed_compose_profile=selected.managed_compose_profile)
    identity = sha256(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return EffectiveSelection(selected, MappingProxyType(values),
        explicit,
        MappingProxyType(environment), location, path, identity)


def _resource_identity(values, declaration, plan, environment):
    from .genai_metrics import resource_plan
    from .privacy import EmissionBoundary
    secrets = [values[field] for field in declaration.secret_fields if type(values.get(field)) is str and values[field]]
    secrets.extend(value for headers in plan.headers.values() for value in headers.values() if value)
    return resource_plan(values, EmissionBoundary(secrets=secrets), environ=environment)
