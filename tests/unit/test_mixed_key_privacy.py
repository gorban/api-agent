"""Ordinary prefixes cannot hide complete credential carriers in native keys."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from api_agent.providers.inference import request_to_wire, result_from_wire
from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from api_agent.providers.setup_registry import SetupRegistry
from api_agent.providers.setup_transport import SetupServer, _Peer, _decode
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.inference_content import content_attachment
from api_agent.testing.recording import RunIdentity, RunRecorder
from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import TestResult
from test_partitioned_credentials import REQUEST


PRIMARY = 'owned-primary-value'
SENSITIVE = [r'{"access_\u0074oken":"owned-secondary-value"}',
             r'"pass\u0077ord"',
             r'{"note":"Be\u0061rer owned-secondary-value"}',
             r'"\u006fwned-primary-value"',
             '{"ordinary":1,"ordinary":2}']
ORDINARY = ['{"ordinary":17}', '"ordinary"', '{broken}', 'unfinished {']


@pytest.mark.parametrize('carrier', SENSITIVE)
@pytest.mark.parametrize('shell', ['', '\x00', '\u200b'])
def test_prefixed_sensitive_key_is_removed_before_rendering(carrier, shell):
    key = 'log ' + shell + carrier + shell + ' suffix'
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(PRIMARY,))
    assert boundary.export({key: 'safe', 'ordinary': 'keep'}) == {'ordinary': 'keep'}
    assert boundary.content_export({key: 'safe', 'ordinary': 'keep'}) == {'ordinary': 'keep'}


@pytest.mark.parametrize('carrier', SENSITIVE[:3])
def test_prefixed_role_carrier_needs_no_registered_secret(carrier):
    key = 'log ' + carrier + ' suffix'
    boundary = EmissionBoundary(ContentPolicy(True))
    assert boundary.export({key: 'safe', 'ordinary': 'keep'}) == {'ordinary': 'keep'}


@pytest.mark.parametrize('carrier', ORDINARY)
@pytest.mark.parametrize('shell', ['', '\x00', '\u200b'])
def test_prefixed_ordinary_key_preserves_original_spelling(carrier, shell):
    key = 'log ' + shell + carrier + shell + ' suffix'
    payload = {key: 'safe', 'ordinary': 'keep'}
    boundary = EmissionBoundary(ContentPolicy(True), secrets=(PRIMARY,))
    assert boundary.export(payload) == payload
    assert boundary.content_export(payload) == payload


@pytest.fixture(params=[False, True], ids=['disabled', 'enabled'])
def evidence_policy(request):
    return request.param


@pytest.fixture(params=[(carrier, True) for carrier in SENSITIVE] +
                       [(carrier, False) for carrier in ORDINARY])
def mixed_key_case(request):
    carrier, sensitive = request.param
    key = 'log \u200b' + carrier + '\u200b suffix'
    payload = {key: 'safe', 'ordinary': 'keep'}
    return payload, {'ordinary': 'keep'} if sensitive else payload


def recorded_worker_evidence(payload, expected, enabled, tmp_path):
    async def scenario():
        boundary = EmissionBoundary(ContentPolicy(enabled), secrets=(PRIMARY,))
        functional = LLMResult(Message('assistant', (TextContent('Exact answer'),)), Usage(7, 3))
        rows = ({'phase': 'response', 'content': boundary.export(payload)},) if enabled else ()
        owner = SimpleNamespace(ensure_ready=AsyncMock(), complete=AsyncMock(return_value=functional),
                                aclose=AsyncMock(), export_content=lambda req, res: rows)
        registry = SetupRegistry('owned-operation', 'owned-selection', lambda: owner)
        server = SetupServer(registry, 'synthetic-native-key-capability-0000000')
        frames = []
        peer = _Peer(asyncio.StreamReader(), SimpleNamespace(write=frames.append, is_closing=lambda: False))
        try:
            registration = await server._dispatch(peer, {'worker_id': 'owned-worker', 'attempt': 1}, 'register')
            wire = await server._dispatch(peer, {'binding': registration['binding'],
                                               'request': request_to_wire(REQUEST)}, 'complete')
            assert result_from_wire(wire['result']) == functional
            assert functional.usage == Usage(7, 3)
            evidence = [_decode(frame) for frame in frames if _decode(frame)['status'] == 'evidence']
            assert [row['content'] for row in evidence] == ([expected] if enabled else [])
            # Parent has no registered secrets: the backend must protect its own frames.
            parent = EmissionBoundary(ContentPolicy(True))
            attachments = [content_attachment(SimpleNamespace(text=json.dumps(
                dict(call_id=str(uuid4()), phase=row['phase'], content=row['content']))), parent)
                for row in evidence]
            recorder = RunRecorder(tmp_path, RunIdentity.create('synthetic', 'integration'), evidence_boundary=boundary)
            result = TestResult(TestIdentity('owned-test', 'synthetic', 'owned.py', 'owned.py::test_owned'), 'passed', 0)
            filename = recorder.record_test(result, attachments=attachments, steps=[dict(name='step', **payload)])
            saved = json.loads((tmp_path / filename).read_text())
            assert saved['steps'] == [dict(name='step', **expected)]
            assert [json.loads((tmp_path / item['source']).read_text()) for item in saved['attachments']] == (
                [expected] if enabled else [])
        finally:
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_mixed_keys_are_protected_in_worker_frames_and_recorded_steps(mixed_key_case, evidence_policy, tmp_path):
    payload, expected = mixed_key_case
    recorded_worker_evidence(payload, expected, evidence_policy, tmp_path)
