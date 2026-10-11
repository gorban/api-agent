"""Suite accounting is attached only after preparation and final snapshots."""
import hashlib
import json
from pathlib import Path

import pytest

from api_agent.testing import recorded
from api_agent.testing.recording import RunRecorder
from test_recording import identity, result
from test_recorded_integration import fixture, fake_runner, Storage


@pytest.mark.parametrize('status', ['passed', 'failed', 'cancelled'])
def test_allure_results_retain_same_frozen_usage(tmp_path, status):
    context, test, config = fixture(tmp_path)
    storage = Storage()
    events = []
    def prepare(usage):
        events.append('prepare')
    def snapshot():
        # Preparation/authentication must finish before the first quota read.
        assert events and events[0] == 'prepare'
        events.append('quota')
        raise RuntimeError('synthetic unavailable quota')
    completed = recorded.run_recorded_integration(context, [test], config,
        accepted=True, storage=storage, prepare=prepare, quota_fetcher=snapshot,
        runner=fake_runner(status))
    assert events == ['prepare', 'quota', 'quota']
    payload = json.loads((completed.directory / 'usage-summary.json').read_bytes())
    assert payload == completed.usage
    assert payload['quota_unavailable_reason'] == 'missing_snapshot'
    for file in completed.directory.glob('*-result.json'):
        attachment = json.loads(file.read_bytes())['attachments']
        assert {item['name'] for item in attachment} == {'Test cost estimate', 'Suite usage', 'Immutable accounting'}
        usage_attachment = next(item for item in attachment if item['name'] == 'Suite usage')
        assert json.loads((completed.directory / usage_attachment['source']).read_bytes()) == completed.usage
        accounting_attachment = next(item for item in attachment if item['name'] == 'Immutable accounting')
        assert json.loads((completed.directory / accounting_attachment['source']).read_bytes())['run_id'] == completed.identity.run_id
        assert completed.manifest['files'][file.name] == hashlib.sha256(file.read_bytes()).hexdigest()
    assert completed.persisted


def test_shared_artifact_includes_automatic_suite_failure(tmp_path):
    recorder = RunRecorder(tmp_path, identity())
    recorder.record_artifact('usage.json', b'{"prompt_count":0}')
    manifest = recorder.finalize(status='failed', shared_artifacts=[('Usage', 'usage.json', 'application/json')])
    row = json.loads(next(tmp_path.glob('*-result.json')).read_bytes())
    assert row['status'] == 'broken'
    assert row['attachments'][0]['source'] == 'usage.json'
    assert manifest['status'] == 'failed'


@pytest.mark.parametrize('title,source,mime', [
    ('Usage', 'missing.json', 'application/json'),
    ('', 'usage.json', 'application/json'),
    (None, 'usage.json', 'application/json'),
    ('Usage', 'usage.json', ''),
    ('Usage', 'usage.json', None),
    ('Usage', '../usage.json', 'application/json'),
    ('Usage', 'result', 'application/json'),
])
def test_invalid_shared_attachment_cannot_publish_or_change_results(tmp_path, title, source, mime):
    recorder = RunRecorder(tmp_path, identity())
    filename = recorder.record_test(result())
    recorder.record_artifact('usage.json', b'{}')
    if source == 'result':
        source = filename
    before = (tmp_path / filename).read_bytes()
    with pytest.raises(ValueError):
        recorder.finalize(shared_artifacts=[(title, source, mime)])
    assert (tmp_path / filename).read_bytes() == before
    assert not (tmp_path / 'manifest.json').exists()
    assert recorder.finalize()['status'] == 'completed'


def test_failed_shared_attachment_replacement_never_publishes_partial_run(tmp_path, monkeypatch):
    recorder = RunRecorder(tmp_path, identity())
    filename = recorder.record_test(result())
    recorder.record_artifact('usage.json', b'{}')
    before = (tmp_path / filename).read_bytes()
    def fail(*args):
        raise OSError('synthetic storage failure')
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError):
        recorder.finalize(shared_artifacts=[('Usage', 'usage.json', 'application/json')])
    assert (tmp_path / filename).read_bytes() == before
    assert not (tmp_path / 'manifest.json').exists()
    assert not list(tmp_path.glob('.*.tmp'))
    with pytest.raises(RuntimeError, match='incomplete artifact write'):
        recorder.finalize(status='failed')


def test_usage_is_output_even_when_publication_fails(tmp_path):
    context, test, config = fixture(tmp_path)
    lines = []
    storage = Storage(fail=True)
    completed = recorded.run_recorded_integration(context, [test], config,
        accepted=True, storage=storage, usage_output=lines.append, runner=fake_runner())
    assert completed.exit_code == 1 and not completed.persisted
    assert lines[0] == 'Starting quota used: unavailable'
    assert 'Final quota used: unavailable' in lines[1]


def test_output_failure_preserves_failure_history(tmp_path):
    context, test, config = fixture(tmp_path)
    lines = []
    def output(text):
        lines.append(text)
        if len(lines) == 2:
            raise RuntimeError('synthetic display failure')
    completed = recorded.run_recorded_integration(context, [test], config,
        accepted=True, storage=Storage(), usage_output=output, runner=fake_runner())
    assert completed.persisted and completed.exit_code == 1
    assert completed.manifest['status'] == 'failed'
    rows = [json.loads(file.read_bytes()) for file in completed.directory.glob('*-result.json')]
    assert any(row['name'] == 'usage_output' and row['status'] == 'broken' for row in rows)
