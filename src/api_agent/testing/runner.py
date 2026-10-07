"""Owned pytest unit execution and fixed, per-file production coverage gates."""
from .contracts import TestResult, RunnerEvent
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

from .collection import CollectionError, TestIdentity, collect_tests
from .processes import finalize


@dataclass(frozen=True)
class CoverageArea:
    repo: str
    file: str
    covered_lines: int
    num_statements: int
    covered_branches: int
    num_branches: int

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
                  traceback=str(report.longrepr) if report.failed else '')
status = pytest.main(sys.argv[2:], plugins=[Events()])
import os
if os.environ.get('API_AGENT_COVERAGE_OUTPUT'):
    from pathlib import Path
    from coverage import Coverage
    coverage = Coverage(data_file=os.environ['COVERAGE_FILE'], config_file=False)
    coverage.load()
    coverage.json_report(morfs=[str(path) for source in json.loads(os.environ['API_AGENT_COVERAGE_SOURCE'])
                               for path in Path(source).rglob('*.py')],
                         outfile=os.environ['API_AGENT_COVERAGE_OUTPUT'])
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


def run_unit_tests(context, tests, *, on_event=None, output=None, cancel_event=None, parallel_jobs=0, require_full_coverage=True):
    """Run selected collected identities once; preserve owner configs and full source scope.

    Callbacks run on the calling thread. Cancellation terminates and reaps only
    owned process groups. Nonzero also signals skipped/incomplete selected tests.
    """
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
    roots = {base.name: base, owner.name: owner}
    if base != owner and base.name == owner.name:
        roots = {'base': base, 'pack': owner}
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join([str(base / 'src'), str(owner / 'src'), env.get('PYTHONPATH', '')])
    env['PYTHONUNBUFFERED'] = '1'
    chunks, results, areas = [], {}, []
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
            args = [sys.executable, '-c', _PLUGIN, str(events), '-o', 'addopts=', '--rootdir', str(root)]
            sources = _production_sources(context, root)
            has_code = bool(sources)
            env.pop('API_AGENT_COVERAGE_OUTPUT', None)
            env.pop('API_AGENT_COVERAGE_SOURCE', None)
            if has_code:
                config = Path(temporary) / 'coverage.ini'
                config.write_text('[run]\nbranch = true\n[report]\ninclude_namespace_packages = true\n')
                env['API_AGENT_COVERAGE_OUTPUT'] = str(coverage)
                env['API_AGENT_COVERAGE_SOURCE'] = json.dumps([str(source) for source in sources])
                args += ['--cov-config=' + str(config)]
                args += ['--cov=' + str(source) for source in sources]
                args += ['--cov-branch', '--cov-report=term-missing', '--cov-report=json:' + str(coverage)]
                env['COVERAGE_FILE'] = str(Path(temporary) / '.coverage')
            if parallel_jobs:
                args += ['-n', str(parallel_jobs)]
            args.extend(selected)
            try:
                process = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, start_new_session=os.name == 'posix')
            except OSError as exc:
                raw(repo, f'Cannot start pytest: {exc}\n')
                failed = True
                continue
            lines = queue.Queue()
            def read_output():
                for line in process.stdout:
                    lines.put(line)
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            offset = 0
            reports = {}
            def drain():
                nonlocal offset
                while not lines.empty():
                    raw(repo, lines.get_nowait())
                with events.open(encoding='utf-8') as stream:
                    stream.seek(offset)
                    while True:
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
                            emit(RunnerEvent('test_started', repo, identity))
                        else:
                            reports.setdefault(identity.id, []).append(row)
                            if row['phase'] == 'teardown':
                                finish(identity)
                    offset = stream.tell()
            def finish(identity):
                rows = reports.get(identity.id, [])
                status = 'failed' if any(r['status'] == 'failed' for r in rows) else 'skipped' if any(r['status'] == 'skipped' for r in rows) else 'passed' if any(r['phase'] == 'call' for r in rows) else 'error'
                result = TestResult(identity, status, sum(r['duration'] for r in rows), '\n'.join(r['traceback'] for r in rows if r['traceback']))
                results[identity.id] = result
                emit(RunnerEvent('test_finished', repo, identity, status, result.duration, result.traceback))
            try:
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        _stop_execution(process)
                        break
                    drain()
                    time.sleep(.03)
                # Successful pytest can leave children attached to capture files
                # as well as stdout. Clean the entire owned group on every exit.
                _stop_execution(process)
                reader.join(timeout=1)
                drain()
            except BaseException:
                _stop_execution(process)
                reader.join(timeout=5)
                raise
            finally:
                if not reader.is_alive():
                    process.stdout.close()
            failed |= process.returncode != 0
            for identity in selected.values():
                if identity.id not in results:
                    if identity.id in reports:
                        finish(identity)
                    else:
                        results[identity.id] = TestResult(identity, 'cancelled' if cancelled else 'error')
            if has_code:
                try:
                    data = json.loads(coverage.read_text())
                    for file, row in sorted(data['files'].items()):
                        s = row['summary']
                        areas.append(CoverageArea(repo, file, s['covered_lines'], s['num_statements'], s.get('covered_branches', 0), s.get('num_branches', 0)))
                    if not data['files']:
                        failed = True
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
                report = Path(temporary) / 'coverage.json'
                coverage.json_report(morfs=[str(path) for source in sources for path in source.rglob('*.py')], outfile=str(report))
                data = json.loads(report.read_text())
                for file, row in sorted(data['files'].items()):
                    summary = row['summary']
                    areas.append(CoverageArea(repo, file, summary['covered_lines'], summary['num_statements'],
                                              summary.get('covered_branches', 0), summary.get('num_branches', 0)))
        except Exception as exc:
            failed = True
            raw(repo, f'Coverage report unavailable for unselected owner: {exc}\n')
    for test in tests:
        results.setdefault(test.id, TestResult(test, 'cancelled' if cancelled else 'error'))
    failed |= cancelled or any(r.status != 'passed' for r in results.values()) or (require_full_coverage and any(not a.complete for a in areas))
    for area in areas:
        raw(area.repo, f'{area.repo}::{area.file}: lines {area.line_percent:.2f}% ({area.covered_lines}/{area.num_statements}), branches {area.branch_percent:.2f}% ({area.covered_branches}/{area.num_branches})\n')
    result = UnitRunResult(130 if cancelled else int(failed), tuple(results[t.id] for t in tests), tuple(areas), ''.join(chunks), cancelled, require_full_coverage)
    emit(RunnerEvent('finished', status='failed' if result.exit_code else 'passed'))
    return result


def format_unit_summary(result):
    """The same precise final accounting for terminal and interactive views."""
    passed = sum(test.status == 'passed' for test in result.tests)
    coverage_only_failure = (bool(result.tests) and passed == len(result.tests)
                             and result.require_full_coverage
                             and any(not area.complete for area in result.coverage))
    state = ('CANCELLED' if result.cancelled else 'PASS' if result.exit_code == 0
             else 'Coverage FAIL' if coverage_only_failure else 'FAIL')
    lines = [f'Unit results: {state} — {passed}/{len(result.tests)} passed; {len(result.tests) - passed} did not pass.',
             ('Production coverage requires every measured file to reach 100% lines and branches.'
              if result.require_full_coverage else 'Partial selection: coverage is informational.')]
    if result.coverage and all(area.complete for area in result.coverage):
        lines.append('Production coverage: 100% lines and branches across all measured files.')
    for area in result.coverage:
        if area.complete:
            continue
        lines.append(f'{area.repo}::{area.file}: lines {area.line_percent:.2f}% ({area.covered_lines}/{area.num_statements}), branches {area.branch_percent:.2f}% ({area.covered_branches}/{area.num_branches})' + (' [below 100%]' if not area.complete else ''))
    for repo in dict.fromkeys(test.identity.repo for test in result.tests):
        if not any(area.repo == repo for area in result.coverage):
            lines.append(f'{repo}: no measured production files (or coverage report unavailable; see output).')
    return '\n'.join(lines)
