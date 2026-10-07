from dataclasses import replace
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.runtime.client_tls import verified_client_context
"""Compose generic application owners without upstream authentication."""
from types import MappingProxyType
from collections.abc import Mapping

from api_agent.auth.extensions import load_identity_adapter
from api_agent.auth.session import SessionStore
from api_agent.config.resolve import ConfigResult
from api_agent.providers.loading import load_provider
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from .application import Runtime


def compose_runtime(context, values, *, callback_origin, tls_material=None):
    result = values if isinstance(values, ConfigResult) else None
    if result is not None:
        if result.errors or result.missing:
            raise ValueError('invalid_runtime_configuration')
        values = result.values
    if not isinstance(values, Mapping):
        raise ValueError('invalid_runtime_configuration')
    values = dict(values)
    if tls_material is not None and not values.get('CLIENT_CA_FILE'):
        values['CLIENT_CA_FILE'] = str(tls_material.ca_file)
    ssl_context = verified_client_context(values.get('CLIENT_CA_FILE'))
    sessions = SessionStore(context.pack_id, callback_origin)
    emissions = EmissionBoundary(ContentPolicy.from_config(result))
    services = MappingProxyType({'sessions': sessions, 'emissions': emissions,
                                 'callback_origin': callback_origin, 'ssl_context': ssl_context})
    for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):
        if values.get(key): emissions.register_secret(values[key])
    trace_config = replace(result, values=values) if result is not None else ConfigResult(values, {}, (), (), ())
    tracing = TraceRuntime(trace_config, boundary=emissions)
    # Health/deferred application composition need not instantiate an LLM.
    # Executable LLM operations must call require_llm_provider first.
    owner = load_provider(values['LLM_PROVIDER'], values, services) if values.get('LLM_PROVIDER') else None
    adapter = load_identity_adapter(context, values)
    return Runtime(context.pack_id, identity_adapter=adapter, sessions=sessions,
                   llm_provider=owner, emissions=emissions, tracing=tracing)
