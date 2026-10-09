from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from api_agent.testing.collection import CollectionError, collect_tests
from api_agent.testing.integration import run_integration_tests


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


def test_synthetic_partial_and_corrupt_event_protocol(tmp_path, monkeypatch):
    base = fixture_repo(tmp_path / 'base')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    monkeypatch.setattr('api_agent.testing.integration.collect_tests', lambda *a, **k: selected)
    node = selected[0].node_id
    script = '''import sys
p = sys.argv[1]
with open(p, 'a') as f:
 f.write('{"kind":"test_started","node":"unknown"}\\n')
 f.write('{bad json}\\n')
 f.write('{"kind":"unknown","node":"%s"}\\n')
 f.write('{"kind":"report","node":"%s","phase":"setup","status":"passed","duration":0.1,"traceback":""}\\n')
print('synthetic')
''' % (node, node)
    monkeypatch.setattr('api_agent.testing.integration._PLUGIN', script)
    result = run_integration_tests(ctx, selected, accepted=True)
    assert result.exit_code == 1 and result.tests[0].status == 'error'
    assert 'Invalid integration test event' in result.output


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


def test_debug_is_serial_and_streams_inspection_output(tmp_path):
    base = fixture_repo(tmp_path / 'base', 'def test_ok():\n    print("inspection-ready")\n')
    ctx = context(base)
    selected = collect_tests(ctx, 'integration')
    result = run_integration_tests(ctx, selected, accepted=True, parallel_jobs=8,
        config={'DEBUG': True})
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    assert 'inspection-ready' in result.output
    assert 'collected 1 item' in result.output
    assert 'workers [' not in result.output


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
    result = run_integration_tests(ctx, selected, accepted=True, config={'DEBUG': True},
        on_event=events.append, after_run=lambda run: publication.append(
            (run.tests[0].status, run.inspections[0].process.poll(), run.inspections[0].closed)))
    assert result.exit_code == 0
    assert publication == [('passed', None, False)]
    assert len(result.inspections) == 1
    handle = result.inspections[0]
    assert any(event.kind == 'inspection_open' and event.text == 'http://127.0.0.1:1234' for event in events)
    assert handle.end_inspection(timeout=5)
    assert handle.wait_closed(timeout=1)
    assert handle.closed and handle.process.poll() == 0
    assert any(event.kind == 'inspection_closed' for event in events)
    assert not Path(handle.directory.temporary.name).exists()


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
    handle = result.inspections[0]
    handle.process.kill()
    assert handle.wait_closed(timeout=5)
    assert any(event.kind == 'inspection_lost' for event in events)


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
    handle = result.inspections[0]
    try:
        assert not handle.closed and handle.process.poll() is None
        assert handle.end_inspection(timeout=10)
        assert handle.process.poll() == 0
    finally:
        handle.abort()


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
    handle = result.inspections[0]
    try:
        assert handle.end_inspection(timeout=5) is False
        assert handle.pending and not handle.closed
        assert handle.process.poll() is None
        assert handle.end_inspection(timeout=5) is True
        assert handle.closed and handle.process.poll() == 0
    finally:
        handle.abort()


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
    handle = result.inspections[0]
    # Closing the sole writer produces the same EOF as abrupt launcher exit.
    os.close(handle.liveness_fd)
    handle.liveness_fd = None
    assert handle.wait_closed(timeout=5)
    assert handle.process.poll() < 0
    assert any(event.kind == 'inspection_lost' for event in events)


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
