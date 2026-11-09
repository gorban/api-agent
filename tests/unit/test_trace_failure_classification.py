"""Terminal classifications are typed, owned and independent of exception prose."""
import asyncio
from contextlib import contextmanager
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.providers.contracts import CandidateOutputError, ContractError, ProviderAccessError
from api_agent.providers.control import OperationalError
from api_agent.providers.llm import LLMResult, Message, TextContent
from api_agent.telemetry.outcomes import (
    FAILURE_CODES, OutcomeCause, OutcomeState, failure_diagnostic, failure_outcome,
    report_failure, OperationOutcome, bind_operation,
)
from api_agent.telemetry.operation import operation
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.telemetry.tracing import TraceRuntime, _structural
from api_agent.workflows.execution import CandidateCorrection, WorkflowError
from api_agent.workflows.observations import WorkflowObservationFactory
from test_workflow_candidate_repair import workflow, replace_node, draft
from test_workflow_observations import config
from test_workflow_stateful import fixture_graph
from test_local_llm import selected

SENTINEL = 'synthetic-private-error-content'


@pytest.mark.parametrize('error,cause,code', [
    (WorkflowError(SENTINEL), 'execution', 'workflow'),
    (ValueError(SENTINEL), 'execution', 'execution'),
    (UnicodeError(SENTINEL), 'execution', 'execution'),
    (RuntimeError(SENTINEL), 'execution', 'execution'),
    (OperationalError('unavailable'), 'execution', 'execution'),
    (ContractError(SENTINEL), 'validation', 'contract_validation'),
    (CandidateCorrection({'reason': SENTINEL}), 'validation', 'candidate_validation'),
    (CandidateOutputError(LLMResult(Message('assistant', (TextContent(SENTINEL),))),
        {'reason': SENTINEL}), 'validation', 'schema_validation'),
    (ProviderAccessError('unknown', SENTINEL, code=SENTINEL,
        origin='local_output_validation'), 'validation', 'schema_validation'),
    (ProviderAccessError('network', SENTINEL, code=SENTINEL), 'provider', 'provider'),
    (TimeoutError(SENTINEL), 'timeout', 'deadline'),
    (OperationalError('deadline'), 'timeout', 'deadline'),
    (asyncio.CancelledError(SENTINEL), 'cancelled', 'cancelled'),
])
def test_fixed_failure_diagnostic_never_uses_exception_prose(error, cause, code):
    actual = failure_diagnostic(error)
    assert actual == {'version': 1, 'cause': cause, 'code': code}
    assert actual['code'] in FAILURE_CODES
    assert SENTINEL not in json.dumps(actual)
    outcome = failure_outcome(error)
    assert outcome.cause == OutcomeCause(cause)
    assert outcome.state == (OutcomeState.CANCELLED if cause == 'cancelled' else OutcomeState.FAILURE)


def test_only_explicit_expired_deadline_reclassifies_cancellation():
    error = asyncio.CancelledError(SENTINEL)
    assert failure_outcome(error, deadline_expired=True).cause == OutcomeCause.TIMEOUT
    with pytest.raises(TypeError, match='explicit deadline'):
        failure_diagnostic(error, deadline_expired='unknown')


def test_failure_code_structural_admission_rejects_arbitrary_content():
    boundary = EmissionBoundary()
    for code in FAILURE_CODES:
        assert _structural(boundary, {'api_agent.failure.code': code}) == {'api_agent.failure.code': code}
    for cause in OutcomeCause:
        assert _structural(boundary, {'api_agent.failure.cause': cause.value}) == {'api_agent.failure.cause': cause.value}
    for value in (SENTINEL, [], {}, 1, None):
        assert _structural(boundary, {'api_agent.failure.code': value}) == {}
        assert _structural(boundary, {'api_agent.failure.cause': value}) == {}


def test_report_failure_requires_explicit_observation_runtime_and_terminal_authority():
    runtime = object()
    owner = OperationOutcome(runtime, None)
    observations = Mock(trace=runtime)
    with bind_operation(owner):
        report_failure(ContractError(SENTINEL), observations=observations, scope_ids=('node',))
        assert owner.final.state == OutcomeState.UNKNOWN
        report_failure(WorkflowError(SENTINEL), observations=SimpleNamespace(trace=object()), terminal=True)
        assert owner.final.state == OutcomeState.UNKNOWN
        report_failure(WorkflowError(SENTINEL), observations=observations, scope_ids=('graph',), terminal=True)
        assert owner.final.cause == OutcomeCause.EXECUTION
    assert observations.report_failure.call_count == 2
    report_failure(WorkflowError(SENTINEL), terminal=True)


@contextmanager
def trace_runtime(log=False, trace=False, mode='OTEL'):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    provider = TracerProvider(shutdown_on_exit=False)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    resolved = config(log=log, trace=trace, mode=mode,
        LANGFUSE_PUBLIC_KEY='synthetic-public', LANGFUSE_SECRET_KEY='synthetic-secret')
    runtime = TraceRuntime(resolved, factory=lambda *args: (provider, provider.get_tracer('failure-test')))
    asyncio.run(runtime.astart())
    try:
        yield runtime, resolved, exporter
    finally:
        assert runtime.shutdown()


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('mode', ['OTEL', 'LangFuse'])
def test_immutable_failure_scopes_and_request_keep_local_cause_without_prose(log, trace, mode):
    from opentelemetry.trace import StatusCode
    flow = workflow()
    async def fail(inputs, context):
        raise WorkflowError(SENTINEL)
    replace_node(flow, 'first_draft', fail)
    with trace_runtime(log, trace, mode) as (runtime, resolved, exporter):
        flow.services['observation_factory'] = WorkflowObservationFactory(
            resolved, boundary=EmissionBoundary(), trace=runtime)
        with operation(runtime) as owner:
            with pytest.raises(WorkflowError, match=SENTINEL):
                asyncio.run(flow.arun({'user_input': 'user'}))
        assert owner.final.cause == OutcomeCause.EXECUTION
        spans = exporter.get_finished_spans()
        failed = [span for span in spans if span.attributes.get('api_agent.outcome.state') == 'failure']
        assert {span.name for span in failed} >= {'request', 'graph GRAPH', 'component first', 'node first_draft'}
        assert all(span.status.status_code == StatusCode.ERROR for span in failed)
        assert all(span.attributes['error.type'] == 'execution' for span in failed)
        assert all(span.attributes.get('api_agent.failure.code') == 'workflow' for span in failed if span.name != 'request')
        assert SENTINEL not in repr([dict(span.attributes) for span in spans])


def test_actual_unrepaired_output_schema_is_validation_not_workflow_failure():
    from api_agent.workflows.application import build
    from pathlib import Path
    async def complete(request):
        return LLMResult(Message('assistant', (TextContent('answer'),)))
    flow = build(Path(__file__).resolve().parents[2], llm=complete, selection='synthetic')
    async def invalid(inputs, context):
        return {}
    replace_node(flow, 'llm_call', invalid)
    with trace_runtime() as (runtime, resolved, exporter):
        flow.services['observation_factory'] = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), trace=runtime)
        with operation(runtime) as owner:
            with pytest.raises(WorkflowError):
                asyncio.run(flow.arun({'messages': [{'role': 'user', 'content': 'user'}]}))
        assert owner.final.cause == OutcomeCause.VALIDATION
        failed = [span for span in exporter.get_finished_spans() if span.attributes.get('api_agent.outcome.state') == 'failure']
        assert all(span.attributes['error.type'] == 'validation' for span in failed)
        assert all(span.attributes.get('api_agent.failure.code') == 'schema_validation' for span in failed if span.name != 'request')


def test_repaired_child_failure_does_not_poison_graph_or_request():
    flow = workflow()
    async def repair(inputs, context):
        if not context.correction_attempt:
            raise CandidateCorrection({'reason': 'revise'})
        return draft()
    replace_node(flow, 'first_draft', repair)
    with trace_runtime() as (runtime, resolved, exporter):
        from api_agent.telemetry.outcomes import ExecutionOutcome
        flow.services.update(observation_factory=WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), trace=runtime),
                             execution_outcome=lambda: ExecutionOutcome(OutcomeState.SUCCESS))
        with operation(runtime) as owner:
            asyncio.run(flow.arun({'user_input': 'user'}))
        assert owner.final.state == OutcomeState.SUCCESS
        spans = exporter.get_finished_spans()
        rejected = next(span for span in spans if span.name == 'node first_draft' and span.attributes.get('error.type') == 'validation')
        assert rejected.attributes['api_agent.failure.code'] == 'candidate_validation'
        for span in spans:
            if span.name in ('request', 'graph GRAPH'):
                assert span.attributes['api_agent.outcome.state'] == 'success'
                assert 'error.type' not in span.attributes


def test_stateful_failure_cause_survives_component_teardown_cancellation(fixture_graph):
    from langgraph.graph.message import add_messages
    from api_agent.workflows.execution import (Workflow)
    from api_agent.workflows.definition import (load_definition)
    from api_agent.workflows.native import (load_native_bindings)
    root, entry = fixture_graph
    native = root / 'native.py'
    native.write_text(native.read_text() + '\nasync def intake(state, context):\n'
                      '    from api_agent.providers.contracts import ContractError\n'
                      '    raise ContractError("' + SENTINEL + '")\n')
    (root / 'group.md.html').write_text('# Component: group\n\n## Flow\n\n'
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)\n')
    entry.write_text(entry.read_text().replace('1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)').replace('| finish | END | - | - |',
        '| finish | END | - | - |\n| group | END | - | - |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    flow = Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages})
    with trace_runtime() as (runtime, resolved, exporter):
        factory = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), trace=runtime)
        with operation(runtime) as owner:
            with pytest.raises(ContractError):
                asyncio.run(flow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='failure', services={'observation_factory': factory}))
        assert owner.final.cause == OutcomeCause.VALIDATION
        spans = exporter.get_finished_spans()
        for name in ('node intake', 'component group', 'graph GRAPH', 'request'):
            span = next(span for span in spans if span.name == name)
            assert span.attributes['error.type'] == 'validation'
            if name != 'request':
                assert span.attributes['api_agent.failure.code'] == 'contract_validation'
        assert SENTINEL not in repr([dict(span.attributes) for span in spans])


def test_studio_local_workflow_failure_keeps_http_response_policy_and_execution_cause():
    from test_studio_bridge import setup_bridge
    backend, _ = setup_bridge()
    runtime = object()
    backend.owner.trace = runtime
    owner = OperationOutcome(runtime, None)
    error = WorkflowError(SENTINEL)
    with bind_operation(owner):
        response = backend._error(error)
    assert json.loads(response.content) == {'code': 'unknown'}
    assert SENTINEL.encode() not in response.content
    assert owner.final.cause == OutcomeCause.EXECUTION
    asyncio.run(backend.aclose())


def test_closed_scope_rejects_late_failure_and_failure_supersedes_earlier_cancellation():
    from uuid import uuid4
    from test_workflow_observations import definition
    with trace_runtime() as (runtime, resolved, exporter):
        observations = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), trace=runtime).create(definition())
        scope = str(uuid4())
        observations.report_failure(scope, WorkflowError(SENTINEL))
        observations.observe(dict(event='scope_start', scope_id=scope, owner='Make', kind='node'))
        observations.report_failure(scope, asyncio.CancelledError())
        observations.report_failure(scope, ContractError(SENTINEL))
        observations.report_failure(scope, asyncio.CancelledError())
        observations.observe(dict(event='scope_end', scope_id=scope, owner='Make', kind='node', outcome='failed'))
        observations.report_failure(scope, WorkflowError(SENTINEL))
        observations.close()
        span, = exporter.get_finished_spans()
        assert span.attributes['error.type'] == 'validation'
        assert span.attributes['api_agent.failure.code'] == 'contract_validation'
        assert observations._scope_failures == {}


@pytest.mark.parametrize('cause,code,cause_id', [
    ('validation', 'schema_validation', None),
    (OutcomeCause.VALIDATION, 'workflow', None),
    (OutcomeCause.VALIDATION, [], None),
    (OutcomeCause.VALIDATION, 'schema_validation', 'not-a-uuid'),
    (OutcomeCause.VALIDATION, 'schema_validation', 7),
])
def test_failure_origin_rejects_forged_classification_and_call_identity(cause, code, cause_id):
    from api_agent.telemetry.outcomes import FailureOrigin
    with pytest.raises(ValueError):
        FailureOrigin(cause, code, cause_id)


def test_provider_boundary_and_explicit_wrappers_preserve_canonical_call_origin():
    from uuid import uuid4
    from api_agent.telemetry.outcomes import retain_failure, inherit_failure
    original = OperationalError('invalid_request')
    assert failure_outcome(original).cause == OutcomeCause.EXECUTION
    retain_failure(original, provider=True)
    assert failure_diagnostic(original)['cause'] == 'provider'
    identifier = str(uuid4())
    retain_failure(original, cause_id=identifier)
    wrapped = inherit_failure(WorkflowError('fixed safe wrapper'), original)
    assert failure_outcome(wrapped).cause == OutcomeCause.PROVIDER
    assert failure_outcome(wrapped).cause_id == identifier
    assert failure_diagnostic(wrapped)['code'] == 'provider'
    local = inherit_failure(WorkflowError('fixed safe wrapper'), ContractError(SENTINEL))
    assert failure_outcome(local).cause == OutcomeCause.VALIDATION
    assert failure_outcome(local).cause_id is None
    cancelled = inherit_failure(asyncio.CancelledError(), original)
    assert failure_outcome(cancelled).state == OutcomeState.CANCELLED
    assert failure_outcome(cancelled, deadline_expired=True).cause == OutcomeCause.TIMEOUT


def test_failure_origin_admission_rejects_nonexception_and_nonboolean_ownership():
    from api_agent.telemetry.outcomes import retain_failure, inherit_failure
    with pytest.raises(TypeError):
        retain_failure(object())
    with pytest.raises(TypeError):
        retain_failure(RuntimeError(), provider='provider')
    with pytest.raises(TypeError):
        inherit_failure(object(), RuntimeError())
    with pytest.raises(TypeError):
        inherit_failure(RuntimeError(), object())
    error = RuntimeError(SENTINEL)
    error._api_agent_failure_origin = {'cause': 'provider', 'code': SENTINEL}
    assert failure_diagnostic(error) == {'version': 1, 'cause': 'execution', 'code': 'execution'}


def test_local_llm_failure_rebinds_origin_to_actual_accounted_call(selected):
    from test_local_llm import request
    owner, implementation, _, _, _ = selected
    failure = OperationalError('invalid_request')
    async def fail(value, *, auth):
        raise failure
    implementation.acomplete = fail
    async def run():
        try:
            with pytest.raises(OperationalError) as caught:
                await owner.complete(request())
            assert caught.value is failure
            row, = owner.usage
            outcome = failure_outcome(caught.value)
            assert outcome.cause_id == row['call_id']
            assert outcome.cause == OutcomeCause.PROVIDER
        finally:
            await owner.aclose()
    asyncio.run(run())


def test_metadata_refusing_provider_error_remains_the_actual_primary(selected):
    from test_local_llm import request
    from api_agent.telemetry.outcomes import retain_failure, inherit_failure
    class PrivateFailure(ProviderAccessError):
        def __getattribute__(self, name):
            if name == '_api_agent_failure_origin':
                raise RuntimeError('metadata unavailable')
            return super().__getattribute__(name)
        def __setattr__(self, name, value):
            if name == '_api_agent_failure_origin':
                raise TypeError('metadata unavailable')
            super().__setattr__(name, value)
    error = PrivateFailure('invalid_request', 'Safe provider failure')
    owner, implementation, _, _, _ = selected
    async def fail(value, *, auth):
        raise error
    implementation.acomplete = fail
    async def run():
        try:
            with pytest.raises(PrivateFailure) as caught:
                await owner.complete(request())
            assert caught.value is error
            assert owner.usage[0]['outcome'] == 'failed'
            assert failure_outcome(error).cause == OutcomeCause.PROVIDER
            assert failure_outcome(error).cause_id is None
        finally:
            await owner.aclose()
    asyncio.run(run())
    assert retain_failure(error, provider=True) is error
    assert inherit_failure(error, ContractError(SENTINEL)) is error


def test_failure_classification_reads_one_stable_origin_snapshot():
    from uuid import uuid4
    from api_agent.telemetry.outcomes import FailureOrigin
    identifier = str(uuid4())
    origin = FailureOrigin(OutcomeCause.PROVIDER, 'provider', identifier)
    class OneReadFailure(RuntimeError):
        reads = 0
        def __getattribute__(self, name):
            if name == '_api_agent_failure_origin':
                self.reads += 1
                return origin if self.reads == 1 else None
            return super().__getattribute__(name)
    error = OneReadFailure()
    result = failure_outcome(error)
    assert error.reads == 1
    assert result.cause == OutcomeCause.EXECUTION and result.cause_id is None


@pytest.mark.parametrize('nested_engine', ['immutable', 'stateful'])
def test_handled_nested_graph_failure_does_not_claim_terminal_request(nested_engine, fixture_graph):
    from api_agent.telemetry.outcomes import ExecutionOutcome
    parent = workflow()
    with trace_runtime() as (runtime, resolved, exporter):
        factory = WorkflowObservationFactory(resolved, boundary=EmissionBoundary(), trace=runtime)
        if nested_engine == 'immutable':
            child = workflow()
            async def fail(inputs, context):
                raise ContractError(SENTINEL)
            replace_node(child, 'first_draft', fail)
            child.services['observation_factory'] = factory
            async def run_child():
                await child.arun({'user_input': 'user'})
        else:
            from langgraph.graph.message import add_messages
            from api_agent.workflows.execution import (Workflow)
            from api_agent.workflows.definition import (load_definition)
            from api_agent.workflows.native import (load_native_bindings)
            root, entry = fixture_graph
            native = root / 'native.py'
            native.write_text(native.read_text() + '\nasync def intake(state, context):\n'
                              '    from api_agent.providers.contracts import ContractError\n'
                              '    raise ContractError("' + SENTINEL + '")\n')
            definition = load_definition(entry, base_root=root, pack_root=root)
            child = Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages})
            async def run_child():
                await child.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='nested',
                                 services={'observation_factory': factory})
        handled = []
        async def producer(inputs, context):
            try:
                await run_child()
            except ContractError as error:
                handled.append(error)
            return draft()
        replace_node(parent, 'first_draft', producer)
        parent.services.update(observation_factory=factory,
                               execution_outcome=lambda: ExecutionOutcome(OutcomeState.SUCCESS))
        with operation(runtime) as owner:
            asyncio.run(parent.arun({'user_input': 'user'}))
        assert len(handled) == 1
        assert owner.final.state == OutcomeState.SUCCESS
        graphs = [span for span in exporter.get_finished_spans() if span.name == 'graph GRAPH']
        assert len(graphs) == 2
        assert {span.attributes['api_agent.outcome.state'] for span in graphs} == {'failure', 'success'}
        request, = [span for span in exporter.get_finished_spans() if span.name == 'request']
        assert request.attributes['api_agent.outcome.state'] == 'success'
        assert 'error.type' not in request.attributes


def test_admitted_terminal_publisher_binds_remote_failure_identity_and_early_return_does_not(tmp_path, monkeypatch):
    from uuid import uuid4
    from api_agent.testing.inference_usage import publish
    from api_agent.workflows.attribution import bind_workflow_visit
    from api_agent.workflows.journal import WorkflowJournal
    from api_agent.providers.llm import LLMRequest, CompletionRequest
    from test_workflow_observations import definition
    error = OperationalError('invalid_request')
    request = LLMRequest('synthetic', CompletionRequest((Message('user', (TextContent('user'),)),)))
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    assert publish(request, failed=True, error=error) is None
    assert failure_outcome(error).cause_id is None
    path = tmp_path / 'terminal-events.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'test_generic.py::test_failure (call)')
    journal = WorkflowJournal(definition())
    visit, identifier = str(uuid4()), str(uuid4())
    journal.observe(dict(event='scope_start', scope_id=visit, owner='Make', kind='node'))
    with bind_workflow_visit(journal, visit):
        assert publish(request, failed=True, error=error, call_id=identifier) == identifier
    call, = [row for row in journal.finalize()['events'] if row['event'] == 'call']
    assert call['call_id'] == failure_outcome(error).cause_id == identifier
    assert failure_outcome(error).cause == OutcomeCause.PROVIDER
    event, = [json.loads(line) for line in path.read_text().splitlines()]
    assert event['data']['call_id'] == identifier


def test_provider_receipts_reject_forgery_copy_and_reset_reused_exception():
    from uuid import uuid4
    from api_agent.telemetry.outcomes import FailureOrigin, retain_failure, inherit_failure
    identifier = str(uuid4())
    error = OperationalError('unavailable')
    error._api_agent_failure_origin = FailureOrigin(OutcomeCause.VALIDATION, 'schema_validation', identifier)
    assert failure_outcome(error).cause == OutcomeCause.EXECUTION
    retain_failure(error, provider=True, cause_id=identifier)
    assert failure_outcome(error).cause_id == identifier
    copied = RuntimeError('another failure')
    copied._api_agent_failure_origin = error._api_agent_failure_origin
    assert failure_outcome(copied).cause_id is None
    assert failure_outcome(copied).cause == OutcomeCause.EXECUTION
    wrapped = inherit_failure(RuntimeError('wrapper'), error)
    assert failure_outcome(wrapped).cause_id == identifier
    retain_failure(error, provider=True)
    assert failure_outcome(error).cause == OutcomeCause.PROVIDER
    assert failure_outcome(error).cause_id is None
    # A wrapper deliberately captured the old call; resetting its source cannot
    # retroactively change the already-issued immutable wrapper receipt.
    assert failure_outcome(wrapped).cause_id == identifier


def test_issued_failure_receipts_do_not_keep_exception_lifetimes_alive():
    import gc
    import weakref
    from api_agent.telemetry.outcomes import retain_failure
    class Error(RuntimeError):
        pass
    error = Error('temporary')
    reference = weakref.ref(error)
    retain_failure(error, provider=True)
    del error
    gc.collect()
    assert reference() is None


def test_actual_provider_reusing_exception_gets_each_current_accounting_identity(selected):
    from test_local_llm import request
    owner, implementation, _, _, _ = selected
    error = OperationalError('invalid_request')
    async def fail(value, *, auth):
        raise error
    implementation.acomplete = fail
    async def run():
        try:
            identifiers = []
            for _ in range(2):
                with pytest.raises(OperationalError) as caught:
                    await owner.complete(request())
                assert caught.value is error
                identifiers.append(failure_outcome(error).cause_id)
                assert identifiers[-1] == owner.usage[-1]['call_id']
            assert identifiers[0] != identifiers[1]
        finally:
            await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('tier', ['auto', 'default', 'flex', 'priority', 'scale', 'standard', 'reserved'])
def test_known_response_service_tiers_are_safe_structural_metadata(tier):
    from api_agent.telemetry.tracing import _structural
    assert _structural(EmissionBoundary(), {'api_agent.response.service_tier': tier}) == {'api_agent.response.service_tier': tier}


@pytest.mark.parametrize('tier', ['unknown', 'Bearer synthetic-private-value', 'reserved-secret', 123, None])
def test_unrecognized_response_service_tiers_are_not_structural_metadata(tier):
    from api_agent.telemetry.tracing import _structural
    assert _structural(EmissionBoundary(), {'api_agent.response.service_tier': tier}) == {}


def test_concurrent_reused_exception_selects_own_call_and_omits_ambiguous_parent():
    from uuid import uuid4
    from api_agent.telemetry.outcomes import retain_failure
    error = OperationalError('unavailable')
    first, second = str(uuid4()), str(uuid4())
    async def run():
        ready = asyncio.Event()
        async def earlier():
            retain_failure(error, provider=True, cause_id=first)
            await ready.wait()
            assert failure_outcome(error).cause_id == first
        async def later():
            retain_failure(error, provider=True, cause_id=second)
            assert failure_outcome(error).cause_id == second
            ready.set()
        await asyncio.gather(earlier(), later())
        assert failure_outcome(error).cause_id is None
    asyncio.run(run())


def test_receipt_owner_overflow_stays_uncorrelated_after_further_reuse():
    from uuid import uuid4
    from api_agent.telemetry.outcomes import retain_failure
    error = OperationalError('unavailable')
    async def run():
        async def bind():
            retain_failure(error, provider=True, cause_id=str(uuid4()))
        await asyncio.gather(*(bind() for _ in range(65)))
        retain_failure(error, provider=True, cause_id=str(uuid4()))
        assert failure_outcome(error).cause_id is None
    asyncio.run(run())


def test_live_exception_receipt_does_not_retain_completed_task_owner():
    import gc
    import weakref
    from api_agent.telemetry.outcomes import retain_failure
    error = RuntimeError('kept alive')
    async def run():
        async def bind():
            retain_failure(error)
        task = asyncio.create_task(bind())
        reference = weakref.ref(task)
        await task
        return reference
    reference = asyncio.run(run())
    gc.collect()
    assert reference() is None
    assert failure_outcome(error).cause == OutcomeCause.EXECUTION


def test_unambiguous_child_receipt_propagates_to_parent_without_foreign_owner_retention():
    from uuid import uuid4
    from api_agent.telemetry.outcomes import retain_failure
    error = OperationalError('unavailable')
    identifier = str(uuid4())
    async def run():
        async def child():
            retain_failure(error, provider=True, cause_id=identifier)
        await asyncio.create_task(child())
        assert failure_outcome(error).cause_id == identifier
    asyncio.run(run())


def test_unrelated_unadmitted_child_prevents_parent_adopting_other_call_identity():
    from uuid import uuid4
    from api_agent.telemetry.outcomes import retain_failure
    error = OperationalError('unavailable')
    identifier = str(uuid4())
    async def run():
        async def admitted():
            retain_failure(error, provider=True, cause_id=identifier)
        async def unadmitted():
            retain_failure(error, provider=True)
            assert failure_outcome(error).cause_id is None
        await asyncio.gather(admitted(), unadmitted())
        assert failure_outcome(error).cause_id is None
    asyncio.run(run())


def test_explicit_common_call_preserves_canonical_identity_across_child_tasks():
    from uuid import uuid4
    from api_agent.telemetry.outcomes import retain_failure, failure_call
    error = OperationalError('unavailable')
    identifier = str(uuid4())
    async def run():
        async def provider():
            retain_failure(error, provider=True)
        async def accounting():
            retain_failure(error, provider=True, cause_id=identifier)
        with failure_call():
            await asyncio.gather(provider(), accounting())
        assert failure_outcome(error).cause_id == identifier
    asyncio.run(run())


def test_common_call_with_conflicting_classification_does_not_guess_parent_cause():
    from api_agent.telemetry.outcomes import retain_failure, inherit_failure, failure_call
    error = RuntimeError('shared wrapper')
    async def run():
        async def validation():
            inherit_failure(error, ContractError('invalid'))
        async def execution():
            retain_failure(error, provider=True)
        with failure_call():
            await asyncio.gather(validation(), execution())
        assert failure_outcome(error).cause == OutcomeCause.EXECUTION
        assert failure_outcome(error).cause_id is None
    asyncio.run(run())
