"""Observed GPT retry payloads retain their physical owners in a recorded graph."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from api_agent.llm_providers.gpt import inference
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, ResponsePolicy, TextContent
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupClient, SetupServer
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.payloads import PayloadResolver, hydrate_journal
from api_agent.testing.recorded import run_recorded_integration
from api_agent.workflows.attribution import bind_workflow_visit
from api_agent.workflows.inference_capture import bind_boundary, capture_rows
from api_agent.workflows.observations import WorkflowObservationFactory
from test_gpt_inference import BASE
from test_recorded_integration import fixture, Storage
from test_workflow_observations import config, definition


@pytest.mark.parametrize('stream', [False, True])
def test_gpt_retry_payloads_reach_recorded_attempt_attachments(tmp_path, monkeypatch, stream):
    from api_agent.providers import inference_retry
    monkeypatch.setattr(inference_retry, 'retry_delay', lambda *args: 0)
    secret = 'synthetic-retry-secret-value'
    authority_secret = 'synthetic-separate-authority-secret'
    authority = EmissionBoundary(ContentPolicy(True), secrets=(authority_secret,))
    physical = []

    async def scenario():
        def respond(request):
            physical.append(json.loads(request.content))
            if len(physical) == 1:
                return httpx.Response(503, json={'error': {'message': 'first rejected ' + secret + ' ' + authority_secret},
                                                 'usage': {'input_tokens': 2, 'output_tokens': 1}})
            success = {'status': 'completed', 'provider_extra': 'observed success',
                'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Ok'}]}],
                'usage': {'input_tokens': 3, 'output_tokens': 1}, 'hidden_reasoning': 'hidden-sentinel'}
            if stream:
                return httpx.Response(200, text='data: ' + json.dumps({'type': 'response.completed', 'response': success}) + '\n\n',
                                      headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, json=success)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            async def authenticate(callback):
                return await callback(auth=secret)
            async def complete(request, *, auth):
                return await inference.complete_text(client=http, base_url=BASE, access_token=auth,
                    subscription=stream, api_mode='responses', request=request, authority=authority)
            owner = SharedSetupOwner('operation', 'selected', lambda _: SimpleNamespace(call=authenticate, aclose=AsyncMock()),
                                     complete=complete, max_attempts=2, captures_effective_payload=True)
            async def source(request):
                bind_boundary(authority)
                return await owner.complete(request)
            registry = SetupRegistry('operation', 'selected', lambda: SimpleNamespace(ensure_ready=AsyncMock(), complete=source, aclose=owner.aclose))
            server = SetupServer(registry, 'synthetic-capability-value-0000000000')
            address = await server.start()
            def consume():
                client = SetupClient(address, timeout=5)
                try:
                    result = client.complete(LLMRequest('gpt-example', CompletionRequest(
                        (Message('user', (TextContent('Say Ok'),)),), response=ResponsePolicy('text', 'reject'))))
                    return result, client.evidence, client.transport_attempts
                finally:
                    client.close()
            try:
                return await asyncio.to_thread(consume)
            finally:
                await server.aclose()
                await registry.aclose()

    result, rows, ledger = asyncio.run(scenario())
    assert result.payload.content[0].text == 'Ok'
    assert len(physical) == 2 and physical[0] == physical[1]
    attempts = ledger['attempts']
    assert [item['outcome'] for item in attempts] == ['failed', 'completed']
    assert attempts[0]['status'] == 503 and attempts[0]['usage']['input_tokens'] == 2
    assert attempts[0]['usage']['output_tokens'] == 1
    assert attempts[0]['response']['actual_mode'] == 'text'
    ids = [item['id'] for item in attempts]
    assert len(set(ids)) == 2
    assert {(row['phase'], row['transport_attempt_id']) for row in rows} == {
        (phase, attempt) for phase in ('request', 'response') for attempt in ids}
    assert len(rows) == 4 and all(row.get('capture_source') == 'provider' for row in rows)

    adapter = WorkflowObservationFactory(config(True, False), boundary=EmissionBoundary()).create(definition())
    visit, call, graph = (str(uuid4()) for _ in range(3))
    adapter.observe(dict(event='scope_start', scope_id=graph, parent_id=None, owner='GRAPH', kind='graph', started_ms=999))
    adapter.observe(dict(event='edge', owner='GRAPH', source='START', target='Make', implicit=True))
    adapter.observe(dict(event='scope_start', scope_id=visit, parent_id=graph, owner='Make', kind='node', started_ms=1000))
    with bind_workflow_visit(adapter, visit):
        capture_rows(rows, call)
    adapter.record_call(visit_id=visit, call_id=call, usage={'input_tokens': 5, 'output_tokens': 2}, outcome='completed', transport_attempts=ledger)
    adapter.observe(dict(event='scope_end', scope_id=visit, parent_id=graph, owner='Make', kind='node', started_ms=1000, stopped_ms=1001, outcome='passed'))
    adapter.observe(dict(event='edge', owner='GRAPH', source='Make', target='END', implicit=True, visit_id=visit))
    adapter.observe(dict(event='scope_end', scope_id=graph, parent_id=None, owner='GRAPH', kind='graph', started_ms=999, stopped_ms=1002, outcome='passed'))
    retained = adapter.finalize()
    context, selected, recording_config = fixture(tmp_path)
    recording_config['LOG_CONTENT'] = True
    def runner(context, tests, **hooks):
        hooks['before_run']()
        for row in rows:
            hooks['on_event'](RunnerEvent('inference_content', selected.repo, selected, text=json.dumps(dict(call_id=call, **row))))
        hooks['on_event'](RunnerEvent('workflow_evidence', selected.repo, selected, text=json.dumps(retained)))
        outcome = TestResult(selected, 'passed', .01)
        hooks['on_result'](outcome)
        execution = IntegrationRunResult(0, (outcome,), '')
        hooks['after_run'](execution)
        return execution
    published = run_recorded_integration(context, (selected,), recording_config, accepted=True, storage=Storage(), runner=runner)
    assert published.persisted and published.exit_code == 0
    rendered = json.loads(next(published.directory.glob('*-result.json')).read_text())
    resolver = PayloadResolver.local(published.directory, published.manifest)
    index = resolver.index(next(row['source'] for row in rendered['attachments'] if row['name'] == 'Payload index'))
    entries = [entry for entry in index['entries'] if entry['occurrence'].get('kind') == 'llm']
    assert len(entries) == 4
    assert all(entry['occurrence']['capture_source'] == 'provider' for entry in entries)
    success_entry = next(entry for entry in entries if entry['occurrence']['phase'] == 'response'
                         and entry['occurrence']['transport_attempt_id'] == ids[1])
    assert success_entry['reason'] == ('partial' if stream else 'captured')
    assert {(entry['occurrence']['phase'], entry['occurrence']['transport_attempt_id']) for entry in entries} == {
        (phase, attempt) for phase in ('request', 'response') for attempt in ids}
    bodies = {(entry['occurrence']['phase'], entry['occurrence']['transport_attempt_id']):
              resolver({'kind': 'payload', 'source': entry['source'], 'sha256': entry['sha256'], 'length': entry['length']}) for entry in entries}
    assert bodies[('response', ids[0])]['error']['message'] == 'first rejected [REDACTED] [REDACTED]'
    assert bodies[('response', ids[1])]['provider_extra'] == 'observed success'
    for attempt in ids:
        assert bodies[('request', attempt)]['provider_payload']['input'][0]['content'] == 'Say Ok'
    rendered_graph = rendered['steps'][0]
    envelope_source = next(item['source'] for item in rendered_graph['attachments'] if item['name'] == 'Execution evidence')
    assert hydrate_journal(json.loads((published.directory / envelope_source).read_text()), resolver) == retained
    def descendants(step):
        yield step
        for child in step.get('steps', []):
            yield from descendants(child)
    steps = list(descendants(rendered_graph))
    for attempt in ids:
        step = next(step for step in steps if any(p['name'] == 'transport_id' and p['value'] == attempt for p in step.get('parameters', [])))
        sources = [item['source'] for item in step.get('attachments', [])]
        expected = [entry['source'] for entry in entries if entry['occurrence']['transport_attempt_id'] == attempt]
        assert sorted(sources) == sorted(expected) and len(sources) == 2
        if attempt == ids[1]:
            response_attachment = next(item for item in step['attachments'] if item['source'] == success_entry['source'])
            assert (', partial)' in response_attachment['name']) is stream
    contents = '\n'.join(path.read_text() for path in published.directory.rglob('*') if path.is_file())
    assert secret not in contents and authority_secret not in contents and 'hidden-sentinel' not in contents
