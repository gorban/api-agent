"""Rejected structural receipts never mutate journals or emit worker evidence."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.testing.inference_usage import publish_delegated_usage, publish_external_invocation
from api_agent.telemetry.usage import SuiteUsage


@pytest.fixture
def sink(tmp_path, monkeypatch):
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test.py::test_guard (call)')
    return path


def receipt(**changes):
    return dict(call_id=str(uuid4()), selection='model', usage=None,
        started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:01+00:00',
        outcome='failed', owner=None, **changes)


@pytest.mark.parametrize('inventory,complete,error', [
    (None, True, 'requires call inventory'),
    ((), True, 'Invalid native call inventory'),
    ([], 1, 'Invalid native call inventory'),
    ([1], True, 'Invalid native call identity'),
    (['AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA'], True, 'Invalid native call identity'),
    (['00000000-0000-0000-0000-000000000001'] * 2, True, 'Duplicate native call inventory'),
])
def test_native_inventory_guards_do_not_emit(sink, inventory, complete, error):
    with pytest.raises(ValueError, match=error):
        publish_external_invocation(str(uuid4()), call_ids=inventory, complete=complete)
    assert not sink.exists()


@pytest.mark.parametrize('field,value', [('started_at', None), ('finished_at', None), ('outcome', 'unknown')])
def test_nonterminal_delegated_receipts_are_rejected(sink, field, value):
    args = receipt()
    args[field] = value
    with pytest.raises(ValueError, match='terminal timing and outcome'):
        publish_delegated_usage(**args)
    assert not sink.exists()


@pytest.mark.parametrize('field', ['turn_id', 'conversation_id'])
def test_delegated_explicit_identity_cannot_override_owner(sink, field):
    from api_agent.testing.inference_usage import DelegatedOwner
    journal = SimpleNamespace(record_call=lambda **kw: pytest.fail('Rejected owner reached journal'))
    owner = DelegatedOwner(journal, 'visit', None, str(uuid4()), 'initial', str(uuid4()), str(uuid4()))
    args = receipt()
    args.update(owner=owner, **{field: str(uuid4())})
    with pytest.raises(ValueError, match='Conflicting delegated'):
        publish_delegated_usage(**args)
    assert not sink.exists()


@pytest.mark.parametrize('disabled', ['permission', 'path', 'node'])
def test_delegated_without_recording_context_emits_nothing(sink, monkeypatch, disabled):
    from api_agent.workflows.run_context import test_evidence
    if disabled == 'path':
        monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS')
    elif disabled == 'node':
        monkeypatch.delenv('PYTEST_CURRENT_TEST')
    with test_evidence(disabled != 'permission'):
        assert publish_delegated_usage(**receipt()) is None
    assert not sink.exists()


def test_owner_snapshot_can_capture_execution_context_before_journal_admission(sink):
    from api_agent.workflows.run_context import conversation
    from api_agent.workflows.attribution import bind_workflow_visit
    from api_agent.testing.inference_usage import capture_delegated_owner
    chat = str(uuid4())
    journal = SimpleNamespace(turn_id=str(uuid4()), invocation_id=str(uuid4()), execution_context=None)
    with conversation(chat), bind_workflow_visit(journal, 'visit'):
        owner = capture_delegated_owner()
    assert owner.conversation_id == chat
    with conversation(chat):
        publish_delegated_usage(**receipt())
    assert json.loads(sink.read_text())['data']['conversation_id'] == chat


@pytest.mark.parametrize('delivery', ['inventory_first', 'receipt_first'])
@pytest.mark.parametrize('wrong', ['test', 'invocation_id', 'turn_id'])
def test_native_inventory_rejects_exact_owner_conflict(delivery, wrong):
    tracker = SuiteUsage('run')
    call, invocation, turn = (str(uuid4()) for _ in range(3))
    facts = dict(call_id=call, invocation_id=invocation, turn_id=turn, usage=None)
    session = 'test'
    if wrong == 'test':
        session = 'other'
    else:
        facts[wrong] = str(uuid4())
    def inventory():
        tracker.record_external_invocation(session_id='test', invocation_id=invocation,
            turn_id=turn, call_ids=[call], complete=True)
    def observation():
        tracker.record_inference(call, session, facts)
    first, second = (inventory, observation) if delivery == 'inventory_first' else (observation, inventory)
    first()
    if delivery == 'inventory_first' and wrong in ('test', 'turn_id'):
        # A different test is disjoint; an unexpected turn remains partial.
        second()
        assert tracker.report()['unmeasured_native_invocations'] == {'test': 1}
    else:
        with pytest.raises(ValueError, match='Conflicting native call owner'):
            second()


def test_native_inventory_cannot_be_rewritten_and_disjoint_tests_are_allowed():
    tracker = SuiteUsage('run')
    invocation, turn, call = (str(uuid4()) for _ in range(3))
    tracker.record_external_invocation(session_id='test', invocation_id=invocation, turn_id=turn, call_ids=[call], complete=True)
    tracker.record_external_invocation(session_id='other', invocation_id=invocation, turn_id=turn, call_ids=[], complete=True)
    tracker.record_external_invocation(session_id='third', invocation_id=str(uuid4()), turn_id=turn, call_ids=[], complete=True)
    with pytest.raises(ValueError, match='Conflicting native call inventory'):
        tracker.record_external_invocation(session_id='test', invocation_id=invocation, turn_id=turn, call_ids=[], complete=True)
