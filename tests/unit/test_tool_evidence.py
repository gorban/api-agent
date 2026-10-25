"""Ordinary report attachments retain structural evidence without tool content."""
import json
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.evidence import attachment, publish
from api_agent.workflows.tool_evidence import tool_evidence, validate_tool_evidence


def journal():
    return {'calls': [{'tool': 'first_read', 'invocation': 'call-1', 'status': 'success',
                      'verified_by_inference': True, 'result': {'hello': 'sensitive-value'},
                      'result_matches': True}],
            'inference': [{'request_id': 'request-1', 'kind': 'conversation', 'outcome': 'length',
                           'usage': None, 'content': ['sensitive-answer']}],
            'final': 'sensitive-answer', 'final_matches': False}


def test_projection_preserves_partial_outcomes_and_excludes_payloads():
    report = tool_evidence(journal(), graph_id='shared_tools', instances={'first_read': 'fixture/primary'})
    assert report['calls'] == [{'tool': 'first_read', 'instance': 'fixture/primary',
        'invocation': 'call-1', 'status': 'success', 'verified_by_inference': True,
        'result_received': True, 'result_matches': True}]
    assert report['inference'][0]['usage'] is None
    assert report['inference'][0]['outcome'] == 'length'
    assert report['final_matches'] is False
    assert report['completed'] is False
    assert 'sensitive' not in json.dumps(report)


def test_projection_supports_preparation_failure_and_explicit_zero_usage():
    partial = {'calls': [{'tool': 'read', 'invocation': 'call-2', 'status': 'preparing'}],
               'inference': [{'request_id': 'request-2', 'kind': 'relevance', 'outcome': 'failed',
                              'usage': {'input_tokens': 0, 'output_tokens': 0}}], 'completed': True}
    report = tool_evidence(partial, graph_id='graph', instances={'read': 'fixture/primary'},
                           ownership={'targets': 1, 'owners_started': 2, 'owners_remaining': 0},
                           sharing={'primary': 'fixture/primary', 'secondary': ['fixture/secondary'],
                                    'derived_slots': ['ENDPOINT', 'HEADERS']})
    assert report['calls'][0]['result_received'] is False
    assert report['calls'][0]['verified_by_inference'] is False
    assert 'result_matches' not in report['calls'][0]
    assert report['inference'][0]['usage']['input_tokens'] == 0
    assert report['completed'] is True
    assert report['ownership']['targets'] == 1
    assert report['sharing']['derived_slots'] == ['ENDPOINT', 'HEADERS']
    assert 'final_matches' not in report


@pytest.mark.parametrize('key,value', [('content', 'raw data'), ('final_matches', 'yes'),
                                     ('ownership', {'endpoint': 'http://127.0.0.1'})])
def test_schema_rejects_unapproved_content_and_false_boolean_claims(key, value):
    report = tool_evidence({}, graph_id='graph', instances={})
    report[key] = value
    with pytest.raises(ValidationError):
        validate_tool_evidence(report)


def test_publish_and_attachment_use_normal_workflow_event_path(monkeypatch, tmp_path):
    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'tests/integration/test_tool_instances.py::test_shared_primary (call)')
    report = tool_evidence(journal(), graph_id='shared_tools', instances={'first_read': 'fixture/primary'})
    publish(report)
    event = json.loads(path.read_text())
    assert event['kind'] == 'workflow_evidence'
    assert event['data'] == report
    name, content, mime = attachment(SimpleNamespace(text=json.dumps(report)),
                                     EmissionBoundary(ContentPolicy(False)))
    assert name == 'Workflow execution evidence'
    assert json.loads(content) == report
    assert mime == 'application/json'


def test_attachment_rejects_credential_boundary_change():
    report = tool_evidence(journal(), graph_id='shared_tools', instances={'first_read': 'fixture/primary'})
    boundary = EmissionBoundary(ContentPolicy(True))
    boundary.register_secret('fixture/primary')
    with pytest.raises(ValueError, match='credential boundary'):
        attachment(SimpleNamespace(text=json.dumps(report)), boundary)
