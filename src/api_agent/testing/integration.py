"""Accepted, integration-only pytest execution.

``output`` and ``RunnerEvent(kind='output')`` are raw pytest evidence and may
contain test data, including secrets. Callers must route them to an explicitly
approved evidence sink. Runner-generated diagnostics are fixed, sanitized text.
Run callbacks run synchronously on the calling thread; retained inspection
completion events run on their monitor thread. ``after_run`` receives the
final result after pytest completes; debug inspection handles retain owned workers.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

from .collection import CollectionError, collect_tests
from .contracts import RunnerEvent, TestResult
from .processes import finalize


@dataclass(frozen=True)
class IntegrationRunResult:
    exit_code: int
    tests: tuple[TestResult, ...]
    output: str
    cancelled: bool = False
    inspections: tuple = ()


_PLUGIN = r'''
import json, os, signal, sys, threading
import pytest
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
    def send(self, **row):
        with open(event_path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(row) + '\n')
    def pytest_runtest_logstart(self, nodeid, location):
        self.send(kind='test_started', node=nodeid)
    def pytest_runtest_logreport(self, report):
        self.send(kind='report', node=report.nodeid, phase=report.when,
                  status=report.outcome, duration=report.duration,
                  traceback=(str(report.longrepr[2]).removeprefix('Skipped: ')
                             if report.skipped and isinstance(report.longrepr, tuple)
                             else str(report.longrepr) if report.failed or report.skipped else ''))
event_path = sys.argv[1]
events = Events()
code = pytest.main(sys.argv[2:], plugins=[events])
events.send(kind='run_complete', exit_code=int(code))
from api_agent.testing.provider_setup import serve_inspections
serve_inspections(events.send)
sys.exit(code)
'''


def run_integration_tests(context, tests, *, accepted=False, no_std_in=False,
                          on_event=None, output=None, cancel_event=None,
                          parallel_jobs=0, before_run=None, on_result=None,
                          after_run=None, config=None, unattended_index_source=None):
    """Run freshly collected integration identities once, with no retry/coverage gate.

    ``accepted`` must represent the picker's explicit Continue action. This
    function never starts prerequisites itself; ``before_run`` is their hook.
    No callback receives a raw exception. Pytest stdout and traceback remain
    explicit test evidence in the result and event stream.
    """
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
    interruption = None
    callback_failed = cancelled = failed = False
    disabled = set()

    def call(name, callback, *args):
        nonlocal callback_failed
        if callback is None or name in disabled:
            return
        try:
            callback(*args)
        except Exception:
            callback_failed = True
            disabled.add(name)
            chunks.append(f'{name} callback failed; details withheld.\n')

    def emit(event):
        call('on_event', on_event, event)

    def raw(repo, chunk):
        chunks.append(chunk)
        call('output', output, chunk)
        emit(RunnerEvent('output', repo, text=chunk))

    def abort_inspections():
        nonlocal failed, interruption
        for inspection in inspections:
            try:
                inspection.abort()
            except BaseException as error:
                if not isinstance(error, Exception) and interruption is None:
                    interruption = error
                chunks.append('Inspection cleanup pending; retry required.\n')
                failed = True

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
        result = TestResult(identity, status, sum(float(row['duration']) for row in rows),
                            '\n'.join(row['traceback'] for row in rows if row['traceback']))
        results[identity.id] = result
        emit(RunnerEvent('test_finished', repo, identity, status, result.duration, result.traceback))
        call('on_result', on_result, result)

    emit(RunnerEvent('started'))
    try:
        try:
            available = {test.id: test for test in collect_tests(context, 'integration', cancel_event=cancel_event, **({'config': config} if config is not None else {}))}
        except Exception:
            raw('', 'Integration collection failed; details withheld.\n')
            failed = True
            available = None
        if available is not None:
            if any(available.get(test.id) != test for test in tests):
                raise CollectionError('Integration selection contains identities outside the freshly collected integration suites')
            from .requirements import plan_integration_requirements
            plan_integration_requirements(tests).validate(context, config or {})
            base, owner = Path(context.base_root).resolve(), Path(context.owner_root).resolve()
            roots = {base.name: base, owner.name: owner}
            if base != owner and base.name == owner.name:
                roots = {'base': base, 'pack': owner}
            env = os.environ.copy()
            env['PYTHONPATH'] = os.pathsep.join([str(base / 'src'), str(owner / 'src'), env.get('PYTHONPATH', '')])
            env['PYTHONUNBUFFERED'] = '1'
            backend_descriptor = None
            for repo in dict.fromkeys(test.repo for test in tests):
                selected = {test.node_id: test for test in tests if test.repo == repo}
                root = next(iter(selected.values())).root or roots.get(repo)
                if root is None:
                    failed = True
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                # One callback per owner suite, before any child is started.
                was_failed = callback_failed
                call('before_run', before_run)
                if callback_failed and not was_failed:
                    failed = True
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if config is not None and not config.get('DEBUG') and backend is None:
                    from .setup_backend import SetupBackend
                    from api_agent.auth.managers import registry_for_context
                    backend = SetupBackend(config, context.owner_root,
                        unattended_index_source=unattended_index_source,
                        password_manager_registry=registry_for_context(context))
                    backend_descriptor = backend.start()
                from .inspection import InspectionDirectory, IntegrationInspection
                directory = InspectionDirectory(tempfile.TemporaryDirectory(prefix='api-agent-integration-'))
                with directory as temporary:
                    if config is not None:
                        from .provider_setup import write_launch_snapshot
                        env['API_AGENT_INTEGRATION_CONTEXT'] = str(write_launch_snapshot(Path(temporary), context, config,
                            unattended_index_source=unattended_index_source,
                            setup_backend=backend_descriptor))
                    event_file = Path(temporary) / 'events.jsonl'
                    event_file.touch()
                    env['API_AGENT_INTEGRATION_EVENTS'] = str(event_file)
                    env['API_AGENT_COMPONENT_EVENTS'] = str(event_file)
                    args = [sys.executable, '-c', _PLUGIN, str(event_file), '-p', 'api_agent.testing.provider_setup', '-p', 'api_agent.testing.progress', '-o', 'addopts=', '--rootdir', str(root)]
                    if config is not None and config.get('DEBUG'):
                        args.append('-s')
                    elif parallel_jobs:
                        args += ['-n', str(parallel_jobs)]
                    args.extend(selected)
                    liveness_read = liveness_write = None
                    if os.name == 'posix':
                        liveness_read, liveness_write = os.pipe()
                        env['API_AGENT_LAUNCHER_LIVENESS_FD'] = str(liveness_read)
                    try:
                        process = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.PIPE,
                                                   stderr=subprocess.STDOUT, text=True,
                                                   start_new_session=os.name == 'posix',
                                                   **({'pass_fds': (liveness_read,)} if liveness_read is not None else {}))
                    except OSError:
                        if liveness_write is not None:
                            os.close(liveness_write)
                        raw(repo, 'Cannot start integration pytest; details withheld.\n')
                        failed = True
                        continue
                    except BaseException:
                        if liveness_write is not None:
                            os.close(liveness_write)
                        raise
                    finally:
                        if liveness_read is not None:
                            os.close(liveness_read)
                    lines = queue.Queue()
                    discard_output = threading.Event()
                    def read_output(process=process, lines=lines, discard_output=discard_output):
                        try:
                            for line in process.stdout:
                                if not discard_output.is_set():
                                    lines.put(line)
                        except OSError:
                            pass
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
                    run_complete = None
                    inspection_ready = False
                    def drain():
                        nonlocal offset, failed, run_complete, inspection_ready
                        while not lines.empty():
                            raw(repo, lines.get_nowait())
                        try:
                            stream = event_file.open(encoding='utf-8')
                        except FileNotFoundError:
                            # A naturally closed retained worker may already have
                            # been reaped by its monitor after reports were read.
                            if directory.retained:
                                return
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
                                    if row['kind'] in {'inspection_closed', 'inspection_cleanup_pending'}:
                                        emit(RunnerEvent(row['kind'], repo))
                                        continue
                                    identity = selected.get(row['node'])
                                    if identity is None:
                                        continue
                                    if row['kind'] == 'test_started':
                                        emit(RunnerEvent('test_started', repo, identity))
                                    elif row['kind'] == 'component_progress':
                                        from api_agent.providers.progress import ComponentProgress
                                        component = ComponentProgress(**row['component'])
                                        emit(RunnerEvent('component_progress', repo, identity, component=component))
                                    elif row['kind'] == 'report':
                                        reports.setdefault(identity.id, []).append(row)
                                        if row['phase'] == 'teardown':
                                            finish(repo, identity, reports[identity.id])
                                except (ValueError, KeyError, TypeError):
                                    raw(repo, 'Invalid integration test event; details withheld.\n')
                                    failed = True
                    try:
                        while process.poll() is None:
                            if cancel_event is not None and cancel_event.is_set():
                                cancelled = True
                                break
                            drain()
                            if run_complete is not None and inspection_ready:
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
                            try:
                                drain()
                            finally:
                                if directory.retained:
                                    discard_output.set()
                    failed |= (run_complete if directory.retained else process.returncode) != 0
                    for identity in selected.values():
                        finish(repo, identity, reports.get(identity.id, []), 'cancelled' if cancelled else 'error')
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
        raw('', 'Integration runner failed; details withheld.\n')
        failed = True
    finally:
        if backend is not None:
            try:
                backend.close()
            except BaseException as error:
                inspections.append(backend)
                if not isinstance(error, Exception) and interruption is None:
                    interruption = error
                if interruption is not None:
                    interruption.inspections = tuple(inspections)
                chunks.append('Shared setup backend cleanup pending; retry required.\n')
                failed = True
        try:
            check_cancellation()
            for test in tests:
                if test.id not in results:
                    finish(test.repo, test, [], 'cancelled' if cancelled else 'error')
            failed |= callback_failed or any(test.status not in {'passed', 'skipped'} for test in results.values())
            result = IntegrationRunResult(130 if cancelled else int(failed),
                                          tuple(results[test.id] for test in tests), ''.join(chunks), cancelled, tuple(inspections))
            # Publication is inside the ownership guard: an interrupt must not
            # lose a worker whose handle has not yet returned to the caller.
            call('after_run', after_run, result)
            check_cancellation()
            result = IntegrationRunResult(130 if cancelled else int(result.exit_code != 0 or callback_failed or failed),
                                          result.tests, ''.join(chunks), cancelled, result.inspections)
            emit(RunnerEvent('finished', status='failed' if result.exit_code else 'passed'))
            check_cancellation()
            result = IntegrationRunResult(130 if cancelled else int(result.exit_code != 0 or callback_failed or failed),
                                          result.tests, ''.join(chunks), cancelled, result.inspections)
        except BaseException as error:
            primary = interruption or error
            interruption = primary
            primary.inspections = tuple(inspections)
            abort_inspections()
            # Failed emergency disposal still needs a reviewable retry handle.
            primary.inspections = tuple(inspections)
            raise primary
        if interruption is not None:
            interruption.inspections = tuple(inspections)
            raise interruption
    return result
