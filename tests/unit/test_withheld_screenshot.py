"""Content-off UI results use only fixed, parent-owned notice pixels."""
from dataclasses import replace
import hashlib
import json
import os
import struct

import pytest

from api_agent.testing import withheld_screenshot as notice


def test_packaged_notice_is_exact_valid_png():
    title, data, mime = notice.withheld_attachment()
    assert title == 'UI screenshot withheld (LOG_CONTENT=False)' and mime == 'image/png'
    assert hashlib.sha256(data).hexdigest() == notice.ASSET_SHA256
    assert data.startswith(b'\x89PNG\r\n\x1a\n')
    assert struct.unpack('>II', data[16:24]) == (1000, 300)
    assert 'LOG_CONTENT=True' in notice.EXPLANATION
    assert 'confidential data' in notice.EXPLANATION
    assert 'Credential masking and provider safety checks still apply' in notice.EXPLANATION


@pytest.mark.parametrize('damage', ['missing', 'directory', 'symlink', 'size', 'pixels'])
def test_damaged_notice_never_exports_replacement_bytes(tmp_path, monkeypatch, damage):
    path = tmp_path / 'notice.png'
    if damage == 'directory':
        path.mkdir()
    elif damage == 'symlink':
        path.symlink_to(notice.ASSET)
    elif damage == 'size':
        path.write_bytes(b'confidential replacement')
    elif damage == 'pixels':
        path.write_bytes(b'x' * notice.ASSET_BYTES)
    monkeypatch.setattr(notice, 'ASSET', path)
    assert notice.withheld_attachment() == (notice.TITLE, notice.EXPLANATION.encode(), 'text/plain')


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='Named pipes require POSIX')
def test_fifo_notice_is_rejected_without_waiting_for_a_writer(tmp_path, monkeypatch):
    path = tmp_path / 'notice.png'
    os.mkfifo(path)
    monkeypatch.setattr(notice, 'ASSET', path)
    assert notice.withheld_attachment()[2] == 'text/plain'


@pytest.mark.parametrize('enabled', [None, False, True])
@pytest.mark.parametrize('status', ['passed', 'failed', 'error'])
@pytest.mark.parametrize('selected_ui', [False, True])
def test_allure_notice_uses_selected_intent_and_never_worker_pixels_when_disabled(
        tmp_path, enabled, status, selected_ui):
    from api_agent.testing import recorded
    from api_agent.testing.contracts import RunnerEvent, TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.browser_evidence import PNG_SIGNATURE
    from test_recorded_integration import fixture, Storage
    context, identity, config = fixture(tmp_path)
    selected = replace(identity, tags={'requires_ui_session'} if selected_ui else set())
    # A returned marker must not override the parent's selected-test declaration.
    returned = replace(identity, tags=set() if selected_ui else {'requires_ui_session'})
    config['LOG_CONTENT'] = enabled
    worker_image = ('UI failure context 1 page 1', PNG_SIGNATURE + b'confidential worker pixels', 'image/png')
    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('browser_attachment', identity=returned, attachment=worker_image))
        outcome = TestResult(returned, status)
        hooks['on_result'](outcome)
        result = IntegrationRunResult(int(status != 'passed'), (outcome,), '')
        hooks['after_run'](result)
        return result
    result = recorded.run_recorded_integration(context, [selected], config, accepted=True,
        storage=Storage(), runner=runner)
    assert result.persisted
    row = json.loads(next(result.directory.glob('*-result.json')).read_text())
    images = [item for item in row['attachments'] if item['type'] == 'image/png']
    assert len(images) == int(enabled is True or selected_ui)
    if enabled is True:
        assert images[0]['name'] == worker_image[0]
        assert (result.directory / images[0]['source']).read_bytes() == worker_image[1]
    elif selected_ui:
        assert images[0]['name'] == notice.TITLE
        assert (result.directory / images[0]['source']).read_bytes() == notice.withheld_attachment()[1]
        assert result.manifest['files'][images[0]['source']] == notice.ASSET_SHA256
    else:
        assert not images
