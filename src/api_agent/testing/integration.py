"""Accepted, integration-only pytest execution.

``output`` and ``RunnerEvent(kind='output')`` are raw pytest evidence and may
contain test data, including secrets. Callers must route them to an explicitly
approved evidence sink. Runner-generated diagnostics are fixed, sanitized text.
Callbacks run synchronously on the calling thread. ``after_run`` receives the
final result after the owned child process group has been reaped.
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


_PLUGIN = r'''
import json, sys
import pytest
class Events:
    def send(self, **row):
        with open(sys.argv[1], 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(row) + '\n')
    def pytest_runtest_logstart(self, nodeid, location):
        self.send(kind='test_started', node=nodeid)
    def pytest_runtest_logreport(self, report):
        self.send(kind='report', node=report.nodeid, phase=report.when,
                  status=report.outcome, duration=report.duration,
                  traceback=(str(report.longrepr[2]).removeprefix('Skipped: ')
                             if report.skipped and isinstance(report.longrepr, tuple)
                             else str(report.longrepr) if report.failed or report.skipped else ''))
sys.exit(pytest.main(sys.argv[2:], plugins=[Events()]))
'''


def run_integration_tests(context, tests, *, accepted=False, no_std_in=False,
                          on_event=None, output=None, cancel_event=None,
                          parallel_jobs=0, before_run=None, on_result=None,
                          after_run=None, config=None):
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
                with tempfile.TemporaryDirectory(prefix='api-agent-integration-') as temporary:
                    if config is not None:
                        from .provider_setup import write_launch_snapshot
                        env['API_AGENT_INTEGRATION_CONTEXT'] = str(write_launch_snapshot(Path(temporary), context, config))
                    event_file = Path(temporary) / 'events.jsonl'
                    event_file.touch()
                    args = [sys.executable, '-c', _PLUGIN, str(event_file), '-p', 'api_agent.testing.provider_setup', '-o', 'addopts=', '--rootdir', str(root)]
                    if config is not None and config.get('DEBUG'):
                        args.append('-s')
                    elif parallel_jobs:
                        args += ['-n', str(parallel_jobs)]
                    args.extend(selected)
                    try:
                        process = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.PIPE,
                                                   stderr=subprocess.STDOUT, text=True,
                                                   start_new_session=os.name == 'posix')
                    except OSError:
                        raw(repo, 'Cannot start integration pytest; details withheld.\n')
                        failed = True
                        continue
                    lines = queue.Queue()
                    def read_output():
                        try:
                            for line in process.stdout:
                                lines.put(line)
                        except OSError:
                            pass
                    reader = threading.Thread(target=read_output, daemon=True)
                    reader.start()
                    offset, reports = 0, {}
                    def drain():
                        nonlocal offset, failed
                        while not lines.empty():
                            raw(repo, lines.get_nowait())
                        with event_file.open(encoding='utf-8') as stream:
                            stream.seek(offset)
                            while True:
                                position = stream.tell()
                                line = stream.readline()
                                if not line or not line.endswith('\n'):
                                    break
                                offset = stream.tell()
                                try:
                                    row = json.loads(line)
                                    identity = selected.get(row['node'])
                                    if identity is None:
                                        continue
                                    if row['kind'] == 'test_started':
                                        emit(RunnerEvent('test_started', repo, identity))
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
                            time.sleep(.03)
                    finally:
                        # Also clean descendants after the pytest leader exits.
                        try:
                            finalize(process)
                        finally:
                            reader.join(timeout=5)
                            if not reader.is_alive():
                                process.stdout.close()
                            drain()
                    failed |= process.returncode != 0
                    for identity in selected.values():
                        finish(repo, identity, reports.get(identity.id, []), 'cancelled' if cancelled else 'error')
                if cancelled:
                    break
    except CollectionError:
        raise
    except Exception:
        raw('', 'Integration runner failed; details withheld.\n')
        failed = True
    finally:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
        for test in tests:
            if test.id not in results:
                finish(test.repo, test, [], 'cancelled' if cancelled else 'error')
        failed |= callback_failed or any(test.status not in {'passed', 'skipped'} for test in results.values())
        result = IntegrationRunResult(130 if cancelled else int(failed),
                                      tuple(results[test.id] for test in tests), ''.join(chunks), cancelled)
        # After-run occurs after synchronous cleanup. Its failure is reflected
        # in the finished event and returned status.
        call('after_run', after_run, result)
        result = IntegrationRunResult(130 if result.cancelled else int(result.exit_code != 0 or callback_failed),
                                      result.tests, ''.join(chunks), result.cancelled)
        emit(RunnerEvent('finished', status='failed' if result.exit_code else 'passed'))
        result = IntegrationRunResult(130 if result.cancelled else int(result.exit_code != 0 or callback_failed),
                                      result.tests, ''.join(chunks), result.cancelled)
    return result
