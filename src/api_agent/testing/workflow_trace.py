"""Owned workflow trace lifetime independent of framework LLM ownership."""

from contextlib import asynccontextmanager

from api_agent.telemetry.tracing import TraceRuntime
from api_agent.telemetry.tracing_providers import admit_binding
from api_agent.workflows.observations import WorkflowObservationFactory


@asynccontextmanager
async def owned_workflow_trace(factory, config, *, owner_root, environ=None, explicit_fields=None,
                               collector_snapshot=None):
    """Bind selected tracing to an existing capture factory for one owned run."""
    binding = admit_binding(config, owner_root=owner_root, boundary=factory.boundary,
                            environ=environ, explicit_fields=explicit_fields)
    if binding is None:
        yield factory
        return
    runtime = TraceRuntime(config, boundary=factory.boundary, binding=binding)
    try:
        if collector_snapshot is not None:
            if not collector_snapshot.get('trace_collector_plan'):
                raise ValueError('Captured tracing collector is unavailable')
            from api_agent.runtime.tracing_collector_snapshot import verify_collector_binding
            verify_collector_binding(collector_snapshot, binding)
        await runtime.astart()
        yield WorkflowObservationFactory(config, boundary=factory.boundary,
                                         sink=factory.sink, trace=runtime)
    finally:
        await runtime.aclose()
