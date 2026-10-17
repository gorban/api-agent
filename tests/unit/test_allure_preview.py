"""Viewer snapshots preserve result identity without publishing partial history."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from api_agent.testing import allure_preview as preview
from api_agent.testing.recording import RunRecorder, RunIdentity
from api_agent.testing.contracts import TestResult
from api_agent.testing import recorded
from test_recorded_integration import fixture, fake_runner, Storage


@pytest.mark.parametrize('status', ['passed', 'failed', 'skipped', 'error', 'cancelled'])
def test_previews_precede_publication_and_final_snapshots_match_history(tmp_path, status):
    context, test, config = fixture(tmp_path)
    config['ALLURE_RESULTS_DIR'] = ''
    snapshots = []
    storage = Storage()
    def receive(snapshot):
        assert not storage.calls
        snapshots.append(snapshot)
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
        storage=storage, runner=fake_runner(status), on_snapshot=receive)
    first, final = snapshots
    filename = first.result_files[test.id]
    assert first.preview is True and final.preview is False
    assert filename in first.files and filename in final.files
    assert first.manifest['preview_version'] == 1
    assert final.manifest == result.manifest
    assert final.result_files[test.id] == filename
    assert result.directory is None
    assert json.loads(first.files[filename])['uuid'] == json.loads(final.files[filename])['uuid']
    assert 'Suite usage' not in str(json.loads(first.files[filename]))
    assert 'Suite usage' in str(json.loads(final.files[filename]))
    assert all(hashlib.sha256(data).hexdigest() == final.manifest['files'][name]
               for name, data in final.files.items())


def test_viewer_callback_failure_does_not_fail_execution_or_publication(tmp_path):
    context, test, config = fixture(tmp_path)
    def failed(snapshot):
        raise RuntimeError('viewer closed')
    result = recorded.run_recorded_integration(context, [test], config, accepted=True,
        storage=Storage(), runner=fake_runner(), on_snapshot=failed)
    assert result.persisted and result.exit_code == 0


@pytest.fixture
def recorder(tmp_path):
    context, test, _ = fixture(tmp_path)
    recorder = RunRecorder(tmp_path/'run', RunIdentity.create(test.repo, 'integration'))
    name = recorder.record_test(TestResult(test,'passed'), attachments=[('Image',b'pixels','image/png')])
    return recorder, {test.id:name}


def test_preview_includes_original_attachment_and_detaches_metadata(recorder):
    owner, mapping = recorder
    received = []
    preview.emit_snapshot(owner,mapping,received.append)
    snapshot = received[0]
    row = json.loads(snapshot.files[mapping[next(iter(mapping))]])
    assert snapshot.files[row['attachments'][0]['source']] == b'pixels'
    mapping.clear()
    assert snapshot.result_files
    assert not (owner.directory/'manifest.json').exists()
    preview.emit_snapshot(owner,{},None)


@pytest.mark.parametrize('damage',['file-count','file-size','total-size','traversal','missing','digest'])
def test_unusable_viewer_copy_never_exports_partial_data(recorder,monkeypatch,damage):
    owner,mapping = recorder
    received=[]
    manifest=None
    if damage=='file-count':
        monkeypatch.setattr(preview,'MAX_FILES',0)
    elif damage=='file-size':
        monkeypatch.setattr(preview,'MAX_FILE_BYTES',1)
    elif damage=='total-size':
        monkeypatch.setattr(preview,'MAX_TOTAL_BYTES',1)
    elif damage=='traversal':
        mapping={'test':'../unowned-result.json'}
        manifest={'files':{'../unowned-result.json':'a'*64}}
    elif damage=='missing':
        mapping={'test':'missing-result.json'}
    else:
        manifest=owner.finalize()
        manifest['files'][next(iter(manifest['files']))]='a'*64
    preview.emit_snapshot(owner,mapping,received.append,manifest=manifest)
    assert received==[]


def test_snapshot_interruption_is_not_swallowed(recorder):
    owner,mapping=recorder
    def interrupted(snapshot):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        preview.emit_snapshot(owner,mapping,interrupted)
