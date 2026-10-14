"""Owned synthetic credential partitioning across HTTP, IPC and artifacts."""
import asyncio
import json
from copy import deepcopy
import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from test_responses_native_metadata import TOKEN, REQUEST, native
from api_agent.llm_providers.gpt import inference
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import LLMResult, Message, TextContent, ToolCall, Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupClient, SetupServer
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.inference_content import content_boundary, export_content, content_attachment
from api_agent.testing.recording import RunIdentity, RunRecorder
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult


async def invoke(data, subscription, request):
    def response(req):
        if not subscription:
            return httpx.Response(200, json=data)
        events = []
        for index, item in enumerate(data['output']):
            added = deepcopy(item)
            if item['type'] == 'function_call':
                added.update(status='in_progress', arguments='')
            events.extend([dict(type='response.output_item.added', output_index=index, item=added),
                           dict(type='response.output_item.done', output_index=index, item=item)])
        events.append(dict(type='response.completed', response=data))
        return httpx.Response(200, text=''.join('data: ' + json.dumps(row) + '\n\n' for row in events),
                              headers={'content-type': 'text/event-stream'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        return await inference.complete_text(client=client, base_url='https://api.openai.com/v1',
            access_token=TOKEN, subscription=subscription, api_mode='responses', request=request)


def partitioned(text):
    data = native([], 'commentary')
    data['output'][1]['content'] = [dict(type='output_text', text=part, annotations=[])
                                  for part in (text[:12], text[12:])]
    return data


@pytest.mark.parametrize('subscription', [False, True])
@pytest.mark.parametrize('text', [TOKEN, 'ordinary split text'])
def test_provider_combined_text_through_actual_ipc(subscription, text):
    async def scenario():
        async def complete(request):
            return await invoke(partitioned(text), subscription, request)
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock())
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-partitioned-capability-000000')
        client = SetupClient(await server.start(), worker_id='owned-worker', timeout=5)
        try:
            if text == TOKEN:
                with pytest.raises(ProviderAccessError) as caught:
                    await asyncio.to_thread(client.complete, REQUEST)
                assert caught.value.usage == Usage(7, 3)
                assert caught.value.execution == 'possibly_executed'
                assert TOKEN not in str(caught.value)
            else:
                result = await asyncio.to_thread(client.complete, REQUEST)
                assert ''.join(part.text for part in result.payload.content) == text
                assert result.usage == Usage(7, 3)
            assert client.evidence == []
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('backend_registered', [False, True])
@pytest.mark.parametrize('secret, parts', [(TOKEN, (TOKEN[:12], TOKEN[12:])), ('1234', ('12 ', '34'))])
def test_independent_result_ipc_to_recorded_attachment(enabled, backend_registered, secret, parts, tmp_path):
    async def scenario():
        result = LLMResult(Message('assistant', tuple(TextContent(part) for part in parts), (ToolCall('call-1', 'lookup', '{}'),)), Usage(2, 1), 'tool_calls')
        backend = content_boundary({'LOG_CONTENT': enabled})
        if backend_registered:
            backend.register_secret(secret)
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=AsyncMock(return_value=result),
                                aclose=AsyncMock(), export_content=lambda req, res: export_content(backend, req, res))
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-partitioned-capability-000000')
        client = SetupClient(await server.start(), worker_id='owned-worker', timeout=5)
        try:
            assert await asyncio.to_thread(client.complete, REQUEST) == result
            parent = EmissionBoundary(ContentPolicy(enabled), secrets=(secret,))
            attachments = [content_attachment(SimpleNamespace(text=json.dumps(dict(call_id=str(uuid4()), **row))), parent)
                           for row in client.evidence]
            if enabled and backend_registered:
                response = next(row for row in client.evidence if row['phase'] == 'response')
                assert ''.join(response['content']['message']['content']) != secret
            recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'), evidence_boundary=parent)
            identity = TestIdentity('owned-test', 'synthetic', 'test_owned.py', 'test_owned.py::test_owned')
            name = recorder.record_test(TestResult(identity, 'passed', 0), attachments=attachments)
            saved = json.loads((tmp_path / name).read_text())
            if enabled:
                row = next(row for row in saved['attachments'] if row['name'].startswith('LLM response'))
                content = json.loads((tmp_path / row['source']).read_bytes())
                assert ''.join(content['message']['content']) != secret
            else:
                assert client.evidence == [] and saved['attachments'] == []
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_generic_boundary_preserves_normal_parts_and_redacts_nested_registered_parts():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    assert boundary.export({'parts': ['ordinary ', 'text']}) == {'parts': ['ordinary ', 'text']}
    assert boundary.export({'parts': [TOKEN[:12], TOKEN[12:]]}) == {'parts': ['[REDACTED]']}
    assert boundary.content_export({'nested': {'parts': (TOKEN[:12], TOKEN[12:])}}) == {
        'nested': {'parts': ['[REDACTED]']}}


def test_final_sanitized_parts_cannot_reconstruct_registered_numeric_secret():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('1234',))
    assert boundary.export({'parts': ['12 ', '34']}) == {'parts': ['[REDACTED]']}
    assert boundary.export({'parts': ['12 ', '35']}) == {'parts': ['12', '35']}
    assert boundary.export({'parts': ['ordinary ', 'text']}) == {'parts': ['ordinary ', 'text']}
    assert EmissionBoundary(secrets=('1234',)).export({'parts': ['12 ', '34']}) == {}


@pytest.mark.parametrize('line', [-1, None])
def test_enabled_owned_log_omits_invalid_source_line(line):
    import logging
    from api_agent.telemetry.privacy import OwnedLogFilter
    record = logging.LogRecord('owned', logging.INFO, '', 1, 'safe context', (), None)
    record.lineno = line
    assert OwnedLogFilter(EmissionBoundary(ContentPolicy(True))).filter(record)
    assert record.lineno == 0


SECONDARY = 'synthetic-secondary-auth'


@pytest.mark.parametrize('subscription', [None, False, True], ids=['constructed', 'http', 'sse'])
@pytest.mark.parametrize('enabled', [False, True], ids=['default-off', 'content-enabled'])
@pytest.mark.parametrize('parts', [('Bearer ', SECONDARY), ('Be', 'arer\t', SECONDARY),
                                  ('ordinary ', 'text'), ('Bearer ', '[REDACTED]'),
                                  ('Bearer ' + SECONDARY,)],
                         ids=['split-prefix', 'split-word', 'ordinary', 'already-redacted', 'contiguous'])
def test_secondary_credentials_through_ipc_and_recording(subscription, enabled, parts, tmp_path):
    async def scenario():
        constructed = LLMResult(Message('assistant', tuple(TextContent(part) for part in parts),
                                       (ToolCall('call-1', 'lookup', '{}'),)), Usage(7, 3), 'tool_calls')
        backend = content_boundary({'LOG_CONTENT': enabled})
        backend.register_secret(TOKEN)

        async def complete(request):
            if subscription is None:
                return constructed
            data = native([], 'commentary')
            data['output'][1]['content'] = [dict(type='output_text', text=part, annotations=[])
                                          for part in parts]
            return await invoke(data, subscription, request)

        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=complete, aclose=AsyncMock(),
                                export_content=lambda req, res: export_content(backend, req, res))
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-secondary-capability-000000')
        client = SetupClient(await server.start(), worker_id='owned-worker', timeout=5)
        try:
            actual = await asyncio.to_thread(client.complete, REQUEST)
            assert tuple(part.text for part in actual.payload.content) == parts
            assert actual.usage == Usage(7, 3)
            parent = EmissionBoundary(ContentPolicy(enabled), secrets=(TOKEN,))
            attachments = [content_attachment(SimpleNamespace(text=json.dumps(dict(call_id=str(uuid4()), **row))), parent)
                           for row in client.evidence]
            recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'), evidence_boundary=parent)
            identity = TestIdentity('owned-test', 'synthetic', 'test_owned.py', 'test_owned.py::test_owned')
            name = recorder.record_test(TestResult(identity, 'passed', 0), attachments=attachments)
            saved = json.loads((tmp_path / name).read_text())
            if not enabled:
                assert client.evidence == []
                assert saved['attachments'] == []
                return
            response = next(row for row in client.evidence if row['phase'] == 'response')
            assert SECONDARY not in ''.join(response['content']['message']['content'])
            row = next(row for row in saved['attachments'] if row['name'].startswith('LLM response'))
            content = json.loads((tmp_path / row['source']).read_bytes())['message']['content']
            expected = ['[REDACTED]'] if len(parts) > 1 and SECONDARY in parts else list(parts)
            if len(parts) == 1:
                expected = ['Bearer [REDACTED]']
            assert content == expected
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('parts', [('Bearer ', SECONDARY), ('Be', 'arer\t', SECONDARY)])
def test_combined_raw_secondary_credential_pattern(parts):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    assert boundary.export({'parts': parts}) == {'parts': ['[REDACTED]']}


def test_combined_normalized_secondary_credential_pattern():
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(TOKEN,))
    # An out-of-range JSON integer is omitted; its removal joins the prefix.
    omitted = "9007199254740992"
    assert boundary.export({"parts": ["Be", omitted, "arer " + SECONDARY]}) == {"parts": ["[REDACTED]"]}
    assert boundary.export({"parts": ["ordinary ", omitted, "text"]}) == {"parts": ["ordinary ", "text"]}
