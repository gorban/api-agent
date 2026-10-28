"""Owned pytest unit execution and fixed, per-file production coverage gates."""
from .contracts import TestResult, RunnerEvent, format_test_summary
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
import uuid

from .collection import CollectionError, TestIdentity, collect_tests, repository_label
from .processes import disposal_complete, finalize
from .suite_lock import suite_locked
from .unit_watchdog import UnitWatchdog
from .coverage_reports import CoverageReport, coverage_table, retain_report


def unit_parallel_jobs(config):
    """An explicit zero stays serial; empty inherits the global worker count."""
    override = config.get('UNIT_TEST_PARALLEL_JOBS')
    return config.get('TEST_PARALLEL_JOBS', 8) if override is None else override


@dataclass(frozen=True)
class CoverageArea:
    repo: str
    file: str
    covered_lines: int
    num_statements: int
    covered_branches: int
    num_branches: int
    missing_lines: tuple[int, ...] = ()
    missing_branches: tuple[tuple[int, int], ...] = ()

    @property
    def line_percent(self):
        return 100 * self.covered_lines / self.num_statements if self.num_statements else 100.0

    @property
    def branch_percent(self):
        return 100 * self.covered_branches / self.num_branches if self.num_branches else 100.0

    @property
    def complete(self):
        return self.covered_lines == self.num_statements and self.covered_branches == self.num_branches


@dataclass(frozen=True)
class UnitRunResult:
    exit_code: int
    tests: tuple[TestResult, ...]
    coverage: tuple[CoverageArea, ...]
    output: str
    cancelled: bool = False
    require_full_coverage: bool = True
    elapsed: float | None = None
    coverage_reports: tuple[CoverageReport, ...] = ()
    coverage_full: bool = False


_PLUGIN = r'''
import json, os, sys, time
import pytest
event_path = sys.argv[1]
class Events:
    def send(self, **row):
        row['at'] = time.monotonic()
        with open(event_path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(row) + '\n')
    def pytest_runtest_logstart(self, nodeid, location):
        self.send(kind='test_started', node=nodeid)
    def pytest_runtest_logreport(self, report):
        self.send(kind='report', node=report.nodeid, phase=report.when,
                  status=report.outcome, duration=report.duration,
                  traceback=str(report.longrepr) if report.failed else '')
with open(os.environ['API_AGENT_UNIT_SELECTION'], encoding='utf-8') as selection_file:
    selected_nodes = json.load(selection_file)
status = pytest.main(sys.argv[2:] + selected_nodes, plugins=[Events()])
import os
if os.environ.get('API_AGENT_COVERAGE_OUTPUT'):
    from pathlib import Path
    from coverage import Coverage
    coverage = Coverage(data_file=os.environ['COVERAGE_FILE'], config_file=False)
    coverage.load()
    files = [str(path) for source in json.loads(os.environ['API_AGENT_COVERAGE_SOURCE'])
             for path in Path(source).rglob('*.py')]
    coverage.json_report(morfs=files,
                         omit=['*/tests/*'],
                         outfile=os.environ['API_AGENT_COVERAGE_OUTPUT'])
    coverage.html_report(morfs=files, omit=['*/tests/*'],
                         directory=os.environ['API_AGENT_COVERAGE_HTML'])
sys.exit(status)
'''


def _stop_execution(process):
    """Reap the owned leader without competing with the output reader."""
    finalize(process)


def _production_sources(context, root):
    candidates = [root / 'src']
    if root == Path(context.owner_root).resolve():
        candidates.extend(getattr(context, 'production_paths', ()))
    return tuple(dict.fromkeys(Path(path).resolve() for path in candidates
                               if Path(path).is_dir() and any(Path(path).rglob('*.py'))))


@suite_locked
def run_unit_tests(context, tests, *, on_event=None, output=None, cancel_event=None, parallel_jobs=0, require_full_coverage=True, coverage_full=False):
    """Run selected collected identities once; preserve owner configs and full source scope.

    Callbacks run on the calling thread. Cancellation terminates and reaps only
    owned process groups. Nonzero also signals skipped/incomplete selected tests.
    """
    started_at = time.monotonic()
    tests = tuple(dict((test.id, test) for test in tests).values())
    if not tests:
        raise CollectionError('Cannot execute an empty unit selection')
    if parallel_jobs < 0:
        raise ValueError('parallel_jobs must be nonnegative')
    # Validate against actual mandatory base/optional pack discovery, preventing
    # callers from passing fabricated node IDs or bypassing a missing base suite.
    available = {test.id: test for test in collect_tests(context, 'unit', cancel_event=cancel_event)}
    if any(available.get(test.id) != test for test in tests):
        raise CollectionError('Unit selection contains identities outside the collected suites')
    base, owner = Path(context.base_root).resolve(), Path(context.owner_root).resolve()
    from api_agent.runtime.frontend import ensure_source_frontend, source_root
    # The reusable runner prepares only the selected source library's own base suite.
    if base == source_root():
        ensure_source_frontend(cancellation=cancel_event)
    roots = {repository_label(context, root): root for root in dict.fromkeys((base, owner))}
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join([str(base / 'src'), str(owner / 'src'), env.get('PYTHONPATH', '')])
    env['PYTHONUNBUFFERED'] = '1'
    chunks, results, areas = [], {}, []
    retained = []
    report_root = owner / 'artifacts' / 'unit' / uuid.uuid4().hex
    report_destinations = {repo: report_root / str(index) for index, repo in enumerate(roots)}
    failed = cancelled = False

    def emit(event):
        if on_event:
            on_event(event)

    def raw(repo, chunk):
        chunks.append(chunk)
        if output:
            output(chunk)
        emit(RunnerEvent('output', repo, text=chunk))

    emit(RunnerEvent('started'))
    for repo in dict.fromkeys(test.repo for test in tests):
        selected = {test.node_id: test for test in tests if test.repo == repo}
        root = roots[repo]
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        with tempfile.TemporaryDirectory(prefix='api-agent-unit-') as temporary:
            events = Path(temporary) / 'events.jsonl'
            coverage = Path(temporary) / 'coverage.json'
            events.touch()
            env['API_AGENT_COMPONENT_EVENTS'] = str(events)
            from .progress import write_lazy_plugin
            plugin = write_lazy_plugin(Path(temporary))
            args = [sys.executable, '-c', _PLUGIN, str(events), '-p', plugin, '-o', 'addopts=', '--rootdir', str(root)]
            sources = _production_sources(context, root)
            has_code = bool(sources)
            env.pop('API_AGENT_COVERAGE_OUTPUT', None)
            env.pop('API_AGENT_COVERAGE_SOURCE', None)
            env.pop('API_AGENT_COVERAGE_HTML', None)
            if has_code:
                config = Path(temporary) / 'coverage.ini'
                config.write_text('[run]\nbranch = true\nomit = */tests/*\n[report]\ninclude_namespace_packages = true\n')
                env['API_AGENT_COVERAGE_OUTPUT'] = str(coverage)
                env['API_AGENT_COVERAGE_SOURCE'] = json.dumps([str(source) for source in sources])
                env['API_AGENT_COVERAGE_HTML'] = str(Path(temporary) / 'html')
                args += ['--cov-config=' + str(config)]
                args += ['--cov=' + str(source) for source in sources]
                args += ['--cov-branch', '--cov-report=']
                env['COVERAGE_FILE'] = str(Path(temporary) / '.coverage')
            if parallel_jobs:
                args += ['-n', str(parallel_jobs), '--dist=worksteal', '--max-worker-restart=0']
            selection_file = Path(temporary) / 'selection.json'
            selection_file.write_text(json.dumps(list(selected)), encoding='utf-8')
            env['API_AGENT_UNIT_SELECTION'] = str(selection_file)
            from api_agent.providers.progress import report_timeout_sync
            with report_timeout_sync('Unit pytest execution', None) as budget:
                process = reader = None
                reader_started = reader_joined = False
                disposed = False
                def stop_owned():
                    nonlocal disposed
                    disposed = disposed or disposal_complete(process)
                    if not disposed:
                        try:
                            _stop_execution(process)
                        finally:
                            disposed = disposal_complete(process)
                        disposed = True
                try:
                    try:
                        child_env = dict(env)
                        child_env['PYTHONPATH'] = temporary + os.pathsep + env['PYTHONPATH']
                        process = subprocess.Popen(args, cwd=root, env=child_env, stdout=subprocess.PIPE,
                                                   stderr=subprocess.STDOUT, text=True, start_new_session=os.name == 'posix')
                    except OSError as exc:
                        raw(repo, f'Cannot start pytest: {exc}\n')
                        failed = True
                        continue
                    watchdog = UnitWatchdog(deadline=budget.deadline)
                    expired = ()
                    timeout_details = {}
                    ordinals = {test.id: index for index, test in enumerate(tests, 1)}
                    lines = queue.Queue()
                    def read_output(process=process, lines=lines):
                        for line in process.stdout:
                            lines.put(line)
                    reader = threading.Thread(target=read_output, daemon=True)
                    reader.start()
                    reader_started = True
                    offset = 0
                    reports = {}
                    def drain_events():
                        nonlocal offset
                        with events.open(encoding='utf-8') as stream:
                            stream.seek(offset)
                            boundary = os.fstat(stream.fileno()).st_size
                            while stream.tell() < boundary:
                                position = stream.tell()
                                line = stream.readline()
                                if not line or not line.endswith('\n'):
                                    stream.seek(position)
                                    break
                                row = json.loads(line)
                                identity = selected.get(row['node'])
                                if identity is None:
                                    continue
                                if row['kind'] == 'test_started':
                                    watchdog.observe(identity.id, 'test_started', at=row.get('at'))
                                    retain_transition_deadlines()
                                    emit(RunnerEvent('test_started', repo, identity))
                                elif row['kind'] == 'component_progress':
                                    from api_agent.providers.progress import ComponentProgress
                                    emit(RunnerEvent('component_progress', repo, identity,
                                                     component=ComponentProgress(**row['component'])))
                                else:
                                    reports.setdefault(identity.id, []).append(row)
                                    if identity.id not in timeout_details:
                                        watchdog.observe(identity.id, 'report', phase=row['phase'], at=row.get('at'))
                                        retain_transition_deadlines()
                                        if row['phase'] == 'teardown':
                                            finish(identity)
                            offset = stream.tell()
                    def retain_transition_deadlines():
                        for deadline in watchdog.violations.values():
                            timeout_details.setdefault(deadline.identity,
                                deadline.diagnostic(ordinals.get(deadline.identity)))
                    def record_deadlines(deadlines):
                        nonlocal failed
                        failed = True
                        for deadline in deadlines:
                            detail = deadline.diagnostic(ordinals.get(deadline.identity))
                            raw(repo, detail + '\n')
                            if deadline.identity is not None:
                                timeout_details.setdefault(deadline.identity, detail)
                        for identity in selected.values():
                            if identity.id not in results:
                                timeout_details.setdefault(identity.id,
                                    'Unit pytest repository stopped after an execution deadline.')
                    def drain(*, final=False):
                        drain_events()
                        remaining = None if final else 32
                        while not lines.empty() and (remaining is None or remaining > 0):
                            if remaining is not None:
                                remaining -= 1
                            raw(repo, lines.get_nowait())
                    def finish(identity):
                        if identity.id in results:
                            return
                        rows = reports.get(identity.id, [])
                        phases = {row['phase']: row for row in rows}
                        complete = ('setup' in phases and 'teardown' in phases and
                                    ('call' in phases or phases['setup']['status'] in {'failed', 'skipped'}))
                        status = ('failed' if any(row['status'] == 'failed' for row in rows) else
                                  'cancelled' if cancelled and not complete else
                                  'error' if not complete or identity.id in timeout_details else
                                  'skipped' if any(row['status'] == 'skipped' for row in rows) else
                                  'passed' if any(row['phase'] == 'call' for row in rows) else 'error')
                        details = [row['traceback'] for row in rows if row['traceback']]
                        if identity.id in timeout_details:
                            details.append(timeout_details[identity.id])
                        elif not complete:
                            details.append('Unit pytest report incomplete: no complete setup/call/teardown report was received.')
                        result = TestResult(identity, status, sum(row['duration'] for row in rows), '\n'.join(details))
                        results[identity.id] = result
                        emit(RunnerEvent('test_finished', repo, identity, status, result.duration, result.traceback))
                    while process.poll() is None:
                        if cancel_event is not None and cancel_event.is_set():
                            cancelled = True
                            stop_owned()
                            break
                        drain()
                        if watchdog.expired():
                            # Teardown already durable on disk wins the race with
                            # the clock, even when raw output delivery was slow.
                            drain_events()
                            expired = watchdog.expired()
                            if expired:
                                record_deadlines(expired)
                                stop_owned()
                                break
                        time.sleep(.03)
                    exit_observed_at = watchdog.clock()
                    # Disposal is acknowledged once per exact process handle;
                    # later reader/drain errors cannot signal its reused PID.
                    stop_owned()
                    reader.join(timeout=1)
                    reader_joined = True
                    drain(final=True)
                    if not cancelled and not expired:
                        expired = watchdog.expired(at=exit_observed_at)
                        if expired:
                            record_deadlines(expired)
                    failed |= process.returncode != 0 or bool(expired)
                    for identity in selected.values():
                        if identity.id not in results:
                            if identity.id in reports or identity.id in timeout_details:
                                finish(identity)
                            else:
                                results[identity.id] = TestResult(identity, 'cancelled' if cancelled else 'error')
                finally:
                    if process is not None:
                        try:
                            stop_owned()
                        finally:
                            if reader_started and not reader_joined:
                                reader.join(timeout=5)
                            if not reader_started or not reader.is_alive():
                                process.stdout.close()
            if has_code:
                try:
                    data = json.loads(coverage.read_text())
                    for file, row in sorted(data['files'].items()):
                        s = row['summary']
                        areas.append(CoverageArea(repo, file, s['covered_lines'], s['num_statements'], s.get('covered_branches', 0), s.get('num_branches', 0),
                                                  tuple(row.get('missing_lines', ())), tuple(tuple(edge) for edge in row.get('missing_branches', ()))))
                    if not data['files']:
                        failed = True
                    else:
                        retained.append(retain_report(repo, temporary, report_destinations[repo]))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    failed = True
                    raw(repo, f'Coverage report unavailable: {exc}\n')
        if cancelled:
            break
    for repo, root in roots.items():
        if repo in {test.repo for test in tests}:
            continue
        sources = _production_sources(context, root)
        if not sources:
            continue
        try:
            from coverage import Coverage
            with tempfile.TemporaryDirectory(prefix='api-agent-unselected-') as temporary:
                coverage = Coverage(source=[str(source) for source in sources], branch=True, config_file=False,
                                    data_file=str(Path(temporary) / '.coverage'))
                coverage.set_option("run:disable_warnings", ["no-data-collected"])
                coverage.start()
                coverage.stop()
                coverage.save()
                report = Path(temporary) / 'coverage.json'
                files = [str(path) for source in sources for path in source.rglob('*.py')]
                coverage.json_report(morfs=files, omit=['*/tests/*'], outfile=str(report))
                coverage.html_report(morfs=files, omit=['*/tests/*'], directory=str(Path(temporary) / 'html'))
                data = json.loads(report.read_text())
                for file, row in sorted(data['files'].items()):
                    summary = row['summary']
                    areas.append(CoverageArea(repo, file, summary['covered_lines'], summary['num_statements'],
                                              summary.get('covered_branches', 0), summary.get('num_branches', 0),
                                              tuple(row.get('missing_lines', ())), tuple(tuple(edge) for edge in row.get('missing_branches', ()))))
                retained.append(retain_report(repo, temporary, report_destinations[repo]))
        except Exception as exc:
            failed = True
            raw(repo, f'Coverage report unavailable for unselected owner: {exc}\n')
    for test in tests:
        results.setdefault(test.id, TestResult(test, 'cancelled' if cancelled else 'error'))
    failed |= cancelled or any(r.status != 'passed' for r in results.values()) or (require_full_coverage and any(not a.complete for a in areas))
    result = UnitRunResult(130 if cancelled else int(failed), tuple(results[t.id] for t in tests), tuple(areas), ''.join(chunks), cancelled, require_full_coverage, time.monotonic() - started_at,
                           tuple(retained), coverage_full)
    emit(RunnerEvent('finished', status='failed' if result.exit_code else 'passed'))
    return result


def format_unit_summary(result, *, include_elapsed=True):
    """The same precise final accounting for terminal and interactive views."""
    passed = sum(test.status == 'passed' for test in result.tests)
    coverage_only_failure = (bool(result.tests) and passed == len(result.tests)
                             and result.require_full_coverage
                             and any(not area.complete for area in result.coverage))
    state = ('CANCELLED' if result.cancelled else 'PASS' if result.exit_code == 0
             else 'Coverage FAIL' if coverage_only_failure else 'FAIL')
    lines = [format_test_summary('unit', result.tests, state=state,
                               elapsed=result.elapsed if include_elapsed else None)]
    if not result.require_full_coverage:
        lines.append('Partial selection: coverage is informational.')
    lines.extend(coverage_table(result.coverage, full=result.coverage_full))
    for report in result.coverage_reports:
        lines.extend((f'{report.repo} coverage JSON: {report.json_path}',
                      f'{report.repo} coverage HTML: {report.html_path}'))
    for repo in dict.fromkeys(test.identity.repo for test in result.tests):
        if not any(area.repo == repo for area in result.coverage):
            lines.append(f'{repo}: no measured code files (or coverage report unavailable; see output).')
    return '\n'.join(lines)
