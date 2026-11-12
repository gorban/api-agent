"""Owned workflow trace lifetime independent of framework LLM ownership."""

import asyncio
from contextlib import asynccontextmanager
import sys
from threading import Lock

from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import report_timeout
from api_agent.runtime.tasks import offload
from api_agent.telemetry.tracing import TraceDeliveryError, TraceRuntime, TRACE_DELIVERY_WARNING
from api_agent.telemetry.tracing_providers import admit_binding
from api_agent.workflows.observations import WorkflowObservationFactory


def _warning(message):
    print('WARNING: ' + message, file=sys.stderr, flush=True)


def _collector_diagnostic(error, factory, config, binding):
    """Explain infrastructure failures locally without exposing arbitrary SDK errors."""
    from api_agent.runtime.prerequisites import PrerequisiteError
    from api_agent.testing.diagnostics import CredentialScrubber, write_exception_diagnostic
    if not isinstance(error, PrerequisiteError):
        return
    try:
        # Acquisition precedes SDK startup, where exporter headers normally
        # become registered secrets. Resolve the admitted plan without opening
        # an exporter so failed startup diagnostics get the same protection.
        plan = binding._export_plan({})
        headers = tuple(value for signal in plan.headers.values() for value in signal.values() if value)
        CredentialScrubber(config.values, boundary=factory.boundary, secret_values=headers)
        # Do not traverse the original exception's cause/context chain: it can
        # contain an SDK response whose narrative is not admitted for logging.
        diagnostic = PrerequisiteError('Tracing collector prerequisite failed.',
                                       local_details=error.local_details)
        write_exception_diagnostic(diagnostic, sys.stderr.write, authority=factory.boundary,
                                   label='Tracing collector prerequisite')
    except Exception:
        # Optional diagnostic delivery must not replace the application outcome.
        pass


class _TraceResources:
    """Keep the collector lease until SDK and binding disposal are confirmed."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.manager = None
        self.runtime_closed = False
        self.closed = False

    async def aclose(self):
        if self.closed:
            return True
        if not self.runtime_closed:
            try:
                await self.runtime.aclose()
            except TraceDeliveryError:
                self.runtime_closed = True
                _warning(TRACE_DELIVERY_WARNING)
            except asyncio.CancelledError:
                _warning('Tracing cleanup failed; resources remain owned for retry.')
                return False
            except Exception:
                _warning('Tracing cleanup failed; resources remain owned for retry.')
                return False
            else:
                self.runtime_closed = True
        if self.manager is not None:
            try:
                await offload(self.manager.release, retain=False)
            except asyncio.CancelledError:
                _warning('Tracing collector release failed; resources remain owned for retry.')
                return False
            except Exception:
                _warning('Tracing collector release failed; resources remain owned for retry.')
                return False
        self.closed = True
        return True


# A worker retains at most one incomplete trace; later scopes retry it first.
_pending = None
_active = Lock()


async def drain_pending_workflow_trace():
    """Retry retained cleanup at the next scope and at worker session finish."""
    global _pending
    owner = _pending
    if owner is None:
        return True
    try:
        await finish_cleanup(owner.aclose())
    finally:
        if owner.closed and _pending is owner:
            _pending = None
    return owner.closed


async def _close_owned(owner):
    global _pending
    if owner is not None:
        _pending = owner
        await drain_pending_workflow_trace()


@asynccontextmanager
async def owned_workflow_trace(factory, config, *, owner_root, environ=None, explicit_fields=None,
                               collector_snapshot=None):
    """Bind selected tracing to an existing capture factory for one owned run."""
    if not _active.acquire(blocking=False):
        _warning('Tracing is already active in this worker; continuing without tracing.')
        yield factory
        return
    held = True
    try:
        async with _owned_workflow_trace_locked(factory, config, owner_root=owner_root,
                environ=environ, explicit_fields=explicit_fields,
                collector_snapshot=collector_snapshot) as scoped:
            if scoped is factory:
                _active.release()
                held = False
            yield scoped
    finally:
        if held:
            _active.release()


@asynccontextmanager
async def _owned_workflow_trace_locked(factory, config, *, owner_root, environ,
                                       explicit_fields, collector_snapshot):
    if not await drain_pending_workflow_trace():
        _warning('Tracing cleanup remains pending; continuing without tracing.')
        yield factory
        return
    binding = owner = None
    stage = 'binding'
    try:
        binding = admit_binding(config, owner_root=owner_root, boundary=factory.boundary,
                                environ=environ, explicit_fields=explicit_fields)
        if binding is not None:
            stage = 'runtime'
            runtime = TraceRuntime(config, boundary=factory.boundary, binding=binding)
            owner = _TraceResources(runtime)
            if collector_snapshot is not None:
                stage = 'collector snapshot'
                if not collector_snapshot.get('trace_collector_plan'):
                    raise ValueError('Captured tracing collector is unavailable')
                from api_agent.runtime.tracing_collector_snapshot import verify_collector_binding
                verify_collector_binding(collector_snapshot, binding)
                from api_agent.runtime.tracing_collector_snapshot import collector_from_snapshot
                manager = owner.manager = collector_from_snapshot(collector_snapshot, config.values)
                if manager is not None:
                    stage = 'collector acquisition'
                    async with report_timeout('Owned workflow trace collector readiness', manager.timeout) as budget:
                        await offload(manager.acquire, _deadline=manager.now() + budget.remaining)
            stage = 'startup'
            await runtime.astart()
    except BaseException as error:
        if isinstance(error, Exception):
            outcome = 'timed out' if isinstance(error, TimeoutError) else 'failed'
            _warning(f'Tracing setup {outcome} at {stage}; continuing without tracing.')
            if stage == 'collector acquisition':
                _collector_diagnostic(error, factory, config, binding)
        await _close_owned(owner)
        if not isinstance(error, Exception):
            raise
        binding = None
    if binding is None:
        yield factory
        return
    try:
        yield WorkflowObservationFactory(config, boundary=factory.boundary,
                                         sink=factory.sink, trace=owner.runtime)
    finally:
        await _close_owned(owner)
