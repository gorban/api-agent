import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.setup_transport import SetupClient, _encode, _evidence_frames, _EvidenceReceiver, MAX_EVIDENCE_BYTES
from api_agent.workflows.inference_capture import select_capture_rows


def test_fallback_selection_is_scoped_to_physical_attempt():
    first, second = str(uuid4()), str(uuid4())
    rows = [dict(phase='request', content={'text': 'first fallback'}, _source='fallback', transport_attempt_id=first),
            dict(phase='request', content={'text': 'second fallback'}, _source='fallback', transport_attempt_id=second),
            dict(phase='request', content={'text': 'second effective'}, transport_attempt_id=second)]
    selected = select_capture_rows(rows)
    assert [(row['transport_attempt_id'], row['content']['text']) for row in selected] == [
        (first, 'first fallback'), (second, 'second effective')]


def test_attempt_id_survives_chunked_evidence_round_trip():
    attempt = str(uuid4())
    row = dict(phase='request', content={'text': 'a' * 180000}, reason='captured',
               transport_attempt_id=attempt, capture_source='fallback')
    receiver = _EvidenceReceiver()
    result = None
    for frame in _evidence_frames(row, MAX_EVIDENCE_BYTES):
        result = receiver.accept(json.loads(frame))
    assert result == row


def test_client_rejects_evidence_for_unowned_attempt():
    async def scenario():
        client = SetupClient(dict(version=1, host='127.0.0.1', port=1,
                                  capability='synthetic-attempt-capability-000000'))
        client._binding = {'version': 1, 'operation_id': 'op', 'selected_identity': 'selected',
                           'worker_id': 'worker', 'attempt': 1}
        client._reader = asyncio.StreamReader()
        client._writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        client._reader.feed_data(_encode(dict(version=1, status='evidence', phase='request',
                                             content={}, transport_attempt_id=str(uuid4()))))
        with pytest.raises(ContractError, match='no transport attempt owner'):
            await client._exchange('complete', {})
    asyncio.run(scenario())


def test_direct_provider_output_supersedes_fallback_in_both_orders(monkeypatch):
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows import inference_capture as capture
    from api_agent.providers import generation_attempts
    monkeypatch.setattr(capture, 'current_workflow_visit', lambda: (SimpleNamespace(boundary=EmissionBoundary(ContentPolicy(True))), 'visit', None))
    attempt = str(uuid4())
    monkeypatch.setattr(generation_attempts, 'current_transport_attempt', lambda: attempt)
    recorded = []
    monkeypatch.setattr(capture, 'capture_rows', lambda rows, call: recorded.extend(rows))
    with capture.bind_inference_call(str(uuid4())):
        capture.capture_inference('output', {'text': 'fallback'}, 'cause', fallback=True)
        capture.capture_inference('output', {'text': 'actual'}, 'cause')
        capture.capture_inference('output', {'text': 'late fallback'}, 'cause', fallback=True)
        assert recorded == []
    assert recorded == [dict(phase='response', content={'text': 'actual'}, reason='captured',
                             transport_attempt_id=attempt, capture_source='provider')]


def test_partial_capture_keeps_existing_withholding_reason():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import capture_inference, collect_inference
    for enabled, expected in ((True, 'partial'), (False, 'withheld')):
        with collect_inference(EmissionBoundary(ContentPolicy(enabled))) as collector:
            capture_inference('output', {'text': 'observed'}, 'cause', partial=True)
        assert collector['rows'][0]['reason'] == expected


def test_trusted_raw_capture_is_bounded_and_copies_original_before_projection():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import capture_inference, collect_inference
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('abc',))
    original = {'text': 'abcdef'}
    with collect_inference(boundary, retain_raw=True) as collector:
        capture_inference('input', original, 'cause')
        original['text'] = 'changed'
    assert collector['rows'][0]['content'] == {'text': 'abcdef'}
    # This raw mode is internal custody only; publication must apply a union.


def test_capture_auth_waits_for_publication_before_registration_and_use():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference, prepare_capture_auth, capture_inference
    async def scenario():
        boundary = EmissionBoundary(ContentPolicy(True))
        lease = boundary.begin_evidence_lease()
        with collect_inference(boundary, call_id='cause') as collector:
            task = asyncio.create_task(prepare_capture_auth('synthetic-late-secret'))
            await asyncio.sleep(0)
            assert not task.done()
            assert 'synthetic-late-secret' not in boundary._secrets
            lease.release()
            await task
            capture_inference('input', {'text': 'synthetic-late-secret'}, 'cause', auth='synthetic-late-secret')
            assert 'synthetic-late-secret' not in repr(collector['rows'])
    asyncio.run(scenario())


def test_unprepared_auth_during_lease_records_unavailable_evidence():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference, capture_inference
    boundary = EmissionBoundary(ContentPolicy(True))
    lease = boundary.begin_evidence_lease()
    try:
        with collect_inference(boundary, call_id='cause') as collector:
            capture_inference('input', {'text': 'synthetic-late-secret'}, 'cause', auth='synthetic-late-secret')
            assert collector['rows'] == [{'phase': 'request', 'content': {}, 'reason': 'unavailable'}]
    finally:
        lease.release()


def test_full_raw_content_and_oversized_fallback_preserve_provider_precedence():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference, capture_inference, select_capture_rows
    limit = 4 * 1024 * 1024
    boundary = EmissionBoundary(ContentPolicy(True))
    content = {'text': 'z' * (limit - len('{"text":""}'))}
    with collect_inference(boundary, retain_raw=True) as collector:
        capture_inference('input', content, 'cause', fallback=True)
        capture_inference('input', content, 'cause')
    selected = select_capture_rows(collector['rows'])
    assert len(selected) == 1
    assert selected[0]['reason'] == 'captured'
    assert selected[0]['content'] == content
    assert selected[0].get('capture_source') != 'fallback'
    with collect_inference(boundary, retain_raw=True) as collector:
        capture_inference('input', {'text': 'z' * limit}, 'cause', fallback=True)
        capture_inference('input', {'text': 'valid'}, 'cause')
    selected = select_capture_rows(collector['rows'])
    assert selected[0]['content'] == {'text': 'valid'}
    assert selected[0].get('capture_source') != 'fallback'
