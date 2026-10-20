"""Launch-owned recording prerequisites around an accepted integration run."""
import os
import sys
from threading import Event

from api_agent.runtime.compose import ENGINE_ENVIRONMENT_KEYS
from api_agent.runtime.ownership import StackScope
from .prerequisites import local_recording_prerequisites
from .recorded import validate_recorded_integration, validate_integration_start
from .suite_lock import suite_locked
from .requirements import plan_integration_requirements


@suite_locked
def run_launched_integration(context, tests, config, *, accepted=False,
                             no_std_in=False, prerequisites=None, reporting_session=None, **options):
    """Acquire storage lazily; retain its cleanup handle through every outcome.

    Test recording finishes before the prerequisite lease is released. Only
    configured local reporting storage is started; selected API/UI providers and
    their listeners are independent operations. Injectable hooks remain owned
    by their caller. Inspection handles transfer with the execution result.
    """
    if config.get('SKIP_ALLURE_REPORTING') is True:
        from time import monotonic
        from .integration import run_integration_tests
        from .recorded import RecordedIntegrationResult
        from .recording import RunIdentity
        from .collection import repository_label
        tests = validate_integration_start(tests, config, accepted=accepted, no_std_in=no_std_in)
        plan = plan_integration_requirements(tests)
        plan.validate(context, config)
        started = monotonic()
        # No report installer, storage lease, recorder, attachment, manifest or
        # publication exists on this explicitly unrecorded path.
        execution = run_integration_tests(context, tests, accepted=accepted,
            no_std_in=False, config=config,
            parallel_jobs=config.get('TEST_PARALLEL_JOBS', 8),
            on_event=options.get('on_event'), output=options.get('output'),
            cancel_event=options.get('cancel_event'),
            unattended_index_source=options.get('unattended_index_source'),
            interaction=options.get('interaction'))
        return RecordedIntegrationResult(execution,
            RunIdentity.create(repository_label(context, context.owner_root), 'integration'),
            None, {}, None, False, monotonic() - started, reporting_skipped=True)
    tests = validate_recorded_integration(tests, config, accepted=accepted, no_std_in=no_std_in)
    plan = plan_integration_requirements(tests)
    plan.validate(context, config)
    cancelled = options.setdefault('cancel_event', Event())
    if prerequisites is None:
        scope = StackScope.from_roots(context.base_root, context.pack_root)
        prerequisites = local_recording_prerequisites(context, scope=scope,
            ownership_directory=context.owner_root / 'artifacts' / 'prerequisites',
            engine_environment={key: os.environ[key] for key in ENGINE_ENVIRONMENT_KEYS if key in os.environ},
            cancel_event=cancelled, provider_config=config,
            provider_kinds=plan.provider_kinds, reporting_session=reporting_session)
    prepare = options.pop('prepare', None)

    def before(usage):
        prerequisites.prepare(tests, config, accepted=accepted, no_std_in=no_std_in)
        if prepare is not None:
            prepare(usage)

    from .recorded import run_recorded_integration
    execution = None
    try:
        execution = run_recorded_integration(context, tests, config, accepted=accepted,
            no_std_in=no_std_in, prepare=before, **options)
        return execution
    finally:
        primary = sys.exc_info()[1]
        try:
            prerequisites.finish()
        except BaseException as error:
            retained = primary if primary is not None else error
            retained.recorded_result = execution
            retained.recording_prerequisites = prerequisites
            from .suite_lock import _cleanup_receipts
            receipts = tuple(_cleanup_receipts(retained))
            pending = prerequisites.pending_cleanup
            if pending is not None and all(pending is not receipt for receipt in receipts):
                target = receipts[-1] if receipts else retained
                target.cleanup_pending = pending
            elif pending is retained and getattr(retained, 'cleanup_pending', None) is None:
                # Existing consumers use this alias even for a direct receipt.
                retained.cleanup_pending = pending
            if primary is None:
                raise
