"""Physical result metadata observation cannot change semantic validation."""
import asyncio
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, ToolCall
from api_agent.providers import rich_inference as inference


def pair():
    request = LLMRequest('selected', CompletionRequest((Message('user', (TextContent('ask'),)),)))
    result = LLMResult(Message('assistant', (), calls=(ToolCall('call-one', 'undeclared', '{}'),)),
                       outcome='tool_calls', actual_model='actual-model', service_tier='standard',
                       serving_revision='revision1')
    return request, result


def reject(request, result):
    with pytest.raises(ProviderAccessError) as caught:
        inference.validate_result(request, result)
    assert caught.value.code == 'tool_name_undeclared'


@pytest.mark.parametrize('validator', ['sync', 'async', 'response'])
def test_common_validators_observe_bounded_physical_result_without_changing_rejection(validator):
    request, result = pair()
    seen = []
    with inference.bind_bounded_result_observer(lambda request, result, error, feedback, fingerprint, checked_text: seen.append(result)):
        if validator == 'sync':
            reject(request, result)
        elif validator == 'async':
            with pytest.raises(ProviderAccessError) as caught:
                asyncio.run(inference.async_validate_result(request, result))
            assert caught.value.code == 'tool_name_undeclared'
        else:
            from test_response_policy_contract import request as declared_request
            with pytest.raises(ProviderAccessError):
                inference.validate_response_result(declared_request('json'), result)
    assert seen == [result]


def test_observation_admission_refusal_preserves_original_semantic_error():
    request, result = pair()
    # Semantic undeclared-tool rejection precedes final structural admission.
    invalid = replace(result, payload=replace(result.payload,
        content=(TextContent('x' * (2 * 1024 * 1024)),)))
    callback = Mock()
    with inference.bind_bounded_result_observer(callback):
        reject(request, invalid)
    callback.assert_not_called()


def test_callback_failure_does_not_replace_rejection_or_suppress_later_observation():
    request, result = pair()
    callback = Mock(side_effect=[RuntimeError('untrusted optional error'), None])
    with inference.bind_bounded_result_observer(callback):
        reject(request, result)
        reject(request, result)
    assert callback.call_count == 2


def test_nested_observers_restore_outer_owner_and_reject_invalid_binding():
    request, result = pair()
    outer, inner = [], []
    with pytest.raises(TypeError):
        with inference.bind_bounded_result_observer(None):
            pytest.fail('invalid observer entered')
    with inference.bind_bounded_result_observer(lambda request, result, error, feedback, fingerprint, checked_text: outer.append(result)):
        reject(request, result)
        with inference.bind_bounded_result_observer(lambda request, result, error, feedback, fingerprint, checked_text: inner.append(result)):
            reject(request, result)
        reject(request, result)
    reject(request, result)
    assert outer == [result, result] and inner == [result]


def test_inherited_late_task_cannot_publish_after_binding_closes_or_adopt_next_owner():
    async def scenario():
        request, result = pair()
        gate = asyncio.Event()
        stale, current = [], []
        async def child():
            await gate.wait()
            reject(request, result)
        with inference.bind_bounded_result_observer(lambda request, result, error, feedback, fingerprint, checked_text: stale.append(result)):
            task = asyncio.create_task(child())
        with inference.bind_bounded_result_observer(lambda request, result, error, feedback, fingerprint, checked_text: current.append(result)):
            gate.set()
            await task
            reject(request, result)
        assert stale == [] and current == [result]
    asyncio.run(scenario())


def test_inherited_offload_cannot_publish_after_binding_closes():
    async def scenario():
        request, result = pair()
        ready = asyncio.Event()
        gate = threading.Event()
        seen = []
        loop = asyncio.get_running_loop()
        def child():
            loop.call_soon_threadsafe(ready.set)
            assert gate.wait(2)
            reject(request, result)
        with inference.bind_bounded_result_observer(lambda request, result, error, feedback, fingerprint, checked_text: seen.append(result)):
            task = asyncio.create_task(asyncio.to_thread(child))
            await asyncio.wait_for(ready.wait(), 2)
        gate.set()
        await task
        assert seen == []
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['exact', 'configured', 'configured-empty', 'caught-success', 'replacement', 'foreign',
    'ambiguous', 'same-error-different-result', 'duplicate'])
def test_shared_physical_receipt_requires_exact_escaping_error_and_unambiguous_source(mode, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.control import OperationalError
    from api_agent.providers.cache_policy import configured_request
    from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
    from api_agent.workflows import inference_capture
    request, result = pair()
    request = replace(request, request_id='' if mode == 'configured-empty' else 'request-one')
    replacement = LLMResult(Message('assistant', (TextContent('accepted'),)))
    seen = []
    monkeypatch.setattr(inference_capture, 'record_provider_fields',
        lambda cause, token, owned, value, **kwargs: seen.append((token, owned, value)))
    async def call(callback):
        return await callback(auth='synthetic-auth')
    async def complete(owned, *, auth):
        source = (configured_request(owned, 'medium', reasoning_effort='high') if mode in ('configured', 'configured-empty')
                  else replace(owned, request_id='foreign') if mode == 'foreign' else owned)
        try:
            await inference.async_validate_result(source, result)
        except ProviderAccessError as error:
            if mode == 'caught-success':
                return replacement
            if mode == 'replacement':
                raise OperationalError('unavailable')
            if mode == 'ambiguous':
                for revision in ('second', 'third'):
                    try:
                        await inference.async_validate_result(owned, replace(result, serving_revision=revision))
                    except ProviderAccessError as conflicting:
                        last = conflicting
                raise last
            if mode == 'same-error-different-result':
                inference._observe_bounded_result(owned, replace(result, serving_revision='second'), error)
            if mode == 'duplicate':
                inference._observe_bounded_result(owned, result, error)
            raise
    async def scenario():
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        ledger = TransportAttemptLedger(1)
        try:
            with bind_transport_attempt_ledger(ledger):
                if mode == 'caught-success':
                    assert await owner.complete(request) is replacement
                else:
                    with pytest.raises(OperationalError if mode == 'replacement' else ProviderAccessError):
                        await owner.complete(request)
            if mode in ('exact', 'configured', 'configured-empty', 'duplicate'):
                assert len(seen) == 1 and seen[0][2] is result
                assert seen[0][0] == ledger.snapshot()['attempts'][0]['id']
            elif mode == 'caught-success':
                assert len(seen) == 1 and seen[0][2] is replacement
            else:
                assert seen == []
        finally:
            await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['id', 'model', 'messages', 'tools', 'schema', 'limit',
                                    'temperature', 'summary', 'authored-cache', 'authored-reasoning'])
def test_provider_normalization_cannot_change_originating_request_contract(change):
    from api_agent.providers.shared_setup import _observed_request_matches
    from api_agent.providers.llm import Tool, StructuredOutput, GenerationOptions
    request, _ = pair()
    request = replace(request, request_id='request-one')
    source = request
    if change == 'id':
        source = replace(request, request_id='other')
    elif change == 'model':
        source = replace(request, selected_llm='other')
    elif change == 'messages':
        source = replace(request, payload=replace(request.payload,
            messages=(Message('user', (TextContent('other'),)),)))
    elif change == 'tools':
        source = replace(request, payload=replace(request.payload,
            tools=(Tool('other', 'authored', '{"type":"object"}', False),)))
    elif change == 'schema':
        source = replace(request, payload=replace(request.payload,
            structured=StructuredOutput('other', '{"type":"object"}', False)))
    else:
        field = {'limit': 'max_output_tokens', 'temperature': 'temperature', 'summary': 'display_summary',
                 'authored-cache': 'cache_policy', 'authored-reasoning': 'reasoning_effort'}[change]
        values = {'limit': 12, 'temperature': .2, 'summary': True,
                  'authored-cache': 'high', 'authored-reasoning': 'high'}
        if change.startswith('authored'):
            request = replace(request, payload=replace(request.payload,
                generation=replace(request.payload.generation, **{field: 'off'})))
        source = replace(request, payload=replace(request.payload,
            generation=replace(request.payload.generation, **{field: values[change]})))
    assert not _observed_request_matches(source, request)


def test_contradictory_candidate_carrier_cannot_override_observed_result(monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.llm import StructuredOutput
    from api_agent.providers.contracts import CandidateOutputError
    from api_agent.workflows import inference_capture
    request = LLMRequest('selected', CompletionRequest((Message('user', (TextContent('ask'),)),),
        structured=StructuredOutput('answer', json.dumps({'type': 'object',
            'properties': {'answer': {'type': 'string', 'maxLength': 2}},
            'required': ['answer'], 'additionalProperties': False}), False)), 'request-one')
    result = LLMResult(Message('assistant', (TextContent('{"answer":"xxx"}'),)), actual_model='actual-model')
    seen = []
    monkeypatch.setattr(inference_capture, 'record_provider_fields', lambda *args, **kwargs: seen.append(args))
    async def call(callback):
        return await callback(auth='synthetic-auth')
    async def complete(owned, *, auth):
        try:
            await inference.async_validate_result(owned, result)
        except CandidateOutputError as error:
            error.result = replace(result, actual_model='contradictory-model')
            raise
    async def scenario():
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        try:
            with pytest.raises(CandidateOutputError):
                await owner.complete(request)
            assert seen == []
        finally:
            await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', [RuntimeError('optional'), asyncio.CancelledError()])
def test_observer_fault_cannot_replace_actual_semantic_exception(fault):
    request, result = pair()
    callback = Mock(side_effect=fault)
    with inference.bind_bounded_result_observer(callback):
        reject(request, result)
    callback.assert_called_once()


@pytest.mark.parametrize('validator', ['sync', 'async', 'response'])
@pytest.mark.parametrize('available', [False, True])
def test_only_local_definitive_verdict_mints_snapshotted_feedback(validator, available, monkeypatch):
    import json
    from api_agent.providers.llm import StructuredOutput
    from api_agent.providers.contracts import CandidateOutputError
    from test_response_policy_contract import request as policy_request, result as policy_result
    request = policy_request('json')
    shape = json.dumps({'type': 'object', 'properties': {'answer': {'type': 'string', 'maxLength': 2}},
                        'required': ['answer'], 'additionalProperties': False})
    request = replace(request, payload=replace(request.payload, response=replace(
        request.payload.response, schema=StructuredOutput('answer', shape, False))))
    result = policy_result(request, text='{"answer":"xxx"}')
    if not available:
        monkeypatch.setattr(inference, 'schema_verdict', lambda *args: None)
        async def unavailable(*args):
            return None
        monkeypatch.setattr(inference, 'async_schema_verdict', unavailable)
    seen = []
    with inference.bind_bounded_result_observer(lambda owned, candidate, error, facts, fingerprint, checked_text: seen.append((error, facts))):
        with pytest.raises(ProviderAccessError) as caught:
            if validator == 'async':
                asyncio.run(inference.async_validate_result(request, result))
            else:
                (inference.validate_result if validator == 'sync' else inference.validate_response_result)(request, result)
    assert len(seen) == 1 and seen[0][0] is caught.value
    if available:
        assert type(caught.value) is CandidateOutputError
        caught.value.feedback['errors'][0]['constraint'] = 123456
        assert seen[0][1]['errors'] == [
            {'path': ['answer'], 'keyword': 'maxLength', 'constraint': 2, 'actual': 3}]
    else:
        assert type(caught.value) is ProviderAccessError and seen[0][1] is None


@pytest.mark.parametrize('fault', [RuntimeError('optional metadata'), asyncio.CancelledError()])
def test_shared_metadata_fault_preserves_candidate_and_allows_later_valid_recording(fault, monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.llm import StructuredOutput
    from api_agent.providers.contracts import CandidateOutputError
    from api_agent.workflows import inference_capture
    request = LLMRequest('selected', CompletionRequest((Message('user', (TextContent('ask'),)),),
        structured=StructuredOutput('answer', json.dumps({'type': 'object',
            'properties': {'answer': {'type': 'string', 'maxLength': 2}},
            'required': ['answer'], 'additionalProperties': False}), False)), 'request-one')
    result = LLMResult(Message('assistant', (TextContent('{"answer":"xxx"}'),)))
    observed = Mock(side_effect=[fault, None])
    monkeypatch.setattr(inference_capture, 'record_provider_fields', observed)
    async def call(callback):
        return await callback(auth='synthetic-auth')
    async def complete(owned, *, auth):
        return await inference.async_validate_result(owned, result)
    async def scenario():
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        try:
            with pytest.raises(CandidateOutputError) as caught:
                await owner.complete(request)
            assert caught.value.result is result and observed.call_count == 2
        finally:
            await owner.aclose()
    asyncio.run(scenario())


def test_empty_id_default_normalization_requires_original_immutable_payload_identity():
    from copy import deepcopy
    from api_agent.providers.cache_policy import configured_request
    from api_agent.providers.shared_setup import _observed_request_matches
    request, _ = pair()
    assert request.request_id == ''
    assert _observed_request_matches(configured_request(request, 'medium', reasoning_effort='high'), request)
    unrelated = replace(request, payload=deepcopy(request.payload))
    assert unrelated == request and not _observed_request_matches(unrelated, request)


@pytest.mark.parametrize('route', ['physical', 'returned'])
@pytest.mark.parametrize('mutation', ['none', 'visible-after', 'metadata-after', 'invalid-after', 'during-await'])
def test_confirmed_facts_and_metadata_refuse_mutated_source_result(route, mutation, monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.cache_policy import configured_request
    from api_agent.providers.llm import StructuredOutput
    from api_agent.providers.contracts import CandidateOutputError
    from api_agent.workflows import inference_capture
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    schema = json.dumps({'type': 'object', 'properties': {'answer': {'type': 'string', 'maxLength': 2}},
                         'required': ['answer'], 'additionalProperties': False})
    request = LLMRequest('selected', CompletionRequest((Message('user', (TextContent('ask'),)),),
        structured=StructuredOutput('answer', schema, False)))
    part = TextContent('{"answer":"xxx"}')
    result = LLMResult(Message('assistant', (part,)), actual_model='actual-model')
    seen = []
    monkeypatch.setattr(inference_capture, 'record_provider_fields', lambda *args, **kwargs: seen.append(args))
    def mutate():
        if mutation == 'visible-after':
            object.__setattr__(part, 'text', '{"answer":"ok"}')
        elif mutation == 'metadata-after':
            object.__setattr__(result, 'actual_model', 'mutated-model')
        elif mutation == 'invalid-after':
            object.__setattr__(part, 'text', 42)
    original_observe = inference._observe_bounded_result
    def observed(*args, **kwargs):
        original_observe(*args, **kwargs)
        if route == 'returned':
            mutate()
    monkeypatch.setattr(inference, '_observe_bounded_result', observed)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        if mutation == 'during-await':
            original_verdict = inference.async_schema_verdict
            async def held_verdict(*args, **kwargs):
                verdict = await original_verdict(*args, **kwargs)
                assert verdict is False
                entered.set()
                await release.wait()
                return verdict
            monkeypatch.setattr(inference, 'async_schema_verdict', held_verdict)
        async def call(callback):
            return await callback(auth='synthetic-auth')
        async def complete(owned, *, auth):
            if route == 'returned':
                return result
            try:
                return await inference.async_validate_result(configured_request(owned, 'medium'), result)
            except CandidateOutputError:
                mutate()
                raise
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        try:
            with inference_capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
                task = asyncio.create_task(owner.complete(request))
                if mutation == 'during-await':
                    await asyncio.wait_for(entered.wait(), 2)
                    object.__setattr__(part, 'text', '{"answer":"yyyy"}')
                    release.set()
                with pytest.raises(CandidateOutputError):
                    await task
            row, = [row for row in state['rows'] if row['phase'] == 'response']
            failure = row['content']['application_failure']
            if mutation == 'none':
                assert failure['candidate']['feedback']['errors'] == [
                    {'path': ['answer'], 'keyword': 'maxLength', 'constraint': 2, 'actual': 3}]
                assert len(seen) == 1
            else:
                assert 'candidate' not in failure
                assert len(seen) == (1 if route == 'returned' else 0)
        finally:
            await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('validator', ['sync', 'response'])
def test_sync_checked_text_mutation_cannot_mint_confirmed_facts(validator, monkeypatch):
    import json
    from api_agent.providers.llm import StructuredOutput
    from api_agent.providers.contracts import CandidateOutputError
    from test_response_policy_contract import request as policy_request, result as policy_result
    request = policy_request('json')
    shape = json.dumps({'type': 'object', 'properties': {'answer': {'type': 'string', 'maxLength': 2}},
                        'required': ['answer'], 'additionalProperties': False})
    request = replace(request, payload=replace(request.payload, response=replace(
        request.payload.response, schema=StructuredOutput('answer', shape, False))))
    result = policy_result(request, text='{"answer":"xxx"}')
    original = inference.schema_verdict
    def verdict(*args):
        rejected = original(*args)
        assert rejected is False
        object.__setattr__(result.payload.content[0], 'text', '{"answer":"ok"}')
        return rejected
    monkeypatch.setattr(inference, 'schema_verdict', verdict)
    seen = []
    with inference.bind_bounded_result_observer(lambda request, result, error, facts, fingerprint, checked_text:
                                                seen.append((facts, fingerprint))):
        with pytest.raises(CandidateOutputError):
            (inference.validate_result if validator == 'sync' else inference.validate_response_result)(request, result)
    assert seen == [(None, None)]


@pytest.mark.parametrize('text', ['{"answer":"xxx"}', ''])
def test_metadata_callback_mutation_cannot_pair_new_visible_body_with_confirmed_facts(text, monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.cache_policy import configured_request
    from api_agent.providers.llm import StructuredOutput
    from api_agent.providers.contracts import CandidateOutputError
    from api_agent.workflows import inference_capture
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    request = LLMRequest('selected', CompletionRequest((Message('user', (TextContent('ask'),)),),
        structured=StructuredOutput('answer', json.dumps({'type': 'object',
            'properties': {'answer': {'type': 'string', 'maxLength': 2}},
            'required': ['answer'], 'additionalProperties': False}), False)))
    part = TextContent(text)
    result = LLMResult(Message('assistant', (part,)), actual_model='actual-model')
    calls = []
    def metadata(*args, **kwargs):
        calls.append(args)
        object.__setattr__(part, 'text', '{"answer":"ok"}')
    monkeypatch.setattr(inference_capture, 'record_provider_fields', metadata)
    async def call(callback):
        return await callback(auth='synthetic-auth')
    async def complete(owned, *, auth):
        return await inference.async_validate_result(configured_request(owned, 'medium'), result)
    async def scenario():
        owner = SharedSetupOwner('operation', 'selected', lambda credentials:
            SimpleNamespace(call=call, aclose=AsyncMock()), complete=complete,
            supports_output_limit=False, max_attempts=1)
        try:
            with inference_capture.collect_inference(EmissionBoundary(ContentPolicy(True))) as state:
                with pytest.raises(CandidateOutputError):
                    await owner.complete(request)
            row, = [row for row in state['rows'] if row['phase'] == 'response']
            candidate = row['content']['application_failure']['candidate']
            assert candidate['visible_text'] == text
            assert len(calls) == 1 and part.text == '{"answer":"ok"}'
            if text:
                assert candidate['feedback']['errors'] == [
                    {'path': ['answer'], 'keyword': 'maxLength', 'constraint': 2, 'actual': 3}]
            else:
                assert candidate['feedback'] == {'reason': 'response_json', 'errors': []}
        finally:
            await owner.aclose()
    asyncio.run(scenario())
