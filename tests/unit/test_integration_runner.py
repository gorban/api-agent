from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from api_agent.testing.collection import CollectionError, collect_tests
from api_agent.testing.integration import run_integration_tests


def stdout_boundary_from_launch(args):
    """Decode the final bootstrap call argument for controlled process doubles."""
    import ast
    call = ast.parse(args[2]).body[-1].value
    return ast.literal_eval(call.args[0])


def fixture_repo(root, test='def test_ok():\n    assert True\n'):
    suite = root / 'tests/integration'
    suite.mkdir(parents=True)
    (suite / 'test_sample.py').write_text(test)
    (root / 'pytest.ini').write_text('[pytest]\naddopts = --invalid-option\n')
    return root


def context(base, owner=None):
    owner = owner or base
    return SimpleNamespace(base_root=base, owner_root=owner,
                           suite_paths=lambda mode: (owner / 'tests/integration',) if mode == 'integration' else (base / 'tests/unit',))


@pytest.mark.parametrize('jobs', [0, 2])
def test_component_progress_is_bound_to_test_in_serial_and_parallel_workers(tmp_path, jobs):
    base = fixture_repo(tmp_path / 'base', '''
import asyncio
import pytest
from api_agent.providers.progress import report_timeout, component_timeout
@pytest.mark.parametrize('n', [0, 1])
def test_timed(n):
    async def run():
        async with report_timeout('Parent component', 5):
            async with component_timeout('Synthetic component', .02):
                await asyncio.sleep(60)
    asyncio.run(run())
''')
    ctx = context(base)
    tests = collect_tests(ctx, 'integration')
    events = []
    result = run_integration_tests(ctx, tests, accepted=True, parallel_jobs=jobs, on_event=events.append)
    assert result.exit_code == 1 and all(test.status == 'failed' for test in result.tests)
    for test in tests:
        scopes = [event.component for event in events
                if event.kind == 'component_progress' and event.identity == test]
        rows = [scope for scope in scopes if scope.name == 'Synthetic component']
        parents = [scope for scope in scopes if scope.name == 'Parent component']
        assert len(parents) == 2 and parents[0].active and not parents[-1].active
        assert parents[0].timeout == 5 and parents[0].parent_id is None
        assert len(rows) == 2 and rows[0].active and not rows[-1].active
        assert rows[0].id == rows[-1].id and rows[0].timeout == .02
        assert rows[0].parent_id == rows[-1].parent_id == parents[0].id
        assert rows[-1].elapsed >= .02


def test_cli_test_cannot_redirect_integration_outcomes_by_changing_argv(tmp_path):
    base = fixture_repo(tmp_path / 'base', 'import sys\n'
        'def test_cli(monkeypatch):\n'
        '    monkeypatch.setattr(sys, "argv", ["app", "unexpected-events.jsonl"])\n')
    ctx = context(base)
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True)
    assert result.exit_code == 0 and [test.status for test in result.tests] == ['passed']
    assert not (base / 'unexpected-events.jsonl').exists()


def test_skip_reporting_marker_reaches_selected_pytest_worker(tmp_path):
    base = fixture_repo(tmp_path / 'base',
        'import os\ndef test_marker():\n'
        '    assert os.environ.get("API_AGENT_ALLURE_REPORTING") == "0"\n')
    ctx = context(base)
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'),
        accepted=True, config={'SKIP_ALLURE_REPORTING': True}, parallel_jobs=0)
    assert result.exit_code == 0 and [row.status for row in result.tests] == ['passed']


def test_refusal_happens_before_collection_and_callbacks(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    selected = collect_tests(context(base), 'integration')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: pytest.fail('collected'))
    for kwargs in ({}, {'accepted': True, 'no_std_in': True}):
        with pytest.raises(PermissionError):
            run_integration_tests(context(base), selected, before_run=lambda: pytest.fail('started'), **kwargs)


def test_selection_requires_fresh_integration_identity(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    tests = collect_tests(ctx, 'integration')
    (base / 'tests/integration/test_sample.py').write_text('def test_changed():\n    pass\n')
    with pytest.raises(CollectionError, match='outside'):
        run_integration_tests(ctx, tests, accepted=True)


def test_success_reports_phases_hooks_and_raw_output(tmp_path):
    base = fixture_repo(tmp_path / 'base', 'def test_ok():\n    print("raw evidence")\n    assert True\n')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    events, output, calls = [], [], []
    result = run_integration_tests(ctx, selected, accepted=True, parallel_jobs=0,
                                   before_run=lambda: calls.append('before'),
                                   on_result=lambda row: calls.append(row.status),
                                   after_run=lambda row: calls.append(('after', row.exit_code)),
                                   on_event=events.append, output=output.append)
    assert result.exit_code == 0
    assert result.tests[0].status == 'passed'
    assert result.tests[0].duration >= 0
    assert calls == ['before', 'passed', ('after', 0)]
    assert ''.join(output) == result.output
    assert {row.kind for row in events} >= {'started', 'test_started', 'test_finished', 'finished', 'output'}


def test_failure_skip_and_setup_error_never_pass(tmp_path):
    base = fixture_repo(tmp_path / 'base', 'import pytest\ndef test_bad():\n    assert False, "multiline\\nsecret"\n@pytest.mark.skip(reason="deliberate")\ndef test_skip():\n    pass\n@pytest.fixture\ndef broken():\n    raise RuntimeError("fixture error")\ndef test_setup(broken):\n    pass\n')
    ctx = context(base)
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True)
    assert result.exit_code == 1
    statuses = {row.identity.node_id.split('::')[-1]: row.status for row in result.tests}
    assert statuses == {'test_bad': 'failed', 'test_skip': 'skipped', 'test_setup': 'error'}
    assert 'multiline' in result.tests[0].traceback
    assert 'secret' in result.tests[0].traceback


@pytest.mark.parametrize('debug', [False, True])
def test_tracebacks_keep_cause_without_default_source_dump(tmp_path, debug):
    base = fixture_repo(tmp_path / 'base', '''
def test_bad():
    """UNNECESSARY_SOURCE_BODY_MARKER"""
    raise ValueError('synthetic useful cause')
''')
    ctx = context(base)
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True,
                                   config={'DEBUG': debug, 'LLM_PROVIDER': '', 'UI_PROVIDER': ''})
    trace = result.tests[0].traceback
    assert result.exit_code == 1 and result.tests[0].status == 'failed'
    assert 'ValueError: synthetic useful cause' in trace
    assert 'test_sample.py' in trace
    assert ('UNNECESSARY_SOURCE_BODY_MARKER' in trace) is debug


@pytest.mark.parametrize('jobs', [0, 2])
@pytest.mark.parametrize('all_skipped', [False, True])
def test_intentional_skips_are_neutral_and_preserve_each_reason(tmp_path, jobs, all_skipped):
    source = 'import pytest\n@pytest.mark.skip(reason="first requirement unavailable")\ndef test_first():\n    pass\n'
    source += 'def test_second():\n    pytest.skip("second requirement unavailable")\n'
    if not all_skipped:
        source += 'def test_success():\n    assert True\n'
    base = fixture_repo(tmp_path / 'base', source)
    ctx = context(base)
    events = []
    run = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True,
                                parallel_jobs=jobs, on_event=events.append)
    assert run.exit_code == 0 and not run.cancelled
    assert [test.status for test in run.tests] == ['skipped', 'skipped'] + ([] if all_skipped else ['passed'])
    assert [test.traceback for test in run.tests[:2]] == ['first requirement unavailable', 'second requirement unavailable']
    assert sorted(event.traceback for event in events if event.kind == 'test_finished' and event.status == 'skipped') == ['first requirement unavailable', 'second requirement unavailable']
    assert events[-1].status == 'passed'


def test_cancellation_reaps_child_before_after_hook(tmp_path):
    base = fixture_repo(tmp_path / 'base', 'import subprocess, sys, time\nfrom pathlib import Path\ndef test_wait():\n    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n    Path("child.pid").write_text(str(child.pid))\n    time.sleep(60)\n')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    cancelled = threading.Event()
    observations = []
    def trigger():
        deadline = time.monotonic() + 15
        while not (base / 'child.pid').exists() and time.monotonic() < deadline:
            time.sleep(.03)
        cancelled.set()
    worker = threading.Thread(target=trigger)
    worker.start()
    result = run_integration_tests(ctx, selected, accepted=True, cancel_event=cancelled,
                                   after_run=lambda row: observations.append(row))
    worker.join()
    assert result.exit_code == 130 and result.cancelled
    assert observations == [result]
    child = int((base / 'child.pid').read_text())
    status = Path(f'/proc/{child}/stat')
    assert not status.exists() or status.read_text().split()[2] == 'Z'


def test_callback_failure_is_sanitized_and_nonzero(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    def fail(_):
        raise RuntimeError('private callback secret')
    result = run_integration_tests(ctx, selected, accepted=True, on_result=fail)
    assert result.exit_code == 1
    assert 'private callback secret' not in result.output
    assert 'on_result callback failed; details withheld' in result.output


def test_pack_owns_integration_suite(tmp_path):
    base = fixture_repo(tmp_path / 'base', 'def test_base():\n    pass\n')
    pack = fixture_repo(tmp_path / 'pack', 'def test_pack():\n    pass\n')
    ctx = context(base, pack)
    selected = collect_tests(ctx, 'integration')
    assert len(selected) == 2 and {test.repo for test in selected} == {'base', 'pack'}
    result = run_integration_tests(ctx, selected, accepted=True)
    assert result.exit_code == 0 and {test.identity.repo for test in result.tests} == {'base', 'pack'}

@pytest.mark.parametrize('jobs', [-1, True, 1.5])
def test_invalid_worker_count(tmp_path, jobs):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    with pytest.raises(ValueError):
        run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True, parallel_jobs=jobs)


def test_empty_selection_rejected(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    with pytest.raises(CollectionError, match='empty'):
        run_integration_tests(context(base), [], accepted=True)


def test_collection_failure_finalizes_missing_as_errors(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    def fail(*args, **kwargs):
        raise RuntimeError('secret collection error')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', fail)
    after = []
    result = run_integration_tests(ctx, selected, accepted=True, after_run=after.append)
    assert result.exit_code == 1 and result.tests[0].status == 'error'
    assert after == [result]
    assert 'secret' not in result.output


def test_before_run_failure_prevents_process_start(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: selected)
    monkeypatch.setattr('api_agent.testing.integration.subprocess.Popen', lambda *a, **k: pytest.fail('started'))
    result = run_integration_tests(ctx, selected, accepted=True,
                                   before_run=lambda: (_ for _ in ()).throw(RuntimeError('secret')))
    assert result.exit_code == 1 and result.tests[0].status == 'error'
    assert 'secret' not in result.output


def test_cancel_before_suite_start(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    cancel = threading.Event()
    cancel.set()
    result = run_integration_tests(ctx, selected, accepted=True, cancel_event=cancel)
    assert result.cancelled and result.exit_code == 130
    assert result.tests[0].status == 'cancelled'


def test_cancel_in_before_run(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    cancel = threading.Event()
    result = run_integration_tests(ctx, selected, accepted=True, cancel_event=cancel, before_run=cancel.set)
    assert result.cancelled and result.tests[0].status == 'cancelled'


def test_parallel_worker_outcomes(tmp_path):
    pytest.importorskip('xdist')
    base = fixture_repo(tmp_path / 'base', 'def test_one():\n    pass\ndef test_two():\n    pass\n')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    result = run_integration_tests(ctx, selected, accepted=True, parallel_jobs=2)
    assert result.exit_code == 0 and len(result.tests) == 2
    assert all(row.status == 'passed' for row in result.tests)


def test_output_and_after_callback_failure_is_sanitized(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    def fail(_):
        raise RuntimeError('private sink detail')
    result = run_integration_tests(ctx, selected, accepted=True, output=fail, after_run=fail)
    assert result.exit_code == 1
    assert 'private sink detail' not in result.output
    assert 'output callback failed' in result.output
    assert 'after_run callback failed' in result.output


def test_child_start_failure_returns_missing_error(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: selected)
    def fail(*args, **kwargs):
        raise OSError('private path')
    monkeypatch.setattr('api_agent.testing.integration.subprocess.Popen', fail)
    result = run_integration_tests(ctx, selected, accepted=True)
    assert result.exit_code == 1 and result.tests[0].status == 'error'
    assert 'private path' not in result.output


@pytest.mark.parametrize('invalid_events', [1, 2])
def test_synthetic_partial_and_corrupt_event_protocol(tmp_path, monkeypatch, invalid_events):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: selected)
    node = selected[0].node_id
    script = '''import sys
p = sys.argv[1]
with open(p, 'a') as f:
 f.write('{"kind":"test_started","node":"unknown"}\\n')
 f.write('{bad json}\\n' * %s)
 f.write('{"kind":"unknown","node":"%s"}\\n')
 f.write('{"kind":"report","node":"%s","phase":"setup","status":"passed","duration":0.1,"traceback":""}\\n')
print('synthetic')
''' % (invalid_events, node, node)
    monkeypatch.setattr('api_agent.testing.integration._PLUGIN', script)
    result = run_integration_tests(ctx, selected, accepted=True)
    assert result.exit_code == 1 and result.tests[0].status == 'error'
    assert result.output.count('Invalid integration test event') == invalid_events
    assert result.tests[0].traceback.count('Integration report protocol failed: invalid pytest event received.') == 1
    assert 'JSONDecodeError:' in result.tests[0].local_diagnostic
    assert 'JSONDecodeError:' not in result.tests[0].traceback


def test_finished_event_callback_failure_changes_status(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    def fail_finished(event):
        if event.kind == 'finished':
            raise RuntimeError('secret')
    result = run_integration_tests(ctx, selected, accepted=True, on_event=fail_finished)
    assert result.exit_code == 1
    assert 'secret' not in result.output


def test_unexpected_runner_error_is_sanitized(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: selected)
    class BadContext:
        @property
        def base_root(self):
            raise RuntimeError('private path')
    result = run_integration_tests(BadContext(), selected, accepted=True)
    assert result.exit_code == 1 and result.tests[0].status == 'error'
    assert 'private path' not in result.output


def test_same_named_repositories_use_pack_root(tmp_path):
    base = fixture_repo(tmp_path / 'a' / 'same')
    pack = fixture_repo(tmp_path / 'b' / 'same')
    ctx = context(base, pack)
    selected = collect_tests(ctx, 'integration')
    assert {test.repo for test in selected} == {'base', 'pack'}
    result = run_integration_tests(ctx, selected, accepted=True)
    assert result.exit_code == 0


def test_cancel_during_collection_before_suite_loop(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    cancelled = threading.Event()
    def collect(*args, **kwargs):
        cancelled.set()
        return selected
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', collect)
    result = run_integration_tests(ctx, selected, accepted=True, cancel_event=cancelled)
    assert result.exit_code == 130 and result.tests[0].status == 'cancelled'


def test_unknown_owner_fails_closed(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    from dataclasses import replace
    synthetic = (replace(selected[0], id='other::' + selected[0].node_id, repo='other'),)
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: synthetic)
    result = run_integration_tests(ctx, synthetic, accepted=True)
    assert result.exit_code == 1 and result.tests[0].status == 'error'


def test_output_reader_oserror_is_reaped_and_accounted(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: selected)
    class BrokenPipe:
        def __iter__(self):
            raise OSError('closed pipe')
        def close(self):
            pass
    class FakeProcess:
        stdout = BrokenPipe()
        returncode = 0
        def poll(self):
            return 0
    monkeypatch.setattr('api_agent.testing.integration.subprocess.Popen', lambda *a, **k: FakeProcess())
    monkeypatch.setattr('api_agent.testing.integration.finalize', lambda process: None)
    result = run_integration_tests(ctx, selected, accepted=True)
    assert result.exit_code == 1 and result.tests[0].status == 'error'


def test_live_reader_timeout_still_finalizes(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: selected)
    class FakeProcess:
        returncode = 0
        stdout = object()
        def poll(self):
            return 0
    class FakeThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def join(self, timeout=None):
            pass
        def is_alive(self):
            return True
    monkeypatch.setattr('api_agent.testing.integration.subprocess.Popen', lambda *a, **k: FakeProcess())
    monkeypatch.setattr('api_agent.testing.integration.finalize', lambda process: None)
    monkeypatch.setattr('api_agent.testing.integration.threading.Thread', FakeThread)
    result = run_integration_tests(ctx, selected, accepted=True)
    assert result.exit_code == 1 and result.tests[0].status == 'error'


@pytest.mark.parametrize('accepted', [1, 'yes', object()])
def test_truthy_nonboolean_acceptance_is_rejected_before_collection(tmp_path, monkeypatch, accepted):
    base = fixture_repo(tmp_path / 'base')
    selected = collect_tests(context(base), 'integration')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: pytest.fail('collected'))
    with pytest.raises(PermissionError):
        run_integration_tests(context(base), selected, accepted=accepted,
                              before_run=lambda: pytest.fail('started'))


def test_after_run_failure_emits_failed_finished_event(tmp_path):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    events = []
    def fail_after(_):
        raise RuntimeError('private storage key')
    result = run_integration_tests(ctx, selected, accepted=True,
                                   on_event=events.append, after_run=fail_after)
    assert result.exit_code == 1
    assert result.tests[0].status == 'passed'
    assert events[-1].kind == 'finished' and events[-1].status == 'failed'
    assert 'private storage key' not in result.output
    assert 'after_run callback failed; details withheld' in result.output


def test_api_marker_survives_collection_and_blocks_runner_before_side_effects(tmp_path, monkeypatch):
    from unittest.mock import Mock
    base = fixture_repo(tmp_path / 'base', 'import pytest\n@pytest.mark.requires_pack_api\ndef test_api():\n    assert True\n')
    ctx = context(base)
    tests = collect_tests(ctx, 'integration')
    assert 'requires_pack_api' in tests[0].tags
    check = Mock(side_effect=ValueError('unavailable'))
    monkeypatch.setattr('api_agent.auth.extensions.validate_operation', check)
    monkeypatch.setattr('api_agent.testing.integration.subprocess.Popen', lambda *a, **k: pytest.fail('started'))
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: tests)
    calls = []
    result = run_integration_tests(ctx, tests, accepted=True, config={'TUI_MODE': 'integration'}, before_run=lambda: calls.append('before'))
    assert result.exit_code != 0 and result.tests[0].status == 'error'
    assert calls == []
    check.assert_called_once_with(ctx, {'TUI_MODE': 'integration'})


def test_debug_runs_parallel_workers_and_streams_output(tmp_path):
    base = fixture_repo(tmp_path / 'base',
        'def test_first():\n    print("inspection-first")\n'
        'def test_second():\n    print("inspection-second")\n')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    result = run_integration_tests(ctx, selected, accepted=True, parallel_jobs=2,
        config={'DEBUG': True})
    assert result.exit_code == 0 and all(test.status == 'passed' for test in result.tests)
    assert '2 workers [2 items]' in result.output


def test_debug_publication_callback_cannot_reenter_runner_operation(tmp_path):
    from api_agent.testing.suite_lock import SuiteBusyError
    base = fixture_repo(tmp_path / 'base', 'def test_ok():\n    assert True\n')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    refused = []

    def after(_):
        with pytest.raises(SuiteBusyError, match='already active'):
            run_integration_tests(ctx, selected, accepted=True, config={'DEBUG': True})
        refused.append(True)

    result = run_integration_tests(ctx, selected, accepted=True, config={'DEBUG': True}, after_run=after)
    assert result.exit_code == 0 and refused == [True]
    assert len(result.inspections) == 1 and result.inspections[0].closed


def test_debug_prepublication_failure_keeps_diagnostics_without_final_publication(tmp_path, monkeypatch):
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing.setup_backend import SetupBackend

    base = fixture_repo(tmp_path / 'base', 'def test_ok():\n    assert True\n')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    order = []
    published = []
    original_close = SetupBackend.close
    close_attempts = 0

    def fail_prepublication(owner):
        order.append('prepare')
        owner.pending = True
        raise owner._receipt()

    def interrupted_close(owner):
        nonlocal close_attempts
        close_attempts += 1
        order.append('close')
        if close_attempts == 1:
            raise RuntimeError('synthetic disposal interruption')
        return original_close(owner)

    monkeypatch.setattr(SetupBackend, 'prepare_publication', fail_prepublication)
    monkeypatch.setattr(SetupBackend, 'close', interrupted_close)

    def publish(result):
        order.append('after_run')
        published.append(result)

    with pytest.raises(CleanupPendingError) as caught:
        run_integration_tests(ctx, selected, accepted=True, config={'DEBUG': True}, after_run=publish)

    assert order == ['prepare', 'close']
    assert published == []
    diagnostic = caught.value.execution
    assert diagnostic.exit_code == 1
    assert [row.status for row in diagnostic.tests] == ['passed']
    assert 'Shared setup execution cleanup pending; retry required.' in diagnostic.output
    backend = diagnostic.inspections[0]
    assert caught.value.inspections[0] is backend
    from api_agent.testing.inspection_controls import InspectionGroup
    assert backend.pending and not backend.closed
    backend.close()
    assert order[-1] == 'close' and close_attempts == 2
    assert backend.closed and not backend.pending
    assert InspectionGroup(caught.value.inspections).close(force=True)


def test_nondebug_cleanup_failure_also_blocks_final_publication(tmp_path, monkeypatch):
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing.setup_backend import SetupBackend
    base = fixture_repo(tmp_path / 'base', 'def test_ok():\n    assert True\n')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    published, diagnostics = [], []
    original_close = SetupBackend.close
    blocked = True
    def cleanup(owner):
        if blocked:
            owner.pending = True
            raise owner._receipt()
        return original_close(owner)
    monkeypatch.setattr(SetupBackend, 'close', cleanup)
    with pytest.raises(CleanupPendingError) as caught:
        run_integration_tests(ctx, selected, accepted=True, config={'DEBUG': False},
            after_run=published.append, on_result=diagnostics.append)
    assert published == []
    assert len(diagnostics) == 1 and diagnostics[0].status == 'passed'
    assert caught.value.execution.exit_code == 1
    backend = caught.value.inspections[0]
    assert backend.pending and not backend.closed
    blocked = False
    backend.close()
    assert backend.closed and not backend.pending
    from api_agent.testing.inspection_controls import InspectionGroup
    assert InspectionGroup(caught.value.inspections).close(force=True)


def test_run_complete_transfers_live_worker_before_publication(tmp_path, monkeypatch):
    """A real pytest process retains its driver loop after auth is disposed."""
    base = fixture_repo(tmp_path / 'base', '''
import asyncio
from types import SimpleNamespace
from api_agent.testing.provider_setup import IntegrationLLM, _inspection_providers

def test_retained():
    owner = IntegrationLLM({'DEBUG': True}, '.')
    async def observe():
        owner.inspection.observe(SimpleNamespace(inspection_endpoint='http://127.0.0.1:1234',
        browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(pages=[object()])))
    owner.loop.run_until_complete(observe())
    owner.finish_run()
''')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    events, publication = [], []
    def worker(run):
        return next(handle for handle in run.inspections if hasattr(handle, 'process'))
    result = run_integration_tests(ctx, selected, accepted=True, config={'DEBUG': True},
        on_event=events.append, after_run=lambda run: publication.append(
            (run.tests[0].status, worker(run).process.poll(), worker(run).closed)))
    assert result.exit_code == 0
    assert publication == [('passed', None, False)]
    assert len([handle for handle in result.inspections if hasattr(handle, 'process')]) == 1
    handle = worker(result)
    assert any(event.kind == 'inspection_open' and event.text == 'http://127.0.0.1:1234' for event in events)
    assert handle.end_inspection(timeout=5)
    assert handle.wait_closed(timeout=1)
    assert handle.closed and handle.process.poll() == 0
    assert any(event.kind == 'inspection_closed' for event in events)
    assert not Path(handle.directory.temporary.name).exists()
    for remaining in result.inspections:
        remaining.end_inspection()


def test_retained_worker_abrupt_exit_cleans_registered_group(tmp_path):
    base = fixture_repo(tmp_path / 'base', '''
from types import SimpleNamespace
from api_agent.testing.provider_setup import IntegrationLLM

def test_retained():
    owner = IntegrationLLM({'DEBUG': True}, '.')
    async def observe():
        owner.inspection.observe(SimpleNamespace(inspection_endpoint=None,
        browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(pages=[object()])))
    owner.loop.run_until_complete(observe())
    owner.finish_run()
''')
    ctx = context(base)
    events = []
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True,
        config={'DEBUG': True}, on_event=events.append)
    handle = next(handle for handle in result.inspections if hasattr(handle, 'process'))
    handle.process.kill()
    assert handle.wait_closed(timeout=5)
    assert any(event.kind == 'inspection_lost' for event in events)
    for remaining in result.inspections:
        remaining.end_inspection()


def test_real_offline_browser_driver_survives_published_results(tmp_path):
    pytest.importorskip('playwright.async_api')
    from api_agent.runtime.browser import _default_paths
    if not all(path.exists() for path in _default_paths(False)):
        pytest.skip('Installed offline Chromium is required')
    base = fixture_repo(tmp_path / 'base', '''
from playwright.async_api import async_playwright
from api_agent.testing.provider_setup import IntegrationLLM
from api_agent.providers.contracts import BrowserSession

def test_browser():
    owner = IntegrationLLM({'DEBUG': True}, '.')
    async def create():
        driver = await async_playwright().start()
        browser = await driver.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.set_content('<title>Offline inspection</title>')
        owner.inspection.retain([(driver, 'stop'), (browser, 'close'), (context, 'close')])
        owner.inspection.observe(BrowserSession(browser, context, page, None))
    owner.loop.run_until_complete(create())
    owner.finish_run()
''')
    ctx = context(base)
    published = []
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True,
        config={'DEBUG': True}, after_run=lambda row: published.append(row.tests[0].status))
    assert published == ['passed'], result.output
    handle = next(handle for handle in result.inspections if hasattr(handle, 'process'))
    try:
        assert not handle.closed and handle.process.poll() is None
        assert handle.end_inspection(timeout=10)
        assert handle.process.poll() == 0
    finally:
        for remaining in result.inspections:
            remaining.abort()


def test_worker_cleanup_pending_requires_explicit_retry(tmp_path):
    base = fixture_repo(tmp_path / 'base', '''
from types import SimpleNamespace
from api_agent.testing.provider_setup import IntegrationLLM

def test_browser():
    owner = IntegrationLLM({'DEBUG': True}, '.')
    class Resource:
        attempts = 0
        async def aclose(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError('private cleanup detail')
    async def create():
        owner.inspection.retain([(Resource(), 'aclose')])
        owner.inspection.observe(SimpleNamespace(inspection_endpoint=None,
            browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(pages=[object()])))
    owner.loop.run_until_complete(create())
    owner.finish_run()
''')
    ctx = context(base)
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True, config={'DEBUG': True})
    handle = next(handle for handle in result.inspections if hasattr(handle, 'process'))
    try:
        assert handle.end_inspection(timeout=5) is False
        assert handle.pending and not handle.closed
        assert handle.process.poll() is None
        assert handle.end_inspection(timeout=5) is True
        assert handle.closed and handle.process.poll() == 0
    finally:
        for remaining in result.inspections:
            remaining.abort()


def test_launcher_liveness_eof_stops_only_registered_worker_group(tmp_path):
    import os
    if os.name != 'posix':
        pytest.skip('POSIX worker group ownership')
    base = fixture_repo(tmp_path / 'base', '''
from types import SimpleNamespace
from api_agent.testing.provider_setup import IntegrationLLM

def test_browser():
    owner = IntegrationLLM({'DEBUG': True}, '.')
    async def create():
        owner.inspection.observe(SimpleNamespace(inspection_endpoint=None,
            browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(pages=[object()])))
    owner.loop.run_until_complete(create())
    owner.finish_run()
''')
    ctx = context(base)
    events = []
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True,
        config={'DEBUG': True}, on_event=events.append)
    handle = next(handle for handle in result.inspections if hasattr(handle, 'process'))
    # Closing the sole writer produces the same EOF as abrupt launcher exit.
    os.close(handle.liveness_fd)
    handle.liveness_fd = None
    assert handle.wait_closed(timeout=5)
    assert handle.process.poll() < 0
    assert any(event.kind == 'inspection_lost' for event in events)
    for remaining in result.inspections:
        remaining.end_inspection()


def test_browser_already_closed_at_run_complete_is_not_protocol_error(tmp_path):
    base = fixture_repo(tmp_path / 'base', '''
from types import SimpleNamespace
from api_agent.testing.provider_setup import IntegrationLLM

def test_browser():
    owner = IntegrationLLM({'DEBUG': True}, '.')
    async def create():
        owner.inspection.observe(SimpleNamespace(inspection_endpoint=None,
            browser=SimpleNamespace(is_connected=lambda: False), context=SimpleNamespace(pages=[])))
    owner.loop.run_until_complete(create())
    owner.finish_run()
''')
    ctx = context(base)
    result = run_integration_tests(ctx, collect_tests(ctx, 'integration'), accepted=True, config={'DEBUG': True})
    assert result.exit_code == 0, result.output
    for handle in result.inspections:
        assert handle.wait_closed(timeout=5)


def test_parallel_completion_reaches_terminal_before_blocked_test_exits(tmp_path):
    from api_agent.testing.terminal_progress import TerminalTestProgress
    base = fixture_repo(tmp_path / 'base', '''
from pathlib import Path
import time

def test_fast():
    print('raw synthetic evidence')

def test_blocked():
    deadline = time.monotonic() + 15
    while not Path('completion-visible').exists():
        assert time.monotonic() < deadline, 'terminal completion was withheld'
        time.sleep(.02)
''')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    lines = []

    def write(line):
        lines.append(line)
        if 'test_fast' in line:
            assert 'test_blocked' not in ''.join(lines)
            (base / 'completion-visible').touch()

    result = run_integration_tests(ctx, selected, accepted=True, parallel_jobs=2,
                                  on_event=TerminalTestProgress(selected, write))
    assert result.exit_code == 0
    assert len(lines) == 2
    assert '1/2 completed — passed' in lines[0] and 'test_fast' in lines[0]
    assert '2/2 completed — passed' in lines[1] and 'test_blocked' in lines[1]
    assert 'raw synthetic evidence' not in ''.join(lines)


def test_parallel_terminal_counts_setup_error_and_skip_without_raw_evidence(tmp_path):
    from api_agent.testing.terminal_progress import TerminalTestProgress
    base = fixture_repo(tmp_path / 'base', '''
import pytest
@pytest.fixture
def broken():
    raise RuntimeError('synthetic private diagnostic')
def test_setup(broken):
    pass
@pytest.mark.skip(reason='synthetic private skip reason')
def test_skip():
    pass
''')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    lines, raw_output = [], []
    result = run_integration_tests(ctx, selected, accepted=True, parallel_jobs=2,
        on_event=TerminalTestProgress(selected, lines.append), output=raw_output.append)
    assert result.exit_code == 1
    assert len(lines) == 2
    assert '1/2 completed' in lines[0] and '2/2 completed' in lines[1]
    assert any('— error ' in line and 'test_setup' in line for line in lines)
    assert any('— skipped ' in line and 'test_skip' in line for line in lines)
    assert 'synthetic private' not in ''.join(lines)
    assert 'synthetic private diagnostic' in ''.join(raw_output)


@pytest.mark.parametrize('cancel', [False, True])
def test_parallel_completion_overtakes_raw_backlog_with_process_handshake(tmp_path, cancel):
    """The sink releases a worker, then waits for its durable teardown report."""
    import json

    base = fixture_repo(tmp_path / 'base', '''
from pathlib import Path
import time

def wait_for(name):
    deadline = time.monotonic() + 15
    while not Path(name).exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert Path(name).exists(), name

def test_fast():
    wait_for('sink-entered')

def test_blocked():
    wait_for('completion-seen')
    Path('blocked-released').touch()
''')
    (base / 'conftest.py').write_text('''
from pathlib import Path
import os

def pytest_configure(config):
    if hasattr(config, 'workerinput') or 'API_AGENT_INTEGRATION_EVENTS' not in os.environ:
        return
    Path('event-path').write_text(os.environ['API_AGENT_INTEGRATION_EVENTS'])
    os.write(1, b'FAIRNESS-NOISE\\n' * 512)
''')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    chunks = []
    observations = []
    cancellation = threading.Event()

    def output(chunk):
        chunks.append(chunk)
        if chunk.strip() != 'FAIRNESS-NOISE' or (base / 'sink-entered').exists():
            return
        (base / 'sink-entered').touch()
        event_file = Path((base / 'event-path').read_text())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rows = [json.loads(line) for line in event_file.read_text().splitlines(keepends=True)
                    if line.endswith('\n')]
            if any(row.get('kind') == 'report' and row.get('phase') == 'teardown'
                   and row.get('node', '').endswith('::test_fast') for row in rows):
                if cancel:
                    cancellation.set()
                return
            time.sleep(.01)
        pytest.fail('fast worker did not persist its teardown report')

    def event(row):
        if row.kind == 'test_finished' and row.identity.node_id.endswith('::test_fast'):
            observations.append((chunks.count('FAIRNESS-NOISE\n'),
                                 (base / 'blocked-released').exists()))
            (base / 'completion-seen').touch()

    result = run_integration_tests(ctx, selected, accepted=True, parallel_jobs=2,
                                  output=output, on_event=event, cancel_event=cancellation)

    assert result.exit_code == (130 if cancel else 0)
    assert result.cancelled is cancel
    assert result.tests[0].status == 'passed'
    assert result.tests[1].status == ('cancelled' if cancel else 'passed')
    assert (base / 'blocked-released').exists() is (not cancel)
    assert len(observations) == 1
    assert observations[0][0] <= 32
    assert observations[0][1] is False
    assert chunks.count('FAIRNESS-NOISE\n') == 512
    assert ''.join(chunks) == result.output


def test_retained_output_seal_waits_for_accepted_line_before_final_flush(tmp_path, monkeypatch):
    import json
    import os
    import queue
    from api_agent.testing import integration

    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    put_started, release_put = threading.Event(), threading.Event()
    sealing, sealed = threading.Event(), threading.Event()
    controller = threading.get_ident()
    readers, retained = [], []
    markers = []

    class OutputLock:
        def __init__(self):
            self.lock = threading.Lock()

        def __enter__(self):
            if threading.get_ident() == controller:
                assert self.lock.locked(), 'accepted queue insertion must own the seal gate'
                sealing.set()
            self.lock.acquire()

        def __exit__(self, *args):
            self.lock.release()
            if threading.get_ident() == controller:
                sealed.set()

    class OutputQueue(queue.Queue):
        def put(self, line):
            put_started.set()
            assert release_put.wait(5), 'accepted output was never released'
            super().put(line)

    class Stdout:
        def __iter__(self):
            yield markers[0]
            yield 'accepted before handoff\n'
            assert sealed.wait(5), 'output was never sealed'
            yield 'discarded after handoff\n'

    class Process:
        stdout = Stdout()
        returncode = None

        def poll(self):
            return None

    def spawn(args, **kwargs):
        markers.append(stdout_boundary_from_launch(args))
        rows = [dict(kind='report', node=selected[0].node_id, phase=phase,
                     status='passed', duration=0, traceback='')
                for phase in ('setup', 'call', 'teardown')]
        rows += [dict(kind='run_complete', exit_code=0), dict(kind='inspection_open')]
        Path(args[3]).write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return Process()

    def retain(process, directory, event_file, reader, **kwargs):
        assert put_started.wait(5), 'reader did not enter accepted queue insertion'
        directory.retained = True
        event_file.unlink()
        readers.append(reader)
        retained.append((directory, kwargs['liveness_fd']))

    def release_accepted_line():
        if sealing.wait(5):
            release_put.set()

    releaser = threading.Thread(target=release_accepted_line)
    monkeypatch.setattr(integration, 'threading', SimpleNamespace(
        Thread=threading.Thread, Event=threading.Event, Lock=OutputLock))
    monkeypatch.setattr(integration, 'queue', SimpleNamespace(Queue=OutputQueue))
    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: selected)
    monkeypatch.setattr(integration.subprocess, 'Popen', spawn)
    monkeypatch.setattr('api_agent.testing.inspection.IntegrationInspection', retain)
    releaser.start()
    try:
        result = run_integration_tests(ctx, selected, accepted=True)
        assert result.exit_code == 0, result.output
        assert result.output == 'accepted before handoff\n'
        assert sealing.is_set()
    finally:
        release_put.set()
        sealed.set()
        releaser.join(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        for directory, liveness_fd in retained:
            if liveness_fd is not None:
                os.close(liveness_fd)
            directory.temporary.cleanup()


def test_retained_handoff_reads_pipe_boundary_before_sealing_slow_reader(tmp_path, monkeypatch):
    import json
    import queue
    from api_agent.testing import integration

    base = fixture_repo(tmp_path / 'base', '''
from types import SimpleNamespace
from api_agent.testing.provider_setup import IntegrationLLM

def test_retained():
    owner = IntegrationLLM({'DEBUG': True}, '.')
    async def observe():
        owner.inspection.observe(SimpleNamespace(inspection_endpoint=None,
            browser=SimpleNamespace(is_connected=lambda: True), context=SimpleNamespace(pages=[object()])))
    owner.loop.run_until_complete(observe())
    owner.finish_run()
''')
    (base / 'conftest.py').write_text('''
import json, os, subprocess, sys
from pathlib import Path

def pytest_configure(config):
    if 'API_AGENT_INTEGRATION_EVENTS' in os.environ:
        for key, value in os.environ.items():
            if key.startswith('API_AGENT_'):
                os.write(1, (key + '=' + value + '\\n').encode())
        strings = {key: value for key, value in vars(sys.modules['__main__']).items()
                   if isinstance(value, str)}
        for key, value in strings.items():
            os.write(1, (key + '=' + value + '\\n').encode())
        child = subprocess.check_output([sys.executable, '-c',
            "import os; print('API_AGENT_STDOUT_BOUNDARY' in os.environ)"], text=True).strip().splitlines()[-1]
        Path('boundary-observations.json').write_text(json.dumps({
            'controller_environment': 'API_AGENT_STDOUT_BOUNDARY' in os.environ,
            'controller_globals': any('[api-agent-stdout-' in value for value in strings.values()),
            'child_environment': child == 'True',
        }))
        os.write(1, b'PIPE-BACKLOG\\n' * 500)
''')
    startup = base / 'src/sitecustomize.py'
    startup.parent.mkdir()
    startup.write_text('''
import json, os
from pathlib import Path
if 'API_AGENT_INTEGRATION_EVENTS' in os.environ:
    for key, value in os.environ.items():
        if key.startswith('API_AGENT_'):
            os.write(1, (key + '=' + value + '\\n').encode())
    with Path('startup-observations.jsonl').open('a') as stream:
        stream.write(json.dumps('API_AGENT_STDOUT_BOUNDARY' in os.environ) + '\\n')
''')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')

    class SlowQueue(queue.Queue):
        def put(self, item):
            time.sleep(.003)
            super().put(item)

    monkeypatch.setattr(integration, 'queue', SimpleNamespace(Queue=SlowQueue))
    monkeypatch.setattr(integration, '_PLUGIN', integration._PLUGIN.replace(
        '    sys.stdout.flush()',
        "    os.write(1, b'final prefix without newline')\n    sys.stdout.flush()", 1))
    result = run_integration_tests(ctx, selected, accepted=True, config={'DEBUG': True})
    try:
        assert result.exit_code == 0
        assert result.output.count('PIPE-BACKLOG\n') == 500
        assert 'final prefix without newline' in result.output
        assert '[api-agent-stdout-' not in result.output
        observations = json.loads((base / 'boundary-observations.json').read_text())
        assert observations == dict(controller_environment=False, controller_globals=False,
                                    child_environment=False)
        assert 'API_AGENT_INTEGRATION_EVENTS=' in result.output
        startup_views = (base / 'startup-observations.jsonl').read_text().splitlines()
        assert len(startup_views) >= 2
        assert all(json.loads(view) is False for view in startup_views)
        assert any(hasattr(handle, 'process') and handle.process.poll() is None
                   for handle in result.inspections)
    finally:
        for handle in result.inspections:
            handle.abort()


@pytest.mark.parametrize('receipt', ['marker', 'false_marker', 'false_marker_live', 'marker_error', 'eof', 'read_error', 'exited_error',
                                     'timeout', 'cancel', 'process_exit', 'decode_error', 'zero_parent', 'late_ack'])
def test_retained_stdout_boundary_receipt_controls_transfer(tmp_path, monkeypatch, receipt):
    from contextlib import contextmanager, nullcontext
    from api_agent.providers import progress
    import json
    import os
    from api_agent.testing import integration

    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    stopped = threading.Event()
    cancel = threading.Event()
    transferred = []
    finalized = []
    marker = []
    wait_entered, release_ack, ack_consumed = (threading.Event() for _ in range(3))
    late_budget = SimpleNamespace(remaining=5)

    class Stdout:
        def __iter__(self):
            if receipt == 'late_ack':
                assert wait_entered.wait(5), 'boundary wait was not entered'
                yield 'late raw callback\n'
                assert release_ack.wait(5), 'late marker was not released'
                yield marker[0]
                ack_consumed.set()
                return
            if receipt in {'read_error', 'exited_error'}:
                raise OSError('synthetic reader failure')
            if receipt == 'decode_error':
                raise UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'synthetic reader failure')
            if receipt == 'false_marker_live':
                yield '[api-agent-stdout-wrong-nonce]\n'
            if receipt in {'timeout', 'cancel', 'process_exit', 'false_marker_live'}:
                assert stopped.wait(5), 'worker was not finalized'
                return
            if receipt == 'false_marker':
                yield '[api-agent-stdout-wrong-nonce]\n'
            yield 'raw prefix' + (marker[0] if receipt != 'eof' else '')
            if receipt == 'marker_error':
                raise OSError('synthetic reader failure')

        def close(self):
            pass

    class Process:
        stdout = Stdout()
        returncode = None
        polls = 0

        def poll(self):
            self.polls += 1
            if receipt in {'exited_error', 'decode_error'} or (receipt == 'process_exit' and self.polls > 1):
                self.returncode = 0
            return self.returncode

    process = Process()

    def spawn(args, **kwargs):
        marker.append(stdout_boundary_from_launch(args))
        rows = [dict(kind='report', node=selected[0].node_id, phase=phase,
                     status='passed', duration=0, traceback='')
                for phase in ('setup', 'call', 'teardown')]
        rows += [dict(kind='run_complete', exit_code=0), dict(kind='inspection_open')]
        Path(args[3]).write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return process

    def finalize(worker):
        finalized.append(worker)
        worker.returncode = 0
        stopped.set()

    def retain(worker, directory, event_file, reader, **kwargs):
        directory.retained = True
        transferred.append((directory, kwargs['liveness_fd'], reader))

    def event(row):
        if receipt == 'cancel' and row.kind == 'inspection_open':
            cancel.set()

    @contextmanager
    def expired_budget(name, timeout):
        assert name == 'Integration retained stdout boundary' and timeout == 5
        yield SimpleNamespace(remaining=0)

    if receipt in {'marker_error', 'zero_parent'}:
        class CompletedReader:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
            def join(self, **kwargs): pass
            def is_alive(self): return False
        monkeypatch.setattr(integration, 'threading', SimpleNamespace(
            Thread=CompletedReader, Event=threading.Event, Lock=threading.Lock))
    @contextmanager
    def late_ack_budget(name, timeout):
        wait_entered.set()
        yield late_budget

    def output(chunk):
        if receipt == 'late_ack' and chunk == 'late raw callback\n':
            late_budget.remaining = 0
            release_ack.set()
            assert ack_consumed.wait(5), 'late marker was not acknowledged'

    monkeypatch.setattr(integration, 'collect_tests', lambda *args, **kwargs: selected)
    monkeypatch.setattr(integration.subprocess, 'Popen', spawn)
    monkeypatch.setattr(integration, 'finalize', finalize)
    monkeypatch.setattr('api_agent.testing.inspection.IntegrationInspection', retain)
    if receipt in {'timeout', 'false_marker_live'}:
        monkeypatch.setattr('api_agent.providers.progress.report_timeout_sync', expired_budget)

    if receipt == 'late_ack':
        monkeypatch.setattr(progress, 'report_timeout_sync', late_ack_budget)
    clock = SimpleNamespace(now=0)
    if receipt == 'zero_parent':
        monkeypatch.setattr(progress, '_clock', lambda: clock.now)
    parent = progress.report_timeout_sync('Parent budget', 5) if receipt == 'zero_parent' else nullcontext()
    with parent:
        if receipt == 'zero_parent':
            clock.now = 5
        result = run_integration_tests(ctx, selected, accepted=True, on_event=event,
                                       cancel_event=cancel, output=output)
    try:
        assert bool(transferred) is (receipt in {'marker', 'false_marker', 'eof'})
        assert bool(finalized) is (not transferred)
        assert result.exit_code == (130 if receipt == 'cancel' else
                                    1 if receipt in {'read_error', 'exited_error', 'decode_error', 'marker_error', 'timeout', 'false_marker_live', 'zero_parent', 'late_ack'} else 0)
        if receipt in {'marker', 'false_marker', 'eof'}:
            assert result.output.endswith('raw prefix')
            assert marker[0] not in result.output
        if receipt == 'false_marker':
            assert result.output.startswith('[api-agent-stdout-wrong-nonce]\n')
        if receipt in {'read_error', 'exited_error', 'decode_error', 'marker_error'}:
            assert 'synthetic reader failure' in result.tests[0].local_diagnostic
        if receipt in {'timeout', 'false_marker_live', 'zero_parent', 'late_ack'}:
            assert 'Integration retained stdout boundary' in result.output
    finally:
        stopped.set()
        for directory, liveness_fd, reader in transferred:
            reader.join(timeout=5)
            if liveness_fd is not None:
                os.close(liveness_fd)
            directory.temporary.cleanup()


@pytest.mark.parametrize('installed,base_name,owner_name', [
    (False, 'same', 'same'), (True, 'api_agent', 'external'),
    (True, 'api_agent', 'api-agent'), (True, 'api_agent', 'api_agent')])
def test_integration_executor_uses_collected_labels_for_each_actual_child_cwd(tmp_path, monkeypatch, installed, base_name, owner_name):
    from api_agent.testing import collection
    def build(path):
        return fixture_repo(path, test='from pathlib import Path\ndef test_cwd():\n    assert Path.cwd() == Path(' + repr(str(path)) + ')\n')
    base = build(tmp_path / 'base' / base_name)
    owner = build(tmp_path / 'owner' / owner_name)
    monkeypatch.setattr(collection, '_installed_base_identity', lambda root: installed and root == base)
    ctx = context(base, owner)
    tests = collect_tests(ctx, 'integration')
    assert len({test.id for test in tests}) == 2 and all(test.root is None for test in tests)
    result = run_integration_tests.__wrapped__(ctx, tests, accepted=True)
    assert result.exit_code == 0
    assert len(result.tests) == 2 and all(row.status == 'passed' for row in result.tests)
