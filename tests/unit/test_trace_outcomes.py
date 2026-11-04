"""Typed terminal outcome precedence and mapping admission are structural contracts."""
import asyncio
from unittest.mock import Mock
from uuid import uuid4

import pytest

from api_agent.telemetry.outcomes import (ExecutionOutcome, OutcomeState, OutcomeCause,
    OperationOutcome, bind_operation, current_operation, report_outcome)
from api_agent.telemetry.operation import operation, safe_trace
from api_agent.telemetry.trace_mapping import TraceMapping, admit_mapping, genai_messages


@pytest.mark.parametrize('args', [dict(state='failure'), dict(state=OutcomeState.SUCCESS, version=True),
    dict(state=OutcomeState.FAILURE), dict(state=OutcomeState.SUCCESS, cause=OutcomeCause.TOOL),
    dict(state=OutcomeState.UNKNOWN, cause='tool'), dict(state=OutcomeState.UNKNOWN, cause_id='bad')])
def test_invalid_outcome_rejects(args):
    with pytest.raises(ValueError):
        ExecutionOutcome(**args)


def test_terminal_failure_supersedes_partial_but_success_cannot_erase_failure():
    owner = OperationOutcome(object(), None)
    with bind_operation(owner):
        report_outcome(ExecutionOutcome(OutcomeState.PARTIAL, OutcomeCause.TOOL), runtime=owner.runtime)
        report_outcome(ExecutionOutcome(OutcomeState.FAILURE, OutcomeCause.VALIDATION, str(uuid4())), runtime=owner.runtime)
        report_outcome(ExecutionOutcome(OutcomeState.SUCCESS), runtime=owner.runtime)
        assert owner.final.state == OutcomeState.FAILURE
        assert owner.final.cause == OutcomeCause.VALIDATION
        assert current_operation(object()) is None
        owner.closed = True
        assert current_operation() is None
        owner.report(ExecutionOutcome(OutcomeState.CANCELLED))
        assert owner.final.state == OutcomeState.FAILURE
    assert current_operation() is None
    report_outcome(ExecutionOutcome(OutcomeState.SUCCESS), runtime=owner.runtime)
    with pytest.raises(TypeError):
        report_outcome('success', runtime=owner.runtime)
    with pytest.raises(TypeError):
        owner.report('success')


@pytest.mark.parametrize('error,state,cause', [(ValueError('private'),OutcomeState.FAILURE,OutcomeCause.EXECUTION),
    (TimeoutError('private'),OutcomeState.FAILURE,OutcomeCause.TIMEOUT),
    (asyncio.CancelledError('private'),OutcomeState.CANCELLED,OutcomeCause.CANCELLED)])
def test_operation_reports_safe_terminal_outcome_and_closes_once(error,state,cause):
    runtime = Mock()
    with pytest.raises(type(error)):
        with operation(runtime) as owner:
            raise error
    assert owner.closed
    runtime.begin.return_value.outcome.assert_called_once_with(ExecutionOutcome(state,cause))
    runtime.begin.return_value.end.assert_called_once_with()


@pytest.mark.asyncio
async def test_late_inherited_task_cannot_mutate_closed_or_nested_runtime():
    ready, release = asyncio.Event(), asyncio.Event()
    runtime = Mock()
    with operation(runtime) as owner:
        async def late():
            ready.set()
            await release.wait()
            report_outcome(ExecutionOutcome(OutcomeState.FAILURE, OutcomeCause.TOOL), runtime=owner.runtime)
        task = asyncio.create_task(late())
        await ready.wait()
        with operation(Mock()) as nested:
            assert current_operation(runtime) is None
            report_outcome(ExecutionOutcome(OutcomeState.SUCCESS), runtime=runtime)
            assert nested.final.state == OutcomeState.UNKNOWN
    release.set()
    await task
    assert owner.final.state == OutcomeState.UNKNOWN


def test_nonfatal_sdk_failure_retains_logging_failure_policy(monkeypatch):
    assert safe_trace(lambda: (_ for _ in ()).throw(ValueError('private'))) is None
    def failed_log(*args):
        raise OSError('diagnostic failed')
    monkeypatch.setattr('logging.Logger.error', failed_log)
    with pytest.raises(OSError,match='diagnostic failed'):
        safe_trace(lambda: (_ for _ in ()).throw(ValueError('private')))


@pytest.mark.parametrize('key', ['gen_ai.request.model','api_agent.outcome.state','error.type',
    'http.response.status_code','service.name','otel.scope.name','other.payload'])
def test_custom_mapper_cannot_overwrite_structural_or_undeclared_fields(key):
    class Mapping:
        version = 1
        def io_attribute(self,direction): return key
    with pytest.raises(ValueError,match='Invalid native trace attribute'):
        admit_mapping(Mapping())


def test_third_mapping_contract_is_frozen_after_admission():
    class Mapping:
        version = 1
        def io_attribute(self,direction): return 'input.value' if direction == 'input' else 'output.value'
    mapper = Mapping()
    admitted = admit_mapping(mapper)
    mapper.io_attribute = lambda _: 'error.type'
    assert admitted.io_attribute('input') == 'input.value'
    assert admitted.io_attribute('output') == 'output.value'
    with pytest.raises(ValueError): admitted.io_attribute('other')
    with pytest.raises(ValueError): TraceMapping().io_attribute('other')
    with pytest.raises(ValueError): admit_mapping(object())


def test_unavailable_trace_begin_does_not_prevent_operation():
    runtime = Mock()
    runtime.begin.side_effect = RuntimeError('private')
    with operation(runtime) as owner:
        report_outcome(ExecutionOutcome(OutcomeState.SUCCESS), runtime=owner.runtime)
    assert owner.final.state == OutcomeState.SUCCESS
    assert owner.closed


@pytest.mark.parametrize('direction,value', [('input',None),('input',{'llm_request':None}),
    ('output',{}),('input',{}),('input',{'messages':[None]}),
    ('input',{'messages':[{'role':'unknown'}]}),('input',{'messages':[{'role':'user','content':7}]}),
    ('input',{'messages':[{'role':'user','content':[7]}]}),
    ('input',{'messages':[{'role':'user','content':['x']*65}]}),
    ('input',{'messages':[{}]*257})])
def test_unknown_native_message_forms_remain_unavailable(direction,value):
    assert genai_messages(direction,value) is None


@pytest.mark.parametrize('direction,value', [('input',{'messages':[{'role':'user','text':'visible'}]}),
    ('input',{'messages':[{'role':'user','content':['visible']}]}),
    ('output',{'message':{'role':'assistant','content':['visible']}}),
    ('output',{'text':'visible'})])
def test_normalized_messages_use_role_parts_and_text(direction,value):
    messages = genai_messages(direction,value)
    assert messages == [{'role':'user' if direction == 'input' else 'assistant',
        'parts':[{'type':'text','content':'visible'}]}]


@pytest.mark.parametrize('version', [True,2])
def test_mapper_version_rejects_before_allocation(version):
    mapper = TraceMapping()
    mapper.version = version
    with pytest.raises(ValueError,match='Unsupported trace mapping contract'):
        admit_mapping(mapper)


@pytest.mark.parametrize('key', [None,'INVALID KEY'])
def test_mapper_invalid_key_rejects_before_allocation(key):
    mapper = TraceMapping()
    mapper.io_attribute = lambda _:key
    with pytest.raises(ValueError,match='Invalid native trace attribute'):
        admit_mapping(mapper)


@pytest.mark.parametrize('cause_id', [7,'ABCDEFAB-CDEF-ABCD-EFAB-CDEFABCDEFAB'])
def test_outcome_identity_requires_exact_canonical_uuid(cause_id):
    with pytest.raises(ValueError,match='Invalid outcome cause identity'):
        ExecutionOutcome(OutcomeState.UNKNOWN,cause_id=cause_id)


def test_builtin_native_mapping_compatibility():
    mapping = admit_mapping(TraceMapping())
    assert mapping.io_attribute('input') == 'langfuse.observation.input'
    assert mapping.io_attribute('output') == 'langfuse.observation.output'


def test_loss_owner_counts_every_failure_and_warns_once(caplog):
    from api_agent.telemetry.operation import TraceLoss
    owner = TraceLoss()
    assert owner.run(lambda: 3) == 3
    for _ in range(2):
        assert owner.run(lambda: (_ for _ in ()).throw(ValueError('private'))) is None
    assert owner.count == 2
    assert len(caplog.records) == 1
    assert 'private' not in caplog.text


def test_sdk_failure_is_nonfatal_and_loss_diagnostic_is_bounded(caplog):
    from api_agent.telemetry.tracing import TraceSpan
    from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
    sdk = Mock()
    sdk.set_attributes.side_effect = ValueError('private')
    sdk.set_attribute.side_effect = ValueError('private')
    sdk.add_event.side_effect = ValueError('private')
    sdk.end.side_effect = ValueError('private')
    span = TraceSpan(sdk,EmissionBoundary(ContentPolicy(True)))
    span.attributes({'status':'ok'})
    span.native_io('input',{'visible':'value'})
    span.event({'visible':'value'})
    span.outcome(ExecutionOutcome(OutcomeState.FAILURE,OutcomeCause.TOOL))
    span.end()
    span.end()
    assert span._losses.count == 5
    assert len(caplog.records) == 1
    assert 'private' not in caplog.text
    sdk.end.assert_called_once_with(end_time=None)


def test_originating_typed_failure_cause_survives_generic_scope_close():
    from api_agent.telemetry.tracing import TraceSpan
    from api_agent.telemetry.privacy import EmissionBoundary
    span = TraceSpan(Mock(),EmissionBoundary())
    original = ExecutionOutcome(OutcomeState.PARTIAL,OutcomeCause.TOOL)
    span.outcome(original)
    span.outcome(ExecutionOutcome(OutcomeState.SUCCESS))
    assert span._outcome == original
    failed = ExecutionOutcome(OutcomeState.FAILURE,OutcomeCause.VALIDATION)
    span.outcome(failed)
    span.outcome(ExecutionOutcome(OutcomeState.FAILURE,OutcomeCause.EXECUTION))
    assert span._outcome == failed


def test_missing_runtime_authority_never_adopts_ambient_owner():
    owner=OperationOutcome(object(),None)
    with bind_operation(owner):
        report_outcome(ExecutionOutcome(OutcomeState.FAILURE,OutcomeCause.TOOL),runtime=None)
        assert owner.final.state==OutcomeState.UNKNOWN
        with pytest.raises(TypeError):
            report_outcome(ExecutionOutcome(OutcomeState.SUCCESS))


@pytest.mark.parametrize('initial', [OutcomeState.UNKNOWN, OutcomeState.PARTIAL, OutcomeState.FAILURE])
def test_completed_response_preserves_terminal_errors_and_ignores_late_cancellation(initial):
    from api_agent.telemetry.outcomes import report_response_complete
    runtime = object()
    owner = OperationOutcome(runtime, None)
    with bind_operation(owner):
        if initial != OutcomeState.UNKNOWN:
            owner.report(ExecutionOutcome(initial, OutcomeCause.TOOL))
        report_response_complete(runtime=None)
        report_response_complete(runtime=object())
        assert owner.final.state == initial
        report_response_complete(runtime=runtime)
        owner.report(ExecutionOutcome(OutcomeState.CANCELLED, OutcomeCause.CANCELLED))
        assert owner.final.state == (OutcomeState.SUCCESS if initial == OutcomeState.UNKNOWN else initial)
        owner.report(ExecutionOutcome(OutcomeState.FAILURE, OutcomeCause.EXECUTION))
        assert owner.final.state == OutcomeState.FAILURE
        owner.finish()
        owner.complete_response()
    report_response_complete(runtime=runtime)


def test_trace_loss_diagnostic_failure_remains_fatal_through_optional_wrappers(monkeypatch):
    from api_agent.telemetry.operation import TraceLoss
    owner=TraceLoss()
    def fail(*args):raise OSError('private logging failure')
    monkeypatch.setattr('logging.Logger.error',fail)
    for _ in range(2):
        with pytest.raises(OSError,match='Could not record tracing loss diagnostic'):
            owner.run(owner.record)


@pytest.mark.parametrize('lost',[False,True])
def test_open_request_summarizes_loss_before_ending(lost):
    from types import SimpleNamespace
    from api_agent.telemetry.operation import TraceLoss
    span=Mock()
    runtime=SimpleNamespace(begin=lambda *args,**kwargs:span,losses=TraceLoss())
    with operation(runtime):
        if lost:runtime.losses.record()
    if lost:span.capture_loss.assert_called_once_with(1)
    else:span.capture_loss.assert_not_called()
    assert span.method_calls[-1][0]=='end'
