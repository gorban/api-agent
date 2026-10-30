"""Provider evidence failures release owned capacity and reject incomplete work."""
import asyncio
import sqlite3

import pytest

from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.testing.authority_scope import authority_scope
from api_agent.testing.provider_setup import RemoteIntegrationLLM


def test_closed_evidence_executor_releases_admitted_slot():
    descriptor = dict(version=1, host='127.0.0.1', port=1,
                      capability='synthetic-authority-capability-0000000', authority_path='/tmp/synthetic-authority')
    provider = RemoteIntegrationLLM(descriptor)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    provider._evidence_pool.shutdown(wait=True)
    try:
        with authority_scope(('coordinator',)) as scope:
            with pytest.raises(RuntimeError, match='shutdown'):
                provider.complete(request)
            assert provider._evidence_slots._available.qsize() == 8
            assert not scope._evidence_jobs
    finally:
        provider.close()


def test_exhausted_evidence_slots_reject_without_leaking_job():
    from api_agent.providers.contracts import ContractError
    descriptor = dict(version=1, host='127.0.0.1', port=2,
                      capability='synthetic-exhausted-capability-0000000', authority_path='/tmp/synthetic-exhausted')
    provider = RemoteIntegrationLLM(descriptor)
    provider.timeout = .05
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    slots = []
    try:
        for _ in range(8):
            slots.append(provider._evidence_slots.acquire(1))
        with authority_scope(('coordinator',)) as scope:
            with pytest.raises(ContractError, match='slots are unavailable'):
                provider.complete(request)
            assert not scope._evidence_jobs
            assert provider._evidence_slots._available.qsize() == 0
    finally:
        for slot in slots:
            provider._evidence_slots.release(slot)
        provider.close()
    assert provider._evidence_slots._available.qsize() == 8


def test_real_runner_marks_unfinished_sidecar_evidence_unavailable(tmp_path):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo
    source = '''
import os
from api_agent.testing.inference_content import _start_marker

def test_publish():
    node = os.environ['PYTEST_CURRENT_TEST'].rsplit(' ', 1)[0]
    _start_marker(os.environ['API_AGENT_INFERENCE_EVIDENCE_DB'], node)
'''
    root = fixture_repo(tmp_path / 'base', source)
    ctx = context(root)
    selected = collect_tests(ctx, 'integration')
    events = []
    result = run_integration_tests(ctx, selected, accepted=True, on_event=events.append)
    assert result.tests[0].status == 'passed'
    assert [(event.identity, event.text) for event in events
            if event.kind == 'inference_evidence_incomplete'] == [(selected[0], 'unavailable')]


def test_real_runner_ignores_legacy_event_content_without_sidecar(tmp_path):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo
    source = '''
import os
from pathlib import Path
from uuid import uuid4
from api_agent.testing.inference_content import publish_content

def test_publish(monkeypatch):
    monkeypatch.delenv('API_AGENT_INFERENCE_EVIDENCE_DB')
    publish_content([{'phase': 'request', 'content': {'text': 'synthetic-untrusted-payload'}}], str(uuid4()))
    assert 'synthetic-untrusted-payload' in Path(os.environ['API_AGENT_INTEGRATION_EVENTS']).read_text()
'''
    root = fixture_repo(tmp_path / 'base', source)
    ctx = context(root)
    selected = collect_tests(ctx, 'integration')
    events = []
    result = run_integration_tests(ctx, selected, accepted=True, on_event=events.append)
    assert result.tests[0].status == 'passed'
    assert not any(event.kind == 'inference_content' for event in events)
    assert all('synthetic-untrusted-payload' not in event.text for event in events)


def test_runner_drains_large_committed_evidence_before_reporting_result(tmp_path, monkeypatch):
    from uuid import uuid4
    from api_agent.testing import inference_content as framing
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo

    root = fixture_repo(tmp_path / 'base')
    ctx = context(root)
    selected = collect_tests(ctx, 'integration')
    initialize = framing.initialize_evidence
    def seed(path, nodes):
        initialize(path, nodes)
        for number in range(40):
            framing._publish_sidecar(path, selected[0].node_id,
                {'phase': 'request', 'content': {'sequence': number}}, str(uuid4()))
    monkeypatch.setattr(framing, 'initialize_evidence', seed)
    events = []
    result = run_integration_tests(ctx, selected, accepted=True, on_event=events.append)
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    evidence = [(index, event) for index, event in enumerate(events) if event.kind == 'inference_content']
    finished = [(index, event) for index, event in enumerate(events) if event.kind == 'test_finished' and event.identity == selected[0]]
    assert len(finished) == 1 and finished[0][1].status == 'passed'
    assert all(event.identity == selected[0] and index < finished[0][0] for index, event in evidence)
    import json
    assert [json.loads(event.text)['content']['sequence'] for _, event in evidence] == list(range(40))
    assert not any(event.kind == 'inference_evidence_incomplete' for event in events)


def test_workflow_authority_is_joined_before_background_submission_fails():
    from types import SimpleNamespace
    from api_agent.providers.contracts import ContractError
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.attribution import bind_workflow_visit
    descriptor = dict(version=1, host='127.0.0.1', port=3,
                      capability='synthetic-workflow-capability-0000000', authority_path='/tmp/synthetic-workflow')
    provider = RemoteIntegrationLLM(descriptor)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-workflow-secret',))
    provider._evidence_pool.shutdown(wait=True)
    try:
        with authority_scope(('workflow',)) as scope, bind_workflow_visit(SimpleNamespace(boundary=boundary), 'visit'):
            with pytest.raises(ContractError):
                scope.authorities()
            submit = provider._evidence_pool.submit
            observed = []
            def inspect_submit(*args, **kwargs):
                observed.append(scope.authorities())
                return submit(*args, **kwargs)
            provider._evidence_pool.submit = inspect_submit
            with pytest.raises(RuntimeError, match='shutdown'):
                provider.complete(request)
            assert observed == [(boundary,)]
            assert scope.authorities() == (boundary,)
            assert not scope._evidence_jobs
            assert provider._evidence_slots._available.qsize() == 8
    finally:
        provider.close()


def test_legacy_remote_setup_joins_workflow_authority_before_dispatch():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import LLMResult, Usage
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.attribution import bind_workflow_visit

    async def scenario():
        boundary = EmissionBoundary(ContentPolicy(True))
        returned = LLMResult(Message('assistant', (TextContent('safe response'),)), Usage(1, 1))
        admitted = []
        class Owner:
            ensure_ready = AsyncMock()
            aclose = AsyncMock()
            async def complete(self, request):
                admitted.append(scope.authorities())
                return returned
        registry = SetupRegistry('op', 'selected', Owner)
        server = SetupServer(registry, 'synthetic-legacy-capability-0000000', timeout=5)
        provider = RemoteIntegrationLLM(await server.start(), config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS': 5})
        assert 'authority_path' not in provider.client._descriptor
        journal = SimpleNamespace(boundary=boundary, capture_json=lambda **kwargs: None)
        request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        try:
            with authority_scope(('workflow',)) as scope, bind_workflow_visit(journal, 'visit'):
                with pytest.raises(ContractError):
                    scope.authorities()
                actual = await asyncio.to_thread(provider.complete, request)
                assert actual == returned
                assert admitted == [(boundary,)]
        finally:
            await asyncio.to_thread(provider.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_background_connection_failure_completes_enrolled_job():
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary

    async def scenario():
        registry = SetupRegistry('op', 'selected', lambda: None)
        server = SetupServer(registry, 'synthetic-disconnected-capability-0000000', timeout=3,
                             authority=EmissionBoundary(ContentPolicy(True)))
        provider = RemoteIntegrationLLM(await server.start(), config={'LLM_SETUP_REQUEST_TIMEOUT_SECONDS': 3})
        await server.aclose()
        request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        try:
            with authority_scope(('coordinator',)) as scope:
                enrolled = []
                enroll = scope.enroll_evidence
                def capture_job(job):
                    enrolled.append(job)
                    return enroll(job)
                scope.enroll_evidence = capture_job
                with pytest.raises(ContractError):
                    await asyncio.to_thread(provider.complete, request)
                await asyncio.to_thread(provider.close)
                scope.finish_evidence()
                assert len(enrolled) == 1 and enrolled[0].done()
                assert not scope._evidence_jobs
                # An unconfirmed release cannot safely reuse this identity.
                assert provider._evidence_slots._available.qsize() == 7
        finally:
            await asyncio.to_thread(provider.close)
            await registry.aclose()
    asyncio.run(scenario())


def test_early_error_followed_by_conflicting_final_error_reports_evidence_loss(monkeypatch):
    from threading import Event
    from api_agent.providers.contracts import ContractError
    from api_agent.providers import setup_transport
    from api_agent.testing import inference_content

    descriptor = dict(version=1, host='127.0.0.1', port=4,
                      capability='synthetic-conflicting-capability-0000000', authority_path='/tmp/synthetic-conflicting')
    provider = RemoteIntegrationLLM(descriptor)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    proceed, entered, closed = Event(), Event(), Event()
    failures, published = [], []
    class ControlledClient:
        response_evidence = None
        transport_attempts = {'version': 1, 'complete': False, 'attempts': []}
        cost_quote = None
        evidence = []
        _failed = False
        _binding = None
        _revoke_confirmed = True
        def __init__(self, *args, **kwargs):
            pass
        def complete(self, request, *, authority_scope, result_ready, error_ready, observation_call_id=None):
            error_ready(ContractError('early failure'))
            entered.set()
            assert proceed.wait(3)
            raise ContractError('different final failure')
        def close(self):
            closed.set()
    monkeypatch.setattr(setup_transport, 'SetupClient', ControlledClient)
    monkeypatch.setattr(inference_content, 'notify_failure', lambda node, scope=None: failures.append((node, scope)))
    monkeypatch.setattr(inference_content, 'publish_content', lambda rows, call_id: published.append((rows, call_id)))
    try:
        with authority_scope(('coordinator',)) as scope:
            with pytest.raises(ContractError, match='early failure'):
                provider.complete(request)
            assert entered.wait(2)
            assert len(scope._evidence_jobs) == 1
            assert not closed.is_set()
            proceed.set()
            scope.finish_evidence()
            assert closed.is_set()
            assert not scope._evidence_jobs
            import os
            from uuid import UUID
            node = os.environ['PYTEST_CURRENT_TEST'].rsplit(' ', 1)[0]
            assert failures == [(node, None), (node, None)]
            assert len(published) == 1
            rows, call_id = published[0]
            assert str(UUID(call_id)) == call_id
            assert rows == [dict(phase=phase, content={}, reason='unavailable') for phase in ('request', 'response')]
            assert 'failure' not in repr(rows)
            assert provider._evidence_slots._available.qsize() == 8
    finally:
        proceed.set()
        provider.close()


def test_early_result_followed_by_conflicting_final_result_withholds_evidence(monkeypatch):
    from threading import Event
    from uuid import UUID
    from api_agent.providers import setup_transport
    from api_agent.providers.llm import LLMResult, Usage
    from api_agent.testing import inference_content

    descriptor = dict(version=1, host='127.0.0.1', port=5,
                      capability='synthetic-result-conflict-capability-0000000', authority_path='/tmp/synthetic-result-conflict')
    provider = RemoteIntegrationLLM(descriptor)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    early = LLMResult(Message('assistant', (TextContent('first response'),)), Usage(1, 1))
    final = LLMResult(Message('assistant', (TextContent('different response'),)), Usage(1, 1))
    proceed, entered, closed = Event(), Event(), Event()
    failures, published = [], []
    class ControlledClient:
        response_evidence = None
        transport_attempts = {'version': 1, 'complete': False, 'attempts': []}
        cost_quote = None
        evidence = [dict(phase='response', content={'text': 'synthetic-sensitive-sentinel'}, reason='captured')]
        _failed = False
        _binding = None
        _revoke_confirmed = True
        def __init__(self, *args, **kwargs):
            pass
        def complete(self, request, *, authority_scope, result_ready, error_ready, observation_call_id=None):
            result_ready(early)
            entered.set()
            assert proceed.wait(3)
            return final
        def close(self):
            closed.set()
    monkeypatch.setattr(setup_transport, 'SetupClient', ControlledClient)
    monkeypatch.setattr(inference_content, 'notify_failure', lambda node, scope=None: failures.append((node, scope)))
    monkeypatch.setattr(inference_content, 'publish_content', lambda rows, call_id: published.append((rows, call_id)))
    try:
        with authority_scope(('coordinator',)) as scope:
            actual = provider.complete(request)
            assert actual.payload == early.payload
            assert entered.wait(2) and len(scope._evidence_jobs) == 1 and not closed.is_set()
            proceed.set()
            scope.finish_evidence()
            assert closed.is_set() and not scope._evidence_jobs
            import os
            node = os.environ['PYTEST_CURRENT_TEST'].rsplit(' ', 1)[0]
            assert failures == [(node, None), (node, None)]
            assert len(published) == 1
            rows, call_id = published[0]
            assert str(UUID(call_id)) == call_id
            assert rows == [dict(phase=phase, content={}, reason='unavailable') for phase in ('request', 'response')]
            assert 'synthetic-sensitive-sentinel' not in repr(published)
            assert provider._evidence_slots._available.qsize() == 8
    finally:
        proceed.set()
        provider.close()


def test_background_client_construction_failure_completes_job_and_releases_slot(monkeypatch):
    from api_agent.providers import setup_transport
    descriptor = dict(version=1, host='127.0.0.1', port=7,
                      capability='synthetic-construction-capability-0000000', authority_path='/tmp/synthetic-construction')
    provider = RemoteIntegrationLLM(descriptor)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    attempts, enrolled = [], []
    def fail_construction(*args, **kwargs):
        attempts.append((args, kwargs))
        raise OSError('synthetic client construction failure')
    monkeypatch.setattr(setup_transport, 'SetupClient', fail_construction)
    try:
        with authority_scope(('coordinator',)) as scope:
            enroll = scope.enroll_evidence
            def capture_job(job):
                enrolled.append(job)
                return enroll(job)
            scope.enroll_evidence = capture_job
            with pytest.raises(OSError, match='synthetic client construction failure'):
                provider.complete(request)
            scope.finish_evidence()
            assert len(attempts) == 1
            assert len(enrolled) == 1 and enrolled[0].done()
            assert not scope._evidence_jobs
            assert provider._evidence_slots._available.qsize() == 8
    finally:
        provider.close()


def test_conflicting_dispatch_policy_rejects_early_result_and_cleans_up(monkeypatch):
    from api_agent.providers import setup_transport
    from api_agent.providers.contracts import ContractError
    from api_agent.providers.llm import LLMResult, ResponseEvidence, Usage
    descriptor = dict(version=1, host='127.0.0.1', port=8,
                      capability='synthetic-policy-conflict-capability-0000000', authority_path='/tmp/synthetic-policy-conflict')
    provider = RemoteIntegrationLLM(descriptor)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    result = LLMResult(Message('assistant', (TextContent('synthetic-sensitive-response'),)), Usage(1, 1))
    clients, enrolled = [], []
    class ControlledClient:
        response_evidence = ResponseEvidence('text', 'allow', 'text', None, None, None)
        evidence = []
        _failed = False
        _binding = None
        _revoke_confirmed = True
        def __init__(self, *args, **kwargs):
            self.closed = False
            clients.append(self)
        def complete(self, request, *, authority_scope, result_ready, error_ready, observation_call_id=None):
            result_ready(result)
            pytest.fail('conflicting response policy was accepted')
        def close(self):
            self.closed = True
    monkeypatch.setattr(setup_transport, 'SetupClient', ControlledClient)
    try:
        with authority_scope(('coordinator',)) as scope:
            enroll = scope.enroll_evidence
            def capture_job(job):
                enrolled.append(job)
                return enroll(job)
            scope.enroll_evidence = capture_job
            with pytest.raises(ContractError, match='Conflicting dispatched response policy') as caught:
                provider.complete(request)
            scope.finish_evidence()
            assert 'synthetic-sensitive-response' not in str(caught.value)
            assert len(clients) == 1 and clients[0].closed
            assert len(enrolled) == 1 and enrolled[0].done()
            assert not scope._evidence_jobs
            assert provider._evidence_slots._available.qsize() == 8
    finally:
        provider.close()


def test_post_result_evidence_write_failure_reports_loss_and_closes_job(tmp_path, monkeypatch):
    import os
    from threading import Event
    from api_agent.providers import setup_transport
    from api_agent.providers.generation_attempts import attach_transport_attempts
    from api_agent.providers.llm import LLMResult, Usage
    from api_agent.testing import inference_content
    descriptor = dict(version=1, host='127.0.0.1', port=9,
                      capability='synthetic-evidence-write-capability-0000000', authority_path='/tmp/synthetic-evidence-write')
    provider = RemoteIntegrationLLM(descriptor)
    request = LLMRequest('model', CompletionRequest((Message('user', (TextContent('hello'),)),)))
    result = LLMResult(Message('assistant', (TextContent('safe response'),)), Usage(1, 1))
    proceed, entered, closed = Event(), Event(), Event()
    failures, attempts = [], []
    publish_content = inference_content.publish_content
    evidence_path = tmp_path / 'evidence' / 'events.jsonl'
    evidence_path.parent.mkdir()
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(evidence_path))
    monkeypatch.delenv('API_AGENT_INFERENCE_EVIDENCE_DB', raising=False)
    class ControlledClient:
        response_evidence = None
        transport_attempts = {'version': 1, 'complete': False, 'attempts': []}
        cost_quote = None
        evidence = [dict(phase='response', content={'text': 'synthetic-sensitive-evidence'}, reason='captured')]
        _failed = False
        _binding = None
        _revoke_confirmed = True
        def __init__(self, *args, **kwargs):
            pass
        def complete(self, request, *, authority_scope, result_ready, error_ready, observation_call_id=None):
            result_ready(result)
            entered.set()
            assert proceed.wait(3)
            return attach_transport_attempts(result, self.transport_attempts)
        def close(self):
            closed.set()
    monkeypatch.setattr(setup_transport, 'SetupClient', ControlledClient)
    monkeypatch.setattr(inference_content, 'notify_failure', lambda node, scope=None: failures.append((node, scope)))
    def inspect_publish(rows, call_id):
        try:
            publish_content(rows, call_id)
        except FileNotFoundError as error:
            attempts.append((rows, call_id, error.filename))
            raise
    monkeypatch.setattr(inference_content, 'publish_content', inspect_publish)
    try:
        with authority_scope(('coordinator',)) as scope:
            actual = provider.complete(request)
            assert actual.payload == result.payload
            assert entered.wait(2) and len(scope._evidence_jobs) == 1 and not closed.is_set()
            assert evidence_path.exists()
            evidence_path.unlink()
            evidence_path.parent.rmdir()
            proceed.set()
            scope.finish_evidence()
            node = os.environ['PYTEST_CURRENT_TEST'].rsplit(' ', 1)[0]
            assert failures == [(node, None), (node, None)]
            from uuid import UUID
            assert len(attempts) == 1
            rows, call_id, failed_path = attempts[0]
            assert str(UUID(call_id)) == call_id
            assert rows == ControlledClient.evidence and failed_path == str(evidence_path)
            assert closed.is_set() and not scope._evidence_jobs
            assert not evidence_path.exists()
            assert provider._evidence_slots._available.qsize() == 8
    finally:
        proceed.set()
        provider.close()


def test_runner_retries_report_after_real_evidence_lock(tmp_path, monkeypatch):
    import sqlite3
    import sys
    from contextlib import closing
    from api_agent.testing import inference_content
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo
    root = fixture_repo(tmp_path / 'base')
    selected = collect_tests(context(root), 'integration')
    original = inference_content.read_evidence
    injections = []
    def lock_at_report(path, after):
        caller = sys._getframe(2)
        row = caller.f_locals.get('row') if caller.f_code.co_name == 'drain_events' else None
        if not injections and type(row) is dict and row.get('kind') == 'report':
            with closing(sqlite3.connect(path)) as db:
                db.execute('BEGIN EXCLUSIVE')
                try:
                    original(path, after)
                except sqlite3.OperationalError as error:
                    assert 'locked' in str(error).lower()
                    injections.append(row['phase'])
                    raise
        return original(path, after)
    monkeypatch.setattr(inference_content, 'read_evidence', lock_at_report)
    events = []
    result = run_integration_tests(context(root), selected, accepted=True, on_event=events.append)
    assert len(injections) == 1
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    finished = [event for event in events if event.kind == 'test_finished']
    assert len(finished) == 1 and finished[0].identity == selected[0] and finished[0].status == 'passed'
    assert not any(event.kind == 'inference_evidence_incomplete' for event in events)


def test_runner_final_audit_reports_evidence_started_after_teardown_check(tmp_path, monkeypatch):
    from api_agent.testing import inference_content
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo
    root = fixture_repo(tmp_path / 'base')
    selected = collect_tests(context(root), 'integration')
    original = inference_content.has_pending
    observations, markers = [], []
    def delayed_evidence(path, node):
        pending = original(path, node)
        observations.append((node, pending))
        if not markers:
            assert not pending
            markers.append(inference_content._start_marker(path, node))
        return pending
    monkeypatch.setattr(inference_content, 'has_pending', delayed_evidence)
    events = []
    result = run_integration_tests(context(root), selected, accepted=True, on_event=events.append)
    assert observations == [(selected[0].node_id, False), (selected[0].node_id, True)]
    assert len(markers) == 1
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    finished = [index for index, event in enumerate(events)
                if event.kind == 'test_finished' and event.identity == selected[0]]
    incomplete = [(index, event.identity, event.text) for index, event in enumerate(events)
                  if event.kind == 'inference_evidence_incomplete']
    assert len(finished) == 1 and len(incomplete) == 1
    assert incomplete[0] == (incomplete[0][0], selected[0], 'unavailable')
    assert finished[0] < incomplete[0][0]


@pytest.mark.parametrize('exception', [OSError('synthetic read failure'), sqlite3.OperationalError('synthetic database failure')], ids=['file', 'database'])
def test_runner_deduplicates_evidence_loss_after_truncation(tmp_path, monkeypatch, exception):
    from uuid import uuid4
    from api_agent.testing import inference_content
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo
    root = fixture_repo(tmp_path / 'base')
    selected = collect_tests(context(root), 'integration')
    initialize = inference_content.initialize_evidence
    read = inference_content.read_evidence
    failures = []
    def seed(path, nodes):
        initialize(path, nodes)
        inference_content._publish_sidecar(path, selected[0].node_id,
            {'phase': 'request', 'content': {}, 'reason': 'truncated'}, str(uuid4()))
    def fail_after_record(path, after):
        if after:
            failures.append(after)
            raise exception
        return read(path, after)
    monkeypatch.setattr(inference_content, 'initialize_evidence', seed)
    monkeypatch.setattr(inference_content, 'read_evidence', fail_after_record)
    events = []
    result = run_integration_tests(context(root), selected, accepted=True, on_event=events.append)
    assert failures
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    assert [(event.identity, event.text) for event in events
            if event.kind == 'inference_evidence_incomplete'] == [(selected[0], 'truncated')]


def test_worker_exit_after_truncated_evidence_does_not_duplicate_notice(tmp_path):
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo
    source = '''
import os
from uuid import uuid4
from api_agent.testing.inference_content import publish_content

def test_publish():
    publish_content([{'phase': 'request', 'content': {}, 'reason': 'truncated'}], str(uuid4()))
    os._exit(17)
'''
    root = fixture_repo(tmp_path / 'base', source)
    selected = collect_tests(context(root), 'integration')
    events = []
    result = run_integration_tests(context(root), selected, accepted=True, on_event=events.append)
    assert result.exit_code != 0 and result.tests[0].status == 'error'
    assert [(event.identity, event.text) for event in events
            if event.kind == 'inference_evidence_incomplete'] == [(selected[0], 'truncated')]
    assert 'incomplete' in result.tests[0].traceback.lower()


def test_runner_ignores_sidecar_evidence_for_unknown_node(tmp_path, monkeypatch):
    from contextlib import closing
    from uuid import uuid4
    from api_agent.testing import inference_content
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo
    root = fixture_repo(tmp_path / 'base')
    selected = collect_tests(context(root), 'integration')
    initialize = inference_content.initialize_evidence
    def seed(path, nodes):
        initialize(path, nodes)
        inference_content._publish_sidecar(path, selected[0].node_id,
            {'phase': 'request', 'content': {'text': 'synthetic-unowned-payload'}}, str(uuid4()))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE evidence SET node=?', ('synthetic-unknown-node',))
        inference_content._publish_sidecar(path, selected[0].node_id,
            {'phase': 'request', 'content': {'text': 'synthetic-selected-payload'}}, str(uuid4()))
    monkeypatch.setattr(inference_content, 'initialize_evidence', seed)
    events = []
    result = run_integration_tests(context(root), selected, accepted=True, on_event=events.append)
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    content = [event for event in events if event.kind == 'inference_content']
    assert len(content) == 1 and content[0].identity == selected[0]
    assert 'synthetic-selected-payload' in content[0].text
    assert all('synthetic-unowned-payload' not in event.text for event in events)


def _exercise_final_evidence_timeout(tmp_path, monkeypatch):
    import sys
    import time
    from uuid import uuid4
    from api_agent.testing import inference_content
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo
    root = fixture_repo(tmp_path / 'base')
    selected = collect_tests(context(root), 'integration')
    initialize = inference_content.initialize_evidence
    read = inference_content.read_evidence
    locks, blocked, final_blocked = [], [], []
    def seed(path, nodes):
        initialize(path, nodes)
        inference_content._publish_sidecar(path, selected[0].node_id,
            {'phase': 'request', 'content': {}, 'reason': 'truncated'}, str(uuid4()))
    def lock_after_notice(path, after):
        if after and not locks:
            db = sqlite3.connect(path)
            db.execute('BEGIN EXCLUSIVE')
            locks.append(db)
        try:
            return read(path, after)
        except sqlite3.OperationalError as error:
            blocked.append(str(error))
            frame = sys._getframe()
            while frame is not None:
                if frame.f_code.co_name == 'drain' and frame.f_locals.get('final') is True:
                    final_blocked.append(time.monotonic())
                    break
                frame = frame.f_back
            raise
    monkeypatch.setattr(inference_content, 'initialize_evidence', seed)
    monkeypatch.setattr(inference_content, 'read_evidence', lock_after_notice)
    events = []
    try:
        result = run_integration_tests(context(root), selected, accepted=True, on_event=events.append)
    finally:
        for db in locks:
            db.close()
    assert len(locks) == 1 and blocked and all('locked' in error.lower() for error in blocked)
    assert len(final_blocked) >= 2 and final_blocked[-1] - final_blocked[0] >= 4.8
    assert [(event.identity, event.text) for event in events
            if event.kind == 'inference_evidence_incomplete'] == [(selected[0], 'truncated')]
    assert result.exit_code == 0 and result.tests[0].status == 'passed'




def _run_final_evidence_timeout_child(path, data_file, source_file, relative_files, trace_enabled, result_queue):
    import traceback
    from pathlib import Path
    tracer = None
    try:
        if trace_enabled:
            import coverage
            tracer = coverage.Coverage(data_file=data_file, include=[source_file], branch=True, config_file=False)
            tracer.set_option('run:relative_files', relative_files)
            tracer.start()
        with pytest.MonkeyPatch.context() as monkeypatch:
            from api_agent.testing import suite_lock
            monkeypatch.setattr(suite_lock, 'suite_lock_path', lambda: Path(path) / 'nested-suite.lock')
            _exercise_final_evidence_timeout(Path(path), monkeypatch)
        if tracer is not None:
            tracer.stop()
            tracer.save()
            tracer = None
        result_queue.put(('ok', ''))
    except BaseException:
        result_queue.put(('error', traceback.format_exc()))
    finally:
        if tracer is not None:
            tracer.stop()


def test_final_evidence_timeout_does_not_duplicate_existing_truncation(tmp_path):
    import multiprocessing
    import queue
    import api_agent.testing.integration as integration
    from api_agent.testing import suite_lock
    try:
        import coverage
    except ModuleNotFoundError:
        active = None
    else:
        active = coverage.Coverage.current()
    data_file = str(tmp_path / 'child-coverage')
    source_file = integration.__file__
    ctx = multiprocessing.get_context('spawn')
    receipt = ctx.Queue()
    worker = ctx.Process(target=_run_final_evidence_timeout_child, args=(
        str(tmp_path), data_file, source_file,
        active.config.relative_files if active else False, active is not None, receipt))
    with pytest.MonkeyPatch.context() as launcher_patch:
        launcher_patch.setattr(suite_lock, 'suite_lock_path', lambda: tmp_path / 'launcher-suite.lock')
        with suite_lock.suite_lock():
            worker.start()
            try:
                try:
                    status, detail = receipt.get(timeout=25)
                except queue.Empty:
                    pytest.fail('runner did not complete within 25 seconds')
                worker.join(5)
                assert not worker.is_alive(), 'runner process did not exit after reporting'
                assert worker.exitcode == 0
                assert status == 'ok', detail
                if active is not None:
                    import ast
                    from pathlib import Path
                    child = coverage.CoverageData(basename=data_file)
                    child.read()
                    files = child.measured_files()
                    assert len(files) == 1
                    recorded_file = next(iter(files))
                    assert recorded_file.endswith('api_agent/testing/integration.py')
                    tree = ast.parse(Path(source_file).read_text())
                    drains = [node for node in ast.walk(tree)
                              if isinstance(node, ast.FunctionDef) and node.name == 'drain']
                    assert len(drains) == 1
                    loops = [node for node in ast.walk(drains[0]) if isinstance(node, ast.While)
                             and isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)
                             and isinstance(node.test.operand, ast.Call)
                             and isinstance(node.test.operand.func, ast.Name)
                             and node.test.operand.func.id == 'drain_events']
                    assert len(loops) == 1
                    retry = loops[0].body[-1]
                    assert (isinstance(retry, ast.Expr) and isinstance(retry.value, ast.Call)
                            and isinstance(retry.value.func, ast.Attribute)
                            and isinstance(retry.value.func.value, ast.Name)
                            and retry.value.func.value.id == 'time' and retry.value.func.attr == 'sleep')
                    assert (retry.lineno, loops[0].lineno) in child.arcs(recorded_file)
                    active.get_data().update(child)
            finally:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
                if worker.is_alive():
                    worker.kill()
                    worker.join(5)
                receipt.close()
                receipt.join_thread()


def test_runner_discards_unknown_scoped_notification_from_faulty_channel(tmp_path, monkeypatch):
    import json
    from api_agent.testing import inference_content
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    from test_integration_runner import context, fixture_repo
    source = '''
import os
from uuid import uuid4
from api_agent.testing.inference_content import notify_failure

def test_publish():
    node = os.environ['PYTEST_CURRENT_TEST'].rsplit(' ', 1)[0]
    scope = {'invocation_id': str(uuid4()), 'visit_id': str(uuid4()),
             'call_id': str(uuid4()), 'direction': 'input'}
    notify_failure(node, scope)
'''
    root = fixture_repo(tmp_path / 'base', source)
    selected = collect_tests(context(root), 'integration')
    original = inference_content.EvidenceFailureChannel
    injected = []
    class FaultyChannel(original):
        def reported_scopes(self):
            actual = super().reported_scopes()
            if not actual:
                return actual
            node, invocation, visit, call, direction = next(iter(actual))
            forged = ('synthetic-unselected-node', invocation, visit, call, direction)
            injected.append((next(iter(actual)), forged))
            return actual | {forged}
    monkeypatch.setattr(inference_content, 'EvidenceFailureChannel', FaultyChannel)
    events = []
    result = run_integration_tests(context(root), selected, accepted=True, on_event=events.append)
    assert result.exit_code == 0 and result.tests[0].status == 'passed'
    assert injected and all(actual[0] == selected[0].node_id for actual, _ in injected)
    actual, forged = injected[0]
    assert forged == ('synthetic-unselected-node', *actual[1:])
    assert all(item == (actual, forged) for item in injected)
    scoped = [event for event in events if event.kind == 'workflow_inference_evidence_incomplete']
    assert len(scoped) == 1 and scoped[0].identity == selected[0]
    assert json.loads(scoped[0].text) == dict(zip(
        ('invocation_id', 'visit_id', 'call_id', 'direction'), actual[1:]))
    assert all('synthetic-unselected-node' not in event.text for event in events)
