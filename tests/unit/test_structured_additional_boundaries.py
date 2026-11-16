"""Additional security boundaries for structured replay and capture."""
import json
import pickle
from dataclasses import replace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.inference import request_to_wire
from api_agent.providers.llm import Message, TextContent, ToolResult
from api_agent.providers.structured_proposals import (
    StructuredProposalReceipt, StructuredReplayAuthority, StructuredWorkerAdmission,
    StructuredWorkerControl, VerifiedStructuredReplay, _bind_history_replay,
    _feedback, _wire_digest, bind_verified_replay, proposal_capture_projection,
)
from test_structured_replay_authority import original, proposal, replay


def test_capabilities_cannot_be_constructed_by_callers():
    for construct, args in ((StructuredProposalReceipt, (None, None, None)),
                            (VerifiedStructuredReplay, (None, None, 0, None)),
                            (StructuredWorkerControl, (None, b'', None, None, None))):
        with pytest.raises(ContractError):
            construct(*args)


@pytest.mark.asyncio
async def test_permit_is_opaque_immutable_unserializable_and_single_scope():
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    permit = authority.consume(replay(request, result, await authority.issue(request, result)))
    assert repr(permit) == '<VerifiedStructuredReplay>'
    with pytest.raises(AttributeError):
        permit._digest = b'changed'
    with pytest.raises(TypeError):
        pickle.dumps(permit)
    with bind_verified_replay(permit):
        pass
    with pytest.raises(ContractError, match='already used'):
        with bind_verified_replay(permit):
            pass


def test_history_and_public_scope_require_owner_evidence():
    with pytest.raises(ContractError):
        with _bind_history_replay(original()):
            pass
    with pytest.raises(ContractError):
        with bind_verified_replay(object()):
            pass


@pytest.mark.parametrize('value', [
    {'x': 'é' * 1048577},
    {'x': '\\' * 1048576},
    {str(i): None for i in range(8193)},
    [[[[[[[[0]]]]]]]] * 0 + [None] * 8193,
])
def test_wire_digest_bounds_utf8_serialized_and_container_size(value):
    with pytest.raises(ContractError, match='evidence'):
        _wire_digest(value)


def test_wire_digest_rejects_excessive_nesting():
    value = None
    for _ in range(65):
        value = [value]
    with pytest.raises(ContractError, match='evidence'):
        _wire_digest(value)


@pytest.mark.parametrize('message', [
    Message('user', (TextContent('x'),)),
    Message('tool', results=(ToolResult('other', (TextContent('{}'),)),)),
    Message('tool', results=(ToolResult('wire-id', (TextContent('{}'), TextContent('{}'))),)),
])
def test_feedback_requires_one_result_for_the_exact_call(message):
    with pytest.raises(ContractError, match='result'):
        _feedback(message, 'wire-id')


@pytest.mark.asyncio
async def test_authority_rejects_foreign_receipts_and_invalid_origin():
    first, other = StructuredReplayAuthority(), StructuredReplayAuthority()
    request, result = original(), proposal()
    receipt = await first.issue(request, result)
    with pytest.raises(ContractError, match='authority'):
        other.release(receipt)
    with pytest.raises(ContractError, match='authority'):
        other.inspect_origin(request, result, receipt)
    with pytest.raises(ContractError, match='origin'):
        first.inspect_origin(request, object(), receipt)
    receipt.release()
    with pytest.raises(ContractError, match='unavailable'):
        first.inspect_origin(request, result, receipt)


@pytest.mark.asyncio
async def test_issue_rejects_changed_source_and_non_proposal():
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    with pytest.raises(ContractError, match='origin'):
        await authority.issue(request, replace(result, structured_proposal=None))
    with pytest.raises(ContractError, match='source'):
        await authority.issue(request, result, source_request=object())
    with pytest.raises(ContractError, match='source'):
        await authority.issue(request, result, source_request=replace(request, selected_llm='different'))


def test_worker_admission_is_opaque_and_rejects_bad_operations():
    worker = StructuredWorkerAdmission(b'x' * 32)
    assert repr(worker) == '<StructuredWorkerAdmission>'
    with pytest.raises(TypeError):
        pickle.dumps(worker)
    for operation in ('', 'x' * 129, 'bad\n'):
        with pytest.raises(ContractError, match='admission'):
            worker.proof(request_to_wire(original()), {'model': 'selected'}, operation)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), {'x': object()}, {'x': '\ud800'}])
def test_capture_rejects_invalid_values(value):
    with pytest.raises(ContractError, match='capture projection'):
        proposal_capture_projection(value)


def test_capture_preserves_finite_numbers_and_removes_nested_internal_content():
    assert proposal_capture_projection({'value': 1.5, 'child': {'type': 'continuation', 'secret': 'x'}}) == {'value': 1.5}
    with pytest.raises(ContractError, match='capture projection'):
        proposal_capture_projection({'type': 'continuation'})


def test_capture_rejects_excessive_depth_and_utf8_size():
    value = None
    for _ in range(33):
        value = [value]
    for invalid in (value, 'é' * 2097153, {'é' * 2097153: 1}, {'x': 'x' * 4194305}):
        with pytest.raises(ContractError, match='capture projection'):
            proposal_capture_projection(invalid)


@pytest.mark.parametrize('profile', [[], {'default_strict': None}, {'default_strict': 'yes', 'schema_policy': 'converter'}])
def test_catalog_rejects_malformed_function_profile(profile):
    from api_agent.providers.catalog import catalog_from_wire, catalog_to_wire
    from api_agent.providers.llm import AvailableSelection
    row = catalog_to_wire((AvailableSelection('selected'),))[0]
    row['structured_function_profile'] = profile
    with pytest.raises(ContractError, match='catalog'):
        catalog_from_wire([row])


@pytest.mark.parametrize('edit', ['encoding', 'legacy_profile', 'missing_structured'])
def test_wire_rejects_inconsistent_structured_encoding(edit):
    from api_agent.providers.rich_inference import request_from_wire
    row = request_to_wire(original())
    if edit == 'encoding':
        row['structured']['encoding'] = 'json_schema'
    elif edit == 'legacy_profile':
        row['version'] = 2
        del row['structured']['encoding']
    else:
        row['structured'] = None
    with pytest.raises(ContractError, match='request'):
        request_from_wire(row)


@pytest.mark.parametrize('edit', ['version', 'marker'])
def test_result_wire_requires_proposal_marker_matching_the_call(edit):
    from api_agent.providers.rich_inference import result_from_wire, result_to_wire
    row = result_to_wire(proposal())
    if edit == 'version':
        row['version'] = 2
    else:
        row['structured_proposal']['call_id'] = 'another-call'
    with pytest.raises(ContractError, match='result'):
        result_from_wire(row)


@pytest.mark.asyncio
async def test_replay_wire_rejects_changes_even_without_receipt():
    from api_agent.providers.rich_inference import request_from_wire, validate_request_instances
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    permit = authority.consume(prepared)
    changed_row = request_to_wire(original())
    with bind_verified_replay(permit):
        changed = replace(prepared, request_id='changed', proposal_replay=None)
        with pytest.raises(ContractError, match='changed'):
            request_to_wire(changed)
        with pytest.raises(ContractError, match='changed'):
            request_from_wire(changed_row)
        with pytest.raises(ContractError, match='changed'):
            await validate_request_instances(changed)


@pytest.mark.asyncio
@pytest.mark.parametrize('event', [object(), 'partial'])
async def test_structured_stream_rejects_nonfinal_events(event):
    from api_agent.providers.loading import ProviderError, _ValidatedStream
    from api_agent.providers.llm import StreamEvent
    value = StreamEvent(TextContent('partial')) if event == 'partial' else event
    async def events():
        yield value
    stream = _ValidatedStream(events(), original())
    with pytest.raises(ProviderError, match='structured_proposal_stream_invalid'):
        await stream.__anext__()


@pytest.mark.asyncio
async def test_structured_stream_accepts_valid_final_proposal():
    from api_agent.providers.loading import _ValidatedStream
    from api_agent.providers.llm import StreamEvent
    event = StreamEvent(proposal(), 'completed')
    async def events():
        yield event
    assert await _ValidatedStream(events(), original()).__anext__() == event


def test_wire_rejects_oversized_utf8_before_serialization(monkeypatch):
    import api_agent.providers.structured_proposals as structured
    serialized = []
    original_dumps = structured.json.dumps
    def observe(*args, **kwargs):
        serialized.append(True)
        return original_dumps(*args, **kwargs)
    monkeypatch.setattr(structured.json, 'dumps', observe)
    with pytest.raises(ContractError, match='evidence'):
        structured._wire_digest({'x': 'é' * 1048577})
    assert not serialized


def test_failed_proposal_capture_is_marked_unavailable():
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import capture_inference, collect_inference
    with collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
        capture_inference('input', {'version': 4, 'structured': {'encoding': 'function_calling'}, 'bad': object()}, 'cause')
    assert collector['rows'] == [{'phase': 'request', 'content': {}, 'reason': 'unavailable'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('bound', [False, True])
async def test_history_save_failure_preserves_error_and_releases_receipt(monkeypatch, bound):
    from api_agent.workflows.inference_history import InferenceHistory
    authority, history = StructuredReplayAuthority(), InferenceHistory()
    request, result = original(), proposal()
    if bound:
        result = await authority.bind(request, result)
    receipt = result.structured_proposal.receipt
    async def callback(_request):
        return result
    def fail_save(*args):
        raise RuntimeError('save failed')
    monkeypatch.setattr(history, '_save', fail_save)
    try:
        with pytest.raises(RuntimeError, match='save failed'):
            await history.call('node', 'llm', callback, request)
        if receipt is not None:
            with pytest.raises(ContractError, match='unavailable'):
                authority.consume(replay(request, proposal(), receipt))
    finally:
        history.close()
        authority.close()


@pytest.mark.asyncio
async def test_optional_receipt_issuance_failure_preserves_completed_proposal(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.shared_setup import SharedSetupOwner
    from test_structured_replay_authority import PROFILE
    request, result = original(), proposal()
    selection = AvailableSelection('selected', features=('structured_function',), structured_function_profile=PROFILE)
    async def authenticate(callback):
        return await callback(auth='synthetic')
    async def complete(_request, *, auth):
        return result
    owner = SharedSetupOwner('operation', 'selected',
        lambda credentials: SimpleNamespace(call=authenticate, aclose=AsyncMock()),
        complete=complete, describe=AsyncMock(return_value=(selection,)))
    monkeypatch.setattr(owner._structured_replay_authority, 'bind', AsyncMock(side_effect=RuntimeError('unavailable')))
    try:
        actual = await owner.complete(request)
        assert actual is result
        assert actual.structured_proposal.receipt is None
    finally:
        await owner.aclose()


def test_function_calling_wire_omits_optional_provider_metadata():
    from api_agent.providers.rich_inference import _request_row
    request = original()
    definition = replace(request.payload.structured, provider_tool_schema=None, function_profile=None)
    row = _request_row(replace(request, payload=replace(request.payload, structured=definition)))
    assert row['structured']['encoding'] == 'function_calling'
    assert 'provider_tool_schema' not in row['structured']
    assert 'function_profile' not in row['structured']


def test_worker_proof_rejects_valid_non_proposal_wire():
    request = original()
    from api_agent.providers.llm import StructuredOutput
    definition = StructuredOutput('InvoiceResponse', request.payload.structured.schema, strict=False)
    ordinary = replace(request, payload=replace(request.payload, structured=definition))
    row = request_to_wire(ordinary)
    with pytest.raises(ContractError, match='worker admission'):
        StructuredWorkerAdmission(b'x' * 32).proof(row, {'model': 'selected'}, 'completions')


@pytest.mark.asyncio
async def test_unverified_replay_cannot_enter_unowned_provider():
    from api_agent.providers.loading import ProviderError, _StreamView, _DeferredStream
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    view = _StreamView(object())
    with pytest.raises(ProviderError, match='structured_replay_unverified'):
        await view._complete_admitted(prepared)
    with pytest.raises(ProviderError, match='structured_replay_unverified'):
        await _DeferredStream(view, prepared, None)._read_next_admitted()


@pytest.mark.asyncio
async def test_proposal_dispatch_requires_an_owner_before_provider_invocation():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import ProviderError, _StreamView, _DeferredStream
    from api_agent.providers.llm import AvailableSelection
    from test_structured_replay_authority import PROFILE
    selection = AvailableSelection('selected', features=('structured_function',), structured_function_profile=PROFILE)
    implementation = SimpleNamespace(adescribe=AsyncMock(return_value=(selection,)), acomplete=AsyncMock())
    view = _StreamView(implementation)
    with pytest.raises(ProviderError, match='structured_proposal_owner_missing'):
        await view._complete_admitted(original())
    with pytest.raises(ProviderError, match='structured_proposal_owner_missing'):
        await _DeferredStream(view, original(), None)._read_next_admitted()
    implementation.acomplete.assert_not_awaited()


@pytest.mark.asyncio
async def test_inspection_does_not_authorize_replay():
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    permit = authority.inspect(prepared)
    with pytest.raises(ContractError, match='permit'):
        with bind_verified_replay(permit):
            pass
    authority.consume(prepared)
    with pytest.raises(ContractError, match='authority'):
        authority.inspect(None)


@pytest.mark.asyncio
async def test_replay_evidence_changed_after_issue_is_rejected():
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    source = replace(request)
    prepared = replay(source, result, await authority.issue(request, result, source_request=source))
    # Only the effective request changes; the separate recorded source remains valid.
    object.__setattr__(request, 'selected_llm', 'changed')
    with pytest.raises(ContractError, match='evidence changed'):
        authority.consume(prepared)


@pytest.mark.asyncio
async def test_replay_can_expire_between_preflight_and_configuration():
    times = iter([0.0, 0.0, 300.0])
    authority = StructuredReplayAuthority(clock=lambda: next(times))
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    with pytest.raises(ContractError, match='unavailable'):
        authority.prepare_replay(prepared)


@pytest.mark.asyncio
async def test_worker_issuer_closes_if_proof_creation_fails(monkeypatch):
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.structured_proposals import _new_admission_authority, issue_worker_control
    from test_structured_replay_authority import PROFILE
    issuers = []
    def fail_proof(self, *args, **kwargs):
        issuers.append(self)
        raise RuntimeError('proof failure')
    monkeypatch.setattr(StructuredWorkerAdmission, 'proof', fail_proof)
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function',), structured_function_profile=PROFILE)
    with _new_admission_authority(owner).bind(owner, original(), selection):
        with pytest.raises(RuntimeError, match='proof failure'):
            issue_worker_control({'model': 'selected'}, 'completions')
    assert len(issuers) == 1 and issuers[0]._closed
    assert not any(issuers[0]._secret)


def test_worker_control_is_opaque_and_unserializable():
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.structured_proposals import _new_admission_authority, issue_worker_control
    from test_structured_replay_authority import PROFILE
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function',), structured_function_profile=PROFILE)
    with _new_admission_authority(owner).bind(owner, original(), selection):
        control = issue_worker_control({'model': 'selected'}, 'completions')
        assert repr(control) == '<StructuredWorkerControl>'
        with pytest.raises(TypeError):
            pickle.dumps(control)


def test_capture_rejects_oversized_key_before_copying():
    with pytest.raises(ContractError, match='capture projection'):
        proposal_capture_projection({'x' * 4194305: 1})


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['tools', 'unexpected_proposal'])
async def test_proposal_output_contract_rejects_incompatible_modes(mode):
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.llm import Tool
    from api_agent.providers.rich_inference import async_validate_result
    request, result = original(), proposal()
    if mode == 'tools':
        # Exercise the defense if a frozen request is corrupted after construction.
        object.__setattr__(request.payload, 'tools', (Tool('lookup', 'Find records', '{}'),))
        expected = 'cannot register executable tools'
    else:
        request = replace(request, payload=replace(request.payload, structured=None))
        expected = 'Unexpected structured proposal'
    with pytest.raises(ProviderAccessError, match=expected):
        await async_validate_result(request, result)


def test_proposal_and_response_policy_cannot_share_result_wire():
    from api_agent.providers.rich_inference import result_to_wire
    from test_response_policy_contract import evidence, request
    result = replace(proposal(), response=evidence(request()))
    with pytest.raises(ContractError, match='response policy'):
        result_to_wire(result)


@pytest.mark.asyncio
async def test_unbound_proposal_rejection_preserves_original_error():
    from api_agent.providers.contracts import ProviderAccessError
    from api_agent.providers.llm import LLMResult, ToolCall
    from api_agent.workflows.inference_history import InferenceHistory
    response = proposal()
    first = response.payload.calls[0]
    duplicate = ToolCall(first.call_id, first.name, first.arguments)
    # Simulate a provider result corrupted after the typed constructor check.
    object.__setattr__(response.payload, "calls", (first, duplicate))
    async def callback(_request):
        return response
    history = InferenceHistory()
    try:
        with pytest.raises(ProviderAccessError, match='repeats a tool call identity'):
            await history.call('node', 'llm', callback, original())
    finally:
        history.close()


@pytest.mark.asyncio
async def test_replay_negotiates_smaller_output_cap_without_changing_evidence():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import AvailableSelection, LLMResult, StructuredProposal, ToolCall, Usage
    from api_agent.providers.output_limits import OutputLimitRejected, with_output_limit
    from api_agent.providers.shared_setup import SharedSetupOwner
    from test_structured_replay_authority import PROFILE
    request = with_output_limit(original(), 100)
    selection = AvailableSelection('selected', features=('structured_function', 'tools', 'max_output_tokens'),
                                   structured_function_profile=PROFILE)
    limits = []
    async def authenticate(callback):
        return await callback(auth='synthetic')
    async def complete(effective, *, auth):
        limits.append(effective.payload.generation.max_output_tokens)
        if len(limits) == 2:
            raise OutputLimitRejected(40)
        call = ToolCall('wire-id' if len(limits) == 1 else 'repair-id', 'InvoiceResponse', '{}')
        return LLMResult(Message('assistant', calls=(call,)), Usage(1, 1), 'tool_calls',
                         structured_proposal=StructuredProposal(call))
    owner = SharedSetupOwner('operation', 'selected',
        lambda credentials: SimpleNamespace(call=authenticate, aclose=AsyncMock()),
        complete=complete, describe=AsyncMock(return_value=(selection,)))
    try:
        first = await owner.complete(request)
        second = await owner.complete(replay(request, first, first.structured_proposal.receipt))
        assert second.structured_proposal.call.call_id == 'repair-id'
        assert limits == [100, 100, 40]
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_worker_proof_accepts_explicit_active_owner_permit():
    from api_agent.providers.inference import async_request_to_wire
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    permit = authority.consume(prepared)
    worker = StructuredWorkerAdmission(b'x' * 32)
    with bind_verified_replay(permit):
        row = await async_request_to_wire(prepared)
        proof = worker.proof(row, {'model': 'selected'}, 'completions', permit=permit)
        assert proof['index'] == len(request.payload.messages)


def test_replay_operations_require_verified_owner_scope():
    from api_agent.providers.structured_proposals import admit_effective_replay
    authority = StructuredReplayAuthority()
    with pytest.raises(ContractError, match='ceiling'):
        authority.set_effective_ceiling(100)
    with pytest.raises(ContractError, match='permit'):
        admit_effective_replay(original())


@pytest.mark.asyncio
async def test_replay_effective_request_accepts_identical_wire():
    from api_agent.providers.structured_proposals import admit_effective_replay
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    permit = authority.consume(prepared)
    with bind_verified_replay(permit):
        assert admit_effective_replay(prepared) is None


@pytest.mark.asyncio
async def test_effective_proposal_requires_receipt_when_replay_scope_active():
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.structured_proposals import _new_admission_authority, admit_effective_structured_request
    from test_structured_replay_authority import PROFILE
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    permit = authority.consume(prepared)
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function',), structured_function_profile=PROFILE)
    with bind_verified_replay(permit):
        with _new_admission_authority(owner).bind(owner, request, selection):
            with pytest.raises(ContractError, match='authority missing'):
                admit_effective_structured_request(request)


@pytest.mark.asyncio
async def test_effective_proposal_rejects_unverified_receipt():
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.structured_proposals import _new_admission_authority, admit_effective_structured_request
    from test_structured_replay_authority import PROFILE
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    without_receipt = replace(prepared, proposal_replay=None)
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function', 'tools'), structured_function_profile=PROFILE)
    with _new_admission_authority(owner).bind(owner, without_receipt, selection):
        with pytest.raises(ContractError, match='permit'):
            admit_effective_structured_request(prepared)


@pytest.mark.parametrize('action', ['current', 'effective', 'worker'])
def test_proposal_scope_revocation_between_active_check_and_use(action):
    import contextvars
    import threading
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.structured_proposals import (
        _ADMITTED_PROPOSAL_REQUEST, _new_admission_authority, admit_effective_structured_request,
        current_structured_request, issue_worker_control,
    )
    from test_structured_replay_authority import PROFILE
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function',), structured_function_profile=PROFILE)
    checked, resume = threading.Event(), threading.Event()
    errors = []
    class PausingLock:
        def __init__(self):
            self.raw = threading.RLock()
            self.child = None
            self.paused = False
        def __enter__(self):
            self.raw.acquire()
            return self
        def __exit__(self, *_):
            self.raw.release()
            if threading.get_ident() == self.child and not self.paused:
                self.paused = True
                checked.set()
                assert resume.wait(5)
    lock = PausingLock()
    def run():
        lock.child = threading.get_ident()
        try:
            if action == 'current':
                current_structured_request()
            elif action == 'effective':
                admit_effective_structured_request(original())
            else:
                issue_worker_control({'model': 'selected'}, 'completions')
        except BaseException as error:
            errors.append(error)
    with _new_admission_authority(owner).bind(owner, original(), selection):
        _ADMITTED_PROPOSAL_REQUEST.get()['lock'] = lock
        context = contextvars.copy_context()
        thread = threading.Thread(target=lambda: context.run(run))
        thread.start()
        assert checked.wait(5)
    resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ContractError)
    assert 'admission expired' in str(errors[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['ceiling', 'effective'])
async def test_replay_revocation_between_active_check_and_use(action):
    import contextvars
    import threading
    from api_agent.providers.structured_proposals import admit_effective_replay
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    permit = authority.consume(prepared)
    checked, resume = threading.Event(), threading.Event()
    errors = []
    class PausingLock:
        def __init__(self):
            self.raw = threading.Lock()
            self.child = None
            self.paused = False
        def __enter__(self):
            self.raw.acquire()
            return self
        def __exit__(self, *_):
            self.raw.release()
            if threading.get_ident() == self.child and not self.paused:
                self.paused = True
                checked.set()
                assert resume.wait(5)
    lock = PausingLock()
    object.__setattr__(permit, '_scope_lock', lock)
    def run():
        lock.child = threading.get_ident()
        try:
            if action == 'ceiling':
                authority.set_effective_ceiling(100)
            else:
                admit_effective_replay(prepared)
        except BaseException as error:
            errors.append(error)
    with bind_verified_replay(permit):
        context = contextvars.copy_context()
        thread = threading.Thread(target=lambda: context.run(run))
        thread.start()
        assert checked.wait(5)
    resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ContractError)
    assert 'permit' in str(errors[0])


@pytest.mark.asyncio
async def test_owner_rejects_a_validator_returning_a_different_result(monkeypatch):
    import api_agent.providers.rich_inference as rich
    async def wrong_result(request, result, *, selection=None):
        return proposal()
    monkeypatch.setattr(rich, 'async_validate_result', wrong_result)
    authority = StructuredReplayAuthority()
    with pytest.raises(ContractError, match='origin'):
        await authority.issue(original(), proposal())
    assert not authority._entries


@pytest.mark.asyncio
async def test_history_preflight_detects_changed_configuration(monkeypatch):
    import api_agent.providers.cache_policy as policy
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    receipt = await authority.issue(request, result)
    prepared = replay(request, result, receipt)
    configure = policy.configured_request
    def changed(request, *args, **kwargs):
        value = configure(request, *args, **kwargs)
        return replace(value, selected_llm='changed')
    monkeypatch.setattr(policy, 'configured_request', changed)
    with pytest.raises(ContractError, match='effective origin mismatch'):
        authority.prepare_replay(prepared)
    # Preflight failure does not consume its owner-issued receipt.
    authority.consume(prepared)


@pytest.mark.asyncio
async def test_worker_control_refuses_corrupted_oversized_frame_and_erases_secret(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.structured_proposals import _new_admission_authority, issue_worker_control
    from test_structured_replay_authority import PROFILE
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function',), structured_function_profile=PROFILE)
    send = AsyncMock()
    monkeypatch.setattr(asyncio.get_running_loop(), 'sock_sendall', send)
    with _new_admission_authority(owner).bind(owner, original(), selection):
        control = issue_worker_control({'model': 'selected'}, 'completions')
        # Corrupt a genuine control after issuance to exercise the final IPC bound.
        control._row['padding'] = 'x' * (8 * 1024 * 1024)
        with pytest.raises(ContractError, match='exceeds its bound'):
            await control.send_private(object())
        assert not any(control._secret)
        assert control._row is control._proof is control._controls is None
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_scope_revokes_between_proposal_check_and_use():
    import contextvars
    import threading
    from api_agent.providers.llm import AvailableSelection
    from api_agent.providers.structured_proposals import _ADMITTED_PROPOSAL_REQUEST, _new_admission_authority, admit_effective_replay
    from test_structured_replay_authority import PROFILE
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    permit = authority.consume(prepared)
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function', 'tools'), structured_function_profile=PROFILE)
    checked, resume = threading.Event(), threading.Event()
    errors = []
    class PausingLock:
        def __init__(self):
            self.raw = threading.RLock()
            self.child = None
            self.paused = False
        def __enter__(self):
            self.raw.acquire()
            return self
        def __exit__(self, *_):
            self.raw.release()
            if threading.get_ident() == self.child and not self.paused:
                self.paused = True
                checked.set()
                assert resume.wait(5)
    lock = PausingLock()
    def run():
        lock.child = threading.get_ident()
        try:
            admit_effective_replay(prepared)
        except BaseException as error:
            errors.append(error)
    with bind_verified_replay(permit):
        with _new_admission_authority(owner).bind(owner, prepared, selection):
            _ADMITTED_PROPOSAL_REQUEST.get()['lock'] = lock
            context = contextvars.copy_context()
            thread = threading.Thread(target=lambda: context.run(run))
            thread.start()
            assert checked.wait(5)
        resume.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ContractError)
    assert 'admission expired' in str(errors[0])


@pytest.mark.asyncio
async def test_worker_rejects_replay_context_appearing_between_checks(monkeypatch):
    import api_agent.providers.structured_proposals as structured
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    permit = authority.consume(prepared)
    worker = StructuredWorkerAdmission(b'x' * 32)
    current = structured._current_verified_replay
    checks = []
    def changing_context():
        checks.append(True)
        return None if len(checks) == 1 else current()
    with bind_verified_replay(permit):
        row = request_to_wire(prepared)
        monkeypatch.setattr(structured, '_current_verified_replay', changing_context)
        with pytest.raises(ContractError, match='worker admission'):
            worker.proof(row, {'model': 'selected'}, 'completions')
    assert len(checks) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['calls', 'encoding'])
async def test_worker_rechecks_authenticated_row_after_concurrent_change(monkeypatch, change):
    import hmac
    import threading
    from api_agent.providers.llm import AvailableSelection
    from test_structured_replay_authority import PROFILE
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    issuer = StructuredWorkerAdmission(b'x' * 32)
    worker = StructuredWorkerAdmission(b'x' * 32)
    body = {'model': 'selected'}
    if change == 'calls':
        prepared = replay(request, result, await authority.issue(request, result))
        permit = authority.consume(prepared)
        with bind_verified_replay(permit):
            row = request_to_wire(prepared)
            proof = issuer.proof(row, body, 'completions')
    else:
        row = request_to_wire(request)
        proof = issuer.proof(row, body, 'completions')
    selection = AvailableSelection('selected', features=('structured_function', 'tools', 'structured'), structured_function_profile=PROFILE)
    verified, resume = threading.Event(), threading.Event()
    compare = hmac.compare_digest
    count = [0]
    errors = []
    def pause_after_verification(left, right):
        verdict = compare(left, right)
        count[0] += 1
        if count[0] == 4:
            verified.set()
            assert resume.wait(5)
        return verdict
    monkeypatch.setattr(hmac, 'compare_digest', pause_after_verification)
    def run():
        try:
            with worker.admit(row, body, 'completions', proof, selection=selection):
                errors.append(AssertionError('changed row was admitted'))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    assert verified.wait(5)
    if change == 'calls':
        row['messages'][proof['index']]['calls'] = []
        row['messages'][proof['index']]['content'] = ['changed']
    else:
        row['version'] = 2
        row['structured'].pop('encoding')
        row['structured'].pop('provider_tool_schema')
        row['structured'].pop('function_profile')
    resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ContractError)
    assert 'worker admission' in str(errors[0])


@pytest.mark.asyncio
async def test_wire_rejects_replay_target_changed_after_digest_check(monkeypatch):
    import contextvars
    import threading
    import api_agent.providers.rich_inference as rich
    authority = StructuredReplayAuthority()
    request, result = original(), proposal()
    prepared = replay(request, result, await authority.issue(request, result))
    permit = authority.consume(prepared)
    checked, resume = threading.Event(), threading.Event()
    exact = rich._exact
    errors = []
    def pause_first(value, keys):
        if not checked.is_set():
            checked.set()
            assert resume.wait(5)
        return exact(value, keys)
    with bind_verified_replay(permit):
        row = request_to_wire(prepared)
        monkeypatch.setattr(rich, '_exact', pause_first)
        def run():
            try:
                rich.request_from_wire(row)
            except BaseException as error:
                errors.append(error)
        context = contextvars.copy_context()
        thread = threading.Thread(target=lambda: context.run(run))
        thread.start()
        assert checked.wait(5)
        row['structured']['name'] = 'OtherResponse'
        resume.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ContractError)
    assert 'rich inference request' in str(errors[0])
