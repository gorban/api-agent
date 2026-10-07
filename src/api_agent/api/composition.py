"""Compose generic application owners without upstream authentication."""
from dataclasses import replace
from api_agent.telemetry.tracing import TraceRuntime
from api_agent.runtime.client_tls import verified_client_context
from types import MappingProxyType
from collections.abc import Mapping

from api_agent.auth.extensions import load_identity_adapter
from api_agent.auth.session import SessionStore
from api_agent.config.resolve import ConfigResult
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from .application import Runtime


def activation_kinds(values, required_kinds=None):
    """Select execution independently of declaration/configuration discovery."""
    if values.get('HELP') or values.get('COLLECT_ONLY'):
        return frozenset()
    if required_kinds is not None:
        kinds = frozenset(required_kinds)
        if not kinds <= {'upstream', 'api', 'llm', 'access', 'ui'}:
            raise ValueError('invalid_activation_kind')
        return kinds | ({'upstream'} if 'api' in kinds else set())
    mode = values.get('TUI_MODE')
    if mode not in {'run-local', 'integration', 'e2e'}:
        return frozenset()
    kinds = {'upstream', 'api', 'llm', 'access'}
    if mode == 'e2e' or mode == 'run-local' and values.get('RUN_UI'):
        kinds.add('ui')
    return frozenset(kinds)


def compose_runtime(context, values, *, callback_origin, tls_material=None, required_kinds=None):
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
                                 'callback_origin': callback_origin, 'ssl_context': ssl_context,
                                 'owner_root': getattr(context, 'owner_root', None), 'tls_material': tls_material})
    for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'):
        if values.get(key): emissions.register_secret(values[key])
    trace_config = replace(result, values=values) if result is not None else ConfigResult(values, {}, (), (), ())
    tracing = TraceRuntime(trace_config, boundary=emissions)
    # Health/deferred application composition need not instantiate an LLM.
    # Executable LLM operations must call require_llm_provider first.
    active = activation_kinds(values, required_kinds)
    plan = []
    for kind, selection in (('upstream', 'UPSTREAM_API_PROVIDER'), ('api', 'API_PROVIDER'), ('llm', 'LLM_PROVIDER'), ('access', 'ACCESS_PROVIDER'), ('ui', 'UI_PROVIDER')):
        if kind not in active or not values.get(selection):
            continue
        plan.append((kind, values[selection], MappingProxyType(values), services))
    return Runtime(context.pack_id, sessions=sessions,
                   identity_plan=(lambda: load_identity_adapter(context, values)) if 'api' in active else None,
                   factory_plan=tuple(plan), services=services, emissions=emissions, tracing=tracing)
