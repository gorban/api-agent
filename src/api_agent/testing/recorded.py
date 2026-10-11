"""Join integration accounting and durable recording through owned runner hooks.

The TUI supplies interactive acceptance; START=True/E2E or integration DEBUG
allows a launch-owned noninteractive start. This adapter records execution and
never supplies acceptance or starts services on its own.
"""
from dataclasses import dataclass, replace
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from time import monotonic

from api_agent.telemetry.usage import SuiteUsage
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.usage_output import starting_usage_line, final_usage_text
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
    elapsed: float | None = None

    @property
    def exit_code(self):
        return self.execution.exit_code or (0 if self.persisted else 1)


def validate_recorded_integration(tests, config, *, accepted=False, no_std_in=False):
    """Validate acceptance before listeners, authentication or artifacts exist."""
    from api_agent.runtime.starting import start_requested
    if (accepted is not True or ((no_std_in or config.get('NO_STD_IN'))
            and not config.get('DEBUG') and not start_requested(config))
            or config.get('COLLECT_ONLY') or config.get('TUI_MODE') != 'integration'):
        raise PermissionError('Recorded integrations require explicit Start acceptance')
    tests = tuple(tests)
    if not tests:
        raise ValueError('Cannot record an empty integration selection')
    if not config.get('ALLURE_S3_BUCKET'):
        raise ValueError('ALLURE_S3_BUCKET is required')
    return tests


def run_recorded_integration(context, tests, config, *, accepted=False, no_std_in=False,
                             storage=None, quota_fetcher=None, prepare=None, finish=None,
                             usage_output=None,
                             on_event=None, output=None, cancel_event=None,
                             runner=run_integration_tests, evidence_boundary=None, secret_values=(), cost_estimator=None,
                             unattended_index_source=None, interaction=None, content_policy=None, historical_estimator=None):
    """Prepare once, record every outcome, and persist only after child cleanup.

    ``prepare(usage)`` may attach this suite tracker to owned provider callbacks.
    A quota-fetch failure is unavailable data; result-persistence failure fails
    the operation. Raw pytest evidence sinks must apply their own approved
    evidence/secret policy, as documented by the integration runner.
    Raw inference attachments require LOG_CONTENT=True; failure diagnostics and
    accounting remain independent. Register credentials before emitting evidence.
    """
    tests = validate_recorded_integration(tests, config, accepted=accepted, no_std_in=no_std_in)
    started_at = monotonic()
    from api_agent.providers.rates import freeze_pricing
    from .cost_history import estimator_for
    # Freeze public prices and historical calibration before any worker starts.
    # Optional accounting never authenticates or changes test outcomes.
    from .cost_context import cost_config
    config = cost_config(context, config)
    config['_COST_RATE_CARD'] = freeze_pricing(context, config)
    estimator = estimator_for(context, tests, config, rate_card=config['_COST_RATE_CARD'])
    if historical_estimator is not None:
        from copy import deepcopy
        estimator.advance_predictions = deepcopy(historical_estimator.predictions)
    if content_policy is not None:
        config = dict(config, LOG_CONTENT=content_policy.allow_content)
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
        recording_ids = {}
        content = {}
        persisted = False
        manifest = None
        usage_summary = None
        prepared = False

        def capture(method):
            if quota_fetcher is not None:
                try:
                    method(quota_fetcher)
                except Exception:
                    # Never retain provider exception bodies or credentials.
                    pass

        def before():
            nonlocal prepared
            if prepared:
                return
            if prepare is not None:
                prepare(usage)
            prepared = True
            capture(usage.capture_before)
            if usage_output is not None:
                usage_output(starting_usage_line(usage.report()))

        def record(result):
            test = result.identity
            if test.repo != identity.repo:
                label = ('provider:' + os.path.relpath(test.root, context.base_root)
                         if test.root is not None else test.repo)
                test = replace(test, repo=identity.repo, node_id=f'{label}::{test.node_id}')
            filename = recorder.record_test(replace(result, identity=test),
                                            attachments=content.pop(result.identity.id, ()))
            recording_ids[result.identity.id] = filename
            seen.add(result.identity.id)

        def after(result):
            nonlocal persisted, manifest, usage_summary
            if prepared:
                capture(usage.capture_after)
            cleanup_failed = False
            if finish is not None:
                try:
                    finish()
                except Exception:
                    cleanup_failed = True
            for outcome in result.tests:
                if outcome.identity.id not in seen:
                    record(outcome)
            if cleanup_failed:
                failure = TestIdentity(f'{identity.repo}::__suite__::cleanup', identity.repo,
                                       '__suite__', '__suite__::cleanup')
                recorder.record_test(TestResult(failure, 'error'))
            with tempfile.TemporaryDirectory(prefix='api-agent-usage-') as temporary:
                csv_path = Path(temporary) / 'token-usage.csv'
                usage.write_csv(csv_path, evidence_boundary=evidence_boundary)
                recorder.record_artifact('token-usage.csv', csv_path.read_bytes())
                external_failed = False
                if config.get('TOKEN_USAGE_CSV_ENABLED') is True and config.get('TOKEN_USAGE_CSV'):
                    try:
                        usage.write_csv(csv_path, config['TOKEN_USAGE_CSV'], evidence_boundary=evidence_boundary)
                    except Exception:
                        external_failed = True
                        failure = TestIdentity(f'{identity.repo}::__suite__::usage_csv', identity.repo,
                                               '__suite__', '__suite__::usage_csv')
                        recorder.record_test(TestResult(failure, 'error'))
            usage_summary = usage.report()
            from api_agent.telemetry.accounting import build_accounting
            artifact = build_accounting(identity, tests, config, usage, result.tests, estimator=estimator, summary=usage_summary)
            stabilized = estimator.complete(artifact)
            # Preserve the initial raw allocation while publishing separately
            # derived completion estimates from this suite's frozen estimator.
            test_costs = usage_summary.setdefault('test_costs', {})
            for test in artifact['tests']:
                key = test['test_id']
                original = test_costs.get(key, {})
                derived = stabilized.get(key, {})
                test_costs[key] = {**original, **derived,
                    'raw_quota_cost_usd': test['raw_quota_cost_usd'],
                    'list_cost_usd': test['list_cost_usd'],
                    'advance_prediction': test['prediction']}
                prompts = original.get('prompt_count', 0)
                amount = derived.get('estimated_cost_usd')
                test_costs[key]['cost_per_prompt_usd'] = None if amount is None or not prompts else amount / prompts
                test['stabilized_estimate'] = derived
            import math
            amounts = [row.get('estimated_cost_usd') for row in test_costs.values()]
            usage_summary['raw_estimated_cost_usd'] = usage_summary.get('estimated_cost_usd')
            usage_summary['estimated_cost_usd'] = None if any(value is None for value in amounts) else math.fsum(amounts)
            bases = {row.get('basis') for row in test_costs.values() if not row.get('known_zero')}
            usage_summary['cost_basis'] = next(iter(bases)) if len(bases) == 1 else 'mixed' if bases else 'no_llm_calls'
            total, prompts = usage_summary['estimated_cost_usd'], usage_summary['prompt_count']
            usage_summary['cost_per_prompt_usd'] = None if total is None or not prompts else total / prompts
            from api_agent.telemetry.accounting import validate_accounting
            artifact = validate_accounting(evidence_boundary.content_export(artifact))
            recorder.record_artifact('accounting.json', json.dumps(artifact, allow_nan=False).encode())
            recorder.attach_test_artifacts({recording_ids[key]: value for key, value in usage_summary.get('test_costs', {}).items()
                                            if key in recording_ids})
            usage_summary = evidence_boundary.content_export(usage_summary)
            recorder.record_artifact('usage-summary.json', json.dumps(usage_summary, allow_nan=False).encode())
            output_failed = False
            if usage_output is not None:
                try:
                    usage_output(final_usage_text(usage_summary))
                except Exception:
                    output_failed = True
                    failure = TestIdentity(f'{identity.repo}::__suite__::usage_output', identity.repo,
                                           '__suite__', '__suite__::usage_output')
                    recorder.record_test(TestResult(failure, 'error'))
            status = 'interrupted' if result.cancelled else 'failed' if result.exit_code or external_failed or cleanup_failed or output_failed else 'completed'
            manifest = recorder.finalize(status=status, shared_artifacts=(
                ('Suite usage', 'usage-summary.json', 'application/json'),
                ('Immutable accounting', 'accounting.json', 'application/json')))
            estimator.ingest(directory, manifest)
            storage.persist(directory, manifest)
            persisted = True
            if external_failed or cleanup_failed or output_failed:
                raise RuntimeError('Recording finalization failed')

        def observe_quota(phase, snapshot):
            usage.observe_snapshot(phase, snapshot)
            if phase == 'before' and usage_output is not None:
                usage_output(starting_usage_line(usage.report()))

        def event(row):
            if row.kind == 'quota_snapshot':
                from .quota_events import capture
                capture(row, observe_quota)
            if row.kind == 'inference_usage':
                from .inference_usage import capture
                data = evidence_boundary.content_export(json.loads(row.text))
                row = replace(row, text=json.dumps(data, allow_nan=False))
                capture(row, usage)
            if row.kind == 'inference_content':
                if config.get('LOG_CONTENT') is not True:
                    return
                from .inference_content import content_attachment
                attachment = content_attachment(row, evidence_boundary)
                content.setdefault(row.identity.id, []).append(attachment)
                data = json.loads(row.text)
                data['content'] = json.loads(attachment[1])
                row = replace(row, text=json.dumps(data, ensure_ascii=False))
            if on_event is not None:
                on_event(row)

        execution = runner(context, tests, accepted=True, no_std_in=False, config=config,
                           parallel_jobs=0 if config.get('DEBUG') else config.get('TEST_PARALLEL_JOBS', 8),
                           before_run=before, on_result=record, after_run=after,
                           on_event=event, output=output, cancel_event=cancel_event,
                           unattended_index_source=unattended_index_source,
                           **({'quota_observer': observe_quota} if runner is run_integration_tests and quota_fetcher is None else {}),
                           **({'interaction': interaction} if interaction is not None else {}))
        return RecordedIntegrationResult(execution, identity, directory if local_directory else None,
                                         usage_summary if usage_summary is not None else usage.report(), manifest, persisted,
                                         monotonic() - started_at)
