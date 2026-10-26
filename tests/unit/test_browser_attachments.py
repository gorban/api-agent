"""Immutable browser bytes are checked again across the worker boundary."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from api_agent.testing import browser_attachments as attachments
from api_agent.testing.browser_evidence import PNG_SIGNATURE, MAX_IMAGE_BYTES


@pytest.fixture
def channel(tmp_path, monkeypatch):
    event = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(event))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','tests/test_ui.py::test_ui (teardown)')
    return event


def test_round_trip_preserves_original_immutable_bytes(channel, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST","tests/test_ui.py::test_ui (teardown)")
    supplied = ('UI success context 1 page 1', PNG_SIGNATURE+b'controlled','image/png')
    attachments.publish_attachment(supplied)
    event = json.loads(channel.read_text())
    assert event['node'] == 'tests/test_ui.py::test_ui'
    assert attachments.read_attachment(event['data'],channel.parent) == supplied
    assert (channel.parent/event['data']['name']).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('missing',['API_AGENT_INTEGRATION_EVENTS','PYTEST_CURRENT_TEST'])
def test_outside_launch_does_not_create_files(channel,monkeypatch,missing):
    monkeypatch.delenv(missing)
    attachments.publish_attachment(None)
    assert not channel.exists()


@pytest.mark.parametrize('title,body,mime',[
    ('secret title',b'x','image/png'),('UI failure context 1 page 1',bytearray(b'x'),'image/png'),
    ('UI failure context 1 page 1',b'not png','image/png'),
    ('UI failure context 1 page 1',PNG_SIGNATURE+b'x'*MAX_IMAGE_BYTES,'image/png'),
    ('UI failure context 1 page 1',b'x','text/html'),
    ('UI failure context 1 page 1',b'x'*1025,'application/json'),
    ('UI failure context 1 page 1',b'\xff','application/json'),
    ('UI failure context 1 page 1',b'{}','application/json'),
    ('UI failure context 1 page 1',b'{"status":"unavailable","phase":"failure","context":true,"page":1}','application/json'),
    ('UI failure context 1 page 1',b'{"status":"unavailable","phase":"failure","context":1,"page":33}','application/json'),
])
def test_invalid_attachment_cannot_enter_channel(channel,title,body,mime):
    with pytest.raises(ValueError): attachments.publish_attachment((title,body,mime))
    assert not channel.exists()


def test_unavailable_metadata_round_trip(channel):
    content=json.dumps({'status':'unavailable','phase':'failure','context':1,'page':0}).encode()
    supplied=('UI failure context 1 page 0',content,'application/json')
    attachments.publish_attachment(supplied)
    assert attachments.read_attachment(json.loads(channel.read_text())['data'],channel.parent)==supplied


@pytest.mark.parametrize('change',[{'name':'../outside'}, {'sha256':'bad'}, {'extra':'bad'}, {'name':None}])
def test_descriptor_cannot_redirect_or_replace_identity(channel,change):
    attachments.publish_attachment(('UI failure context 1 page 1',PNG_SIGNATURE,'image/png'))
    row=json.loads(channel.read_text())['data']
    row.update(change)
    with pytest.raises(ValueError): attachments.read_attachment(row,channel.parent)


@pytest.mark.parametrize('damage',['hash','mode','large','symlink','fifo','content'])
def test_file_replacement_is_rejected_before_recording(channel,damage,tmp_path):
    attachments.publish_attachment(('UI failure context 1 page 1',PNG_SIGNATURE,'image/png'))
    row=json.loads(channel.read_text())['data']
    path=channel.parent/row['name']
    if damage=='hash': path.write_bytes(PNG_SIGNATURE+b'changed')
    elif damage=='mode': path.chmod(0o644)
    elif damage=='large':
        with path.open('r+b') as stream: stream.truncate(MAX_IMAGE_BYTES+1)
    elif damage=='content':
        path.write_bytes(b'malformed')
        row['sha256']=hashlib.sha256(b'malformed').hexdigest()
    elif damage=='symlink':
        other=tmp_path/'other'
        other.write_bytes(PNG_SIGNATURE)
        path.unlink()
        path.symlink_to(other)
    else:
        path.unlink()
        os.mkfifo(path,0o600)
    with pytest.raises(ValueError): attachments.read_attachment(row,channel.parent)


@pytest.mark.parametrize('enabled',[True,False])
def test_real_worker_attachment_integrity_and_content_gate(tmp_path,enabled,monkeypatch):
    from types import SimpleNamespace
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    suite=tmp_path/'tests/integration'
    suite.mkdir(parents=True)
    (suite/'test_ui.py').write_text('''import os,json,pytest
from pathlib import Path
from api_agent.testing.browser_attachments import publish_attachment
from api_agent.testing.browser_evidence import PNG_SIGNATURE
@pytest.mark.requires_ui_session
def test_ui():
    publish_attachment(('UI success context 1 page 1',PNG_SIGNATURE+b'controlled','image/png'), owner={
      'kind':'turn','conversation_id':'00000000-0000-0000-0000-000000000001',
      'turn_id':'00000000-0000-0000-0000-000000000002'})
    event=Path(os.environ['API_AGENT_INTEGRATION_EVENTS'])
    name='0'*32+'.evidence'
    descriptor=os.open(event.parent/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as stream:stream.write(PNG_SIGNATURE)
    row={'kind':'browser_attachment','node':'tests/integration/test_ui.py::test_ui','data':{
      'name':name,'title':'UI success context 1 page 2','mime':'image/png','sha256':'0'*64}}
    with event.open('a') as stream:
      stream.write(json.dumps(row)+'\\n')
      stream.write(json.dumps({'kind':'inspection_lost'})+'\\n')
''')
    context=SimpleNamespace(base_root=tmp_path,owner_root=tmp_path,suite_paths=lambda mode:[suite])
    config={'LOG_CONTENT':enabled}
    selected=collect_tests(context,'integration',config=config)
    if not enabled:
        monkeypatch.setattr(attachments,'read_attachment',lambda *args:pytest.fail('content-off file read'))
    events=[]
    result=run_integration_tests(context,selected,accepted=True,config=config,on_event=events.append)
    assert result.exit_code==0 and result.tests[0].status=='passed'
    images=[row for row in events if row.kind=='browser_attachment']
    assert len(images)==int(enabled)
    if enabled:
        assert images[0].attachment[1]==PNG_SIGNATURE+b'controlled'
        assert json.loads(images[0].text)['owner'] == dict(kind='turn',
            conversation_id='00000000-0000-0000-0000-000000000001',
            turn_id='00000000-0000-0000-0000-000000000002')
    assert any(row.kind=='inspection_lost' for row in events)


@pytest.mark.parametrize('enabled,has_attachment',[(True,True),(True,False),(False,True)])
def test_recording_attaches_browser_bytes_to_matching_result_only(tmp_path,enabled,has_attachment):
    from api_agent.testing import recorded
    from api_agent.testing.contracts import RunnerEvent,TestResult
    from api_agent.testing.integration import IntegrationRunResult
    from test_recorded_integration import fixture,Storage
    context,test,config=fixture(tmp_path)
    config['LOG_CONTENT']=enabled
    supplied=('UI success context 1 page 1',PNG_SIGNATURE+b'controlled','image/png')
    def runner(context,tests,**hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('browser_attachment',identity=test,attachment=supplied if has_attachment else None))
        outcome=TestResult(test,'passed')
        hooks['on_result'](outcome)
        result=IntegrationRunResult(0,(outcome,),'')
        hooks['after_run'](result)
        return result
    result=recorded.run_recorded_integration(context,[test],config,accepted=True,storage=Storage(),runner=runner)
    row=json.loads(next(result.directory.glob('*-result.json')).read_text())
    images=[a for a in row['attachments'] if a['type']=='image/png']
    assert len(images)==int(enabled and has_attachment)
    if images:assert (result.directory/images[0]['source']).read_bytes()==supplied[1]
