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


def test_optional_rejections_preserve_v1_and_project_only_approved_fields():
    previous = tool_evidence({}, graph_id='graph', instances={})
    assert previous['version'] == 1 and 'rejections' not in previous
    partial = {'calls':[{'tool':'read', 'invocation':'call', 'status':'rejected'}],
        'rejections':[{'reason':'unknown_tool','model_round':2,'call_index':1000,'request_id':'safe-request',
                       'tool':'secret-name','invocation':'secret-id','arguments':{'secret':'payload'}}]}
    report = tool_evidence(partial, graph_id='graph', instances={'read':'fixture/read'})
    assert report['calls'][0]['status'] == 'rejected'
    assert report['rejections'] == [{'reason':'unknown_tool','model_round':2,'call_index':1000,'request_id':'safe-request'}]
    assert 'secret' not in json.dumps(report)


@pytest.mark.parametrize('change', [
    {'reason':'arbitrary'}, {'model_round':0}, {'call_index':0},
    {'request_id':'invalid\nlabel'}, {'tool':'credential'},
])
def test_rejection_schema_rejects_unapproved_fields_or_invalid_structure(change):
    report = tool_evidence({}, graph_id='graph', instances={})
    marker = {'reason':'duplicate_call','model_round':1,'call_index':2,'request_id':'safe'}
    marker.update(change)
    report['rejections'] = [marker]
    with pytest.raises(ValidationError): validate_tool_evidence(report)


def test_rejection_array_has_finite_public_bound():
    report = tool_evidence({}, graph_id='graph', instances={})
    report['rejections'] = [{'reason':'unknown_tool','model_round':1,'call_index':1,'request_id':'safe'}] * 129
    with pytest.raises(ValidationError): validate_tool_evidence(report)


@pytest.mark.parametrize('later', ['provider', 'cleanup'])
def test_actual_shared_integration_publishes_immediate_proof_after_failure(tmp_path, monkeypatch, later):
    import asyncio
    from contextlib import asynccontextmanager
    import importlib.util
    from pathlib import Path
    from api_agent.api.application import Runtime
    from api_agent.providers.llm import LLMResult, Message, TextContent, ToolCall, Usage
    from test_tool_fixture import ROOT, external_plan, get, server

    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_tools.py::test_failure (call)')
    module_path = ROOT / 'tests/integration/test_tool_instances.py'
    spec = importlib.util.spec_from_file_location('shared_reporting_trial', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with server(tmp_path / 'backend') as endpoint:
        get(endpoint, '/seed')
        expected = get(endpoint, '/control/value')['hello']
        plan = external_plan(endpoint, endpoint)
        definition, = plan.definitions
        operation = SimpleNamespace(plan=SimpleNamespace(shared_primary=True, tools=[
            SimpleNamespace(id='fixture/primary'), SimpleNamespace(id='fixture/shared-secondary',
                derived_from={'ENDPOINT': 'fixture/primary', 'HEADERS': 'fixture/primary'})]),
            endpoint_for=lambda identity: endpoint + '/graphql', receipt={})

        @asynccontextmanager
        async def open_operation(selected):
            runtime = Runtime('fixture', tool_plan=plan, services={'owner_root': ROOT})
            await runtime.astart()
            operation.tools = runtime.tools_for(selected)
            try:
                yield operation
            finally:
                await runtime.aclose()
                operation.receipt.update(tool_count=2, remaining_owners=0, service_count=0,
                    closed_tools=2, released=True)
                if later == 'cleanup':
                    raise RuntimeError('Synthetic cleanup failure')

        responses = iter([
            LLMResult(Message('assistant', calls=(
                ToolCall('primary-call', 'primary_read', '{"query":"{ hello }"}'),
                ToolCall('secondary-call', 'secondary_read', '{"query":"{ hello }"}'))),
                Usage(5, 2), outcome='tool_calls'),
            LLMResult(Message('assistant', (TextContent('{"relevant":true,"grounded":true}'),)), Usage(3, 1)),
            LLMResult(Message('assistant', (TextContent('{"relevant":true,"grounded":true}'),)), Usage(3, 1)),
            RuntimeError('Synthetic later provider failure') if later == 'provider' else
                LLMResult(Message('assistant', (TextContent(json.dumps(dict.fromkeys(
                    ('primary_read', 'secondary_read'), expected))),)), Usage(5, 2)),
        ])

        def complete(request):
            response = next(responses)
            if isinstance(response, BaseException):
                raise response
            return response

        tools = SimpleNamespace(definition_for_case=lambda case: definition,
            open=open_operation, run=asyncio.run)
        with pytest.raises(RuntimeError, match='Synthetic'):
            module.test_shared_primary(tools, SimpleNamespace(complete=complete), 'synthetic',
                SimpleNamespace(values={}))
    events = [json.loads(line) for line in path.read_text().splitlines()]
    report = next(event['data'] for event in events if event['data'].get('kind') == 'tool_execution')
    assert len(report['calls']) == 2
    assert all(call['result_matches'] and call['result_received'] for call in report['calls'])
    assert report['ownership']['owners_remaining'] == 0
    assert expected not in path.read_text()
