"""Accepted recording preparation, without API, listener or TLS startup."""
from threading import Event

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.operation import OperationCleanup
from api_agent.runtime.prerequisites import PrerequisiteCancelled


class RecordingPrerequisites:
    """Synchronous recording hooks with retained, asynchronously retryable cleanup.

    Creating this object is inert. Only an accepted integration selection can prepare
    report dependencies or acquire a configured local storage lease. Keep the
    owner until finish succeeds; callers must finish in their outer finally too.
    """

    def __init__(self, *, prepare_dependencies, plan, manager, cancel_event=None,
                 timeout=120, reporting_session=None):
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        self.prepare_dependencies = prepare_dependencies
        self.plan = plan
        self.manager = manager
        self.reporting_session = reporting_session
        self.cancel_event = cancel_event if cancel_event is not None else Event()
        self.timeout = timeout
        self.owned_manager = None
        self.lease = None
        self.pending_cleanup = None
        self.prepared = False

    def _check_cancelled(self):
        if self.cancel_event.is_set():
            raise PrerequisiteCancelled('Recording preparation cancelled.')

    def prepare(self, tests, config, *, accepted=False, no_std_in=False):
        from .recorded import validate_recorded_integration
        validate_recorded_integration(tests, config, accepted=accepted, no_std_in=no_std_in)
        if self.pending_cleanup is not None:
            raise self.pending_cleanup
        if self.prepared:
            return self.lease
        self._check_cancelled()
        self.prepare_dependencies()
        self._check_cancelled()
        # Reports require storage only. Tracing/API profiles belong to their
        # independent operation owner and cannot be selected by this adapter.
        storage_config = {key: value for key, value in config.items()
                          if key.startswith('ALLURE_S3_') or key == 'ALLURE_LOCALSTACK_PORT'}
        selected_plan = self.plan(storage_config)
        self._check_cancelled()
        self.owned_manager = self.manager(selected_plan, self.cancel_event.is_set)
        acquired = False
        try:
            self.lease = self.owned_manager.acquire()
            acquired = True
            self._check_cancelled()
            if self.reporting_session is not None:
                self.reporting_session(selected_plan)
            self._check_cancelled()
        except BaseException as primary:
            self.lease = getattr(self.owned_manager, 'lease', None)
            # Successful storage acquisition stays leased until the interrupted
            # outcome has been published. The outer owner then rolls it back.
            if not acquired:
                try:
                    self.finish()
                except CleanupPendingError as pending:
                    primary.cleanup_pending = pending
            raise
        self.prepared = True
        return self.lease

    def _complete(self):
        self.owned_manager = self.lease = self.pending_cleanup = None
        self.prepared = False

    def finish(self):
        """Relinquish active use only after recording/publication settles."""
        if self.pending_cleanup is not None:
            raise self.pending_cleanup
        if self.owned_manager is None:
            return
        retain = self.prepared and getattr(self.owned_manager, 'session_lifecycle', False) is not True
        try:
            self.owned_manager.release(retain=retain)
        except BaseException:
            cleanup = OperationCleanup(self.owned_manager, None, None,
                self.lease or getattr(self.owned_manager, 'lease', None) or self.owned_manager, retain,
                self.timeout, complete=self._complete)
            self.pending_cleanup = CleanupPendingError((self.owned_manager,), cleanup.retry)
            raise self.pending_cleanup from None
        self._complete()


def local_recording_prerequisites(context, *, scope, ownership_directory, argv=(),
                                 engine_environment=None, cancel_event=None, timeout=120,
                                 provider_config=None, provider_kinds=(), reporting_session=None):
    """Build inert hooks using the shared reports installer and scoped manager."""
    from api_agent.runtime.dependencies import active_environment, ensure_environment
    from api_agent.runtime.compose import plan_compose
    from api_agent.runtime.prerequisites import PrerequisiteManager
    from api_agent.runtime.reporting_registry import SharedReportingManager

    def dependencies():
        owner = context.pack_root if getattr(context, 'external_pack', False) else context.base_root
        if not active_environment(owner / '.venv'):
            raise RuntimeError('interpreter_handoff_required')
        from api_agent.providers.distribution import selected_manifests
        manifests = selected_manifests(provider_config or {}, owner_root=getattr(context, 'owner_root', context.base_root),
                                       active_kinds=provider_kinds)
        options = {'selected_provider_manifests': manifests} if manifests else {}
        if ensure_environment(context, argv, groups=('reports',), **options) is not None:
            raise RuntimeError('interpreter_handoff_required')

    return RecordingPrerequisites(prepare_dependencies=dependencies,
        plan=lambda config: plan_compose(config, scope, engine_environment=engine_environment),
        manager=lambda plan, cancelled: (SharedReportingManager(plan,
            engine_environment=engine_environment, cancelled=cancelled, deadline_seconds=timeout)
            if plan.profiles == ('localstack',) else PrerequisiteManager(plan, scope, ownership_directory,
            engine_environment=engine_environment, cancelled=cancelled, deadline_seconds=timeout)),
        cancel_event=cancel_event, timeout=timeout,
        reporting_session=(lambda plan: reporting_session(plan, engine_environment=engine_environment,
            deadline_seconds=timeout)) if reporting_session is not None else None)
