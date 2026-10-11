"""Keep the async API/refresh owners alive around recorded pytest execution.

The interactive consumer supplies acceptance and the observed login adapter.
This module does not synthesize acceptance or implement browser locators.
"""
import asyncio
import sys
from threading import Event

from api_agent.runtime.tasks import settle as _settle
from .recorded import run_recorded_integration, validate_recorded_integration
from .suite_lock import suite_locked, suite_worker


@suite_locked
async def run_owned_integration(context, tests, config, *, operation, accepted=False,
                                session_factory=None, gateway_factory=None,
                                cancel_event=None, storage=None, runner=None,
                                on_event=None, output=None, usage_output=None,
                                quota_timeout=20):
    """Drain pytest, capture final quota, stop refresh, persist, then release services.

    A supplied session factory receives ``(OperationSession, SuiteUsage)`` and
    returns an unattended owner with async start/current_obo/stop methods. Its
    login adapter remains explicit until the human-assisted browser walkthrough.
    Factories and all async clients live on this event loop; pytest runs in an
    owned worker thread. Client/session cleanup finishes before this returns.
    """
    tests = validate_recorded_integration(tests, config, accepted=accepted)
    if (session_factory is None) != (gateway_factory is None):
        raise ValueError('Unattended session and quota client must be supplied together')
    cancel_event = cancel_event if cancel_event is not None else Event()
    loop = asyncio.get_running_loop()
    session = gateway = None
    finished = False
    callbacks = set()
    interruptible = set()

    def submit(coroutine, *, protected=False):
        async def invoke():
            task = asyncio.current_task()
            callbacks.add(task)
            if not protected:
                interruptible.add(task)
            try:
                return await coroutine
            finally:
                callbacks.discard(task)
                interruptible.discard(task)
        return asyncio.run_coroutine_threadsafe(invoke(), loop).result()

    async def drain_callbacks():
        await asyncio.gather(*tuple(callbacks), return_exceptions=True)

    async def finish():
        nonlocal finished
        if finished:
            return
        finished = True
        failure = None
        for owner, method in ((session, 'stop'), (gateway, 'aclose')):
            if owner is not None:
                try:
                    await getattr(owner, method)()
                except BaseException as error:
                    failure = failure or error
        if failure is not None:
            raise failure

    def validate(active):
        validate_recorded_integration(tests, active.values, accepted=accepted)

    async with operation.open(retain=True, validate=validate) as owned:
        # The coordinator resolves again after any TUI edits; never reuse the
        # initial configuration snapshot for the actual workload or storage.
        active = owned.config.values
        tests = validate_recorded_integration(tests, active, accepted=accepted)

        async def prepare(usage):
            nonlocal session, gateway
            if cancel_event.is_set():
                raise asyncio.CancelledError
            if session_factory is not None:
                session = session_factory(owned, usage)
                gateway = gateway_factory(owned)
                await session.start()
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                await gateway.astart()

        async def quota():
            obo = await session.current_obo()
            return await gateway.fetch(obo.access_token)

        options = dict(accepted=True, storage=storage,
            prepare=lambda usage: submit(prepare(usage)),
            finish=lambda: submit(finish(), protected=True),
            quota_fetcher=(lambda: submit(asyncio.wait_for(quota(),
                min(5, quota_timeout) if cancel_event.is_set() else quota_timeout)))
                if session_factory is not None else None,
            cancel_event=cancel_event, on_event=on_event, output=output,
            usage_output=usage_output, evidence_boundary=owned.runtime.emissions,
            content_policy=getattr(owned.runtime.emissions, 'policy', None))
        if runner is not None:
            options['runner'] = runner
        worker = asyncio.create_task(asyncio.to_thread(suite_worker(
            run_recorded_integration, context, tests, active, **options)))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()
            for task in tuple(interruptible):
                task.cancel()
            try:
                await _settle(worker)
            except BaseException:
                pass
            raise
        finally:
            primary = sys.exc_info()[1]
            cancellations = asyncio.current_task().cancelling()
            try:
                await _settle(asyncio.create_task(drain_callbacks()))
                await _settle(asyncio.create_task(finish()))
            except BaseException:
                if primary is None:
                    raise
            if primary is None and asyncio.current_task().cancelling() > cancellations:
                raise asyncio.CancelledError
