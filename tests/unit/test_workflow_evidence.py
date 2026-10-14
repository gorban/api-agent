import json
from types import SimpleNamespace
import pytest
from api_agent.workflows.evidence import publish, attachment


def test_publish_requires_recording_context(monkeypatch, tmp_path):
    path=tmp_path/'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.delenv('PYTEST_CURRENT_TEST',raising=False)
    publish({'trial':1})
    assert not path.exists()


def test_publish_numeric_trace(monkeypatch,tmp_path):
    path=tmp_path/'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test_workflow.py::test_coverage (call)')
    calibration = {'minimum_runs':16, 'safety_margin':4, 'max_runs':20,
                   'minimum_missing_node_bound':0.0009, 'missing_node_bound':0.0003}
    publish({'trial':1,'events':[{'event':'guard','accepted':False,'attempt':1}],
             'calibration':dict(calibration, receipt='synthetic private signature',
                                private_secret='synthetic private credential')})
    row=json.loads(path.read_text())
    assert row['kind']=='workflow_evidence'
    assert row['node']=='test_workflow.py::test_coverage'
    assert row['data']['events'][0]['accepted'] is False
    assert row['data']['calibration'] == calibration
    assert 'synthetic private' not in path.read_text()


def test_attachment_exports_through_boundary():
    boundary=SimpleNamespace(content_export=lambda value: {'sanitized':value['trial']})
    title,content,kind=attachment(SimpleNamespace(text='{"trial":2}'),boundary)
    assert json.loads(content)=={'sanitized':2}
    assert kind=='application/json'
    assert title=='Workflow execution evidence'


def test_attachment_rejects_nonobject():
    with pytest.raises(ValueError):
        attachment(SimpleNamespace(text='[]'),None)


def test_sanitize_drops_content_and_nonfinite():
    from api_agent.workflows.evidence import sanitize
    assert sanitize({'events':[{'event':'node','original_input':'secret','elapsed':float('nan')}], 'trial':1}) == {'events':[{'event':'node','elapsed':None}],'trial':1}
    assert sanitize('email@example.com') is None
    assert sanitize(None) is None


def test_sanitize_finite_number():
    from api_agent.workflows.evidence import sanitize
    assert sanitize(0.25) == 0.25


@pytest.fixture
def measured_report():
    from api_agent.workflows.sampling import measured_calibration, DECLARED
    return measured_calibration([{'trial':trial,'route':'next_a','nodes':sorted(DECLARED)} for trial in range(1,61)],DECLARED)


def test_calibration_receipt_rejects_record_fabrication(tmp_path,measured_report):
    from api_agent.workflows.evidence import reserve_calibration,store_calibration,read_calibration
    path=tmp_path/'pilot.json'
    with reserve_calibration(path):
        store_calibration(path,measured_report)
    assert read_calibration(path)==measured_report
    edited=json.loads(path.read_text())
    edited['records'][0]['route']='next_b'
    path.write_text(json.dumps(edited))
    with pytest.raises(ValueError,match='receipt'):
        read_calibration(path)


def test_existing_destination_prevents_pilot(tmp_path):
    from api_agent.workflows.evidence import reserve_calibration
    path=tmp_path/'pilot.json'
    path.write_text('{}')
    calls=[]
    with pytest.raises(FileExistsError):
        with reserve_calibration(path):
            calls.append('provider')
    assert calls==[]


def test_concurrent_reservation_serializes_and_cleans_up(tmp_path):
    from api_agent.workflows.evidence import reserve_calibration
    path=tmp_path/'pilot.json'
    with reserve_calibration(path):
        with pytest.raises(FileExistsError):
            with reserve_calibration(path):
                pytest.fail('Competing pilot entered')
    assert not path.with_name('pilot.json.reservation').exists()


def test_reservation_cleans_after_defect(tmp_path):
    from api_agent.workflows.evidence import reserve_calibration
    path=tmp_path/'pilot.json'
    with pytest.raises(RuntimeError):
        with reserve_calibration(path):
            raise RuntimeError('controlled defect')
    assert not path.with_name('pilot.json.reservation').exists()


def test_destination_race_is_rejected(tmp_path,monkeypatch):
    from api_agent.workflows import evidence
    path=tmp_path/'pilot.json'
    original=evidence.os.close
    def occupy(descriptor):
        original(descriptor)
        path.write_text('{}')
    monkeypatch.setattr(evidence.os,'close',occupy)
    with pytest.raises(FileExistsError):
        with evidence.reserve_calibration(path):
            pytest.fail('Occupied destination entered')


def test_storage_requires_reservation(tmp_path,measured_report):
    from api_agent.workflows.evidence import store_calibration
    with pytest.raises(ValueError,match='ownership'):
        store_calibration(tmp_path/'pilot.json',measured_report)


@pytest.mark.parametrize('value',['[]','{}','{"receipt":3}'])
def test_calibration_without_receipt_rejected(tmp_path,value):
    from api_agent.workflows.evidence import read_calibration
    path=tmp_path/'pilot.json'
    path.write_text(value)
    with pytest.raises(ValueError,match='receipt'):
        read_calibration(path)


def test_short_local_receipt_key_rejected(tmp_path,measured_report):
    from api_agent.workflows.evidence import reserve_calibration,store_calibration,read_calibration
    path=tmp_path/'pilot.json'
    with reserve_calibration(path):
        store_calibration(path,measured_report)
    path.with_name('pilot.json.receipt-key').write_bytes(b'bad')
    with pytest.raises(ValueError,match='receipt'):
        read_calibration(path)


def test_atomic_store_preserves_competing_destination(tmp_path,measured_report):
    from api_agent.workflows.evidence import reserve_calibration,store_calibration
    path=tmp_path/'pilot.json'
    with reserve_calibration(path):
        path.write_text('competing')
        with pytest.raises(FileExistsError):
            store_calibration(path,measured_report)
    assert path.read_text()=='competing'


def test_reviewed_fingerprint_metadata_survives_without_private_selector():
    from api_agent.workflows.evidence import sanitize
    metadata = {'fingerprint_version': 2,
                'sampling_method': 'simultaneous_exact_binomial_independent_uuid_v1',
                'provider_identity': {'kind': 'bundled_gpt', 'entrypoint': 'factory',
                                      'source_sha256': 'a' * 64,
                                      'private_selector': '/private/provider.py:factory'}}
    assert sanitize(metadata) == {'fingerprint_version': 2,
                                 'sampling_method': 'simultaneous_exact_binomial_independent_uuid_v1',
                                 'provider_identity': {'kind': 'bundled_gpt', 'entrypoint': 'factory',
                                                       'source_sha256': 'a' * 64}}
