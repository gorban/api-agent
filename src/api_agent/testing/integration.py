"""Accepted, integration-only pytest execution.

``output`` and ``RunnerEvent(kind='output')`` are raw pytest evidence and may
contain test data, including secrets. Callers must route them to an explicitly
approved evidence sink. Emitted runner diagnostics are fixed, sanitized text.
TestResult.local_diagnostic is transient captured evidence containing exception
chains and command or worker output, for TUI or an explicitly credential-scrubbed
CI terminal channel. Never put it in artifacts/events; reporting callbacks must
select the explicit approved report fields.
The local_stderr and local_stdout fields follow the same local-only boundary.
Run callbacks run synchronously on the calling thread; retained inspection
completion events run on their monitor thread. ``after_run`` receives the
final result after pytest completes; debug inspection handles retain owned workers.
"""
from api_agent.runtime.processes import observe_exit
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import wraps
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

from .collection import repository_label, CollectionError, collect_tests
from .contracts import RunnerEvent, TestResult
from .diagnostics import format_exception
from api_agent.runtime.processes import finalize
from .suite_lock import suite_locked
from api_agent.providers.setup_transport import _diagnostic

_RUNNER_OPERATION = threading.local()


def _prepare_suite_prerequisites(context, config, output, cancel_event=None):
    if not config.get('E2E_PREREQUISITE_PROVIDER'):
        return
    from .suite_prerequisite_provider import SuitePrerequisiteProvider
    owner = getattr(_RUNNER_OPERATION, 'prerequisites', None)
    recovery = _RUNNER_OPERATION.recovery
    if owner is None:
        owner = SuitePrerequisiteProvider(context, config, recovery.directory.parent, recovery, output=output)
        _RUNNER_OPERATION.prerequisites = owner
    with recovery.bind():
        owner.start(cancelled=cancel_event)


def _close_suite_prerequisites(inspections=()):
    if any(callable(getattr(item, '_when_closed', None)) and not item.closed for item in inspections):
        return
    owner = getattr(_RUNNER_OPERATION, 'prerequisites', None)
    if owner is not None:
        from ..runtime.processes import disposal_complete
        recovery = _RUNNER_OPERATION.recovery
        disposed = bool(recovery.workers) and all(disposal_complete(worker) for worker in recovery.workers)
        with recovery.bind():
            owner.close(workers_disposed=disposed)


def _admit_operation(function):
    """Adapters may nest, but a callback cannot launch a second runner operation."""
    @wraps(function)
    def guarded(*args, **kwargs):
        from .suite_lock import SuiteBusyError
        if getattr(_RUNNER_OPERATION, 'active', False):
            raise SuiteBusyError('An integration operation is already active in this owner.')
        _RUNNER_OPERATION.active = True
        try:
            # Confirmation waves and repository workers belong to one operation.
            # Their individual snapshot directories have shorter lifetimes.
            from .native_recovery import NativeRecoveryOwner
            temporary = tempfile.TemporaryDirectory(prefix='api-agent-suite-')
            recovery = NativeRecoveryOwner(temporary.name)
            recovery.prepare()
            _RUNNER_OPERATION.recovery = recovery
            _RUNNER_OPERATION.prerequisites = None
            result = None
            recovered = False
            try:
                result = function(*args, **dict(kwargs, suite_directory=temporary.name))
                return result
            finally:
                primary = sys.exception()
                inspections = getattr(result if result is not None else primary, 'inspections', ())
                retained = tuple(item for item in inspections
                    if callable(getattr(item, '_when_closed', None)) and not item.closed)
                contribution = getattr(_RUNNER_OPERATION, 'prerequisites', None)
                def close():
                    nonlocal recovered
                    from api_agent.runtime.processes import disposal_complete
                    with recovery._lock:
                        if recovery.closed:
                            recovery._acknowledge_closed()
                            return
                        if not recovered:
                            if any(recovery.directory.iterdir()) or contribution is not None:
                                for worker in recovery.workers:
                                    if not disposal_complete(worker):
                                        finalize(worker)
                            if contribution is not None:
                                disposed = bool(recovery.workers) and all(disposal_complete(worker) for worker in recovery.workers)
                                with recovery.bind():
                                    contribution.close(workers_disposed=disposed)
                            recovery.recover()
                            recovered = True
                        temporary.cleanup()
                        recovery.closed = True
                        recovery._acknowledge_closed()
                try:
                    if retained:
                        from .suite_lock import retain_suite_cleanup
                        retain_suite_cleanup(recovery)
                        def after_inspection():
                            if all(item.closed for item in retained):
                                close()
                        for item in retained:
                            item._when_closed(after_inspection)
                    else:
                        close()
                except BaseException as error:
                    from api_agent.providers.control import CleanupPendingError
                    from api_agent.runtime.tasks import offload
                    async def retry():
                        await offload(close)
                    pending = CleanupPendingError((recovery, temporary), retry)
                    temporary._finalizer.detach()
                    if primary is None:
                        raise pending from error
                    pending.cleanup_pending = getattr(primary, 'cleanup_pending', None)
                    primary.cleanup_pending = pending
        finally:
            _RUNNER_OPERATION.recovery = None
            _RUNNER_OPERATION.prerequisites = None
            _RUNNER_OPERATION.active = False
    return guarded


@dataclass(frozen=True)
class IntegrationRunResult:
    exit_code: int
    tests: tuple[TestResult, ...]
    output: str
    cancelled: bool = False
    inspections: tuple = ()
    attempts: tuple[TestResult, ...] = ()
    e2e_reruns: int = 0
    infrastructure_failed: bool = False
    prerequisite_events: tuple = ()


_PLUGIN = r'''
import os
def run(stdout_boundary):
    import json, signal, sys, threading
    import pytest
    from api_agent.providers.diagnostic_output import bounded_stderr, bounded_stdout
    from api_agent.testing.structured_diagnostics import profile_records
    if os.name == 'posix' and 'API_AGENT_LAUNCHER_LIVENESS_FD' in os.environ:
        fd = int(os.environ['API_AGENT_LAUNCHER_LIVENESS_FD'])
        def parent_liveness():
            try:
                while os.read(fd, 1):
                    pass
            finally:
                # This still-running worker owns exactly its newly created group.
                if os.getpgrp() == os.getpid():
                    os.killpg(os.getpid(), signal.SIGKILL)
                os._exit(130)
        threading.Thread(target=parent_liveness, daemon=True).start()
    class Events:
        def pytest_configure(self, config):
            config.addinivalue_line("markers", "feature(name): explicit functional feature for reporting")

        def send(self, **row):
            with open(event_path, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(row) + '\n')
        def pytest_runtest_logstart(self, nodeid, location):
            self.send(kind='test_started', node=nodeid)
        def pytest_runtest_logreport(self, report):
            self.send(kind='report', node=report.nodeid, phase=report.when,
                      status=report.outcome, duration=report.duration,
                      stderr=bounded_stderr(''.join(text for title, text in report.sections
                                     if title == 'Captured stderr ' + report.when)),
                      stdout=(bounded_stdout(''.join(text for title, text in report.sections
                                     if title == 'Captured stdout ' + report.when))
                              if os.environ.get('API_AGENT_SUCCESSFUL_TEST_DIAGNOSTICS_LEVEL') == 'info' else ''),
                      profile_evidence=(profile_records(report.user_properties)
                          if report.when == 'setup' and os.environ.get('API_AGENT_SUCCESSFUL_TEST_DIAGNOSTICS_LEVEL') == 'info' else []),
                      traceback=(str(report.longrepr[2]).removeprefix('Skipped: ')
                                 if report.skipped and isinstance(report.longrepr, tuple)
                                 else str(report.longrepr) if report.failed or report.skipped else ''))
    event_path = sys.argv[1]
    events = Events()
    code = pytest.main(sys.argv[2:], plugins=[events])
    sys.stdout.flush()
    if sys.__stdout__ is not sys.stdout:
        sys.__stdout__.flush()
    os.write(1, stdout_boundary.encode('utf-8'))
    events.send(kind='run_complete', exit_code=int(code))
    from api_agent.testing.provider_setup import serve_inspections
    serve_inspections(events.send)
    from api_agent.testing.ui_inspection import finish_parallel_inspections
    finish_parallel_inspections(events.send)
    sys.exit(code)
run(__API_AGENT_STDOUT_BOUNDARY__)
'''


def _complete_report(rows):
    phases = {row['phase']: row for row in rows}
    return ('setup' in phases and 'teardown' in phases
            and ('call' in phases or phases['setup']['status'] in {'failed', 'skipped'}))


@suite_locked
def _run_integration_tests(context, tests, *, accepted=False, no_std_in=False,
                          on_event=None, output=None, cancel_event=None,
                          parallel_jobs=0, before_run=None, on_result=None,
                          after_run=None, config=None, config_fields=None, resolved_test_profiles=None, unattended_index_source=None, interaction=None, quota_observer=None, tool_plan=None, evidence_boundary=None, explicit_debug_test_ids=(), suite_directory=None):
    """Run freshly collected integration identities once, with no retry/coverage gate.

    ``accepted`` must represent the picker's explicit Continue action. This
    function prepares recording through ``before_run``. Shared provider backends
    independently acquire configured managed trace collectors before readiness.
    No callback receives a raw exception. ``on_result`` receives a transient
    ``local_diagnostic`` field for TUI or explicitly scrubbed terminal display;
    reporting callbacks must use explicit report fields and exclude this field
    from artifacts/events. Pytest
    stdout and traceback remain explicit test evidence in the result/event stream.
    """
    mode = 'e2e' if (config or {}).get('TUI_MODE') == 'e2e' else 'integration'
    diagnostics_level = (config or {}).get('SUCCESSFUL_TEST_DIAGNOSTICS_LEVEL', 'warning')
    if diagnostics_level not in ('warning', 'info'):
        raise ValueError('SUCCESSFUL_TEST_DIAGNOSTICS_LEVEL must be warning or info.')
    if mode == 'e2e':
        from .recorded import validate_integration_start
        tests = validate_integration_start(tests, config, accepted=accepted, no_std_in=no_std_in)
        config = dict(config, DEBUG=config.get('DEBUG') is True or config.get('E2E_DEBUG') is True)
    if no_std_in or accepted is not True:
        raise PermissionError('Interactive Continue is required for integration execution')
    if not isinstance(parallel_jobs, int) or isinstance(parallel_jobs, bool) or parallel_jobs < 0:
        raise ValueError('parallel_jobs must be a nonnegative integer')
    tests = tuple(dict((test.id, test) for test in tests).values())
    if not tests:
        raise CollectionError('Cannot execute an empty integration selection')

    chunks, results = [], {}
    inspections = []
    backend = None
    from .trace_cleanup import optional_trace_cleanup
    tool_plan_ready = False
    interaction_server = None
    interruption = None
    publication_error = None
    callback_failed = cancelled = failed = infrastructure_failed = False
    disabled = set()
    diagnostics = {}
    local_diagnostics = {}
    test_stderr = {}
    test_stdout = {}
    test_profile_evidence = {}
    reader_diagnostics = {}
    stage = 'runner initialization'
    from .diagnostics import CredentialScrubber
    # Cost compatibility contains hashes under request-setting names, not raw
    # credentials. Never enroll those hashes into the shared live authority.
    stderr_config = {name: value for name, value in (config or {}).items()
                     if not name.startswith('_COST_')}
    stderr_scrub = CredentialScrubber(stderr_config, boundary=evidence_boundary)

    def local_exception(boundary, error):
        return format_exception(error, boundary)

    def record_local(boundary, error, identities=tests):
        local = local_exception(boundary, error)
        for identity in identities:
            local_diagnostics.setdefault(identity.id, []).append(local)

    def enrich_local(result):
        from api_agent.providers.diagnostic_output import bounded_stderr, bounded_stdout
        local = '\n\n'.join([*local_diagnostics.get(result.identity.id, ()),
                               *reader_diagnostics.get(result.identity.id, ())])
        if local and result.traceback:
            local += '\n\nTest report diagnostic:\n' + result.traceback
        shared_stderr = (backend.stderr_for(result.identity.id)
                         if backend is not None and hasattr(backend, 'stderr_for') else '')
        stderr = test_stderr.get(result.identity.id, result.local_stderr)
        if shared_stderr:
            stderr += ('\n' if stderr else '') + 'Shared setup stderr:\n' + shared_stderr
        stdout = test_stdout.get(result.identity.id, result.local_stdout)
        stdout = stderr_scrub(stdout) if stdout else ''
        from .structured_diagnostics import append_profile_records
        from api_agent.providers.diagnostic_output import diagnostic_output
        emitted = []
        with diagnostic_output(emitted.append):
            stdout = append_profile_records(stdout, test_profile_evidence.get(result.identity.id, ()), stderr_scrub)
        if emitted:
            stderr += ('\n' if stderr and not stderr.endswith('\n') else '') + ''.join(emitted)
        return replace(result, local_diagnostic=local or result.local_diagnostic,
                       local_stderr=bounded_stderr(stderr_scrub(stderr)) if stderr else '',
                       local_stdout=bounded_stdout(stdout) if stdout else '')

    def final_tests():
        return tuple(enrich_local(results[test.id]) for test in tests)

    def diagnose(boundary, error, identities=tests):
        text = _diagnostic(error, boundary)
        local = local_exception(boundary, error)
        for identity in identities:
            diagnostics.setdefault(identity.id, []).append(text)
            local_diagnostics.setdefault(identity.id, []).append(local)
        return text

    def call(name, callback, *args):
        nonlocal callback_failed, infrastructure_failed
        if callback is None or name in disabled:
            return
        try:
            callback(*args)
        except Exception as error:
            from .suite_lock import _cleanup_receipts
            if name == 'after_run' and any(_cleanup_receipts(error)):
                raise
            callback_failed = infrastructure_failed = True
            disabled.add(name)
            chunks.append(f'{name} callback failed; details withheld.\n')
            if name == 'before_run':
                chunks.append(diagnose('Integration prerequisites (before_run)', error) + '\n')
            else:
                record_local(f'Integration {name} callback', error)

    def emit(event):
        call('on_event', on_event, event)

    def raw(repo, chunk):
        chunks.append(chunk)
        call('output', output, chunk)
        emit(RunnerEvent('output', repo, text=chunk))

    def abort_inspections():
        nonlocal failed, interruption, infrastructure_failed
        for inspection in inspections:
            try:
                inspection.abort()
            except BaseException as error:
                if not isinstance(error, Exception) and interruption is None:
                    interruption = error
                record_local('Integration inspection cleanup', error)
                chunks.append('Inspection cleanup pending; retry required.\n')
                failed = infrastructure_failed = True

    def check_cancellation():
        nonlocal cancelled
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
        if cancelled:
            abort_inspections()

    def finish(repo, identity, rows, fallback='error'):
        if identity.id in results:
            return
        phases = {row['phase']: row for row in rows}
        if any(row['status'] == 'failed' for row in rows):
            status = 'failed' if phases.get('call', {}).get('status') == 'failed' else 'error'
        elif any(row['status'] == 'skipped' for row in rows):
            status = 'skipped'
        elif phases.get('call', {}).get('status') == 'passed' and phases.get('setup', {}).get('status') == 'passed' and phases.get('teardown', {}).get('status') == 'passed':
            status = 'passed'
        else:
            status = fallback
        traceback = '\n'.join(row['traceback'] for row in rows if row['traceback'])
        if not traceback and status == 'error':
            traceback = '\n\n'.join(diagnostics.get(identity.id, ())) or (
                'Integration pytest report incomplete: no complete setup/call/teardown report was received. '
                'No failure diagnostic was captured for this test.')
        result = TestResult(identity, status, sum(float(row['duration']) for row in rows), traceback,
                            '\n\n'.join(local_diagnostics.get(identity.id, ())))
        from api_agent.providers.diagnostic_output import bounded_stderr
        stderr = bounded_stderr(''.join(row.get('stderr', '')
                         if type(row.get('stderr', '')) is str else 'stderr unavailable: invalid capture.\n'
                         for row in rows))
        test_stderr[identity.id] = stderr
        test_stdout[identity.id] = (''.join(row.get('stdout', '') if type(row.get('stdout', '')) is str
                           else 'stdout unavailable: invalid capture.\n' for row in rows)
                           if status == 'passed' and diagnostics_level == 'info' else '')
        from .structured_diagnostics import report_records
        test_profile_evidence[identity.id] = (report_records(rows)
            if status == 'passed' and diagnostics_level == 'info' else ())
        result = enrich_local(result)
        results[identity.id] = result
        emit(RunnerEvent('test_finished', repo, identity, status, result.duration, result.traceback))
        call('on_result', on_result, result)

    emit(RunnerEvent('started'))
    try:
        stage = 'Integration collection'
        try:
            available = {test.id: test for test in collect_tests(context, mode, cancel_event=cancel_event, **({'config': config} if config is not None else {}))}
        except Exception as error:
            raw('', diagnose(stage, error) + '\n')
            failed = infrastructure_failed = True
            available = None
        if available is not None:
            if mode == 'e2e' and config.get('DEBUG'):
                from .debug_confirmation import validate_e2e_debug_selection
                validate_e2e_debug_selection(tests, available.values(), explicit_debug_test_ids)
            if any(available.get(test.id) != test for test in tests):
                raise CollectionError('Integration selection contains identities outside the freshly collected integration suites')
            from .requirements import plan_integration_requirements
            stage = 'Integration requirements validation'
            selected_requirements = plan_integration_requirements(tests)
            selected_requirements.validate(context, config or {})
            from .test_profiles import resolve_test_profiles
            test_profiles = (resolve_test_profiles(tests, config, owner_root=context.owner_root, fields=config_fields, context=context)
                             if resolved_test_profiles is None and config is not None else
                             deepcopy(resolved_test_profiles or {}))
            if config is not None:
                if set(test_profiles) != {test.id for test in tests} or any(
                        test_profiles[test.id].get('node_id') != test.node_id or
                        test_profiles[test.id].get('repo') != test.repo for test in tests):
                    raise CollectionError('Test profiles do not match the freshly admitted integration identities')
                from .cost_context import cost_config
                for row in (() if resolved_test_profiles is not None else test_profiles.values()):
                    values = {key: value for key, value in row['config'].items() if not key.startswith('_COST_')}
                    row['config'] = cost_config(context, values)
            stage = 'Integration runner configuration'
            base, owner = Path(context.base_root).resolve(), Path(context.owner_root).resolve()
            from .pack_sources import composition_roots, composition_pythonpath
            ordered = bool(getattr(context, 'pack_layers', ()))
            owner_roots = composition_roots(context) if ordered else (base, owner)
            roots = {repository_label(context, root): root for root in dict.fromkeys(owner_roots)}
            env = os.environ.copy()
            env['API_AGENT_TEST_ATTEMPT'] = str((config or {}).get('_E2E_ATTEMPT', 1))
            if config is not None and config.get('SKIP_ALLURE_REPORTING') is True:
                env['API_AGENT_ALLURE_REPORTING'] = '0'
            elif config is not None and config.get('_ALLURE_REPORTING_ACTIVE') is True:
                env['API_AGENT_ALLURE_REPORTING'] = '1'
            else:
                env.pop('API_AGENT_ALLURE_REPORTING', None)
            env['PYTHONPATH'] = (composition_pythonpath(context, env.get('PYTHONPATH', ''))
                                 if ordered else os.pathsep.join([str(base / 'src'), str(owner / 'src'), env.get('PYTHONPATH', '')]))
            env['PYTHONUNBUFFERED'] = '1'
            backend_descriptor = None
            interaction_descriptor = None
            if config is not None and interaction is not None:
                from api_agent.auth.interaction_transport import InteractionServer
                interaction_server = InteractionServer(interaction)
                interaction_descriptor = interaction_server.descriptor
            from api_agent.auth.managers import registry_for_context
            manager_registry = registry_for_context(context) if config is not None else None
            for repo in dict.fromkeys(test.repo for test in tests):
                selected = {test.node_id: test for test in tests if test.repo == repo}
                root = next(iter(selected.values())).root or roots.get(repo)
                if root is None:
                    for identity in selected.values():
                        diagnostics[identity.id] = ['Integration owner resolution failed: no suite root is available.']
                    failed = infrastructure_failed = True
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                # One callback per owner suite, before any child is started.
                was_failed = callback_failed
                call('before_run', before_run)
                if callback_failed and not was_failed:
                    failed = infrastructure_failed = True
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if tool_plan is not None and not tool_plan_ready:
                    stage = 'Integration selected tool dependency admission'
                    from api_agent.tooling.services import prepare_tool_plan
                    tool_plan = prepare_tool_plan(tool_plan)
                    tool_plan_ready = True
                if config is not None and backend is None and plan_integration_requirements(tests).requires_llm:
                    stage = 'Integration shared setup backend startup'
                    from .setup_pool import SetupPool
                    from .prerequisites import local_trace_prerequisites
                    backend = SetupPool(test_profiles, context.owner_root,
                        unattended_index_source=unattended_index_source,
                        password_manager_registry=manager_registry, interaction=interaction, quota_observer=quota_observer,
                        authority=evidence_boundary,
                        trace_prerequisites_factory=lambda values: local_trace_prerequisites(
                            context, values, cancel_event=cancel_event, tool_plan=tool_plan),
                        **({'config_fields': config_fields} if config_fields is not None else {}))
                    try:
                        backend_descriptor = backend.start()
                    finally:
                        if config.get('DEBUG'):
                            inspections.extend(backend.owners)
                stage = 'Integration pytest launch preparation'
                from .inspection import InspectionDirectory, IntegrationInspection
                directory = InspectionDirectory(tempfile.TemporaryDirectory(prefix='api-agent-integration-'))
                from .inference_content import EvidenceFailureChannel
                with directory as temporary, EvidenceFailureChannel(selected) as failure_channel:
                    env['API_AGENT_EVIDENCE_FAILURE_CHANNEL'] = failure_channel.descriptor
                    if config is not None:
                        from .provider_setup import write_launch_snapshot
                        env['API_AGENT_INTEGRATION_CONTEXT'] = str(write_launch_snapshot(Path(temporary), context, config,
                            suite_directory=suite_directory,
                            unattended_index_source=unattended_index_source, config_fields=config_fields,
                            test_profiles={identity.node_id: (backend_descriptor[identity.id] if backend_descriptor is not None
                                           else test_profiles[identity.id]) for identity in selected.values()}, password_manager_registry=manager_registry,
                            interaction_descriptor=interaction_descriptor,
                            **({'tool_plan': tool_plan,
                                'tool_cases': {identity.node_id: identity.id for identity in selected.values()}}
                               if tool_plan is not None else {})))
                    event_file = Path(temporary) / 'events.jsonl'
                    env['API_AGENT_SUCCESSFUL_TEST_DIAGNOSTICS_LEVEL'] = diagnostics_level
                    event_file.touch()
                    from .inference_content import initialize_evidence, read_evidence, failure_reported, has_pending
                    evidence_file = str(Path(temporary) / 'inference.sqlite')
                    initialize_evidence(evidence_file, selected)
                    env['API_AGENT_INFERENCE_EVIDENCE_DB'] = evidence_file
                    evidence_offset = 0
                    evidence_read_failed = False
                    evidence_incomplete = set()
                    evidence_scoped = set()
                    browser_sizes = {}
                    browser_budget_reported = set()
                    env['API_AGENT_INTEGRATION_EVENTS'] = str(event_file)
                    env['API_AGENT_COMPONENT_EVENTS'] = str(event_file)
                    stdout_marker = f'[api-agent-stdout-{uuid4().hex}]\n'
                    traceback_style = 'long' if config is not None and config.get('DEBUG') else 'short'
                    args = [sys.executable, '-c', _PLUGIN.replace('__API_AGENT_STDOUT_BOUNDARY__', repr(stdout_marker)), str(event_file), '-p', 'api_agent.testing.provider_setup', '-p', 'api_agent.testing.inference_content', '-p', 'api_agent.testing.ui_inspection', '-p', 'api_agent.testing.progress', '-o', 'addopts=', f'--tb={traceback_style}', '--rootdir', str(root)]
                    if config is not None and config.get('DEBUG'):
                        args.append('--capture=sys' if config.get('NO_STD_IN') else '--capture=tee-sys')
                    if parallel_jobs:
                        args += ['-n', str(parallel_jobs), '--dist=worksteal']
                    args.extend(selected)
                    coverage_directory = None
                    if config is not None and config.get('E2E_COVERAGE') is True:
                        from .e2e_coverage import prepare
                        from .runner import _production_sources
                        sources = tuple(path for source_root in owner_roots
                                        for path in _production_sources(context, Path(source_root).resolve()))
                        coverage_directory = owner / 'artifacts/e2e-coverage' / uuid4().hex
                        prepare(coverage_directory, sources, args, env)
                    liveness_read = liveness_write = None
                    if os.name == 'posix':
                        liveness_read, liveness_write = os.pipe()
                        env['API_AGENT_LAUNCHER_LIVENESS_FD'] = str(liveness_read)
                    try:
                        process = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.PIPE,
                                                   stderr=subprocess.STDOUT, text=True,
                                                   start_new_session=os.name == 'posix',
                                                   **({'pass_fds': (liveness_read,)} if liveness_read is not None else {}))
                        if getattr(_RUNNER_OPERATION, 'recovery', None) is not None:
                            _RUNNER_OPERATION.recovery.workers.append(process)
                    except OSError as error:
                        if liveness_write is not None:
                            os.close(liveness_write)
                        raw(repo, diagnose('Integration pytest process startup', error, selected.values()) + '\n')
                        failed = infrastructure_failed = True
                        continue
                    except BaseException:
                        if liveness_write is not None:
                            os.close(liveness_write)
                        raise
                    finally:
                        if liveness_read is not None:
                            os.close(liveness_read)
                    stage = 'Integration pytest execution and report collection'
                    lines = queue.Queue()
                    discard_output = threading.Event()
                    output_lock = threading.Lock()
                    reader_errors = []
                    stdout_boundary = threading.Event()
                    for identity in selected.values():
                        reader_diagnostics[identity.id] = reader_errors
                    def read_output(process=process, lines=lines, discard_output=discard_output, reader_errors=reader_errors,
                                    output_lock=output_lock, stdout_marker=stdout_marker,
                                    stdout_boundary=stdout_boundary):
                        try:
                            for line in process.stdout:
                                boundary = line.endswith(stdout_marker)
                                if boundary:
                                    line = line[:-len(stdout_marker)]
                                with output_lock:
                                    if line and not discard_output.is_set():
                                        lines.put(line)
                                    if boundary:
                                        stdout_boundary.set()
                            # Clean EOF also proves that no pipe backlog remains.
                            stdout_boundary.set()
                        except (OSError, UnicodeError) as error:
                            reader_errors.append(local_exception('Integration pytest output reader', error))
                    reader = threading.Thread(target=read_output, daemon=True)
                    try:
                        reader.start()
                    except BaseException:
                        try:
                            finalize(process)
                        finally:
                            if liveness_write is not None:
                                os.close(liveness_write)
                            process.stdout.close()
                        raise
                    offset, reports = 0, {}
                    worker_output, dropped_output_chars = '', 0
                    run_complete = None
                    inspection_ready = False
                    def drain_scoped_failures():
                        for item in failure_channel.reported_scopes() - evidence_scoped:
                            node, invocation_id, visit_id, call_id, direction = item
                            identity = selected.get(node)
                            if identity is not None:
                                emit(RunnerEvent('workflow_inference_evidence_incomplete', repo, identity,
                                    text=json.dumps(dict(invocation_id=invocation_id, visit_id=visit_id,
                                                         call_id=call_id, direction=direction))))
                            evidence_scoped.add(item)

                    def drain_evidence():
                        nonlocal evidence_offset, evidence_read_failed, infrastructure_failed
                        drain_scoped_failures()
                        if evidence_read_failed:
                            return True
                        for _ in range(32):
                            try:
                                evidence_rows = read_evidence(evidence_file, evidence_offset)
                            except sqlite3.OperationalError as error:
                                if 'locked' in str(error).lower() or 'busy' in str(error).lower():
                                    return False
                                evidence_read_failed = infrastructure_failed = True
                                for node, identity in selected.items():
                                    if node not in evidence_incomplete:
                                        evidence_incomplete.add(node)
                                        emit(RunnerEvent('inference_evidence_incomplete', repo, identity, text='unavailable'))
                                return True
                            except (OSError, ValueError, TypeError, sqlite3.Error):
                                evidence_read_failed = infrastructure_failed = True
                                for node, identity in selected.items():
                                    if node not in evidence_incomplete:
                                        evidence_incomplete.add(node)
                                        emit(RunnerEvent('inference_evidence_incomplete', repo, identity, text='unavailable'))
                                return True
                            if not evidence_rows:
                                break
                            ident, node, data = evidence_rows[0]
                            evidence_offset = ident
                            identity = selected.get(node)
                            if identity is not None:
                                emit(RunnerEvent('inference_content', repo, identity, text=json.dumps(data)))
                                if data.get('reason') == 'truncated' and node not in evidence_incomplete:
                                    evidence_incomplete.add(node)
                                    emit(RunnerEvent('inference_evidence_incomplete', repo, identity, text='truncated'))
                        else:
                            return False
                        drain_scoped_failures()
                        for node, identity in selected.items():
                            if node not in evidence_incomplete and (node in failure_channel.reported() or failure_reported(evidence_file, node)):
                                evidence_incomplete.add(node)
                                emit(RunnerEvent('inference_evidence_incomplete', repo, identity, text='unavailable'))
                        return True
                    def drain_events():
                        nonlocal offset, failed, infrastructure_failed, run_complete, inspection_ready
                        if not drain_evidence():
                            return False
                        try:
                            stream = event_file.open(encoding='utf-8')
                        except FileNotFoundError:
                            # A naturally closed retained worker may already have
                            # been reaped by its monitor after reports were read.
                            if directory.retained:
                                return True
                            raise
                        with stream:
                            stream.seek(offset)
                            while True:
                                position = stream.tell()
                                line = stream.readline()
                                if not line or not line.endswith('\n'):
                                    break
                                offset = stream.tell()
                                try:
                                    row = json.loads(line)
                                    if row['kind'] == 'run_complete':
                                        if type(row.get('exit_code')) is not int:
                                            raise ValueError
                                        run_complete = row['exit_code']
                                        continue
                                    if row['kind'] == 'inspection_open':
                                        inspection_ready = True
                                        emit(RunnerEvent('inspection_open', repo, text=row.get('endpoint', '')))
                                        continue
                                    if row['kind'] in {'inspection_closed', 'inspection_cleanup_pending', 'inspection_lost'}:
                                        emit(RunnerEvent(row['kind'], repo))
                                        continue
                                    identity = selected.get(row['node'])
                                    if identity is None:
                                        continue
                                    if row['kind'] == 'test_started':
                                        emit(RunnerEvent('test_started', repo, identity))
                                    elif row['kind'] == 'quota_snapshot':
                                        emit(RunnerEvent('quota_snapshot', repo, identity, text=json.dumps(row['data'])))
                                    elif row['kind'] == 'inference_usage':
                                        emit(RunnerEvent('inference_usage', repo, identity, text=json.dumps(row['data'])))
                                    elif row['kind'] == 'native_usage_boundary':
                                        emit(RunnerEvent('native_usage_boundary', repo, identity, text=json.dumps(row['data'])))
                                    elif row['kind'] == 'native_invocation':
                                        emit(RunnerEvent('native_invocation', repo, identity, text=json.dumps(row['data'])))
                                    elif row['kind'] == 'inference_content':
                                        # Evidence is accepted only from the bounded sidecar.
                                        continue
                                    elif row['kind'] == 'browser_attachment' and (
                                            (config or {}).get('LOG_CONTENT') is True or
                                            (type(row.get('data')) is dict and
                                             row['data'].get('masked_content') is True and
                                             bool({'requires_ui_session', 'requires_owned_ui_session'}
                                                  & set(identity.tags)))):
                                        from .browser_attachments import read_attachment
                                        count, size = browser_sizes.get(identity.id, (0, 0))
                                        if count >= 64 or size >= 16 * 1024 * 1024:
                                            if identity.id not in browser_budget_reported:
                                                browser_budget_reported.add(identity.id)
                                                marker = json.dumps({'status': 'unavailable', 'phase': 'failure',
                                                    'context': 0, 'page': 0, 'reason': 'budget_exceeded'}).encode()
                                                emit(RunnerEvent('browser_attachment', repo, identity, attachment=(
                                                    'UI failure context 0 page 0', marker, 'application/json')))
                                            continue
                                        try:
                                            attachment = read_attachment(row['data'], event_file.parent)
                                        except (OSError, ValueError, TypeError):
                                            continue
                                        browser_sizes[identity.id] = (count + 1, size + len(attachment[1]))
                                        if size + len(attachment[1]) > 16 * 1024 * 1024:
                                            browser_sizes[identity.id] = (64, size)
                                            marker = json.dumps({'status': 'unavailable', 'phase': 'failure',
                                                'context': 0, 'page': 0, 'reason': 'budget_exceeded'}).encode()
                                            attachment = ('UI failure context 0 page 0', marker, 'application/json')
                                            browser_budget_reported.add(identity.id)
                                            emit(RunnerEvent('browser_attachment', repo, identity, attachment=attachment))
                                            continue
                                        emit(RunnerEvent('browser_attachment', repo, identity, text=json.dumps(row['data']), attachment=attachment))
                                    elif row['kind'] == 'workflow_evidence':
                                        emit(RunnerEvent('workflow_evidence', repo, identity, text=json.dumps(row['data'])))
                                    elif row['kind'] == 'component_progress':
                                        from api_agent.providers.progress import ComponentProgress
                                        component = ComponentProgress(**row['component'])
                                        emit(RunnerEvent('component_progress', repo, identity, component=component))
                                    elif row['kind'] == 'report':
                                        if not drain_evidence():
                                            offset = position
                                            return False
                                        if row.get('phase') == 'teardown' and has_pending(evidence_file, row['node']):
                                            if row['node'] not in evidence_incomplete:
                                                evidence_incomplete.add(row['node'])
                                                emit(RunnerEvent('inference_evidence_incomplete', repo, identity, text='unavailable'))
                                        reports.setdefault(identity.id, []).append(row)
                                        if row['phase'] == 'teardown':
                                            # Missing phases need the eventual worker exit diagnostic.
                                            # Setup failures/skips legitimately have no call report.
                                            if _complete_report(reports[identity.id]):
                                                finish(repo, identity, reports[identity.id])
                                except (ValueError, KeyError, TypeError) as error:
                                    record_local('Integration pytest report protocol', error, selected.values())
                                    raw(repo, 'Invalid integration test event; details withheld.\n')
                                    for test in selected.values():
                                        detail = 'Integration report protocol failed: invalid pytest event received.'
                                        if detail not in diagnostics.setdefault(test.id, []):
                                            diagnostics[test.id].append(detail)
                                    failed = infrastructure_failed = True
                        return True
                    def drain(*, final=False):
                        nonlocal worker_output, dropped_output_chars, evidence_read_failed, infrastructure_failed
                        # Structured completion must not wait behind a slow raw sink.
                        if final:
                            deadline = time.monotonic() + 5
                            while not drain_events():
                                if time.monotonic() >= deadline:
                                    evidence_read_failed = infrastructure_failed = True
                                    for node, identity in selected.items():
                                        if node not in evidence_incomplete:
                                            evidence_incomplete.add(node)
                                            emit(RunnerEvent('inference_evidence_incomplete', repo, identity, text='unavailable'))
                                    drain_events()
                                    break
                                time.sleep(.01)
                        else:
                            drain_events()
                        remaining = None if final else 32
                        while not lines.empty() and (remaining is None or remaining > 0):
                            if remaining is not None:
                                remaining -= 1
                            chunk = lines.get_nowait()
                            worker_output += chunk
                            excess = max(0, len(worker_output) - 16384)
                            dropped_output_chars += excess
                            if excess:
                                worker_output = worker_output[excess:]
                            raw(repo, chunk)
                    try:
                        while observe_exit(process) is None:
                            if cancel_event is not None and cancel_event.is_set():
                                cancelled = True
                                break
                            drain()
                            if run_complete is not None and inspection_ready:
                                from api_agent.providers.progress import report_timeout_sync
                                stage = 'Integration retained stdout boundary'
                                with report_timeout_sync(stage, 5) as budget:
                                    while not stdout_boundary.is_set():
                                        if cancel_event is not None and cancel_event.is_set():
                                            cancelled = True
                                            break
                                        if observe_exit(process) is not None:
                                            break
                                        if reader_errors:
                                            raise OSError('Worker output boundary reader failed')
                                        if budget.remaining == 0:
                                            raise TimeoutError('Worker output boundary was not acknowledged')
                                        drain()
                                        time.sleep(min(.03, budget.remaining))
                                    if not cancelled and budget.remaining == 0:
                                        raise TimeoutError('Worker output boundary was not acknowledged before its deadline')
                                if cancelled or not stdout_boundary.is_set():
                                    break
                                if reader_errors:
                                    raise OSError('Worker output boundary reader failed')
                                IntegrationInspection(process, directory, event_file, reader,
                                    on_event=on_event, repo=repo, liveness_fd=liveness_write, registry=inspections)
                                break
                            time.sleep(.03)
                    finally:
                        # Also clean descendants after the pytest leader exits.
                        try:
                            if not directory.retained:
                                finalize(process)
                        finally:
                            if not directory.retained:
                                if liveness_write is not None:
                                    os.close(liveness_write)
                                reader.join(timeout=5)
                                if not reader.is_alive():
                                    process.stdout.close()
                                else:
                                    reader_errors.append('Integration pytest output reader did not finish during cleanup.')
                            if directory.retained:
                                # Stop accepting new retained-worker output before
                                # exhausting the finite evidence already queued.
                                with output_lock:
                                    discard_output.set()
                            drain(final=True)
                    code = run_complete if directory.retained else process.returncode
                    infrastructure_failed |= bool(reader_errors) or code not in (0, 1)
                    failed |= bool(reader_errors) or code != 0
                    for node, identity in selected.items():
                        if node not in evidence_incomplete and has_pending(evidence_file, node):
                            evidence_incomplete.add(node)
                            emit(RunnerEvent('inference_evidence_incomplete', repo, identity, text='unavailable'))
                        rows = reports.get(identity.id, [])
                        if not _complete_report(rows):
                            if node not in evidence_incomplete:
                                evidence_incomplete.add(node)
                                emit(RunnerEvent('inference_evidence_incomplete', repo, identity, text='unavailable'))
                            code = run_complete if directory.retained else process.returncode
                            diagnostics.setdefault(identity.id, []).append(
                                f'Integration pytest report incomplete: worker exit code {code}; '
                                'no complete setup/call/teardown report was received. '
                                'The worker ended without completing this test report.')
                            local = f'Integration pytest report incomplete: worker exit code {code}.'
                            if worker_output:
                                local += '\nCaptured worker output (tail):\n'
                                if dropped_output_chars:
                                    local += f'[{dropped_output_chars} earlier characters omitted]\n'
                                local += worker_output
                            local_diagnostics.setdefault(identity.id, []).append(local)
                        finish(repo, identity, reports.get(identity.id, []), 'cancelled' if cancelled else 'error')
                if coverage_directory is not None:
                    from .e2e_coverage import finalize as finalize_coverage
                    receipt = finalize_coverage(coverage_directory,
                        selection=[identity.id for identity in selected.values()], parallel_jobs=parallel_jobs,
                        e2e_reruns=0, complete=not cancelled and not infrastructure_failed)
                    raw(repo, 'E2E diagnostic coverage: ' + receipt['report'] + '\n')
                if cancelled:
                    break
    except BaseException as error:
        if not isinstance(error, Exception):
            interruption = error
            error.inspections = tuple(inspections)
            abort_inspections()
            raise
        if isinstance(error, CollectionError):
            raise
        raw('', diagnose(stage, error) + '\n')
        failed = infrastructure_failed = True
    finally:
        active_error = sys.exception()
        if active_error is not None and interruption is None:
            interruption = active_error
        interaction_cleanup = None
        interaction_error = None
        if interaction_server is not None:
            try:
                interaction_server.close()
            except BaseException as error:
                from api_agent.providers.control import CleanupPendingError
                from api_agent.runtime.tasks import offload
                async def retry_interaction():
                    await offload(interaction_server.close)
                interaction_cleanup = CleanupPendingError((interaction_server,), retry_interaction)
                interaction_error = error
                failed = infrastructure_failed = True
                if not isinstance(error, Exception) and interruption is None:
                    interruption = error
        if backend is not None and not (config is not None and config.get('DEBUG')):
            try:
                backend.close()
            except BaseException as error:
                inspections.extend(backend.owners)
                if optional_trace_cleanup(error):
                    raw('', 'Tracing cleanup remains pending; resources are retained for retry.\n')
                else:
                    publication_error = error
                    if not isinstance(error, Exception) and interruption is None:
                        interruption = error
                    if interruption is not None:
                        interruption.inspections = tuple(inspections)
                    record_local('Integration shared setup backend cleanup', error)
                    chunks.append('Shared setup backend cleanup pending; retry required.\n')
                    failed = infrastructure_failed = True
        if interaction_cleanup is not None:
            primary = interruption or publication_error
            if primary is None:
                publication_error = interaction_cleanup
            else:
                from .suite_lock import _cleanup_receipts
                receipts = dict.fromkeys(receipt
                    for source in (primary, publication_error, interaction_error)
                    for receipt in _cleanup_receipts(source) if receipt is not primary)
                tail = interaction_cleanup
                for receipt in receipts:
                    tail.cleanup_pending = receipt
                    tail = receipt
                tail.cleanup_pending = None
                primary.cleanup_pending = interaction_cleanup
            record_local('Integration credential interaction cleanup', interaction_error)
            chunks.append('Credential interaction cleanup pending; retry required.\n')
        try:
            check_cancellation()
            for test in tests:
                if test.id not in results:
                    finish(test.repo, test, [], 'cancelled' if cancelled else 'error')
            if backend is not None and config is not None and config.get('DEBUG'):
                try:
                    backend.prepare_publication()
                except Exception as error:
                    if optional_trace_cleanup(error):
                        raw('', 'Tracing cleanup remains pending; resources are retained for retry.\n')
                    else:
                        chunks.append('Shared setup execution cleanup pending; retry required.\n')
                        # Provider work must stop before immutable accounting.
                        publication_error = error
                        failed = infrastructure_failed = True
            failed |= callback_failed or any(reader_diagnostics.values()) or any(
                test.status not in {'passed', 'skipped'} for test in results.values())
            result = IntegrationRunResult(130 if cancelled else int(failed),
                                          final_tests(), ''.join(chunks), cancelled, tuple(inspections),
                                          infrastructure_failed=infrastructure_failed or callback_failed or any(reader_diagnostics.values()))
            if publication_error is not None:
                publication_error.execution = result
                raise publication_error
            # Publication is inside the ownership guard: an interrupt must not
            # lose a worker whose handle has not yet returned to the caller.
            call('after_run', after_run, result)
            if backend is not None and config is not None and config.get('DEBUG'):
                try:
                    for endpoint in backend.finish_run():
                        emit(RunnerEvent('inspection_open', text=endpoint))
                except Exception as error:
                    if not optional_trace_cleanup(error):
                        raise
                    raw('', 'Tracing cleanup remains pending; resources are retained for retry.\n')
                backend.watch_inspection(lambda kind: emit(RunnerEvent(kind)))
            check_cancellation()
            if after_run is not None and not callback_failed and not cancelled:
                from .inspection_gate import publish_manual_accounting_ready
                publish_manual_accounting_ready(inspections)
            result = IntegrationRunResult(130 if cancelled else int(result.exit_code != 0 or callback_failed or failed),
                                          final_tests(), ''.join(chunks), cancelled, result.inspections,
                                          infrastructure_failed=infrastructure_failed or callback_failed or any(reader_diagnostics.values()))
            emit(RunnerEvent('finished', status='failed' if result.exit_code else 'passed'))
            check_cancellation()
            result = IntegrationRunResult(130 if cancelled else int(result.exit_code != 0 or callback_failed or failed),
                                          final_tests(), ''.join(chunks), cancelled, result.inspections,
                                          infrastructure_failed=infrastructure_failed or callback_failed or any(reader_diagnostics.values()))
        except BaseException as error:
            primary = interruption or error
            interruption = primary
            primary.inspections = tuple(inspections)
            abort_inspections()
            # Failed emergency disposal still needs a reviewable retry handle.
            primary.inspections = tuple(inspections)
            from .inspection_controls import preserve_cleanup_receipts
            preserve_cleanup_receipts(primary)
            raise primary
        if interruption is not None:
            interruption.inspections = tuple(inspections)
            raise interruption
    return result


@suite_locked
@_admit_operation
def run_integration_tests(context, tests, **options):
    """Execute integrations once, or E2Es with all-pass failure confirmations.

    Each wave keeps the configured worker count. Physical callback identities
    own evidence and accounting; returned logical outcomes own UI/exit status.
    """
    config = options.get('config') or {}
    if config.get('TUI_MODE') != 'e2e':
        return _run_integration_tests(context, tests, **options)
    from .confirmations import confirmation_count, confirmation_outcome
    from .recorded import validate_integration_start
    tests = validate_integration_start(tests, config,
        accepted=options.get('accepted', False), no_std_in=options.get('no_std_in', False))
    allowance = confirmation_count(config.get('E2E_RERUNS'))
    if config.get('E2E_COVERAGE') is True and (allowance != 0 or options.get('parallel_jobs', 0) != 8):
        raise ValueError('E2E coverage diagnostic requires --e2e-reruns 0 and eight parallel workers')
    if config.get('E2E_COVERAGE') is True and config.get('DEBUG') is True:
        raise ValueError('E2E coverage diagnostic requires owned shutdown; disable retained DEBUG inspection')
    logical = {test.id: test for test in tests}
    histories = {test.id: [] for test in tests}
    attempts, chunks, inspections = [], [], []
    pending = tests
    admitted_profiles = deepcopy(options.get('resolved_test_profiles'))
    prepared = cancelled = infrastructure_failed = False
    callbacks = {key: options.get(key) for key in ('on_event', 'on_result', 'before_run', 'after_run')}
    def aggregate():
        outcomes = []
        for key, test in logical.items():
            rows = histories[key]
            if not rows:
                outcomes.append(TestResult(test, 'cancelled' if cancelled else 'error'))
                continue
            status, incomplete = confirmation_outcome([row.status for row in rows], allowance)
            if incomplete:
                status = 'cancelled' if cancelled else 'failed'
            outcomes.append(replace(rows[-1], identity=test, status=status,
                duration=sum(row.duration for row in rows), first_status=rows[0].status))
        execution = IntegrationRunResult(130 if cancelled else int(infrastructure_failed or any(
            row.status not in {'passed', 'skipped'} for row in outcomes)), tuple(outcomes), ''.join(chunks),
            cancelled, tuple(inspections), tuple(attempts), allowance,
            infrastructure_failed=infrastructure_failed,
            prerequisite_events=(owner.events if (owner := getattr(_RUNNER_OPERATION, 'prerequisites', None))
                                 is not None else ()))
        return execution
    publication_attempted = False
    try:
        if callbacks['on_event'] is not None:
            callbacks['on_event'](RunnerEvent('started'))
        while pending:
            physical = {test.id: replace(test, id=(test.id if not histories[test.id]
                else test.id + '::confirmation:' + str(len(histories[test.id]) + 1))) for test in pending}
            attempt_labels = {test.id: (f'Confirmation {len(histories[test.id])}/{allowance}'
                if histories[test.id] else '') for test in pending}
            def event(row):
                # Journals retain the collected node ID; only their owning identity
                # changes. This keeps journal validation and Allure history stable.
                if row.identity is not None:
                    key = row.identity.id
                    if row.kind in {'test_started', 'component_progress'}:
                        # Live progress belongs to the collected UI row; immutable
                        # evidence below still belongs to its physical attempt.
                        row = replace(row, text=attempt_labels[key])
                    else:
                        row = replace(row, identity=physical[key])
                if callbacks['on_event'] is not None and row.kind not in {'started', 'finished', 'test_finished'}:
                    callbacks['on_event'](row)
            def result(row):
                key = row.identity.id
                owned = replace(row, identity=physical[key], logical_id=key,
                    attempt=len(histories[key]) + 1)
                histories[key].append(owned)
                attempts.append(owned)
                if callbacks['on_result'] is not None:
                    callbacks['on_result'](owned)
            def before():
                nonlocal prepared
                if not prepared:
                    if callbacks['before_run'] is not None:
                        callbacks['before_run']()
                    _prepare_suite_prerequisites(context, config, options.get('output'), options.get('cancel_event'))
                    prepared = True
            wave_options = dict(options)
            if admitted_profiles is not None:
                # The first wave validates the complete frozen map. Later waves
                # retain only profiles for identities still requiring confirmation.
                wave_options['resolved_test_profiles'] = (admitted_profiles if not attempts else
                    {test.id: admitted_profiles[test.id] for test in pending})
            wave = _run_integration_tests(context, pending, **dict(wave_options,
                on_event=event, on_result=result, before_run=before, after_run=None,
                config=dict(config, _E2E_ATTEMPT=len(histories[pending[0].id]) + 1)))
            chunks.append(wave.output)
            inspections.extend(wave.inspections)
            cancelled |= wave.cancelled or (options.get('cancel_event') is not None and options['cancel_event'].is_set())
            # A worker/setup/cleanup failure independent of reported test outcomes
            # cannot be repaired by confirming a functional test failure.
            infrastructure_failed |= wave.infrastructure_failed
            if cancelled or infrastructure_failed:
                break
            pending = tuple(test for test in pending
                if confirmation_outcome([row.status for row in histories[test.id]], allowance)[1])
        _close_suite_prerequisites(inspections)
        execution = aggregate()
        outcomes = execution.tests
        if callbacks['on_event'] is not None:
            for row in outcomes:
                callbacks['on_event'](RunnerEvent('test_finished', row.identity.repo, row.identity,
                    row.status, row.duration, row.traceback))
        if options.get('cancel_event') is not None and options['cancel_event'].is_set():
            cancelled = True
            execution = replace(execution, exit_code=130, cancelled=True)
        if callbacks['after_run'] is not None:
            publication_attempted = True
            callbacks['after_run'](execution)
        if options.get('cancel_event') is not None and options['cancel_event'].is_set():
            cancelled = True
            execution = replace(execution, exit_code=130, cancelled=True)
        if not cancelled and not (options.get('cancel_event') is not None and options['cancel_event'].is_set()):
            from .inspection_gate import publish_manual_accounting_ready
            publish_manual_accounting_ready(inspections)
        else:
            for inspection in inspections:
                inspection.abort()
        if callbacks['on_event'] is not None:
            callbacks['on_event'](RunnerEvent('finished', status='failed' if execution.exit_code else 'passed'))
        if options.get('cancel_event') is not None and options['cancel_event'].is_set():
            execution = replace(execution, exit_code=130, cancelled=True)
            for inspection in inspections:
                inspection.abort()
        return execution
    except BaseException as error:
        from .suite_lock import _cleanup_receipts
        retained = tuple(dict.fromkeys([*inspections, *getattr(error, 'inspections', ())]))
        error.inspections = retained
        cleanup_errors = []
        try:
            _close_suite_prerequisites(retained)
        except BaseException as cleanup:
            cleanup_errors.append(cleanup)
        for inspection in retained:
            try:
                inspection.abort()
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        if cleanup_errors:
            error.confirmation_cleanup_errors = tuple(cleanup_errors)
        if (not publication_attempted and (prepared or attempts or getattr(_RUNNER_OPERATION, 'prerequisites', None) is not None) and not cleanup_errors
                and not any(_cleanup_receipts(error))):
            cancelled |= not isinstance(error, Exception)
            infrastructure_failed = True
            execution = aggregate()
            error.execution = execution
            if callbacks['after_run'] is not None:
                publication_attempted = True
                try:
                    callbacks['after_run'](execution)
                except BaseException as publication_error:
                    error.confirmation_publication_error = publication_error
        raise
