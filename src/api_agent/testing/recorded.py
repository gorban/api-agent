"""Join integration accounting and durable recording through owned runner hooks.

The TUI consumer is deferred until live backend prerequisites are ready. This
adapter never supplies interactive acceptance or starts services on its own.
"""
from dataclasses import dataclass
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile

from api_agent.telemetry.usage import SuiteUsage
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from .collection import TestIdentity, repository_label
from .contracts import TestResult
from .integration import run_integration_tests
from .recording import RunIdentity, RunRecorder
from .storage import RunStorage


@dataclass(frozen=True)
class RecordedIntegrationResult:
    execution: object
    identity: RunIdentity
    directory: Path | None
    usage: dict
    manifest: dict | None
    persisted: bool

    @property
    def exit_code(self):
        return self.execution.exit_code or (0 if self.persisted else 1)


def run_recorded_integration(context, tests, config, *, accepted=False, no_std_in=False,
                             storage=None, quota_fetcher=None, prepare=None,
                             on_event=None, output=None, cancel_event=None,
                             runner=run_integration_tests, evidence_boundary=None, secret_values=(), cost_estimator=None):
    """Prepare once, record every outcome, and persist only after child cleanup.

    ``prepare(usage)`` may attach this suite tracker to owned provider callbacks.
    A quota-fetch failure is unavailable data; result-persistence failure fails
    the operation. Raw pytest evidence sinks must apply their own approved
    evidence/secret policy, as documented by the integration runner.
    Allure evidence is separate from diagnostic LOG_CONTENT. Register dynamically
    obtained credentials on the supplied boundary before emitting test evidence.
    """
    if (accepted is not True or no_std_in or config.get('NO_STD_IN')
            or config.get('COLLECT_ONLY') or config.get('TUI_MODE') != 'integration'):
        raise PermissionError('Recorded integrations require interactive Start')
    tests = tuple(tests)
    if not tests:
        raise ValueError('Cannot record an empty integration selection')
    if not config.get('ALLURE_S3_BUCKET'):
        raise ValueError('ALLURE_S3_BUCKET is required')
    identity = RunIdentity.create(repository_label(context, context.owner_root), 'integration')
    with ExitStack() as resources:
        local_directory = config.get('ALLURE_RESULTS_DIR')
        root = local_directory or resources.enter_context(
            tempfile.TemporaryDirectory(prefix='api-agent-allure-'))
        directory = Path(root) / identity.repo / identity.run_id
        if evidence_boundary is None:
            evidence_boundary = EmissionBoundary(ContentPolicy(True))
        for value in secret_values:
            evidence_boundary.register_secret(value)
        recorder = RunRecorder(directory, identity, evidence_boundary=evidence_boundary)
        storage = storage if storage is not None else RunStorage(
            config['ALLURE_S3_BUCKET'], prefix=config.get('ALLURE_S3_PREFIX', 'allure'),
            secondary_directory=config.get('ALLURE_SECONDARY_DIR') or None,
            endpoint_url=config.get('ALLURE_S3_ENDPOINT'), region_name=config.get('ALLURE_S3_REGION'))
        usage = SuiteUsage(identity.run_id, cost_estimator=cost_estimator)
        seen = set()
        persisted = False
        manifest = None
        usage_summary = None

        def capture(method):
            if quota_fetcher is not None:
                try:
                    method(quota_fetcher)
                except Exception:
                    # Never retain provider exception bodies or credentials.
                    pass

        def before():
            capture(usage.capture_before)
            if prepare is not None:
                prepare(usage)

        def record(result):
            recorder.record_test(result)
            seen.add(result.identity.id)

        def after(result):
            nonlocal persisted, manifest, usage_summary
            capture(usage.capture_after)
            for outcome in result.tests:
                if outcome.identity.id not in seen:
                    record(outcome)
            with tempfile.TemporaryDirectory(prefix='api-agent-usage-') as temporary:
                csv_path = Path(temporary) / 'token-usage.csv'
                usage.write_csv(csv_path)
                recorder.record_artifact('token-usage.csv', csv_path.read_bytes())
                external_failed = False
                if config.get('TOKEN_USAGE_CSV_ENABLED') is True and config.get('TOKEN_USAGE_CSV'):
                    try:
                        usage.write_csv(csv_path, config['TOKEN_USAGE_CSV'])
                    except Exception:
                        external_failed = True
                        failure = TestIdentity(f'{identity.repo}::__suite__::usage_csv', identity.repo,
                                               '__suite__', '__suite__::usage_csv')
                        recorder.record_test(TestResult(failure, 'error'))
            usage_summary = usage.report()
            recorder.record_artifact('usage-summary.json', json.dumps(usage_summary, allow_nan=False).encode())
            status = 'interrupted' if result.cancelled else 'failed' if result.exit_code or external_failed else 'completed'
            manifest = recorder.finalize(status=status)
            storage.persist(directory, manifest)
            persisted = True
            if external_failed:
                raise RuntimeError('Optional usage CSV append failed')

        execution = runner(context, tests, accepted=True, no_std_in=False,
                           parallel_jobs=config.get('TEST_PARALLEL_JOBS', 8),
                           before_run=before, on_result=record, after_run=after,
                           on_event=on_event, output=output, cancel_event=cancel_event)
        return RecordedIntegrationResult(execution, identity, directory if local_directory else None,
                                         usage_summary if usage_summary is not None else usage.report(), manifest, persisted)
