"""Join integration accounting and durable recording through owned runner hooks.

The TUI supplies interactive acceptance; START=True/E2E or integration DEBUG
allows a launch-owned noninteractive start. This adapter records execution and
never supplies acceptance or starts services on its own.
"""
from dataclasses import dataclass, replace
from copy import deepcopy
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
from .suite_lock import suite_locked


def _step_identity(step):
    # scope_steps admits these identities; names alone never identify a visit.
    scope = next((item['value'] for item in step['parameters'] if item['name'] == 'scope_id'), None)
    return ('scope', scope) if scope is not None else ('call', step['name'])


def _step_envelopes(step, *, parent=None):
    identity = _step_identity(step)
    fixed = {'attempt', 'max_passes', 'declared_timeout', 'effective_timeout', 'timeout_key', 'outcome'}
    envelope = ({key: step[key] for key in ('name', 'start', 'stop')}, parent,
                {item['name']: item['value'] for item in step['parameters'] if item['name'] in fixed})
    result = {identity: envelope}
    # Untimed calls remain parameters in the rendered tree, but keep the same
    # compatibility identity and owner as timed call steps. Missing timing is
    # enrichment, never permission to change an observed outcome or visit.
    for item in step['parameters']:
        name = item['name']
        if name.startswith('call ') and name.endswith(' outcome'):
            call_name = name.removesuffix(' outcome')
            result[('call', call_name)] = ({'name': call_name}, identity, {'outcome': item['value']})
    for child in step['steps']:
        for key, value in _step_envelopes(child, parent=identity).items():
            result[key] = value
    return result


def _compatible_steps(previous, incoming):
    first, second = _step_envelopes(previous), _step_envelopes(incoming)
    for key in first.keys() & second.keys():
        first_base, first_parent, first_fixed = first[key]
        second_base, second_parent, second_fixed = second[key]
        if (any(first_base[name] != second_base[name] for name in first_base.keys() & second_base.keys())
                or first_parent != second_parent or any(
                first_fixed[name] != second_fixed[name] for name in first_fixed.keys() & second_fixed.keys())):
            raise ValueError('Conflicting workflow scope projection')


def _merge_step(previous, incoming, *, prefer_incoming):
    from .workflow_steps import attention_status
    previous['status'] = attention_status(previous['status'], incoming['status'])
    parameters = {item['name']: item for item in previous['parameters']}
    for item in incoming['parameters']:
        if prefer_incoming or item['name'] not in parameters:
            parameters[item['name']] = deepcopy(item)
    previous['parameters'] = list(parameters.values())
    children = {_step_identity(child): child for child in previous['steps']}
    for child in incoming['steps']:
        key = _step_identity(child)
        if key in children:
            _merge_step(children[key], child, prefer_incoming=prefer_incoming)
        else:
            # Merged output owns its subtree; source projections stay pristine.
            children[key] = deepcopy(child)
            previous['steps'].append(children[key])


class _WorkflowStepBridge:
    """Join journal and legacy views without dropping additional admitted scopes."""
    def __init__(self):
        self.entries = []
        self._projections = []

    @property
    def steps(self):
        return [entry['step'] for entry in self.entries]

    def add(self, packet):
        from .workflow_steps import scope_steps
        # Keep validated originals: later invocation claims may make an earlier
        # legacy attribution ambiguous. Never use an already merged projection
        # as the source when rebuilding that attribution.
        steps = scope_steps(packet.get('events', []))
        authoritative = packet.get('version') == 1 and 'projection' in packet
        invocation = packet['invocation_id'] if authoritative else None
        projections = self._projections + [dict(identity=_step_identity(step),
            invocation=invocation, authoritative=authoritative, step=step) for step in steps]
        entries = []
        # Merge each namespace independently before attaching unnamespaced data.
        for source in projections:
            previous = next((entry for entry in entries
                if entry['identity'] == source['identity'] and
                entry['invocation'] == source['invocation']), None)
            if previous is None:
                entries.append(deepcopy(source))
            else:
                _compatible_steps(previous['step'], source['step'])
                _merge_step(previous['step'], source['step'], prefer_incoming=False)
        for legacy in tuple(entries):
            if legacy['authoritative']:
                continue
            candidates = [entry for entry in entries
                if entry['identity'] == legacy['identity'] and entry['authoritative']]
            if len(candidates) == 1:
                journal = candidates[0]
                _compatible_steps(journal['step'], legacy['step'])
                _merge_step(journal['step'], legacy['step'], prefer_incoming=False)
                entries.remove(legacy)
        from .workflow_steps import bubble_attention
        for entry in entries:
            bubble_attention(entry['step'])
        # Failed conflict checks leave both provenance and rendered state intact.
        self._projections = projections
        self.entries = entries


@dataclass(frozen=True)
class RecordedIntegrationResult:
    execution: object
    identity: RunIdentity
    directory: Path | None
    usage: dict
    manifest: dict | None
    persisted: bool
    elapsed: float | None = None
    reporting_skipped: bool = False

    @property
    def exit_code(self):
        return self.execution.exit_code or (0 if self.persisted or self.reporting_skipped else 1)


def validate_integration_start(tests, config, *, accepted=False, no_std_in=False):
    """Validate accepted execution independently of reporting selection."""
    from api_agent.runtime.starting import start_requested
    if (accepted is not True or ((no_std_in or config.get('NO_STD_IN'))
            and not config.get('DEBUG') and not start_requested(config))
            or config.get('COLLECT_ONLY') or config.get('TUI_MODE') != 'integration'):
        raise PermissionError('Recorded integrations require explicit Start acceptance')
    tests = tuple(tests)
    if not tests:
        raise ValueError('Cannot run an empty integration selection')
    return tests


def validate_recorded_integration(tests, config, *, accepted=False, no_std_in=False):
    """Validate acceptance before listeners, authentication or artifacts exist."""
    if config.get('SKIP_ALLURE_REPORTING') is True:
        raise ValueError('Allure reporting is explicitly disabled for this invocation')
    tests = validate_integration_start(tests, config, accepted=accepted, no_std_in=no_std_in)
    if not config.get('ALLURE_S3_BUCKET'):
        raise ValueError('ALLURE_S3_BUCKET is required')
    return tests


@suite_locked
def run_recorded_integration(context, tests, config, *, accepted=False, no_std_in=False,
                             storage=None, quota_fetcher=None, prepare=None, finish=None,
                             usage_output=None, on_snapshot=None,
                             on_event=None, output=None, cancel_event=None,
                             runner=run_integration_tests, evidence_boundary=None, secret_values=(), cost_estimator=None,
                             unattended_index_source=None, interaction=None, content_policy=None, historical_estimator=None,
                             workflow_admissions=None, tool_plan=None):
    """Prepare once, record every outcome, and persist only after child cleanup.

    ``prepare(usage)`` may attach this suite tracker to owned provider callbacks.
    A quota-fetch failure is unavailable data; result-persistence failure fails
    the operation. Raw pytest evidence sinks must apply their own approved
    evidence/secret policy, as documented by the integration runner.
    Raw inference attachments require LOG_CONTENT=True; failure diagnostics and
    accounting remain independent. Register credentials before emitting evidence.
    """
    tests = validate_recorded_integration(tests, config, accepted=accepted, no_std_in=no_std_in)
    from .workflow_admission import capture_workflow_admissions
    workflow_admissions = capture_workflow_admissions(context, workflow_admissions, tool_plan=tool_plan)
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
            endpoint_url=config.get('ALLURE_S3_ENDPOINT'), region_name=config.get('ALLURE_S3_REGION'),
                                 localstack_port=config.get('ALLURE_LOCALSTACK_PORT', 4566),
                                 managed_local=config.get('ALLURE_S3_USE_LOCALSTACK', False))
        usage = SuiteUsage(identity.run_id, cost_estimator=cost_estimator)
        seen = set()
        recording_ids = {}
        content = {}
        ui_test_ids = {test.id for test in tests if 'requires_ui_session' in test.tags}
        workflow_steps = {}
        workflow_journals = {}
        persisted = False
        manifest = None
        usage_summary = None
        prepared = False

        def capture(method, phase):
            if quota_fetcher is not None:
                try:
                    method(quota_fetcher)
                except Exception:
                    # Never retain provider exception bodies or credentials.
                    from api_agent.telemetry.usage import QuotaReadError
                    usage.observe_snapshot(phase, QuotaReadError('request'))

        def before():
            nonlocal prepared
            if prepared:
                return
            if prepare is not None:
                prepare(usage)
            prepared = True
            capture(usage.capture_before, 'before')
            if usage_output is not None and (line := starting_usage_line(usage.report())):
                usage_output(line)

        def record(result):
            test = result.identity
            if test.repo != identity.repo:
                label = ('provider:' + os.path.relpath(test.root, context.base_root)
                         if test.root is not None else test.repo)
                test = replace(test, repo=identity.repo, node_id=f'{label}::{test.node_id}')
            attachments = tuple(content.pop(result.identity.id, ()))
            if config.get('LOG_CONTENT') is not True and result.identity.id in ui_test_ids:
                from .withheld_screenshot import withheld_attachment
                attachments += (withheld_attachment(),)
            filename = recorder.record_test(replace(result, identity=test),
                                            attachments=attachments,
                                            steps=(workflow_steps.pop(result.identity.id).steps
                                                   if result.identity.id in workflow_steps else None))
            recording_ids[result.identity.id] = filename
            seen.add(result.identity.id)
            from .allure_preview import emit_snapshot
            emit_snapshot(recorder, {result.identity.id: filename}, on_snapshot,
                          version=len(recording_ids))

        def after(result):
            nonlocal persisted, manifest, usage_summary
            if prepared:
                capture(usage.capture_after, 'after')
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
            artifact = build_accounting(identity, tests, config, usage, result.tests, estimator=estimator, summary=usage_summary, context=context)
            stabilized = estimator.complete(artifact)
            completed_averages = estimator.completed_averages(artifact, stabilized)
            # Preserve the initial raw allocation while publishing separately
            # derived completion estimates from this suite's frozen estimator.
            test_costs = usage_summary.setdefault('test_costs', {})
            for test in artifact['tests']:
                key = test['test_id']
                original = test_costs.get(key, {})
                derived = {**stabilized.get(key, {}), 'historical_average': completed_averages.get(key)}
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
            from api_agent.telemetry.usage_costs import attach_category_costs
            attach_category_costs(usage_summary, artifact)
            from api_agent.telemetry.accounting import validate_accounting
            artifact = validate_accounting(evidence_boundary.content_export(artifact))
            recorder.record_artifact('accounting.json', json.dumps(artifact, allow_nan=False).encode())
            if workflow_journals:
                from .workflow_accounting import join_workflow_accounting, apply_frozen_steps
                joined, facts = join_workflow_accounting(workflow_journals, artifact,
                                                         admissions=workflow_admissions)
                recorder.record_artifact('workflow-accounting-join.json',
                    json.dumps(evidence_boundary.content_export(joined), sort_keys=True, allow_nan=False).encode())
                updates = {}
                for test_id in workflow_journals:
                    filename = recording_ids[test_id]
                    row = json.loads((recorder.directory / filename).read_bytes())
                    updates[filename] = apply_frozen_steps(row.get('steps', []), test_id, facts)
                recorder.update_workflow_steps(updates)
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
            shared = [
                ('Suite usage', 'usage-summary.json', 'application/json'),
                ('Immutable accounting', 'accounting.json', 'application/json')]
            if workflow_journals:
                shared.append(('Workflow accounting join', 'workflow-accounting-join.json', 'application/json'))
            manifest = recorder.finalize(status=status, shared_artifacts=tuple(shared))
            from .allure_preview import emit_snapshot
            emit_snapshot(recorder, recording_ids, on_snapshot, manifest=manifest)
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
            if row.kind == 'browser_attachment':
                if config.get('LOG_CONTENT') is not True:
                    return
                if row.attachment is not None:
                    content.setdefault(row.identity.id, []).append(row.attachment)
            if row.kind == 'workflow_evidence':
                from api_agent.workflows.evidence import attachment
                frozen = attachment(row, evidence_boundary)
                content.setdefault(row.identity.id, []).append(frozen)
                packet = json.loads(frozen[1])
                row = replace(row, text=json.dumps(packet, ensure_ascii=False, allow_nan=False))
                workflow_steps.setdefault(row.identity.id, _WorkflowStepBridge()).add(packet)
                if packet.get('version') == 1 and 'projection' in packet:
                    workflow_journals.setdefault(row.identity.id, []).append(packet)
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

        execution = runner(context, tests, accepted=True, no_std_in=False,
                           config=dict(config, _ALLURE_REPORTING_ACTIVE=True),
                           parallel_jobs=config.get('TEST_PARALLEL_JOBS', 8),
                           before_run=before, on_result=record, after_run=after,
                           on_event=event, output=output, cancel_event=cancel_event,
                           unattended_index_source=unattended_index_source,
                           **({'quota_observer': observe_quota} if runner is run_integration_tests and quota_fetcher is None else {}),
                           **({'interaction': interaction} if interaction is not None else {}),
                           **({'tool_plan': tool_plan} if tool_plan is not None else {}))
        return RecordedIntegrationResult(execution, identity, directory if local_directory else None,
                                         usage_summary if usage_summary is not None else usage.report(), manifest, persisted,
                                         monotonic() - started_at)
