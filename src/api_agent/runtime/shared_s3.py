"""The one launcher-level S3 prerequisite (LocalStack) every S3 consumer leases.

Allure result history, the Run local application's data bucket and pack
tests that declare the built-in ``api_agent:shared_s3`` provider all lease the same
``SharedS3Manager``. It keeps one disposable LocalStack per container engine and
endpoint behind the original lease journal: the last consumer to release removes
the container (never the ``localstack-data`` volume) and a later acquire provisions
a fresh container on that volume. ``S3_USE_LOCALSTACK=False`` starts nothing and
uses ``S3_ENDPOINT`` as given.
"""
from contextlib import contextmanager
import os
import time

from .compose import ENGINE_ENVIRONMENT_KEYS, s3_buckets
from .ownership import StackScope

SETTINGS = ('S3_USE_LOCALSTACK', 'LOCALSTACK_PORT', 'S3_ENDPOINT', 'S3_REGION',
            'S3_EXTRA_BUCKETS', 'ALLURE_S3_BUCKET')
# What a consumer gets without launcher configuration, such as a direct library call.
DEFAULTS = {'S3_USE_LOCALSTACK': True, 'LOCALSTACK_PORT': 4566, 'S3_REGION': 'us-east-1',
            'S3_EXTRA_BUCKETS': 'application-data'}
APPLICATION_BUCKET_VARIABLE = 'S3_APPLICATION_BUCKET'


def declares_application_s3(config):
    """True for a pack that opts into the shared S3 by declaring the application bucket setting."""
    return APPLICATION_BUCKET_VARIABLE in getattr(config, 'active_fields', ())


def s3_config(values):
    """The resolved shared S3 settings, ignoring every other launcher value."""
    return {key: values[key] for key in SETTINGS if key in values}


def shared_s3_plan(values, scope, *, engine_environment=None):
    """Plan only the shared LocalStack; the plan has no profiles when S3 is external."""
    from .compose import plan_compose
    return plan_compose(s3_config(values), scope, engine_environment=engine_environment, storage=True)


def s3_environment(values):
    """Connection variables for a consumer; absent endpoint means the AWS default."""
    values = {**DEFAULTS, **s3_config(values)}
    endpoint = values.get('S3_ENDPOINT')
    if endpoint is None and values['S3_USE_LOCALSTACK']:
        endpoint = f"http://localhost:{values['LOCALSTACK_PORT']}"
    result = {'S3_REGION': values['S3_REGION']}
    if endpoint is not None:
        result['S3_ENDPOINT'] = endpoint
    return result


def application_s3_environment(values):
    """The S3 settings the Run local application reads from its process environment.

    They follow from configuration alone (LocalStack's endpoint is fixed by
    ``LOCALSTACK_PORT``), so they can be exported before the application is
    constructed. A configured ``S3_APPLICATION_BUCKET`` is kept; otherwise
    LocalStack's first extra bucket is the application's.
    """
    values = {**DEFAULTS, **s3_config(values)}
    exported = s3_environment(values)
    if values['S3_USE_LOCALSTACK'] and not os.environ.get(APPLICATION_BUCKET_VARIABLE):
        application = s3_buckets({'S3_EXTRA_BUCKETS': values['S3_EXTRA_BUCKETS']})
        if application:
            exported[APPLICATION_BUCKET_VARIABLE] = application[0]
    return exported


def export_environment(exported):
    """Set ``exported`` in this process; return the callable that restores the previous values."""
    previous = {key: os.environ.get(key) for key in exported}
    os.environ.update(exported)

    def restore():
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return restore


def engine_environment_from_process():
    return {key: os.environ[key] for key in ENGINE_ENVIRONMENT_KEYS if key in os.environ}


def shared_s3_manager(plan, *, engine_environment=None, cancelled=lambda: False, deadline_seconds=120):
    from .reporting_registry import SharedS3Manager
    return SharedS3Manager(plan, engine_environment=engine_environment, cancelled=cancelled,
                           deadline_seconds=deadline_seconds)


@contextmanager
def shared_s3(services):
    """Built-in test-service provider: lease the shared S3 for the whole run.

    Referenced from a pack as ``localstack = "api_agent:shared_s3"`` under
    ``[tests.prerequisites]``. It reads the resolved S3 settings from
    ``services['s3']`` (defaults when absent), starts nothing for external S3, and
    yields ``S3_ENDPOINT`` and ``S3_REGION`` for the test environment. The buckets
    in ``S3_EXTRA_BUCKETS`` are ensured but not reported; tests name their own.
    """
    values = {**DEFAULTS, **(services.get('s3') or {})}
    owner = services['owner_root']
    engine = engine_environment_from_process()
    plan = shared_s3_plan(values, StackScope.from_roots(owner, owner), engine_environment=engine)
    manager = None
    if plan.enabled:
        manager = shared_s3_manager(plan, engine_environment=engine,
                                    cancelled=services['cancelled'],
                                    deadline_seconds=max(.001, services['deadline'] - time.monotonic()))
        try:
            manager.acquire()
        except BaseException as error:
            try:
                manager.release()
            except BaseException as cleanup:
                # The acquire failure is the root cause; keep the release failure beside it.
                error.add_note(f'Shared S3 release also failed: {cleanup!r}')
            raise
    try:
        yield s3_environment(values)
    finally:
        if manager is not None:
            manager.release()


class SharedS3Companion:
    """Lease the shared S3 next to another prerequisite manager (Run local).

    Acquires S3 first and exports the running endpoint, region and the
    application's data bucket to this process environment, because the
    application reads them from there; release restores the previous environment
    and relinquishes both leases, whichever fails. Everything else is delegated
    to the primary manager so status, logs and endpoints stay unchanged.
    """

    def __init__(self, primary, values, s3=None):
        self.primary, self.s3, self.values = primary, s3, values
        self.restore_environment = None

    def __getattr__(self, name):
        return getattr(self.primary, name)

    @property
    def lease(self):
        return self.primary.lease

    def _export(self):
        self.restore_environment = export_environment(application_s3_environment(self.values))

    def _restore(self):
        restore, self.restore_environment = self.restore_environment, None
        if restore is not None:
            restore()

    def acquire(self, **options):
        if self.s3 is not None:
            self.s3.acquire()
        try:
            self._export()
            return self.primary.acquire(**options)
        except BaseException as error:
            try:
                self._restore()
                if self.s3 is not None:
                    self.s3.release()
            except BaseException as cleanup:
                error.cleanup_pending = getattr(cleanup, 'cleanup_pending', None) or getattr(error, 'cleanup_pending', None)
            raise

    def release(self, *, retain=False, **options):
        failures, result = [], ()
        try:
            result = self.primary.release(retain=retain, **options)
        except BaseException as error:
            failures.append(error)
        try:
            self._restore()
            if self.s3 is not None:
                self.s3.release()
        except BaseException as error:
            failures.append(error)
        if failures:
            raise failures[0]
        return result


def with_shared_s3(primary, values, scope, *, engine_environment=None, cancelled=lambda: False,
                   deadline_seconds=120):
    """Wrap a run-local manager so it always leases the shared S3 and exports it."""
    values = {**DEFAULTS, **s3_config(values)}
    plan = shared_s3_plan(values, scope, engine_environment=engine_environment)
    s3 = (shared_s3_manager(plan, engine_environment=engine_environment, cancelled=cancelled,
                            deadline_seconds=deadline_seconds) if plan.enabled else None)
    return SharedS3Companion(primary, values, s3)
