"""Selected tracing infrastructure through existing captured planning and leases."""
from dataclasses import replace
from pathlib import Path
from collections.abc import Mapping

from api_agent.telemetry.tracing_providers import TracingProviderError, effective_selection


def tracing_requested(config, *, owner_root=None):
    """Skip disabled profiles while leaving invalid optional tracing to its owner."""
    values = config if isinstance(config, Mapping) else config.values
    if not values.get('TRACE_PROVIDER'):
        return False
    try:
        return effective_selection(config, owner_root=owner_root) is not None
    except (ValueError, OSError):
        return True


def selected_tracing_infrastructure(context, values, *, selection=None):
    from .infrastructure_contract import load_infrastructure_contributions
    selected = selection if selection is not None else effective_selection(values, owner_root=context.owner_root)
    if selected is None:
        return (), context
    location, path, declaration = selected.location, selected.path, selected.declaration
    if not declaration.infrastructure:
        return (), context
    contributions = load_infrastructure_contributions(context, selected_provider_locations=(location,))
    ordered = selected_closure(contributions, declaration.infrastructure)
    provider_sources = [item for item in ordered if item.source.parent == path.parent]
    if provider_sources:
        owners = {item.owner for item in provider_sources}
        if owners != {declaration.id}:
            raise TracingProviderError('Tracing infrastructure owner must match its provider declaration')
        source = (declaration.id, path.parent, (path.parent / 'infrastructure.toml',))
        context = replace(context, inventory_sources=(*context.inventory_sources, source))
    return tuple(ordered), context


def selected_closure(contributions, roots):
    """One pure selected dependency closure for live and captured consumers."""
    by_id = {item.id: item for item in contributions}
    ordered, visiting, seen = [], set(), set()

    def visit(identity):
        if identity in visiting or identity not in by_id:
            raise TracingProviderError('Selected tracing infrastructure has a missing or cyclic dependency')
        if identity not in seen:
            visiting.add(identity)
            for dependency in by_id[identity].dependencies:
                visit(dependency)
            visiting.remove(identity)
            seen.add(identity)
            ordered.append(by_id[identity])

    for identity in roots:
        visit(identity)
    return tuple(ordered)


def custom_trace_prerequisites(context, values, *, cancel_event=None, tool_plan=None, require_export=True):
    from api_agent.config import load_shared_schema
    from api_agent.config.resolve import ConfigResult
    from api_agent.providers.loading import bootstrap_schema
    from .pack_admission import configuration_inputs
    from .pack_composition import load_context_schema
    from .compose import ENGINE_ENVIRONMENT_KEYS, _tracing_destination
    from .infrastructure_plan import plan_infrastructure
    from .inventory import capture_inventory
    from .ownership import StackScope
    from .prerequisites import PrerequisiteManager
    import os
    selected_trace = effective_selection(values, owner_root=context.owner_root, require_export=require_export)
    if selected_trace is None:
        return None
    values = selected_trace.values
    contributions, admitted_context = selected_tracing_infrastructure(context, values, selection=selected_trace)
    profile = selected_trace.declaration.managed_compose_profile
    if not contributions and not profile:
        return None
    scope = StackScope.from_roots(context.base_root, context.pack_root)
    schema = load_context_schema(context, load_shared_schema(context.base_root))
    schema = bootstrap_schema(schema, (), {}, {}, values, owner_root=context.owner_root,
                              configuration_paths=context.provider_config_paths, **configuration_inputs(context))
    # A custom tracing selection cannot acquire legacy collectors or report storage.
    selected = {**values, 'ALLURE_S3_USE_LOCALSTACK': False}
    environment = {key: os.environ[key] for key in ENGINE_ENVIRONMENT_KEYS if key in os.environ}
    plan = plan_infrastructure(ConfigResult(selected, {}, (), (), ()), schema, contributions, scope,
        engine_environment=environment, inventory=capture_inventory(admitted_context, tool_plan),
        tracing_profiles=(profile,) if profile else (),
        tracing_destination=_tracing_destination(selected_trace))
    if {item.id for item in plan.conditional} != {item.id for item in contributions}:
        raise TracingProviderError('Selected tracing infrastructure requires its admitted activation configuration')
    return PrerequisiteManager(plan, scope, context.owner_root / 'artifacts/prerequisites',
        engine_environment=environment, cancelled=cancel_event.is_set if cancel_event is not None else lambda: False,
        cleanup_timeout_seconds=60)
