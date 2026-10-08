from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4

import pytest

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing.recording import RunIdentity, RunRecorder
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


def identity(mode='integration'):
    return RunIdentity(str(uuid4()), 'example', mode, datetime(2026, 1, 1, tzinfo=timezone.utc))


def result(status='passed'):
    test = TestIdentity('example::tests/test_demo.py::test_ok', 'example', 'tests/test_demo.py',
                        'tests/test_demo.py::test_ok')
    return TestResult(test, status, 1.25, 'secret traceback')


def test_allure_hierarchy_and_attachments(tmp_path):
    run = RunRecorder(tmp_path, identity())
    filename = run.record_test(result(), attempt=2, attachments=[('response_html', b'<p>ok</p>', 'text/html')])
    row = json.loads((tmp_path / filename).read_text())
    assert [(x['name'], x['value']) for x in row['labels']] == [
        ('parentSuite', 'example'), ('suite', 'integration'), ('subSuite', 'test_demo.py')]
    assert row['name'] == 'test_ok'
    assert row['parameters'] == [{'name': 'attempt', 'value': '2'}]
    assert (tmp_path / row['attachments'][0]['source']).read_bytes() == b'<p>ok</p>'
    assert 'secret traceback' not in (tmp_path / filename).read_text()
    manifest = run.finalize({'token': 'secret', 'duration_ms': 100})
    assert manifest['status'] == 'completed'
    assert manifest['metadata'] == {'duration_ms': 100}
    assert manifest['files'][filename] == hashlib.sha256((tmp_path / filename).read_bytes()).hexdigest()
    with pytest.raises(RuntimeError):
        run.record_test(result())


@pytest.mark.parametrize('reason', ['', 'Missing required configuration: ACCESS_KEY'])
def test_allure_skips_keep_reason_without_failing_run(tmp_path, reason):
    from dataclasses import replace
    recorder = RunRecorder(tmp_path, identity())
    outcome = replace(result('skipped'), traceback=reason)
    row = json.loads((tmp_path / recorder.record_test(outcome)).read_text())
    assert row['status'] == 'skipped'
    assert row['statusDetails']['message'] == (reason or 'Test skipped')
    assert recorder.finalize()['status'] == 'completed'


def test_skip_reason_redacts_registered_secrets(tmp_path):
    from dataclasses import replace
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('PRIVATE',))
    recorder = RunRecorder(tmp_path, identity(), evidence_boundary=boundary)
    outcome = replace(result('skipped'), traceback='Requirement unavailable: PRIVATE')
    row = json.loads((tmp_path / recorder.record_test(outcome)).read_text())
    assert row['statusDetails']['message'] == 'Requirement unavailable: [REDACTED]'
    assert 'PRIVATE' not in json.dumps(row)


@pytest.mark.parametrize('status,expected', [('failed', 'failed'), ('broken', 'failed'),
                                              ('interrupted', 'interrupted'), ('cancelled', 'interrupted')])
def test_nonpassing_outcomes_are_honest(tmp_path, status, expected):
    run = RunRecorder(tmp_path, identity('e2e'))
    row = json.loads((tmp_path / run.record_test(result(status))).read_text())
    assert row['status'] in {'failed', 'broken'}
    assert run.finalize()['status'] == expected


def test_unit_and_invalid_results_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        identity('unit')
    run = RunRecorder(tmp_path, identity())
    with pytest.raises(ValueError):
        run.record_test(result(), attempt=0)
    with pytest.raises(ValueError):
        run.record_test(result('unknown'))
    with pytest.raises(ValueError):
        run.record_test(TestResult(TestIdentity('other::x', 'other', 'x.py', 'x.py::test_x'), 'passed'))
    assert list(tmp_path.iterdir()) == []


def test_identity_validation_and_finalize_once(tmp_path):
    with pytest.raises(ValueError):
        RunIdentity('bad', 'example', 'integration', datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        RunIdentity(str(uuid4()), '../escape', 'integration', datetime.now(timezone.utc))
    with pytest.raises(ValueError):
        RunIdentity(str(uuid4()), 'example', 'integration', datetime.now())
    with pytest.raises(TypeError):
        RunRecorder(tmp_path, object())
    run = RunRecorder(tmp_path, identity())
    with pytest.raises(ValueError):
        run.record_test(result(), attachments=[('bad', 'string', 'text/plain')])
    with pytest.raises(ValueError):
        run.finalize(status='unknown')
    run.finalize(status='interrupted')
    with pytest.raises(RuntimeError):
        run.finalize()


def test_setup_error_and_bad_test_identity(tmp_path):
    run = RunRecorder(tmp_path, identity())
    test = TestIdentity('example::bad', 'example', '', '')
    with pytest.raises(ValueError):
        run.record_test(TestResult(test, 'passed'))
    filename = run.record_test(result('error'))
    assert json.loads((tmp_path / filename).read_text())['statusDetails']['message'] == 'Test setup or execution failed'
    assert run.finalize()['status'] == 'failed'


def test_generated_identity_and_noncanonical_uuid():
    made = RunIdentity.create('example', 'integration')
    assert made.repo == 'example'
    with pytest.raises(ValueError):
        RunIdentity(made.run_id.upper(), 'example', 'integration', made.started_at)


def test_suite_artifact_and_setup_failure_without_tests(tmp_path):
    run = RunRecorder(tmp_path, identity())
    run.record_artifact('usage.csv', b'tokens\n12\n')
    with pytest.raises(ValueError):
        run.record_artifact('../escape', b'bad')
    with pytest.raises(ValueError):
        run.record_artifact('manifest.json', b'bad')
    with pytest.raises(ValueError):
        run.record_artifact('bad.txt', 'text')
    manifest = run.finalize(status='failed')
    assert manifest['status'] == 'failed'
    assert 'usage.csv' in manifest['files']
    result_files = list(tmp_path.glob('*-result.json'))
    assert len(result_files) == 1
    assert json.loads(result_files[0].read_text())['status'] == 'broken'
    with pytest.raises(RuntimeError):
        run.record_artifact('later.txt', b'late')


def test_failed_write_blocks_finalization(tmp_path):
    run = RunRecorder(tmp_path, identity())
    run.record_artifact('usage.csv', b'first')
    with pytest.raises(FileExistsError):
        run.record_artifact('usage.csv', b'overwrite')
    with pytest.raises(RuntimeError):
        run.finalize(status='failed')
    assert not (tmp_path / 'manifest.json').exists()


def test_honest_finalize_and_evidence_scrubbing(tmp_path):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('TOPSECRET',))
    recorder = RunRecorder(tmp_path, identity(), evidence_boundary=boundary,
                           clock=lambda: datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
    test = TestIdentity('example::x', 'example', 'TOPSECRET.py', 'TOPSECRET.py::test_TOPSECRET')
    filename = recorder.record_test(TestResult(test, 'failed', 2, 'assert TOPSECRET == 1'))
    row = json.loads((tmp_path / filename).read_text())
    assert row['start'] == row['stop'] - 2000
    assert 'TOPSECRET' not in json.dumps(row)
    assert '[REDACTED]' in row['statusDetails']['trace']
    with pytest.raises(ValueError):
        recorder.finalize(status='completed')
    assert recorder.finalize()['status'] == 'failed'


def test_partial_attachments_rejected_before_writes(tmp_path):
    recorder = RunRecorder(tmp_path, identity())
    with pytest.raises(ValueError):
        recorder.record_test(result(), attachments=[('valid', b'good', 'text/plain'), ('bad', 'text', 'text/plain')])
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        recorder.finalize()


@pytest.mark.parametrize('final_status,allure_status', [('failed', 'broken'),
                                                       ('interrupted', 'broken')])
def test_suite_failure_after_pass_is_visible_in_allure(tmp_path, final_status, allure_status):
    recorder = RunRecorder(tmp_path, identity())
    recorder.record_test(result('passed'))
    manifest = recorder.finalize(status=final_status)
    rows = [json.loads(path.read_text()) for path in tmp_path.glob('*-result.json')]
    assert manifest['status'] == final_status
    assert len(rows) == 2
    assert {row['status'] for row in rows} == {'passed', allure_status}
    suite = next(row for row in rows if row['fullName'] == '__suite__::setup')
    assert suite['statusDetails']['message'] == ('Test interrupted' if final_status == 'interrupted'
                                                  else 'Test setup or execution failed')


def test_interrupt_after_failure_adds_interrupted_suite_outcome(tmp_path):
    recorder = RunRecorder(tmp_path, identity())
    recorder.record_test(result('failed'))
    assert recorder.finalize(status='interrupted')['status'] == 'interrupted'
    rows = [json.loads(path.read_text()) for path in tmp_path.glob('*-result.json')]
    assert len(rows) == 2
    assert any(row.get('statusDetails', {}).get('message') == 'Test interrupted' for row in rows)
