"""Explicit owned-test receipts cross real child tasks without manual accounting."""
import asyncio
from types import MappingProxyType

import pytest

from api_agent.providers.control import OperationalError
from api_agent.workflows.attribution import bind_workflow_visit
from api_agent.workflows.journal import WorkflowJournal
from test_local_llm import request, selected
from test_workflow_observations import definition


def test_owned_observer_receives_fallback_receipt_from_child_task(selected):
    owner, _, _, _, _ = selected
    receipts = []
    owner.on_terminal_receipt = receipts.append
    journal = WorkflowJournal(definition())
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))

    async def scenario():
        try:
            with bind_workflow_visit(journal, 'visit'):
                result = await asyncio.create_task(owner.complete(request()))
            assert result.usage.input_tokens == 2
        finally:
            await owner.aclose()

    asyncio.run(scenario())
    assert len(receipts) == 1
    receipt = receipts[0]
    calls = [row for row in journal.finalize()['events'] if row['event'] == 'call']
    assert receipt['call_id'] == calls[0]['call_id'] == owner.usage[0]['call_id']
    assert receipt['usage'] == owner.usage[0]['usage']
    assert receipt['transport_attempts'] == calls[0]['transport_attempts']
    assert receipt['started_at'] <= receipt['finished_at']
    receipt['usage']['input_tokens'] = 999
    assert owner.usage[0]['usage']['input_tokens'] == 2


@pytest.mark.parametrize('failed', [False, True])
def test_observer_failure_never_replaces_source_result_or_error(selected, failed):
    owner, implementation, _, _, _ = selected
    statuses = []
    owner.on_status = statuses.append
    source_error = OperationalError('unknown')
    owner.config = MappingProxyType({**owner.config, 'LLM_MAX_ATTEMPTS': 1})
    if failed:
        async def failure(value, *, auth):
            raise source_error
        implementation.acomplete = failure
    def observer(receipt):
        raise RuntimeError('observational sink unavailable')
    owner.on_terminal_receipt = observer

    async def scenario():
        try:
            if failed:
                with pytest.raises(OperationalError) as caught:
                    await owner.complete(request())
                assert caught.value is source_error
            else:
                assert (await owner.complete(request())).usage.input_tokens == 2
        finally:
            await owner.aclose()

    asyncio.run(scenario())
    assert any(row['category'] == 'accounting' and row['outcome'] == 'unavailable'
               for row in statuses)


@pytest.mark.parametrize('log_content', [False, True])
@pytest.mark.parametrize('trace_content', [False, True])
def test_failed_asgi_child_task_keeps_known_receipts_and_partial_recorded_hierarchy(
        selected, tmp_path, monkeypatch, log_content, trace_content):
    import json
    from uuid import uuid4

    import httpx

    from api_agent.testing import local_accounting
    from api_agent.testing.contracts import RunnerEvent, TestResult
    from api_agent.testing.inference_usage import delegated_test_node
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.recorded import run_recorded_integration
    from api_agent.workflows.evidence import publish
    from api_agent.workflows.run_context import conversation, turn
    from test_native_usage_boundary import boundary
    from test_recorded_integration import Storage, fixture

    owner, _, _, _, _ = selected
    owner.on_terminal_receipt = local_accounting.publish_terminal_receipt
    context, identity, config = fixture(tmp_path)
    config.update(LOG_CONTENT=log_content, TRACELOOP_TRACE_CONTENT=trace_content)
    events = tmp_path / 'events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(events))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', identity.node_id + ' (call)')
    cid, tid, graph_id, visit_id = (str(uuid4()) for _ in range(4))
    source_error = TimeoutError('controlled source deadline')
    journals = []

    async def application(scope, receive, send):
        with delegated_test_node(identity.node_id), conversation(cid), turn(tid), \
                local_accounting.owned_source_payloads(enabled=log_content):
            from api_agent.workflows.observations import WorkflowObservationFactory
            from test_workflow_observations import config as resolved_config
            journal = WorkflowObservationFactory(resolved_config(log=log_content, trace=trace_content),
                boundary=owner.emissions).create(
                definition(), turn_id=tid, test_id=identity.node_id)
            journal.admit_execution_context({'kind': 'conversation', 'conversation_id': cid})
            journal.journal.subscribe(lambda event: local_accounting.publish_root_invocation(journal, event))
            journal.observe(dict(event='scope_start', scope_id=graph_id, parent_id=None,
                                 kind='graph', owner='GRAPH', started_ms=1))
            journal.observe(dict(event='scope_start', scope_id=visit_id, parent_id=graph_id,
                                 kind='node', owner='Make', started_ms=1))
            try:
                with bind_workflow_visit(journal, visit_id):
                    for _ in range(13):
                        await owner.complete(request())
                raise source_error
            finally:
                journal.observe(dict(event='scope_end', scope_id=visit_id, parent_id=graph_id,
                                     kind='node', owner='Make', started_ms=1, stopped_ms=2, outcome='failed'))
                journal.observe(dict(event='scope_end', scope_id=graph_id, parent_id=None,
                                     kind='graph', owner='GRAPH', started_ms=1, stopped_ms=3, outcome='failed'))
                journal.journal.record_evidence_loss()
                packet = journal.finalize()
                journals.append(packet)
                local_accounting.publish_final_inventory(packet)
                publish(packet)

    async def scenario():
        try:
            quota = boundary()
            quota['before']['pct_used'] = quota['after']['pct_used'] = 0
            assert local_accounting.publish_quota_boundary(quota, allocation_id='a' * 64)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                         base_url='http://testserver') as client:
                with pytest.raises(TimeoutError) as caught:
                    await asyncio.create_task(client.get('/'))
                assert caught.value is source_error
        finally:
            await owner.aclose()

    asyncio.run(scenario())
    assert journals[0]['completeness'] == 'partial'
    published = [json.loads(line) for line in events.read_text().splitlines()]
    assert len([row for row in published if row['kind'] == 'inference_usage']) == 13
    wires = [row for row in published if row['kind'] == 'inference_content']
    assert len(wires) == (26 if log_content else 0)
    if log_content:
        assert any('values' in row and row.get('cause_id') for row in journals[0]['events'])
        assert {row['data']['phase'] for row in wires} == {'request', 'response'}
        assert all(row['data']['capture_source'] == 'fallback' for row in wires)
        assert len({(row['data']['call_id'], row['data']['transport_attempt_id']) for row in wires}) == 13

    def runner(context, tests, **hooks):
        hooks['before_run']()
        for row in published:
            event = RunnerEvent(row['kind'], identity.repo, identity, text=json.dumps(row['data']))
            hooks['on_event'](event)
            if row['kind'] == 'inference_usage':
                hooks['on_event'](event)  # Duplicate transport delivery is one logical receipt.
        outcome = TestResult(identity, 'failed', .1, 'controlled deadline')
        hooks['on_result'](outcome)
        result = IntegrationRunResult(1, (outcome,), '')
        hooks['after_run'](result)
        return result

    result = run_recorded_integration(context, [identity], config, accepted=True,
        runner=runner, storage=Storage(),
        workflow_admissions={journals[0]['projection']['definition_id']: journals[0]['projection']})
    assert result.exit_code == 1
    summary = json.loads((result.directory / 'usage-summary.json').read_text())
    assert summary['prompt_count'] == 1
    assert summary['measured_calls'][identity.id] == 13
    assert summary['unmeasured_native_invocations'][identity.id] == 1
    assert sum(row['input_tokens'] for row in summary['usage_observations']) == 26
    assert sum(row['output_tokens'] for row in summary['usage_observations']) == 39
    assert all(row['cache_read_tokens'] is None for row in summary['usage_observations'])
    recorded = json.loads(next(result.directory.glob('*-result.json')).read_text())
    assert recorded['status'] == 'failed'
    conversation_step = next(step for step in recorded['steps'] if step['name'].startswith('Conversation '))
    graph = conversation_step['steps'][0]['steps'][0]
    assert graph['name'] == 'graph GRAPH'
    node = next(step for step in graph['steps'] if step['name'] == 'node Make')
    assert len([step for step in node['steps'] if step['name'].startswith('call ')]) == 13
    joined = json.loads((result.directory / 'workflow-accounting-join.json').read_text())
    assert joined['tests'][0]['attribution'] == 'partial'
    assert len(joined['tests'][0]['leaf_calls']) == 13


def test_manual_runtime_and_quota_bridge_cannot_write_finalized_test_events(tmp_path, monkeypatch):
    from api_agent.testing.local_accounting import publish_quota_boundary
    from api_agent.workflows.run_context import test_evidence
    from test_native_usage_boundary import boundary
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_manual.py::test_call (call)')
    with test_evidence(False):
        assert publish_quota_boundary(boundary()) is False
    assert not path.exists()


@pytest.mark.parametrize('count', [1, 2])
def test_central_publishers_keep_exact_ledgers_quotes_and_do_not_trigger_hook(
        selected, tmp_path, monkeypatch, count):
    from uuid import uuid4

    from api_agent.providers.generation_attempts import TransportAttemptLedger, attach_transport_attempts
    from api_agent.providers.llm import LLMResult, Message, TextContent, Usage
    from api_agent.testing.inference_usage import publish

    owner, implementation, _, _, _ = selected
    observed = []
    owner.on_terminal_receipt = observed.append
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_central.py::test_call (call)')
    expected = []

    async def central(value, *, auth):
        for _ in range(count):
            result = LLMResult(Message('assistant', (TextContent('answer'),)), Usage(2, 3))
            ledger = TransportAttemptLedger(1)
            token = ledger.begin(1)
            ledger.finish(token, result=result)
            result = attach_transport_attempts(result, ledger.snapshot())
            identifier = str(uuid4())
            quote = {'list_cost_usd': .25, 'payment': None}
            publish(value, result, call_id=identifier, quote=quote)
            expected.append((identifier, ledger.snapshot(), quote))
        return result

    implementation.acomplete = central
    journal = WorkflowJournal(definition(), model_identity=lambda value: value == 'sample')
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))

    async def scenario():
        try:
            with bind_workflow_visit(journal, 'visit'):
                await owner.complete(request())
        finally:
            await owner.aclose()

    asyncio.run(scenario())
    assert observed == []
    assert [(row['call_id'], row['transport_attempts'], row['quote'])
            for row in owner.usage] == expected


def test_unstarted_observer_coroutine_is_closed_without_execution(selected):
    import inspect
    owner, _, _, _, _ = selected
    coroutines = []
    async def invalid():
        pytest.fail('A synchronous receipt observer cannot schedule async publication')
    def observer(receipt):
        value = invalid()
        coroutines.append(value)
        return value
    owner.on_terminal_receipt = observer
    async def scenario():
        try:
            assert (await owner.complete(request())).usage.input_tokens == 2
        finally:
            await owner.aclose()
    asyncio.run(scenario())
    assert len(coroutines) == 1 and inspect.getcoroutinestate(coroutines[0]) == inspect.CORO_CLOSED


def test_returned_observer_task_stays_owned_until_cancellation_is_acknowledged(selected):
    from api_agent.providers.control import CleanupPendingError
    owner, _, loaded, _, _ = selected
    owner._timeout = .02
    async def scenario():
        started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def invalid_publication():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        task = asyncio.create_task(invalid_publication())
        await started.wait()
        owner.on_terminal_receipt = lambda receipt: task
        try:
            assert (await owner.complete(request())).usage.input_tokens == 2
            await cancelled.wait()
            assert (await owner.complete(request())).usage.input_tokens == 2
            assert task.cancelling() == 1
            with pytest.raises(CleanupPendingError):
                await owner.aclose()
            loaded.aclose.assert_not_awaited()
            assert not task.done()
        finally:
            release.set()
            await task
            # The short budget made the blocked close fail promptly; the final
            # close only needs to finish, however slowly the host runs it.
            owner._timeout = 5
            await owner.aclose()
        assert task.done()
        loaded.aclose.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['function', 'object', 'scalar'])
def test_async_or_noncallable_terminal_observer_is_rejected_before_work(tmp_path, kind):
    from api_agent.runtime.local_llm import LocalLLM
    async def observer(receipt):
        pass
    class AsyncObserver:
        async def __call__(self, receipt):
            pass
    value = {'function': observer, 'object': AsyncObserver(), 'scalar': 1}[kind]
    with pytest.raises(ValueError, match='terminal receipt observer'):
        LocalLLM({}, tmp_path, on_terminal_receipt=value)


@pytest.mark.parametrize('allocation', [None, 'a' * 64])
def test_quota_projection_uses_task_owner_and_never_guesses_account_identity(
        tmp_path, monkeypatch, allocation):
    import json
    from api_agent.testing.inference_usage import delegated_test_node
    from api_agent.testing.local_accounting import publish_quota_boundary
    from test_native_usage_boundary import boundary
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'unrelated.py::test_later (call)')
    with delegated_test_node('owned.py::test_turn'):
        assert publish_quota_boundary(boundary(), allocation_id=allocation) is (allocation is not None)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2 and {row['node'] for row in rows} == {'owned.py::test_turn'}
    if allocation is None:
        assert all(row['data']['error']['code'] == 'identity' for row in rows)
    else:
        assert all(row['data']['snapshot']['allocation_id'] == allocation for row in rows)
        assert all(row['data']['snapshot']['window_id'] == '2026-11-09T00:00:00+00:00' for row in rows)


def test_quota_projection_keeps_different_verified_users_disjoint(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from api_agent.telemetry.usage import SuiteUsage
    from api_agent.testing.local_accounting import publish_quota_boundary
    from api_agent.testing.quota_events import capture
    from test_native_usage_boundary import boundary
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'owned.py::test_turn (call)')
    publish_quota_boundary(boundary(), allocation_id='a' * 64)
    other = boundary()
    other['before']['captured_at'] = 3
    other['after']['captured_at'] = 4
    publish_quota_boundary(other, allocation_id='b' * 64)
    usage = SuiteUsage('owned-test-run')
    for row in map(json.loads, path.read_text().splitlines()):
        capture(SimpleNamespace(text=json.dumps(row['data'])), usage.observe_snapshot)
    assert usage.report()['quota_unavailable_reason'] == 'changed_allocation'


def test_quota_projection_rejects_reversed_capture_order_without_emitting(tmp_path, monkeypatch):
    from api_agent.testing.local_accounting import publish_quota_boundary
    from test_native_usage_boundary import boundary
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'owned.py::test_turn (call)')
    value = boundary()
    value['before']['captured_at'] = 3
    with pytest.raises(ValueError, match='chronological'):
        publish_quota_boundary(value, allocation_id='a' * 64)
    assert not path.exists()


@pytest.mark.parametrize('scheduled', [False, True])
def test_foreign_loop_observer_work_blocks_disposal_until_its_loop_acknowledges(selected, scheduled):
    from api_agent.providers.control import CleanupPendingError
    owner, _, loaded, _, _ = selected
    foreign = asyncio.new_event_loop()
    async def invalid_publication():
        await asyncio.sleep(10)
    returned = foreign.create_task(invalid_publication()) if scheduled else foreign.create_future()
    owner.on_terminal_receipt = lambda receipt: returned
    async def scenario():
        assert (await owner.complete(request())).usage.input_tokens == 2
        if scheduled:
            with pytest.raises(CleanupPendingError):
                await owner.aclose()
            loaded.aclose.assert_not_awaited()
        else:
            await owner.aclose()
    own_loop = asyncio.new_event_loop()
    try:
        own_loop.run_until_complete(scenario())
        if scheduled:
            async def settle():
                await asyncio.gather(returned, return_exceptions=True)
            foreign.run_until_complete(settle())
            own_loop.run_until_complete(owner.aclose())
        loaded.aclose.assert_awaited_once()
        assert not owner._foreign_receipt_tasks
    finally:
        own_loop.close()
        foreign.close()
    assert returned.cancelled()


def test_foreign_running_loop_acknowledges_invalid_observer_future(selected):
    from threading import Event, Thread
    owner, _, _, _, _ = selected
    foreign = asyncio.new_event_loop()
    future = foreign.create_future()
    stopped = Event()
    future.add_done_callback(lambda done: stopped.set())
    thread = Thread(target=foreign.run_forever)
    thread.start()
    owner.on_terminal_receipt = lambda receipt: future
    async def scenario():
        try:
            assert (await owner.complete(request())).usage.input_tokens == 2
            assert await asyncio.to_thread(stopped.wait, 2)
        finally:
            await owner.aclose()
    try:
        asyncio.run(scenario())
        assert future.cancelled()
    finally:
        foreign.call_soon_threadsafe(foreign.stop)
        thread.join()
        foreign.close()


@pytest.mark.parametrize('reason', ['manual', 'no-owner', 'no-node', 'no-path'])
def test_terminal_accounting_requires_explicit_test_owner_and_sink(tmp_path, monkeypatch, reason):
    from api_agent.testing.local_accounting import publish_terminal_receipt
    from api_agent.workflows.run_context import test_evidence
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    if reason == 'no-path':
        monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS')
    if reason == 'no-node':
        monkeypatch.delenv('PYTEST_CURRENT_TEST')
    journal = WorkflowJournal(definition())
    with test_evidence(reason != 'manual'), bind_workflow_visit(
            None if reason == 'no-owner' else journal, 'visit'):
        assert publish_terminal_receipt({}) is False
    assert not path.exists()


@pytest.mark.parametrize('reason', ['no-node', 'no-path', 'interrupted', 'unavailable', 'unknown-pct', 'unknown-reset', 'unknown-time'])
def test_quota_projection_never_promotes_incomplete_or_unknown_boundary(tmp_path, monkeypatch, reason):
    from api_agent.testing.local_accounting import publish_quota_boundary
    from test_native_usage_boundary import boundary
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    if reason == 'no-path':
        monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS')
    if reason == 'no-node':
        monkeypatch.delenv('PYTEST_CURRENT_TEST')
    value = boundary()
    if reason == 'interrupted':
        value['status'] = 'interrupted'
    elif reason == 'unavailable':
        value['after']['available'] = False
    elif reason.startswith('unknown-'):
        key = {'unknown-pct': 'pct_used', 'unknown-reset': 'reset_at', 'unknown-time': 'captured_at'}[reason]
        value['after'][key] = None
    assert publish_quota_boundary(value, allocation_id='a' * 64) is False
    assert not path.exists()


@pytest.mark.parametrize('conflict', ['quote', 'usage'])
def test_central_receipt_quote_enrichment_rejects_conflicting_authority(conflict):
    from api_agent.workflows.attribution import bind_terminal_call_receipts, admit_terminal_call_receipt
    journal = WorkflowJournal(definition())
    first = dict(call_id='central', selection='sample', usage={'input_tokens': 2},
                 quote={'amount': '.25'})
    with bind_workflow_visit(journal, 'visit'), bind_terminal_call_receipts() as receipts:
        admit_terminal_call_receipt(first, journal)
        changed = {**first, conflict: {'amount': '.5'} if conflict == 'quote' else {'input_tokens': 3}}
        admit_terminal_call_receipt(changed, journal)
        assert receipts == [first]
    assert journal.finalize()['completeness'] == 'partial'


def test_source_cancellation_survives_observer_cancellation(selected):
    owner, implementation, _, _, _ = selected
    source = asyncio.CancelledError('source cancellation')
    observed = []
    async def cancel(value, *, auth):
        raise source
    implementation.acomplete = cancel
    def observer(receipt):
        observed.append(receipt)
        raise asyncio.CancelledError('observer cancellation')
    owner.on_terminal_receipt = observer
    async def scenario():
        try:
            with pytest.raises(asyncio.CancelledError) as caught:
                await owner.complete(request())
            assert type(caught.value) is asyncio.CancelledError
        finally:
            await owner.aclose()
    asyncio.run(scenario())
    assert len(observed) == 1 and observed[0]['outcome'] == 'cancelled'


def test_owned_receipt_without_conversation_does_not_invent_one(selected, tmp_path, monkeypatch):
    import json
    from api_agent.testing.local_accounting import publish_terminal_receipt
    owner, _, _, _, _ = selected
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    owner.on_terminal_receipt = publish_terminal_receipt
    journal = WorkflowJournal(definition())
    journal.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    async def scenario():
        try:
            with bind_workflow_visit(journal, 'visit'):
                await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(scenario())
    row = json.loads(path.read_text())
    assert row['kind'] == 'inference_usage'
    assert 'conversation_id' not in row['data']
    assert row['data']['invocation_id'] == journal.invocation_id


def test_central_quote_with_withheld_identity_keeps_wrapper_result_and_privacy(selected, tmp_path, monkeypatch):
    from uuid import uuid4
    from api_agent.testing.inference_usage import publish
    from api_agent.workflows.observations import WorkflowObservationFactory
    owner, implementation, _, _, _ = selected
    observed = []
    owner.on_terminal_receipt = observed.append
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    original = implementation.acomplete
    call_id = str(uuid4())
    async def central(value, *, auth):
        result = await original(value, auth=auth)
        publish(value, result, call_id=call_id, quote={'list_cost_usd': .25, 'payment': None})
        return result
    implementation.acomplete = central
    observations = WorkflowObservationFactory({}, boundary=owner.emissions).create(definition())
    observations.observe(dict(event='scope_start', scope_id='visit', kind='node', owner='Make'))
    async def scenario():
        try:
            with bind_workflow_visit(observations, 'visit'):
                assert (await owner.complete(request())).usage.input_tokens == 2
        finally:
            await owner.aclose()
    asyncio.run(scenario())
    assert observed == []
    assert len(owner.usage) == 1 and owner.usage[0]['call_id'] == call_id
    assert owner.usage[0]['selection'] is None
    assert 'quote' not in owner.usage[0]
    packet = observations.finalize()
    assert packet['completeness'] == 'partial'
    assert next(row for row in packet['events'] if row['event'] == 'call')['selection'] is None


@pytest.mark.parametrize('call_kind', ['central-withheld', 'central-usage', 'central-untyped', 'central-cost', 'central-empty', 'unverified-model', 'unverified-tool', 'ambiguous', 'legacy-model'])
def test_complete_journal_inventory_keeps_known_ids_without_inventing_model_identity(
        tmp_path, monkeypatch, call_kind):
    import json
    from uuid import uuid4
    from api_agent.testing.local_accounting import publish_final_inventory
    path = tmp_path / 'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    journal = WorkflowJournal(definition(), model_identity=(lambda value: value == 'sample')
                              if call_kind in ('legacy-model', 'unverified-model') else None)
    journal.observe(dict(event='scope_start', scope_id='graph', kind='graph', owner='GRAPH', started_ms=1))
    journal.observe(dict(event='edge', source='START', target='Make', owner='GRAPH', implicit=True))
    journal.observe(dict(event='scope_start', scope_id='visit', parent_id='graph', kind='node', owner='Make', started_ms=1))
    journal.observe(dict(event='node', scope_id='visit', node='Make'))
    call_id = str(uuid4())
    if call_kind == 'central-withheld':
        journal.record_call(visit_id='visit', call_id=call_id, selection='sample', usage={'input_tokens': 3})
    elif call_kind in ('central-usage', 'central-untyped', 'central-cost', 'central-empty'):
        journal.record_call(visit_id='visit', call_id=call_id,
                            usage={'central-usage': {'input_tokens': 3}, 'central-untyped': None,
                                   'central-cost': {'estimated_cost_usd': .2}, 'central-empty': {}}[call_kind])
    elif call_kind in ('unverified-tool', 'unverified-model'):
        call_id = journal.record_call(visit_id='visit',
            selection='sample' if call_kind == 'unverified-model' else None,
            usage={'input_tokens': 3} if call_kind == 'unverified-model' else None)
    else:
        journal.observe(dict(event='call', visit_id='visit', call_id=call_id,
                             selection='sample' if call_kind in ('legacy-model', 'unverified-model') else None))
    journal.observe(dict(event='scope_end', scope_id='visit', parent_id='graph', kind='node', owner='Make', outcome='passed', started_ms=1, stopped_ms=2))
    journal.observe(dict(event='edge', source='Make', target='END', owner='GRAPH', implicit=True, visit_id='visit'))
    journal.observe(dict(event='scope_end', scope_id='graph', kind='graph', owner='GRAPH', outcome='passed', started_ms=1, stopped_ms=3))
    packet = journal.finalize()
    assert packet['completeness'] == 'complete'
    assert publish_final_inventory(packet)
    inventory = json.loads(path.read_text())['data']
    assert inventory['call_ids'] == ([call_id] if call_kind in ('central-withheld', 'central-usage', 'legacy-model') else [])
    assert inventory['complete'] is (call_kind not in ('ambiguous', 'central-untyped', 'central-cost', 'central-empty', 'unverified-model'))
    if call_kind == 'central-withheld':
        row = next(row for row in packet['events'] if row['event'] == 'call')
        assert row['selection'] is None and row['accounting'] == 'central_call_id'

    if call_kind == 'central-withheld':
        from api_agent.telemetry.usage import SuiteUsage
        usage = SuiteUsage('run')
        usage.record_external_invocation(session_id='owned-test', **inventory)
        assert usage.report()['unobserved_calls'] == {'owned-test': 1}
