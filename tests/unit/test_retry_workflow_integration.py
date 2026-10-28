import asyncio
import json
from pathlib import Path
from hashlib import sha256
from uuid import UUID
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupServer
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.testing.provider_setup import RemoteIntegrationLLM
from api_agent.testing.workflow_accounting import apply_frozen_steps, join_workflow_accounting
from api_agent.testing.workflow_steps import scope_steps
from api_agent.workflows.application import build
from api_agent.workflows.journal import WorkflowJournal

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('exhausted', [False, True])
def test_retry_crosses_ipc_into_frozen_workflow(tmp_path, monkeypatch, exhausted):
    from api_agent.providers import inference_retry

    monkeypatch.setattr(inference_retry, 'retry_delay', lambda number, hint: 0)
    path = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_example.py::test_call (call)')

    async def scenario():
        physical = []
        tools = []
        logical_calls = []
        async def invoke(callback):
            return await callback(auth='selected')
        async def complete(request, *, auth):
            physical.append(request)
            if len(physical) == 1 or exhausted:
                error = ProviderAccessError('unavailable', 'Synthetic failure', status=503,
                                            usage=Usage(7, 1) if len(physical) == 1 else None)
                error.execution = 'possibly_executed'
                error.http_status = 503
                raise error
            return LLMResult(Message('assistant', (TextContent('Ok'),)), Usage(3, 2),
                             billing={'usd': 0.2, 'request_id': None, 'generation_id': None})
        owner = SharedSetupOwner('operation', 'synthetic', lambda _: SimpleNamespace(call=invoke, aclose=AsyncMock()), complete=complete, max_attempts=2)
        owner.quote_cost = lambda request, result: ({'list_cost_usd': 0.3, 'payment': None}
                                                    if isinstance(result, LLMResult) else None)
        registry = SetupRegistry('operation', 'synthetic', lambda: owner)
        server = SetupServer(registry, 'synthetic-capability-value-0000000000')
        endpoint = await server.start()
        remote = RemoteIntegrationLLM(endpoint)
        from api_agent.providers.tools import ToolResult, ToolSpec
        from api_agent.workflows.tool_gate import ToolGate
        class ToolOwner:
            async def describe(self):
                return ToolSpec('lookup', 'Read a synthetic value', {'type': 'object'},
                                {'type': 'object'}, sha256(b'lookup').hexdigest())
            async def validate(self, prepared):
                pass
            async def invoke(self, prepared):
                tools.append(prepared.arguments)
                return ToolResult('success', {'value': 'ok'})
            async def aclose(self):
                pass
        async def logical_call(request):
            logical_calls.append(request)
            gate = ToolGate(ToolOwner(), invocation_id='synthetic-turn')
            prepared = await gate.prepare({})
            verified = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
            await gate.execute(verified)
            return await asyncio.to_thread(remote.complete, request)
        workflow = build(ROOT, llm=logical_call, selection='synthetic')
        journal = WorkflowJournal(workflow.definition, component_passes=workflow.component_passes)
        workflow.services['journal'] = journal
        try:
            if exhausted:
                with pytest.raises(ProviderAccessError):
                    await workflow.arun({'messages': [{'role': 'user', 'content': 'Hello'}]})
            else:
                result = await workflow.arun({'messages': [{'role': 'user', 'content': 'Hello'}]})
        finally:
            await asyncio.to_thread(remote.close)
            await server.aclose()
            await registry.aclose()
        if not exhausted:
            assert result['llm_call']['response_text'] == 'Ok'
        assert len(tools) == 1 and len(logical_calls) == 1 and len(physical) == 2
        captured = journal.finalize()
        calls = [row for row in captured['events'] if row['event'] == 'call']
        assert len(calls) == 1
        attempts = calls[0]['transport_attempts']['attempts']
        assert [row['outcome'] for row in attempts] == ['failed', 'failed' if exhausted else 'completed']
        assert [row['index'] for row in attempts] == [1, 2]
        assert len({str(UUID(row['id'])) for row in attempts}) == 2
        assert attempts[0]['usage']['input_tokens'] == 7
        assert attempts[1]['usage'] is None if exhausted else attempts[1]['usage']['input_tokens'] == 3
        events = [json.loads(line)['data'] for line in path.read_text().splitlines()]
        published = [row for row in events if row['finished_at'] is not None]
        accounting = {'tests': [{'test_id': 'case', 'calls': published, 'calls_complete': True}]}
        _, facts = join_workflow_accounting({'case': [captured]}, accounting,
            admissions={captured['projection']['definition_id']: captured['projection']})
        graph = scope_steps(captured['events'])[0]
        apply_frozen_steps([graph], 'case', facts)
        assert graph['status'] == ('failed' if exhausted else 'broken')
        component = graph['steps'][0]
        node = component['steps'][0]
        call = node['steps'][0]
        assert call['status'] == ('failed' if exhausted else 'broken')
        assert [step['status'] for step in call['steps']] == ['broken', 'failed' if exhausted else 'passed']
        for scope in (graph, component, node):
            params = {item['name']: item['value'] for item in scope['parameters']}
            assert params['attempt'] == '1' and params['max_passes'] == '1'
        params = {item['name']: item['value'] for item in call['parameters']}
        assert params['transport_known_input_tokens'] == ('7' if exhausted else '10')
        assert params['input_tokens'] == ('unknown' if exhausted else '10')
        assert params['output_tokens'] == ('unknown' if exhausted else '3')
        assert params['list_cost_usd'] == 'unknown'
        assert len(published) == 1 and len({event['call_id'] for event in events}) == 1
        assert calls[0]['call_id'] == published[0]['call_id']
        assert call['name'] == 'call ' + calls[0]['call_id']
        assert params['transport_known_list_cost_usd'] == ('0.0' if exhausted else '0.3')
        assert params['transport_unknown_cost_attempts'] == ('2' if exhausted else '1')
        assert params['transport_known_reported_charge_usd'] == ('0.0' if exhausted else '0.2')
        assert params['transport_unknown_charge_attempts'] == ('2' if exhausted else '1')
    asyncio.run(scenario())
