"""Replay is an owner-bound one-use capability, never a general tool bypass."""

import json
from dataclasses import replace

import pytest

from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import (AvailableSelection, CompletionRequest, LLMRequest, LLMResult, Message,
    StructuredOutput, StructuredFunctionProfile, StructuredProposal, TextContent, ToolCall, ToolResult, Usage)
from api_agent.providers.structured_proposals import StructuredReplayAuthority

PROFILE = StructuredFunctionProfile(None, 'converter')
SCHEMA = json.dumps({'type': 'object', 'properties': {'reference': {'type': 'string'}}, 'required': ['reference']})


def original():
    return LLMRequest('selected', CompletionRequest((Message('user', (TextContent('Read invoices.'),)),),
        structured=StructuredOutput('InvoiceResponse', SCHEMA, strict=False, encoding='function_calling', provider_tool_schema=SCHEMA, function_profile=PROFILE)), 'initial')


def proposal():
    call = ToolCall('wire-id', 'InvoiceResponse', '{}')
    return LLMResult(Message('assistant', calls=(call,)), Usage(4, 2), 'tool_calls',
        structured_proposal=StructuredProposal(call))


def replay(request, response, receipt):
    feedback = json.dumps({'error': 'structured_validation_failed',
        'issues': [{'field_path': 'reference', 'constraint': 'missing', 'message': 'Field required'}], 'truncated': False})
    tool_result = Message('tool', results=(ToolResult('wire-id', (TextContent(feedback),)),))
    return replace(request, request_id='repair', proposal_replay=receipt,
        payload=replace(request.payload, messages=(*request.payload.messages, response.payload, tool_result)))


@pytest.mark.asyncio
async def test_receipt_authorizes_exact_replay_once_and_is_not_represented():
    authority = StructuredReplayAuthority()
    initial, response = original(), proposal()
    receipt = await authority.issue(initial, response)
    assert 'wire-id' not in repr(receipt) and '{}' not in repr(receipt)
    prepared = replay(initial, response, receipt)
    authority.consume(prepared)
    with pytest.raises(ContractError):
        authority.consume(prepared)


@pytest.mark.parametrize('change', ['owner', 'model', 'schema', 'prefix', 'call_id', 'arguments', 'feedback', 'continuation'])
@pytest.mark.asyncio
async def test_changed_scope_or_proposal_cannot_consume_receipt(change):
    authority, other = StructuredReplayAuthority(), StructuredReplayAuthority()
    initial, response = original(), proposal()
    receipt = await authority.issue(initial, response)
    prepared = replay(initial, response, receipt)
    if change == 'owner':
        target = other
    else:
        target = authority
    if change == 'model':
        prepared = replace(prepared, selected_llm='other')
    elif change == 'schema':
        definition = replace(prepared.payload.structured, name='OtherResponse')
        prepared = replace(prepared, payload=replace(prepared.payload, structured=definition))
    elif change == 'prefix':
        messages = (Message('user', (TextContent('Other evidence.'),)), *prepared.payload.messages[1:])
        prepared = replace(prepared, payload=replace(prepared.payload, messages=messages))
    elif change in ('call_id', 'arguments', 'continuation'):
        old = prepared.payload.messages[-2]
        call = old.calls[0]
        if change == 'call_id':
            call = replace(call, call_id='forged')
        if change == 'arguments':
            call = replace(call, arguments='{"reference":"forged"}')
        altered = replace(old, calls=(call,), continuation='opaque' if change == 'continuation' else None)
        prepared = replace(prepared, payload=replace(prepared.payload, messages=(*prepared.payload.messages[:-2], altered, prepared.payload.messages[-1])))
    elif change == 'feedback':
        tool = Message('tool', results=(ToolResult('wire-id', (TextContent('{"not":"validation"}'),)),))
        prepared = replace(prepared, payload=replace(prepared.payload, messages=(*prepared.payload.messages[:-1], tool)))
    with pytest.raises(ContractError):
        target.consume(prepared)


@pytest.mark.asyncio
async def test_strict_wire_requires_exact_owner_permit_and_worker_proof():
    from api_agent.providers.inference import async_request_to_wire, request_from_wire, request_to_wire
    from api_agent.providers.structured_proposals import (StructuredWorkerAdmission, bind_verified_replay,
        current_structured_request, is_structured_proposal)

    authority = StructuredReplayAuthority()
    initial, response = original(), proposal()
    receipt = await authority.issue(initial, response)
    replay_request = replay(initial, response, receipt)
    with pytest.raises(ContractError):
        request_to_wire(replay_request, _validate_instances=False)
    permit = authority.consume(replay_request)
    secret = b'x' * 32
    issuer, worker = StructuredWorkerAdmission(secret), StructuredWorkerAdmission(secret)
    provider_body = {'model': 'selected', 'tool_choice': {'name': 'InvoiceResponse'}}
    with bind_verified_replay(permit):
        row = await async_request_to_wire(replay_request)
        assert 'proposal_replay' not in json.dumps(row)
        proof = issuer.proof(row, provider_body, 'chat/completions')
    with pytest.raises(ContractError):
        request_from_wire(row, _validate_instances=False)
    with worker.admit(row, provider_body, 'chat/completions', proof, selection=AvailableSelection('selected', features=('structured', 'structured_function', 'tools'), structured_function_profile=PROFILE)) as decoded:
        assert is_structured_proposal() and current_structured_request() == decoded
        assert decoded.proposal_replay is None
        assert decoded.payload.messages[-2].calls[0].arguments == '{}'
    assert not is_structured_proposal() and current_structured_request() is None
    with pytest.raises(ContractError):
        with worker.admit(row, provider_body, 'chat/completions', proof, selection=AvailableSelection('selected', features=('structured', 'structured_function', 'tools'), structured_function_profile=PROFILE)):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', ['row', 'body', 'operation', 'mac', 'secret', 'controls'])
async def test_worker_proof_binds_request_body_operation_and_worker_key(tamper):
    from api_agent.providers.inference import request_to_wire
    from api_agent.providers.structured_proposals import StructuredWorkerAdmission

    row = request_to_wire(original())
    issuer = StructuredWorkerAdmission(b'x' * 32)
    worker = StructuredWorkerAdmission((b'y' if tamper == 'secret' else b'x') * 32)
    body = {'model': 'selected', 'tool_choice': {'name': 'InvoiceResponse'}}
    controls = {'LLM_REASONING_LOW_TOKENS': 1024}
    proof = issuer.proof(row, body, 'chat/completions', provider_controls=controls)
    if tamper == 'row':
        row['selected_llm'] = 'other'
    elif tamper == 'body':
        body['tool_choice']['name'] = 'OtherResponse'
    elif tamper == 'mac':
        proof['mac'] = '0' * 64
    elif tamper == 'controls':
        controls['LLM_REASONING_LOW_TOKENS'] = 2048
    operation = 'other' if tamper == 'operation' else 'chat/completions'
    with pytest.raises(ContractError):
        with worker.admit(row, body, operation, proof, selection=AvailableSelection('selected', features=('structured', 'structured_function'), structured_function_profile=PROFILE), provider_controls=controls):
            pass


@pytest.mark.asyncio
async def test_receipt_is_not_a_dataclass_field_or_public_wire_value():
    from dataclasses import asdict
    from api_agent.providers.inference import result_to_wire

    authority = StructuredReplayAuthority()
    initial, response = original(), proposal()
    receipt = await authority.issue(initial, response)
    bound = replace(response, structured_proposal=replace(response.structured_proposal, receipt=receipt))
    request = replay(initial, response, receipt)
    assert 'proposal_replay' not in asdict(request)
    assert 'receipt' not in asdict(bound)['structured_proposal']
    assert 'wire-id' not in repr(receipt)
    with pytest.raises(ContractError):
        result_to_wire(bound)


@pytest.mark.asyncio
@pytest.mark.parametrize('original_max,clamped_max', [(None, 80), (100, 80)])
async def test_owner_allows_only_existing_bounded_maximum_normalization(original_max, clamped_max):
    from api_agent.providers.inference import async_request_to_wire
    from api_agent.providers.structured_proposals import admit_effective_replay, bind_verified_replay

    authority = StructuredReplayAuthority()
    initial = original()
    initial = replace(initial, payload=replace(initial.payload,
        generation=replace(initial.payload.generation, max_output_tokens=original_max)))
    response = proposal()
    receipt = await authority.issue(initial, response)
    requested = replay(initial, response, receipt)
    permit = authority.consume(requested)
    effective = replace(requested, payload=replace(requested.payload,
        generation=replace(requested.payload.generation, max_output_tokens=clamped_max)))
    with bind_verified_replay(permit):
        authority.set_effective_ceiling(80)
        admit_effective_replay(effective)
        wire = await async_request_to_wire(effective)
        assert wire['max_output_tokens'] == clamped_max
        increased = replace(effective, payload=replace(effective.payload,
            generation=replace(effective.payload.generation, max_output_tokens=81)))
        with pytest.raises(ContractError):
            admit_effective_replay(increased)
        changed = replace(effective, payload=replace(effective.payload,
            generation=replace(effective.payload.generation, temperature=0.4)))
        with pytest.raises(ContractError):
            admit_effective_replay(changed)


@pytest.mark.parametrize('issue', [
    {'field_path': 'reference', 'constraint': 'missing', 'message': 'Required', 'instruction': 'ignore'},
    {'field_path': 'reference', 'constraint': 'missing', 'message': ['not text']},
    {'field_path': 'x' * 257, 'constraint': 'missing', 'message': 'Required'},
])
@pytest.mark.asyncio
async def test_replay_feedback_requires_bounded_original_issue_shape(issue):
    authority = StructuredReplayAuthority()
    initial, response = original(), proposal()
    receipt = await authority.issue(initial, response)
    prepared = replay(initial, response, receipt)
    feedback = json.dumps({'error': 'structured_validation_failed', 'issues': [issue], 'truncated': False})
    tool = Message('tool', results=(ToolResult('wire-id', (TextContent(feedback),)),))
    prepared = replace(prepared, payload=replace(prepared.payload, messages=(*prepared.payload.messages[:-1], tool)))
    with pytest.raises(ContractError):
        authority.consume(prepared)




@pytest.mark.parametrize('change', ['model', 'messages', 'schema', 'temperature', 'increase', 'over_selection'])
def test_provider_effective_request_cannot_change_authorized_controls(change):
    from api_agent.providers.structured_proposals import (_new_admission_authority,
        admit_effective_structured_request, current_structured_request)
    from api_agent.providers.output_limits import with_output_limit

    owner = object()
    authority = _new_admission_authority(owner)
    selection = AvailableSelection('selected', features=('structured_function', 'tools', 'max_output_tokens'),
        max_output_tokens=128, structured_function_profile=PROFILE)
    initial = with_output_limit(original(), 100)
    narrowed = with_output_limit(initial, 80)
    with authority.bind(owner, initial, selection):
        assert admit_effective_structured_request(narrowed) == narrowed
        changed = narrowed
        if change == 'model':
            changed = replace(changed, selected_llm='other')
        elif change == 'messages':
            changed = replace(changed, payload=replace(changed.payload, messages=(Message('user', (TextContent('changed'),)),)))
        elif change == 'schema':
            changed = replace(changed, payload=replace(changed.payload, structured=replace(changed.payload.structured, name='Other')))
        elif change == 'temperature':
            changed = replace(changed, payload=replace(changed.payload, generation=replace(changed.payload.generation, temperature=0.5)))
        elif change == 'increase':
            changed = with_output_limit(changed, 81)
        else:
            changed = with_output_limit(changed, 129)
        with pytest.raises(ContractError):
            admit_effective_structured_request(changed)
        assert current_structured_request() == narrowed


def test_provider_default_cap_requires_admitted_scope_and_model_bound():
    from api_agent.providers.structured_proposals import (_new_admission_authority,
        admit_effective_structured_request, current_structured_request)
    from api_agent.providers.output_limits import with_output_limit
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function', 'max_output_tokens'),
        max_output_tokens=128, structured_function_profile=PROFILE)
    initial = original()
    with pytest.raises(ContractError):
        admit_effective_structured_request(with_output_limit(initial, 64))
    with _new_admission_authority(owner).bind(owner, initial, selection):
        with pytest.raises(ContractError):
            admit_effective_structured_request(with_output_limit(initial, 129))
        assert current_structured_request() == initial
        assert admit_effective_structured_request(with_output_limit(initial, 64)).payload.generation.max_output_tokens == 64


@pytest.mark.asyncio
async def test_worker_control_transfers_only_once_on_private_socket():
    import asyncio
    import socket
    from api_agent.providers.structured_proposals import (_new_admission_authority,
        issue_worker_control, StructuredWorkerAdmission)

    owner = object()
    selection = AvailableSelection('selected', features=('structured_function',),
                                   structured_function_profile=PROFILE)
    request = original()
    body = {'model': 'selected', 'tool_choice': {'name': 'InvoiceResponse'}}
    parent, child = socket.socketpair()
    parent.setblocking(False)
    child.setblocking(False)
    try:
        with _new_admission_authority(owner).bind(owner, request, selection):
            control = issue_worker_control(body, 'chat/completions')
            assert 'wire-id' not in repr(control)
            await control.send_private(parent)
            with pytest.raises(ContractError):
                await control.send_private(parent)
        incoming = bytearray()
        while not incoming.endswith(b'\n'):
            chunk = await asyncio.get_running_loop().sock_recv(child, 65536)
            assert chunk
            incoming.extend(chunk)
        frame = json.loads(incoming)
        worker = StructuredWorkerAdmission(bytes.fromhex(frame['key']))
        try:
            with worker.admit(frame['row'], body, 'chat/completions', frame['proof'], selection=selection, provider_controls=frame['controls']) as admitted:
                assert admitted == request
        finally:
            worker.close()
    finally:
        parent.close()
        child.close()


@pytest.mark.asyncio
async def test_provider_effective_replay_preserves_exact_authorized_call():
    from api_agent.providers.inference import request_to_wire
    from api_agent.providers.output_limits import with_output_limit
    from api_agent.providers.structured_proposals import (_new_admission_authority,
        admit_effective_structured_request, bind_verified_replay, current_structured_request)
    authority = StructuredReplayAuthority()
    initial, response = original(), proposal()
    receipt = await authority.issue(initial, response)
    prepared = replay(initial, response, receipt)
    permit = authority.consume(prepared)
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function', 'tools', 'max_output_tokens'),
                                   max_output_tokens=128, structured_function_profile=PROFILE)
    with bind_verified_replay(permit):
        authority.set_effective_ceiling(100)
        with _new_admission_authority(owner).bind(owner, prepared, selection):
            effective = with_output_limit(prepared, 80)
            admit_effective_structured_request(effective)
            wire = request_to_wire(effective)
            assert wire['max_output_tokens'] == 80
            assert wire['messages'][-2]['calls'][0]['arguments'] == '{}'
            assert current_structured_request().proposal_replay is None
            with pytest.raises(ContractError):
                admit_effective_structured_request(with_output_limit(effective, 81))


@pytest.mark.asyncio
async def test_unsent_worker_control_is_revoked_with_admission_scope():
    import socket
    from api_agent.providers.structured_proposals import _new_admission_authority, issue_worker_control
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function',),
                                   structured_function_profile=PROFILE)
    with _new_admission_authority(owner).bind(owner, original(), selection):
        control = issue_worker_control({'model': 'selected'}, 'chat/completions')
    parent, child = socket.socketpair()
    parent.setblocking(False)
    try:
        with pytest.raises(ContractError):
            await control.send_private(parent)
    finally:
        parent.close()
        child.close()


@pytest.mark.parametrize('bad', ['other', 'tagged\n', 1, None])
def test_worker_reasoning_format_is_a_fixed_enum(bad):
    from api_agent.providers.inference import request_to_wire
    from api_agent.providers.structured_proposals import StructuredWorkerAdmission
    issuer = StructuredWorkerAdmission(b'x' * 32)
    with pytest.raises(ContractError):
        issuer.proof(request_to_wire(original()), {'model': 'selected'}, 'completions',
                     provider_controls={'reasoning_format': bad})


@pytest.mark.asyncio
async def test_child_task_cannot_reuse_expired_proposal_admission():
    import asyncio
    from api_agent.providers.structured_proposals import (_new_admission_authority,
        current_structured_request, issue_worker_control, is_structured_proposal)
    owner = object()
    selection = AvailableSelection('selected', features=('structured_function',),
                                   structured_function_profile=PROFILE)
    gate = asyncio.Event()
    async def child():
        await gate.wait()
        for action in (is_structured_proposal, current_structured_request,
                       lambda: issue_worker_control({'model': 'selected'}, 'completions')):
            with pytest.raises(ContractError):
                action()
    with _new_admission_authority(owner).bind(owner, original(), selection):
        task = asyncio.create_task(child())
    gate.set()
    await task


@pytest.mark.asyncio
async def test_child_task_cannot_reuse_expired_replay_permit():
    import asyncio
    from api_agent.providers.inference import request_to_wire
    from api_agent.providers.structured_proposals import bind_verified_replay, current_verified_replay
    authority = StructuredReplayAuthority()
    initial, response = original(), proposal()
    receipt = await authority.issue(initial, response)
    prepared = replay(initial, response, receipt)
    permit = authority.consume(prepared)
    gate = asyncio.Event()
    async def child():
        await gate.wait()
        with pytest.raises(ContractError):
            current_verified_replay()
        with pytest.raises(ContractError):
            request_to_wire(prepared, _validate_instances=False)
    with bind_verified_replay(permit):
        task = asyncio.create_task(child())
    gate.set()
    await task






@pytest.mark.asyncio
async def test_threaded_issuance_is_revoked_when_admitted_scope_closes(monkeypatch):
    import contextvars
    import secrets
    import socket
    import threading
    from api_agent.providers.structured_proposals import (
        _ADMITTED_PROPOSAL_REQUEST, _new_admission_authority, issue_worker_control,
    )

    owner = object()
    selection = AvailableSelection('selected', features=('structured_function',),
                                   structured_function_profile=PROFILE)
    started, attempted, probed, revoked, release = (threading.Event() for _ in range(5))
    original_bytes = secrets.token_bytes
    parent_ident = threading.get_ident()
    controls, errors, probe = [], [], []
    scope = None

    class ObservedLock:
        def __init__(self):
            self.raw = threading.RLock()
            self.observed = False

        def __enter__(self):
            # This is the actual parent cleanup lock acquisition, not an
            # event asserted merely before leaving the with statement.
            if threading.get_ident() == parent_ident and started.is_set() and not self.observed:
                self.observed = True
                attempted.set()
                assert probed.wait(5)
            self.raw.acquire()
            return self

        def __exit__(self, *exc):
            self.raw.release()
            if threading.get_ident() == parent_ident and not scope['active']:
                revoked.set()

    def delayed_bytes(count):
        started.set()
        assert release.wait(5)
        return original_bytes(count)

    def coordinate():
        try:
            assert attempted.wait(5)
            # Parent has reached the exact cleanup lock boundary and is held
            # until this probe completes. In the protected case the issuing
            # thread owns the lock; a lock-disabled mutation leaves it free.
            unlocked = observed_lock.raw.acquire(blocking=False)
            if unlocked:
                observed_lock.raw.release()
            probe.append(unlocked)
            probed.set()
            if unlocked:
                assert revoked.wait(5)
            release.set()
        except BaseException as error:
            errors.append(error)
            probed.set()
            release.set()

    def issue():
        try:
            controls.append(issue_worker_control({'model': 'selected'}, 'completions'))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(secrets, 'token_bytes', delayed_bytes)
    with _new_admission_authority(owner).bind(owner, original(), selection):
        scope = _ADMITTED_PROPOSAL_REQUEST.get()
        observed_lock = ObservedLock()
        scope['lock'] = observed_lock
        context = contextvars.copy_context()
        thread = threading.Thread(target=lambda: context.run(issue))
        thread.start()
        assert started.wait(5)
        coordinator = threading.Thread(target=coordinate)
        coordinator.start()
    thread.join(5)
    coordinator.join(5)
    assert not thread.is_alive() and not coordinator.is_alive() and not errors
    assert attempted.is_set() and revoked.is_set() and probe == [False]
    assert len(controls) == 1
    parent, child = socket.socketpair()
    parent.setblocking(False)
    try:
        with pytest.raises(ContractError):
            await controls[0].send_private(parent)
    finally:
        parent.close()
        child.close()


def test_effective_controls_preserve_json_numeric_types():
    from api_agent.providers.structured_proposals import _new_admission_authority, admit_effective_structured_request
    owner = object()
    initial = original()
    initial = replace(initial, payload=replace(initial.payload,
        generation=replace(initial.payload.generation, temperature=1)))
    changed = replace(initial, payload=replace(initial.payload,
        generation=replace(initial.payload.generation, temperature=1.0)))
    selection = AvailableSelection('selected', features=('structured_function', 'temperature'),
                                   structured_function_profile=PROFILE)
    with _new_admission_authority(owner).bind(owner, initial, selection):
        with pytest.raises(ContractError):
            admit_effective_structured_request(changed)


@pytest.mark.asyncio
async def test_releasing_successful_proposal_receipts_prevents_capacity_exhaustion():
    authority = StructuredReplayAuthority()
    for _ in range(70):
        bound = await authority.bind(original(), proposal())
        receipt = bound.structured_proposal.receipt
        assert receipt is not None
        receipt.release()
        receipt.release()
        with pytest.raises(ContractError):
            authority.consume(replay(original(), proposal(), receipt))


@pytest.mark.asyncio
async def test_receipt_release_does_not_retire_other_proposal():
    authority = StructuredReplayAuthority()
    first = await authority.issue(original(), proposal())
    second = await authority.issue(original(), proposal())
    first.release()
    authority.consume(replay(original(), proposal(), second))
    with pytest.raises(ContractError):
        authority.consume(replay(original(), proposal(), first))


@pytest.mark.asyncio
@pytest.mark.parametrize('allow_content', [False, True])
async def test_owner_captures_schema_mismatched_proposal_as_completed(allow_content):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    from api_agent.workflows.inference_capture import collect_inference
    call = ToolCall('native-call-id', 'InvoiceResponse', '{"reference":3,"note":"actual-proposal"}')
    response = LLMResult(Message('assistant', calls=(call,)), Usage(4, 2), 'tool_calls',
                         structured_proposal=StructuredProposal(call))
    selection = AvailableSelection('selected', features=('structured_function', 'tools'),
                                   structured_function_profile=PROFILE)
    async def authenticate(callback):
        return await callback(auth='private-auth-token')
    async def provider(request, *, auth):
        return response
    owner = SharedSetupOwner('operation', 'selected',
        lambda credentials: SimpleNamespace(call=authenticate, aclose=AsyncMock()),
        complete=provider, describe=AsyncMock(return_value=(selection,)))
    try:
        with collect_inference(EmissionBoundary(ContentPolicy(allow_content))) as collector:
            admitted = await owner.complete(original())
            assert admitted.structured_proposal.receipt is not None
            admitted.structured_proposal.receipt.release()
        captured = json.dumps(collector['rows'])
        assert ('Read invoices.' in captured) is allow_content
        assert ('actual-proposal' in captured) is allow_content
        assert 'private-auth-token' not in captured
        assert 'StructuredProposalReceipt' not in captured
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_borrowed_history_keeps_proposals_stateless_and_explicit_repair_authorized():
    from api_agent.providers.structured_proposals import bind_verified_replay
    from api_agent.workflows.inference_history import InferenceHistory
    authority = StructuredReplayAuthority()
    history = InferenceHistory()
    seen = []
    async def callback(request):
        seen.append(request.payload.messages)
        if request.proposal_replay is not None:
            permit = authority.consume(request)
            with bind_verified_replay(permit):
                call = ToolCall('repair-wire-id', 'InvoiceResponse', '{"reference":"ok"}')
                return LLMResult(Message('assistant', calls=(call,)), Usage(4, 2), 'tool_calls',
                                 structured_proposal=StructuredProposal(call))
        return await authority.bind(request, proposal())
    try:
        for _ in range(70):
            accepted = await history.call('node', 'llm', callback, original())
            accepted.structured_proposal.receipt.release()
        assert len(seen) == 70
        assert all(messages == original().payload.messages for messages in seen)
        first = await history.call('node', 'llm', callback, original())
        repair_request = replay(original(), first, first.structured_proposal.receipt)
        second = await history.call('node', 'llm', callback, repair_request)
        assert seen[-1] == repair_request.payload.messages
        assert second.structured_proposal.call.call_id == 'repair-wire-id'
        assert len(seen) == 72
    finally:
        history.close()
        authority.close()


@pytest.mark.asyncio
async def test_borrowed_history_releases_receipt_when_result_is_rejected():
    from api_agent.workflows.inference_history import InferenceHistory
    authority, history = StructuredReplayAuthority(), InferenceHistory()
    kept = []
    async def callback(request):
        # The result is bound to a different originating request and must not
        # retain authority after history rejects it before handoff.
        bad = await authority.bind(original(), proposal())
        kept.append(bad.structured_proposal.receipt)
        return bad
    changed = replace(original(), request_id='different')
    try:
        with pytest.raises(ContractError):
            await history.call('node', 'llm', callback, changed)
        with pytest.raises(ContractError):
            authority.consume(replay(original(), proposal(), kept[0]))
    finally:
        history.close()
        authority.close()
