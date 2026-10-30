"""Workflow capture loss reaches the same acknowledged channel as sidecar loss."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.inference_content import EvidenceFailureChannel, EvidencePublicationError
from api_agent.workflows.attribution import bind_workflow_visit
from api_agent.workflows.inference_capture import capture_inference, capture_rows, collect_inference, select_capture_rows


def test_conflicting_capture_authority_withholds_prior_and_subsequent_evidence():
    from api_agent.workflows.inference_capture import bind_boundary
    original = EmissionBoundary(ContentPolicy(True), secrets=('original-secret',))
    other = EmissionBoundary(ContentPolicy(True), secrets=('other-secret',))
    with collect_inference(original) as collector:
        capture_inference('input', {'text': 'other-secret'}, str(uuid4()))
        bind_boundary(other)
        capture_inference('output', {'text': 'original-secret'}, str(uuid4()))
        bind_boundary(original)
        capture_inference('input', {'text': 'ordinary'}, str(uuid4()))
    assert select_capture_rows(collector['rows']) == [
        {'phase': 'request', 'content': {}, 'reason': 'withheld'},
        {'phase': 'response', 'content': {}, 'reason': 'withheld'},
    ]


def test_policy_variants_share_capture_authority():
    from api_agent.workflows.inference_capture import bind_boundary
    original = EmissionBoundary(ContentPolicy(True), secrets=('registered-secret',))
    variant = original.for_policy(ContentPolicy(True))
    with collect_inference(original) as collector:
        bind_boundary(variant)
        capture_inference('input', {'text': 'registered-secret'}, str(uuid4()))
    rows = select_capture_rows(collector['rows'])
    assert len(rows) == 1
    assert rows[0]['reason'] != 'withheld'
    assert 'registered-secret' not in str(rows)


def test_policy_variant_cannot_enable_initially_disabled_capture():
    from api_agent.workflows.inference_capture import bind_boundary
    original = EmissionBoundary(ContentPolicy(False))
    with collect_inference(original) as collector:
        bind_boundary(original.for_policy(ContentPolicy(True)))
        capture_inference('input', {'text': 'private-content'}, str(uuid4()))
    assert select_capture_rows(collector['rows']) == [
        {'phase': 'request', 'content': {}, 'reason': 'withheld'}]


def test_disabling_policy_discards_previously_captured_content():
    from api_agent.workflows.inference_capture import bind_boundary
    original = EmissionBoundary(ContentPolicy(True))
    with collect_inference(original) as collector:
        capture_inference('input', {'text': 'private-content'}, str(uuid4()))
        bind_boundary(original.for_policy(ContentPolicy(False)))
        bind_boundary(original.for_policy(ContentPolicy(True)))
        capture_inference('output', {'text': 'later-content'}, str(uuid4()))
    assert select_capture_rows(collector['rows']) == [
        {'phase': 'request', 'content': {}, 'reason': 'withheld'},
        {'phase': 'response', 'content': {}, 'reason': 'withheld'}]


@pytest.mark.parametrize('failure', [ValueError('failure'), asyncio.CancelledError()])
@pytest.mark.parametrize('path', ['direct', 'provider_setup'])
def test_loss_reaches_runner_when_journal_rejects_capture(monkeypatch, failure, path):
    node = 'tests/test_example.py::test_example'
    monkeypatch.setenv('PYTEST_CURRENT_TEST', node + ' (call)')
    def fail(**kwargs):
        raise failure
    owner = SimpleNamespace(boundary=EmissionBoundary(ContentPolicy(True)), capture_json=fail)
    with EvidenceFailureChannel([node]) as channel:
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        with bind_workflow_visit(owner, 'visit'):
            if path == 'direct':
                capture_inference('input', {'text': 'safe'}, str(uuid4()))
            else:
                with collect_inference(owner.boundary) as collector:
                    capture_inference('input', {'text': 'safe'}, str(uuid4()))
                capture_rows(select_capture_rows(collector['rows']), str(uuid4()))
        assert channel.reported() == {node}


def test_unacknowledged_loss_fails_test(monkeypatch):
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'tests/test_example.py::test_example (call)')
    monkeypatch.delenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', raising=False)
    owner = SimpleNamespace(capture_json=lambda **kwargs: (_ for _ in ()).throw(ValueError()))
    with bind_workflow_visit(owner, 'visit'), pytest.raises(EvidencePublicationError):
        capture_rows([{'phase': 'request', 'content': {}}], str(uuid4()))


def test_projection_failure_becomes_provider_setup_evidence(monkeypatch):
    from api_agent.workflows import capture
    monkeypatch.setattr(capture, 'project_values', lambda *a, **kw: (_ for _ in ()).throw(ValueError()))
    with collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
        capture_inference('input', {'text': 'secret'}, str(uuid4()))
    assert select_capture_rows(collector['rows']) == [
        {'phase': 'request', 'content': {}, 'reason': 'unavailable'}]

@pytest.mark.parametrize('failure', [ValueError('failure'), asyncio.CancelledError()])
def test_direct_projection_loss_reaches_runner(monkeypatch, failure):
    from api_agent.workflows import capture
    node = 'tests/test_example.py::test_example'
    monkeypatch.setenv('PYTEST_CURRENT_TEST', node + ' (call)')
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(capture, 'project_values', fail)
    owner = SimpleNamespace(boundary=EmissionBoundary(ContentPolicy(True)))
    with EvidenceFailureChannel([node]) as channel:
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        with bind_workflow_visit(owner, 'visit'):
            capture_inference('input', {'text': 'safe'}, str(uuid4()))
        assert channel.reported() == {node}

@pytest.mark.parametrize('failure', [ValueError('failure'), asyncio.CancelledError()])
@pytest.mark.parametrize('gap', [False, True])
def test_scoped_loss_retains_invocation_visit_and_call(monkeypatch, failure, gap):
    node = 'tests/test_example.py::test_example'
    monkeypatch.setenv('PYTEST_CURRENT_TEST', node + ' (call)')
    invocation, visit, call = (str(uuid4()) for _ in range(3))
    def capture(**kwargs):
        if not gap:
            raise failure
    journal = SimpleNamespace(invocation_id=invocation, _gap='event_limit' if gap else None,
                              capture_json=capture)
    with EvidenceFailureChannel([node]) as channel:
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        with bind_workflow_visit(journal, visit):
            capture_rows([{'phase': 'request', 'content': {}}], call)
        assert channel.reported_scopes() == {(node, invocation, visit, call, 'input')}


def test_scoped_channel_capacity_failure_is_unacknowledged(monkeypatch):
    from api_agent.testing import inference_content
    node = 'tests/test_example.py::test_capacity'
    monkeypatch.setenv('PYTEST_CURRENT_TEST', node + ' (call)')
    invocation, visit, call = (str(uuid4()) for _ in range(3))
    journal = SimpleNamespace(invocation_id=invocation, _gap='event_limit', capture_json=lambda **kw: None)
    with EvidenceFailureChannel([node]) as channel:
        channel._scoped = {(node, str(i), '', '', '') for i in range(1024)}
        monkeypatch.setenv('API_AGENT_EVIDENCE_FAILURE_CHANNEL', channel.descriptor)
        with bind_workflow_visit(journal, visit), pytest.raises(EvidencePublicationError):
            capture_rows([{'phase': 'request', 'content': {}}], call)
        assert node in inference_content._unacknowledged
    inference_content._unacknowledged.discard(node)


@pytest.mark.parametrize('failure', [ValueError('private payload'), asyncio.CancelledError()])
def test_non_test_workflow_capture_failure_warns_without_interrupting(monkeypatch, caplog, failure):
    monkeypatch.delenv('PYTEST_CURRENT_TEST', raising=False)
    def fail(**kwargs):
        raise failure
    journal = SimpleNamespace(capture_json=fail)
    with bind_workflow_visit(journal, str(uuid4())):
        capture_rows([{'phase': 'request', 'content': {'private': 'payload'}}], str(uuid4()))
    assert 'Workflow inference evidence could not be recorded' in caplog.text
    assert 'private payload' not in caplog.text
    assert "'private': 'payload'" not in caplog.text
