"""Owned source evidence survives bounded storage and unsuccessful model calls."""
import asyncio
import json

import pytest

from api_agent.providers.control import OperationalError
from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
from api_agent.testing import inference_content
from api_agent.testing.inference_usage import delegated_test_node
from api_agent.testing.local_accounting import owned_source_payloads
from api_agent.testing.payloads import PayloadStore
from api_agent.workflows.attribution import bind_workflow_visit
from api_agent.workflows.inference_capture import capture_inference, withholding_inference_content
from test_local_llm import request, selected
from test_owned_source_payloads import observations
from test_payloads import recorder


def retained_source(selected, tmp_path, monkeypatch, payload, *, outcome='completed', withheld=False, unavailable=False):
    owner, provider, _, _, _ = selected
    provider.captures_effective_payload = True
    error = asyncio.CancelledError('synthetic cancellation') if outcome == 'cancelled' else OperationalError('unknown')

    async def complete(value, *, auth):
        with withholding_inference_content(withheld):
            capture_inference('input', payload, value.request_id, auth=auth, unavailable=unavailable)
            if outcome != 'completed':
                raise error
            capture_inference('output', {'answer': 'visible answer'}, value.request_id, auth=auth)
        return LLMResult(Message('assistant', (TextContent('visible answer'),)), Usage(2, 3))

    provider.acomplete = complete
    journal = observations(owner)
    node = 'owned.py::test_source_edges'
    sidecar = str(tmp_path / 'wire.sqlite')
    inference_content.initialize_evidence(sidecar, [node])
    monkeypatch.setenv('API_AGENT_INFERENCE_EVIDENCE_DB', sidecar)
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))

    async def run():
        try:
            with delegated_test_node(node), owned_source_payloads(enabled=True), bind_workflow_visit(journal, 'visit'):
                if outcome == 'completed':
                    assert (await owner.complete(request())).usage == Usage(2, 3)
                else:
                    with pytest.raises(type(error)) as caught:
                        await owner.complete(request())
                    if outcome == 'failed':
                        assert caught.value is error
        finally:
            await owner.aclose()

    asyncio.run(run())
    rows, after = [], 0
    while batch := inference_content.read_evidence(sidecar, after):
        rows.extend(item[2] for item in batch)
        after = batch[-1][0]
    assert rows and {row['call_id'] for row in rows} == {owner.usage[0]['call_id']}
    store = PayloadStore(recorder(tmp_path / 'artifacts'), 'owned-source')
    for row in rows:
        store.put_wire(row['call_id'], row['phase'], row['content'], reason=row['reason'],
                       transport_attempt_id=row.get('transport_attempt_id'), capture_source=row.get('capture_source'))
    packet = journal.finalize()
    durable = store.materialize_journal(packet)
    assert len(durable['events']) == len(packet['events'])
    return rows, packet, durable, store, owner.usage


def test_sidecar_budget_loss_materializes_as_omitted_not_conflicting_wire(selected, tmp_path, monkeypatch):
    monkeypatch.setattr(inference_content, 'MAX_TEST_BYTES', 60)
    rows, packet, durable, store, usage = retained_source(
        selected, tmp_path, monkeypatch, {'text': 'visible ' * 5})
    assert rows[0]['reason'] == 'captured'
    assert rows[-1]['phase'] == 'response'
    assert rows[-1]['reason'] == 'truncated' and rows[-1]['content'] == {}
    captures = [event for event in durable['events'] if event['event'] == 'capture']
    assert captures[-1]['payload']['kind'] == 'omitted'
    assert captures[-1]['reason'] == 'truncated'
    assert usage[0]['usage']['input_tokens'] == 2


def test_early_sidecar_exhaustion_retains_later_phase_omission_authority(selected, tmp_path, monkeypatch):
    monkeypatch.setattr(inference_content, 'MAX_TEST_BYTES', 32)
    rows, packet, durable, store, usage = retained_source(
        selected, tmp_path, monkeypatch, {'text': 'visible ' * 100})
    assert [row['phase'] for row in rows] == ['request', 'response']
    assert all(row['content'] == {} and row['reason'] == 'truncated' for row in rows)
    captures = [event for event in durable['events'] if event['event'] == 'capture']
    assert len(captures) == 2
    assert all(event['payload']['kind'] == 'omitted' for event in captures)
    assert usage[0]['usage']['input_tokens'] == 2


@pytest.mark.parametrize('outcome', ['failed', 'cancelled'])
def test_unsuccessful_call_retains_authoritative_request_and_original_error(selected, tmp_path, monkeypatch, outcome):
    rows, packet, durable, store, usage = retained_source(
        selected, tmp_path, monkeypatch, {'text': 'original visible request'}, outcome=outcome)
    assert rows[0]['phase'] == 'request'
    assert rows[0]['content'] == {'text': 'original visible request'}
    assert usage[0]['outcome'] == outcome
    assert any(event.get('payload', {}).get('projection') == 'llm_request' for event in durable['events'])


@pytest.mark.parametrize('mode', ['withheld', 'unavailable', 'deep', 'secret'])
def test_source_privacy_and_loss_remain_explicit_through_strict_materialization(selected, tmp_path, monkeypatch, mode):
    payload = {'text': 'visible text'}
    if mode == 'deep':
        for _ in range(25):
            payload = {'nested': payload}
    if mode == 'secret':
        payload = {'safe': 'visible text', 'parts': ['sec', 'ret']}
    rows, packet, durable, store, usage = retained_source(
        selected, tmp_path, monkeypatch, payload, withheld=mode == 'withheld', unavailable=mode == 'unavailable')
    request_row = rows[0]
    if mode in {'withheld', 'unavailable'}:
        assert request_row['reason'] == mode
    elif mode == 'deep':
        assert request_row['reason'] in {'truncated', 'withheld'}
    else:
        assert 'secret' not in json.dumps(rows)
        assert request_row['content'] != payload
    assert usage[0]['usage']['input_tokens'] == 2
    assert 'secret' not in json.dumps(packet)
