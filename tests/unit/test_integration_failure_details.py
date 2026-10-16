"""Runner errors retain safe causes for every selected test."""
import json
from types import SimpleNamespace
import pytest
from api_agent.testing.collection import collect_tests
from api_agent.testing.integration import run_integration_tests
from api_agent.testing.recording import RunIdentity, RunRecorder
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.tui.test_results import test_details as render_details

@pytest.fixture
def selection(tmp_path):
    suite = tmp_path / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_sample.py').write_text('def test_first():\n    pass\ndef test_second():\n    pass\n')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, suite_paths=lambda mode: (suite,))
    return ctx, collect_tests(ctx, 'integration')

@pytest.mark.parametrize('failure,stage,category', [
    ('before_run', 'Integration prerequisites (before_run)', 'deadline'),
    ('collection', 'Integration collection', 'unavailable'),
    ('spawn', 'Integration pytest process startup', 'unavailable'),
    ('backend', 'Integration shared setup backend startup', 'deadline'),
])
def test_safe_cause_reaches_each_result_and_recording(selection, monkeypatch, tmp_path, failure, stage, category):
    ctx, tests = selection
    secret = 'SECRET_SENTINEL /private/customer/path credential=hidden'
    def fail(*args, **kwargs):
        raise (TimeoutError if category == 'deadline' else OSError)(secret)
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', fail if failure == 'collection' else lambda *a, **k: tests)
    kwargs = {'before_run': fail} if failure == 'before_run' else {}
    if failure == 'spawn':
        monkeypatch.setattr('api_agent.testing.integration.subprocess.Popen', fail)
    if failure == 'backend':
        monkeypatch.setattr('api_agent.testing.setup_backend.SetupBackend.start', fail)
        kwargs['config'] = {}
    events = []
    result = run_integration_tests(ctx, tests, accepted=True, on_event=events.append, **kwargs)
    recorder = RunRecorder(tmp_path / 'report', RunIdentity.create(tests[0].repo, 'integration'), evidence_boundary=EmissionBoundary(ContentPolicy(True)))
    assert result.exit_code == 1 and len(result.tests) == 2
    for row in result.tests:
        assert row.status == 'error'
        assert f'{stage} failure category: {category}' in row.traceback
        assert 'api_agent/testing/integration.py:' in row.traceback
        assert secret not in row.traceback and str(tmp_path) not in row.traceback
        assert secret in row.local_diagnostic
        details = render_details(row, {}, {}).plain
        assert row.local_diagnostic in details
        assert details.index(stage) < details.index('Execution time:')
        recorder.record_test(row)
    assert [event.traceback for event in events if event.kind == 'test_finished'] == [row.traceback for row in result.tests]
    for file in (tmp_path / 'report').glob('*-result.json'):
        assert stage in json.loads(file.read_text())['statusDetails']['trace']
    assert secret not in result.output

@pytest.mark.parametrize('teardown_only', [False, True])
def test_no_reports_include_worker_exit(selection, monkeypatch, teardown_only):
    ctx, tests = selection
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: tests)
    class Process:
        returncode = 4
        stdout = SimpleNamespace(close=lambda: None)
        def poll(self):
            return 4
    class Reader:
        def __init__(self, **kwargs): pass
        def start(self): pass
        def join(self, **kwargs): pass
        def is_alive(self): return False
    def spawn(args, **kwargs):
        if teardown_only:
            from pathlib import Path
            Path(args[3]).write_text(''.join(json.dumps(dict(kind='report', node=test.node_id, phase='teardown', status='passed', duration=0, traceback='')) + '\n' for test in tests))
        return Process()
    monkeypatch.setattr('api_agent.testing.integration.subprocess.Popen', spawn)
    monkeypatch.setattr('api_agent.testing.integration.threading.Thread', Reader)
    monkeypatch.setattr('api_agent.testing.integration.finalize', lambda process: None)
    finished, events = [], []
    result = run_integration_tests(ctx, tests, accepted=True, on_result=finished.append, on_event=events.append)
    assert finished == list(result.tests)
    assert all('report incomplete' in row.traceback for row in result.tests)
    assert all('worker exit code 4' in row.traceback for row in result.tests)
    assert [event.traceback for event in events if event.kind == 'test_finished'] == [row.traceback for row in result.tests]
    recorder = RunRecorder(ctx.base_root / 'report', RunIdentity.create(tests[0].repo, 'integration'), evidence_boundary=EmissionBoundary(ContentPolicy(True)))
    for row in result.tests:
        filename = recorder.record_test(row)
        trace = json.loads((ctx.base_root / 'report' / filename).read_text())['statusDetails']['trace']
        assert 'worker exit code 4' in trace

def test_legacy_result_explains_missing_diagnostic(selection):
    from api_agent.testing.contracts import TestResult
    _, tests = selection
    assert 'No failure diagnostic was captured' in render_details(TestResult(tests[0], 'error', 0, ''), {}, {}).plain

def test_two_synthetic_pytest_failures_keep_real_causes(selection):
    ctx, _ = selection
    (ctx.base_root / 'tests/integration/test_sample.py').write_text('def test_first():\n    assert False, "synthetic first cause"\ndef test_second():\n    raise ValueError("synthetic second cause")\n')
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True)
    assert result.exit_code == 1
    assert [row.status for row in result.tests] == ['failed', 'failed']
    assert 'synthetic first cause' in result.tests[0].traceback
    assert 'synthetic second cause' in result.tests[1].traceback
    for row in result.tests:
        details = render_details(row, {}, {}).plain
        assert details.index(row.traceback) < details.index('Execution time:')


@pytest.mark.parametrize('explicit_cause', [False, True, 'suppressed'])
def test_local_unknown_failure_preserves_chain_without_exporting_it(selection, monkeypatch, explicit_cause):
    from dataclasses import replace
    ctx, tests = selection
    local_message = 'synthetic local startup cause'
    command_evidence = 'synthetic command stderr tail'
    frame_local = 'FRAME_LOCAL_ONLY_SENTINEL'
    def fail():
        internal_value = frame_local
        try:
            raise ValueError(local_message)
        except ValueError as cause:
            cause.local_details = command_evidence + 'x' * 20000
            error = RuntimeError('synthetic outer startup failure')
            if explicit_cause == 'suppressed':
                raise error from None
            if explicit_cause:
                raise error from cause
            raise error
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: tests)
    events = []
    run = run_integration_tests(ctx, tests, accepted=True, before_run=fail, on_event=events.append)
    for row in run.tests:
        assert 'failure category: unknown' in row.traceback
        assert local_message not in row.traceback
        assert 'RuntimeError: synthetic outer startup failure' in row.local_diagnostic
        if explicit_cause == 'suppressed':
            assert 'Suppressed exception context' in row.local_diagnostic
        assert 'ValueError: ' + local_message in row.local_diagnostic
        assert command_evidence in row.local_diagnostic
        assert 'x' * 20000 in row.local_diagnostic
        assert frame_local not in row.local_diagnostic
        assert row.local_diagnostic not in repr(row)
        assert row == replace(row, local_diagnostic='')
        display = render_details(row, {}, {}).plain
        assert display.index(local_message) < display.index('Execution time:')
    assert local_message not in run.output and command_evidence not in run.output
    assert all(local_message not in event.traceback and local_message not in event.text for event in events)
    recorder = RunRecorder(ctx.base_root / 'report', RunIdentity.create(tests[0].repo, 'integration'), evidence_boundary=EmissionBoundary(ContentPolicy(True)))
    for row in run.tests:
        filename = recorder.record_test(row)
        persisted = (ctx.base_root / 'report' / filename).read_text()
        assert 'failure category: unknown' in persisted
        assert local_message not in persisted and command_evidence not in persisted


def test_incomplete_report_retains_only_local_worker_tail(selection, monkeypatch):
    ctx, tests = selection
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: tests)
    script = 'import sys\nprint("x" * 20000)\nprint("synthetic worker startup cause")\nsys.exit(4)\n'
    monkeypatch.setattr('api_agent.testing.integration._PLUGIN', script)
    events = []
    run = run_integration_tests(ctx, tests, accepted=True, on_event=events.append)
    assert run.exit_code == 1
    for row in run.tests:
        assert 'synthetic worker startup cause' in row.local_diagnostic
        assert 'earlier characters omitted]' in row.local_diagnostic
        assert 'synthetic worker startup cause' not in row.traceback
        assert 'x' * 16385 not in row.local_diagnostic
        assert 'worker exit code 4' in row.traceback
    assert all('synthetic worker startup cause' not in event.traceback for event in events)
    recorder = RunRecorder(ctx.base_root / 'report', RunIdentity.create(tests[0].repo, 'integration'), evidence_boundary=EmissionBoundary(ContentPolicy(True)))
    for row in run.tests:
        filename = recorder.record_test(row)
        assert 'synthetic worker startup cause' not in (ctx.base_root / 'report' / filename).read_text()


@pytest.mark.parametrize('callback', ['on_result', 'after_run', 'on_event', 'output'])
def test_swallowed_callbacks_retain_local_cause_without_changing_test_status(selection, callback):
    ctx, tests = selection
    message = 'synthetic local callback cause'
    def fail(*args):
        raise RuntimeError(message)
    run = run_integration_tests(ctx, tests, accepted=True, **{callback: fail})
    assert run.exit_code == 1
    assert all(row.status == 'passed' for row in run.tests)
    assert all(message in row.local_diagnostic for row in run.tests)
    for row in run.tests:
        details = render_details(row, {}, {}).plain
        assert details.index(message) < details.index('Execution time:')
    assert message not in run.output
    assert all(not row.traceback for row in run.tests)


def test_backend_cleanup_failure_retains_local_cause_after_test_finish(selection, monkeypatch):
    ctx, tests = selection
    from api_agent.testing.setup_backend import SetupBackend
    def close(self):
        raise RuntimeError('synthetic local backend cleanup cause')
    monkeypatch.setattr(SetupBackend, 'start', lambda self: None)
    monkeypatch.setattr(SetupBackend, 'close', close)
    published = []
    with pytest.raises(RuntimeError, match='synthetic local backend cleanup cause') as caught:
        run_integration_tests(ctx, tests, accepted=True, config={}, after_run=published.append)
    run = caught.value.execution
    assert published == []
    assert run.inspections and caught.value.inspections == run.inspections
    assert run.exit_code == 1 and all(row.status == 'passed' for row in run.tests)
    assert all('synthetic local backend cleanup cause' in row.local_diagnostic for row in run.tests)
    assert 'synthetic local backend cleanup cause' not in run.output
    # The synthetic failed cleanup handle stays owned until an explicit safe close.
    monkeypatch.setattr(SetupBackend, 'close', lambda self: None)
    for handle in run.inspections:
        handle.close()
        handle.closed = True


def test_failed_pytest_keeps_original_trace_with_supplemental_local_callback(selection):
    ctx, _ = selection
    (ctx.base_root / 'tests/integration/test_sample.py').write_text('def test_failed():\n    assert False, "synthetic original pytest cause"\n')
    def fail(result):
        raise RuntimeError('synthetic supplemental callback cause')
    run = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True, on_result=fail)
    row = run.tests[0]
    assert row.status == 'failed'
    details = render_details(row, {}, {}).plain
    assert 'synthetic original pytest cause' in details
    assert 'RuntimeError: synthetic supplemental callback cause' in details
    assert row.traceback in row.local_diagnostic
    assert 'synthetic supplemental callback cause' not in row.traceback


def test_recorded_prepare_local_cause_does_not_enter_any_persisted_file(selection):
    from api_agent.testing.recorded import run_recorded_integration
    ctx, tests = selection
    message = 'synthetic recorded local prepare cause'
    class Storage:
        def persist(self, directory, manifest):
            pass
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': ctx.base_root / 'artifacts', 'TEST_PARALLEL_JOBS': 0}
    def prepare(usage):
        raise RuntimeError(message)
    result = run_recorded_integration(ctx, tests, config, accepted=True,
        prepare=prepare, storage=Storage())
    assert result.exit_code == 1 and result.persisted
    assert all(message in row.local_diagnostic for row in result.execution.tests)
    for path in result.directory.iterdir():
        if path.is_file():
            assert message not in path.read_text()


def test_nested_suppressed_context_keeps_inner_chain(selection, monkeypatch):
    ctx, tests = selection
    def fail():
        try:
            try:
                raise KeyError('synthetic innermost cause')
            except KeyError:
                raise ValueError('synthetic intermediate cause')
        except ValueError:
            raise RuntimeError('synthetic outer suppressed cause') from None
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: tests)
    run = run_integration_tests(ctx, tests, accepted=True, before_run=fail)
    for row in run.tests:
        assert 'Suppressed exception context' in row.local_diagnostic
        for message in ('synthetic innermost cause', 'synthetic intermediate cause', 'synthetic outer suppressed cause'):
            assert message in row.local_diagnostic
            assert message not in row.traceback and message not in run.output


def test_local_exception_cycle_is_finite_and_nontext_details_are_not_formatted(selection, monkeypatch):
    ctx, tests = selection
    error = RuntimeError('synthetic cyclic local cause')
    error.__cause__ = error
    error.local_details = object()
    def fail():
        raise error
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: tests)
    run = run_integration_tests(ctx, tests, accepted=True, before_run=fail)
    assert run.exit_code == 1
    for row in run.tests:
        assert row.local_diagnostic.count('RuntimeError: synthetic cyclic local cause') == 1
        assert 'Local command evidence' not in row.local_diagnostic


def test_late_prior_suite_reader_failure_does_not_contaminate_next_suite(tmp_path, monkeypatch):
    import os
    from api_agent.testing.collection import TestIdentity
    roots = [tmp_path / 'base', tmp_path / 'pack']
    for root in roots:
        root.mkdir()
    tests = tuple(TestIdentity(root.name + '::test.py::test_ok', root.name,
        'test.py', 'test.py::test_ok', root=root) for root in roots)
    ctx = SimpleNamespace(base_root=roots[0], owner_root=roots[1])
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: tests)
    readers = []
    class Stream:
        def __init__(self, first): self.first = first
        def __iter__(self):
            if self.first:
                raise OSError('synthetic late first-suite reader cause')
            return iter(())
        def close(self): pass
    class Reader:
        def __init__(self, target, **kwargs): self.target = target
        def start(self):
            readers.append(self)
            if len(readers) == 2:
                readers[0].target()
                self.target()
        def join(self, **kwargs): pass
        def is_alive(self): return False
    def spawn(args, cwd, **kwargs):
        first = cwd == roots[0]
        rows = [dict(kind='report', node='test.py::test_ok', phase=phase,
            status='passed', duration=0, traceback='') for phase in ('setup', 'call', 'teardown')]
        rows.append(dict(kind='run_complete', exit_code=0))
        if first:
            rows.append(dict(kind='inspection_open', endpoint='synthetic'))
        from pathlib import Path
        Path(args[3]).write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return SimpleNamespace(stdout=Stream(first), returncode=None if first else 0,
            poll=lambda: None if first else 0)
    def retain(process, directory, event_file, reader, registry, liveness_fd, **kwargs):
        directory.retained = True
        def abort():
            directory.temporary.cleanup()
            if liveness_fd is not None:
                os.close(liveness_fd)
        registry.append(SimpleNamespace(abort=abort))
    monkeypatch.setattr('api_agent.testing.integration.subprocess.Popen', spawn)
    monkeypatch.setattr('api_agent.testing.integration.threading.Thread', Reader)
    monkeypatch.setattr('api_agent.testing.integration.finalize', lambda process: None)
    monkeypatch.setattr('api_agent.testing.inspection.IntegrationInspection', retain)
    run = run_integration_tests(ctx, tests, accepted=True)
    try:
        assert [row.status for row in run.tests] == ['passed', 'passed']
        assert 'synthetic late first-suite reader cause' in run.tests[0].local_diagnostic
        assert 'synthetic late first-suite reader cause' not in run.tests[1].local_diagnostic
    finally:
        for handle in run.inspections:
            handle.abort()


def test_grouped_startup_cause_and_child_command_evidence_reach_captured_result(selection, monkeypatch):
    from test_terminal_diagnostics import grouped_error
    ctx, tests = selection
    error = grouped_error()
    def fail():
        raise error
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: tests)
    run = run_integration_tests(ctx, tests, accepted=True, before_run=fail)
    for row in run.tests:
        assert 'synthetic sixteenth actual cause' in row.local_diagnostic
        assert 'synthetic suppressed child cause' in row.local_diagnostic
        assert 'child command exit 1 stdout cause stderr specific cause' in row.local_diagnostic
        assert 'synthetic sixteenth actual cause' not in row.traceback


@pytest.mark.parametrize('parallel_jobs', [0, 2])
@pytest.mark.parametrize('outcome', ['failed', 'skipped'])
def test_setup_terminal_report_is_complete_without_call(selection, parallel_jobs, outcome):
    ctx, _ = selection
    action = 'raise RuntimeError("setup witness")' if outcome == 'failed' else 'pytest.skip("setup witness")'
    (ctx.base_root / 'tests/integration/test_sample.py').write_text(
        'import pytest\n@pytest.fixture\ndef prerequisite():\n    ' + action +
        '\ndef test_sample(prerequisite):\n    pytest.fail("test body must not run")\n')
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'),
                                   accepted=True, parallel_jobs=parallel_jobs)
    row, = result.tests
    assert row.status == ('error' if outcome == 'failed' else 'skipped')
    assert 'setup witness' in row.traceback
    assert 'report incomplete' not in row.traceback + row.local_diagnostic
    assert 'test body must not run' not in row.traceback
