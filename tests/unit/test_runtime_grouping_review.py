"""Independent execution grouping checks across real async runtime boundaries."""
import asyncio
import json
from contextlib import nullcontext
from pathlib import Path

import pytest
from langgraph.graph.message import add_messages

from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.testing.inference_usage import publish
from api_agent.workflows.contrived import build
from api_agent.workflows.journal import WorkflowJournal
from api_agent.workflows.run_context import conversation, current_execution_context, current_turn_id, turn
from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_catalog, load_stateful_definition
from test_studio_bridge import payload, request, setup_bridge
from test_workflow_stateful import fixture_graph

CONVERSATION = '00000000-0000-4000-8000-000000000021'
EXPLICIT = '00000000-0000-4000-8000-000000000022'
OUTER = '00000000-0000-4000-8000-000000000023'


@pytest.mark.parametrize('executor', ['declarative', 'stateful'])
@pytest.mark.parametrize('inherited', [False, True])
@pytest.mark.parametrize('recorded', [False, True])
def test_explicit_turn_is_visible_to_nested_journals_and_usage(
        fixture_graph, tmp_path, monkeypatch, executor, inherited, recorded):
    usage_path = tmp_path / 'usage.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(usage_path))
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1' if recorded else '0')
    monkeypatch.setattr('api_agent.workflows.provenance.verified_documents', lambda *a, **k: None)
    monkeypatch.setattr('api_agent.workflows.stateful._source_manifest', lambda *a, **k: None)
    packets, observed = [], []
    monkeypatch.setattr('api_agent.workflows.evidence.publish', packets.append)

    def record():
        nested = WorkflowJournal(definition).finalize()
        observed.append((current_turn_id(), nested))
        request = LLMRequest('example', CompletionRequest((Message('user', (TextContent('x'),)),)))
        result = LLMResult(Message('assistant', (TextContent('x'),)), Usage(1, 1, 0, cache_write_tokens=0))
        publish(request, result)

    if executor == 'declarative':
        async def complete(request):
            record()
            return LLMResult(Message('assistant', (TextContent(json.dumps(dict(
                original_input='x', route='skip', ready=True, final_output='x'))),)))
        from test_workflow_trial_decisions import trial_uuid
        workflow = build(Path(__file__).resolve().parents[2], llm=complete, selection='example', trial=trial_uuid())
        definition = workflow.definition
        journal = WorkflowJournal(definition, turn_id=EXPLICIT)
        workflow.services['journal'] = journal
        operation = lambda: workflow.arun({'user_input': 'x'}, turn_id=EXPLICIT)
    else:
        root, entry = fixture_graph
        source = root / 'native.py'
        source.write_text(source.read_text().replace('def intake(state, context):',
            "def intake(state, context):\n    context.services['record']()"))
        definition = load_stateful_definition(entry, base_root=root, pack_root=root)
        journal = WorkflowJournal(definition, turn_id=EXPLICIT)
        workflow = StatefulWorkflow(definition, catalog=load_stateful_catalog(definition),
            reducers={'messages': add_messages}, journal=journal, services={'record': record})
        operation = lambda: workflow.arun({'messages': [], 'pending': False, 'visits': 0},
            thread_id='example', turn_id=EXPLICIT)

    with conversation(CONVERSATION), turn(OUTER) if inherited else nullcontext():
        asyncio.run(operation())
        assert current_turn_id() == (OUTER if inherited else None)

    assert len(observed) == 1
    actual_turn, nested = observed[0]
    assert actual_turn == EXPLICIT
    assert nested['turn_id'] == EXPLICIT
    assert nested['execution_context'] == {'kind': 'conversation', 'conversation_id': CONVERSATION}
    assert nested['invocation_id'] != journal.invocation_id
    usage = json.loads(usage_path.read_text().splitlines()[0])['data']
    assert usage['turn_id'] == EXPLICIT
    assert usage['conversation_id'] == CONVERSATION
    envelope = packets[0] if recorded else journal.finalize()
    assert envelope['turn_id'] == EXPLICIT
    assert current_execution_context() is None
    assert current_turn_id() is None


def test_interleaved_cancelled_context_restores_without_affecting_other_task():
    async def execute():
        ready, resume = asyncio.Event(), asyncio.Event()
        async def cancelled():
            try:
                with conversation(CONVERSATION), turn(EXPLICIT):
                    ready.set()
                    await resume.wait()
            except asyncio.CancelledError:
                assert current_execution_context() is None
                assert current_turn_id() is None
                raise
        task = asyncio.create_task(cancelled())
        await ready.wait()
        with conversation(OUTER), turn(OUTER):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert current_execution_context() == {'kind': 'conversation', 'conversation_id': OUTER}
            assert current_turn_id() == OUTER
        assert current_execution_context() is None
        assert current_turn_id() is None
    asyncio.run(execute())


@pytest.mark.parametrize('streaming', [False, True])
def test_backend_interleaved_requests_keep_context_inside_actual_execution(streaming):
    from api_agent.providers.llm import StreamEvent

    async def execute():
        both_entered = asyncio.Event()
        observed = {}
        entered = 0

        async def capture(value):
            nonlocal entered
            identity = value.request_id
            observed.setdefault(identity, []).append((current_execution_context(), current_turn_id()))
            if identity in (EXPLICIT, OUTER):
                entered += 1
                if entered == 2:
                    both_entered.set()
                await both_entered.wait()
            await asyncio.sleep(0)
            observed[identity].append((current_execution_context(), current_turn_id()))

        async def complete(value, *, auth, context):
            await capture(value)
            return LLMResult(Message('assistant', (TextContent('hello'),)))

        async def stream(value, *, auth, context):
            await capture(value)
            yield StreamEvent(TextContent('hello'))
            await asyncio.sleep(0)
            observed[value.request_id].append((current_execution_context(), current_turn_id()))
            yield StreamEvent(LLMResult(Message('assistant', (TextContent('hello'),))), 'completed')

        backend, _ = setup_bridge(complete=complete, stream=stream)
        async def send(turn_id, conversation_id=None):
            changes = {'session_id': turn_id}
            if conversation_id is not None:
                changes['conversation_id'] = conversation_id
            response = await backend.handle(request('POST', payload(**changes)),
                'chat_stream' if streaming else 'chat')
            if streaming:
                frames = [chunk async for chunk in response.streaming_content]
                assert any(b'event: done' in chunk for chunk in frames)
            else:
                assert response.status_code == 200

        try:
            await asyncio.gather(send(EXPLICIT, CONVERSATION), send(OUTER, OUTER))
            await send(CONVERSATION)
            for identity, grouping in [(EXPLICIT, CONVERSATION), (OUTER, OUTER), (CONVERSATION, None)]:
                expected = None if grouping is None else {'kind': 'conversation', 'conversation_id': grouping}
                assert observed[identity] == [(expected, identity)] * (3 if streaming else 2)
            assert current_execution_context() is None
            assert current_turn_id() is None
        finally:
            await backend.aclose()

    asyncio.run(execute())


@pytest.mark.parametrize('failure', ['completion', 'stream', 'cancellation'])
def test_backend_restores_context_in_execution_task_after_failure(monkeypatch, failure):
    from api_agent.providers.llm import StreamEvent

    async def execute():
        entered = asyncio.Event()
        restored = []

        async def fail():
            assert current_execution_context() == {'kind': 'conversation', 'conversation_id': CONVERSATION}
            assert current_turn_id() == EXPLICIT
            entered.set()
            if failure == 'cancellation':
                await asyncio.Event().wait()
            await asyncio.sleep(0)
            raise RuntimeError('controlled failure')

        async def complete(value, *, auth, context):
            await fail()

        async def stream(value, *, auth, context):
            yield StreamEvent(TextContent('partial'))
            await fail()

        backend, _ = setup_bridge(complete=complete, stream=stream)
        original_start = backend.lifecycle.start

        def start(owner_id, session_id, invoke, *, context):
            async def observed(context):
                try:
                    return await invoke(context)
                finally:
                    restored.append((current_execution_context(), current_turn_id()))
            return original_start(owner_id, session_id, observed, context=context)
        monkeypatch.setattr(backend.lifecycle, 'start', start)

        try:
            operation = backend.handle(request('POST', payload(session_id=EXPLICIT,
                conversation_id=CONVERSATION)), 'chat_stream' if failure == 'stream' else 'chat')
            if failure == 'cancellation':
                task = asyncio.create_task(operation)
                await asyncio.wait_for(entered.wait(), timeout=3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                response = await operation
                if failure == 'stream':
                    frames = [chunk async for chunk in response.streaming_content]
                    assert any(b'event: error' in chunk for chunk in frames)
                else:
                    assert response.status_code == 502
            assert restored == [(None, None)]
        finally:
            await backend.aclose()

    asyncio.run(execute())


@pytest.mark.parametrize('identity', [None, 1, '', '0' * 36,
    '00000000-0000-4000-8000-00000000002A', '00000000_0000-4000-8000-000000000021'])
def test_invalid_conversation_identity_is_rejected_before_catalog_access(identity):
    async def execute():
        backend, calls = setup_bridge()
        try:
            response = await backend.handle(request('POST', payload(conversation_id=identity)), 'chat')
            assert response.status_code == 400
            assert calls == []
        finally:
            await backend.aclose()
    asyncio.run(execute())


@pytest.mark.parametrize('index', [1, 60])
def test_valid_trial_journal_round_trip_and_context_copy(index):
    from api_agent.workflows.run_context import trial, validate_execution_context
    from api_agent.workflows.journal import validate_journal
    from test_execution_grouping import definition

    with trial(index):
        copy = current_execution_context()
        copy['trial_index'] = 99
        payload = WorkflowJournal(definition()).finalize()
        assert payload['execution_context'] == {'kind': 'trial', 'trial_index': index}
        assert validate_journal(payload) == payload
        assert validate_execution_context(payload['execution_context']) == payload['execution_context']
    assert current_execution_context() is None


@pytest.mark.parametrize('identity', [None, 1, '', '0' * 36,
    '00000000-0000-4000-8000-00000000002A'])
def test_invalid_turn_identity_does_not_replace_outer_binding(identity):
    with turn(OUTER):
        with pytest.raises(ValueError, match='execution identity'):
            with turn(identity):
                pytest.fail('invalid turn entered execution scope')
        assert current_turn_id() == OUTER


@pytest.mark.parametrize('retained', ['started', 'events', 'finalized', 'gap'])
def test_context_admission_does_not_relabel_any_retained_journal_state(retained):
    from test_execution_grouping import definition

    journal = WorkflowJournal(definition())
    if retained == 'started':
        journal._started = True
    elif retained == 'events':
        journal._events = [{'event': 'sentinel'}]
    elif retained == 'finalized':
        journal._finalized = True
    else:
        journal._gap = 'event_limit'
    before = list(journal._events)

    with pytest.raises(ValueError, match='execution context disagrees'):
        journal.admit_execution_context({'kind': 'conversation', 'conversation_id': CONVERSATION})

    assert journal.execution_context is None
    assert journal._events == before


def test_journal_context_admission_returns_copies_and_retains_legacy_absence():
    from test_execution_grouping import definition

    journal = WorkflowJournal(definition())
    assert journal.admit_execution_context(None) is None
    context = {'kind': 'conversation', 'conversation_id': CONVERSATION}
    admitted = journal.admit_execution_context(context)
    admitted['conversation_id'] = OUTER
    context['conversation_id'] = OUTER
    assert journal.execution_context == {'kind': 'conversation', 'conversation_id': CONVERSATION}
    journal._started = True
    assert journal.admit_execution_context(None) == journal.execution_context
    assert journal.admit_execution_context(dict(journal.execution_context)) == journal.execution_context
    assert journal.finalize()['execution_context'] == {'kind': 'conversation', 'conversation_id': CONVERSATION}
    legacy = WorkflowJournal(definition())
    legacy.admit_execution_context(None)
    assert 'execution_context' not in legacy.finalize()


@pytest.mark.parametrize('executor', ['declarative', 'stateful'])
@pytest.mark.parametrize('identity', ['', False, 0, [], 'invalid', EXPLICIT + '\n'])
def test_explicit_invalid_turn_rejects_before_accessing_execution_services(executor, identity):
    from api_agent.workflows.execution import Workflow

    async def execute():
        workflow = (Workflow if executor == 'declarative' else StatefulWorkflow).__new__(
            Workflow if executor == 'declarative' else StatefulWorkflow)
        if executor == 'declarative':
            operation = workflow.arun({}, turn_id=identity)
        else:
            operation = workflow.arun({}, thread_id='uninitialized', turn_id=identity)
        with pytest.raises(ValueError, match='execution identity'):
            await operation
    asyncio.run(execute())
