"""Regression controls for owner-configured structured proposal replay."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ContractError, ProviderCapabilities
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, StructuredProposal, ToolCall, Usage
from api_agent.workflows.inference_history import InferenceHistory
from test_local_llm import selected
from test_structured_replay_authority import PROFILE, original, replay


def install_provider(selected, *, reasoning, cache, cap=128000):
    owner, implementation, _, _, _ = selected
    owner.config = {**owner.config, 'LLM_REASONING_EFFORT': reasoning, 'LLM_CACHE_POLICY': cache}
    implementation.capabilities = ProviderCapabilities('llm', features=('structured_function', 'tools',
        'reasoning_effort', 'cache_policy', 'max_output_tokens'))
    implementation.adescribe = AsyncMock(return_value=(AvailableSelection('sample',
        features=('structured_function', 'tools', 'reasoning_effort', 'cache_policy', 'max_output_tokens'),
        reasoning_efforts=('low', 'high'), max_output_tokens=cap, structured_function_profile=PROFILE),))
    calls = []
    async def complete(value, *, auth):
        calls.append(value)
        call = ToolCall('wire-id' if len(calls) == 1 else 'repair-id', 'InvoiceResponse',
                        '{}' if len(calls) == 1 else '{"reference":"ok"}')
        return LLMResult(Message('assistant', calls=(call,)), Usage(4, 2), 'tool_calls',
                         structured_proposal=StructuredProposal(call))
    implementation.acomplete = complete
    request = replace(original(), selected_llm='sample')
    return owner, calls, request


@pytest.mark.asyncio
@pytest.mark.parametrize('reasoning,cache', [('', ''), ('high', ''), ('', 'off'), ('low', 'medium')])
@pytest.mark.parametrize('history_enabled', [False, True])
@pytest.mark.parametrize('raw_max,cap', [(None, 128000), (200, 64)])
async def test_configured_owner_repairs_exact_raw_proposal(selected, reasoning, cache, history_enabled, raw_max, cap):
    owner, calls, request = install_provider(selected, reasoning=reasoning, cache=cache, cap=cap)
    request = replace(request, payload=replace(request.payload, generation=replace(request.payload.generation, max_output_tokens=raw_max)))
    history = InferenceHistory() if history_enabled else None
    async def invoke(value):
        return await history.call('analysis', 'llm', owner.complete, value) if history else await owner.complete(value)
    try:
        first = await invoke(request)
        assert first.structured_proposal.receipt is not None
        second = await invoke(replay(request, first, first.structured_proposal.receipt))
        assert second.structured_proposal.call.call_id == 'repair-id'
        assert len(calls) == 2
        assert calls[0].payload.generation.reasoning_effort == reasoning
        assert calls[1].payload.generation.reasoning_effort == reasoning
        assert calls[0].payload.generation.cache_policy == (cache or None)
        assert calls[1].payload.generation.cache_policy == (cache or None)
        assert calls[0].payload.generation.max_output_tokens == cap
        assert calls[1].payload.generation.max_output_tokens == cap
        with pytest.raises(ContractError):
            await invoke(replay(request, first, first.structured_proposal.receipt))
        assert len(calls) == 2
    finally:
        if history:
            history.close()
        await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['effort', 'cache', 'max', 'temperature', 'schema', 'profile', 'prefix', 'call', 'feedback'])
async def test_configured_replay_rejects_raw_caller_mutation_before_second_dispatch(selected, changed):
    owner, calls, request = install_provider(selected, reasoning='high', cache='medium', cap=64)
    request = replace(request, payload=replace(request.payload,
        generation=replace(request.payload.generation, reasoning_effort='high', cache_policy='medium', max_output_tokens=200)))
    try:
        first = await owner.complete(request)
        second = replay(request, first, first.structured_proposal.receipt)
        if changed in ('effort', 'cache', 'max'):
            values = {'effort': {'reasoning_effort': None}, 'cache': {'cache_policy': None}, 'max': {'max_output_tokens': 64}}[changed]
            second = replace(second, payload=replace(second.payload, generation=replace(second.payload.generation, **values)))
        elif changed == 'temperature':
            second = replace(second, payload=replace(second.payload, generation=replace(second.payload.generation, temperature=0.5)))
        elif changed == 'profile':
            from api_agent.providers.llm import StructuredFunctionProfile
            definition = replace(second.payload.structured, function_profile=StructuredFunctionProfile(True, 'converter'))
            second = replace(second, payload=replace(second.payload, structured=definition))
        elif changed == 'schema':
            second = replace(second, payload=replace(second.payload, structured=replace(second.payload.structured, name='Other')))
        elif changed == 'prefix':
            second = replace(second, payload=replace(second.payload, messages=(Message('user'), *second.payload.messages[1:])))
        elif changed == 'call':
            altered = replace(second.payload.messages[-2].calls[0], arguments='{"reference":"changed"}')
            message = replace(second.payload.messages[-2], calls=(altered,))
            second = replace(second, payload=replace(second.payload, messages=(*second.payload.messages[:-2], message, second.payload.messages[-1])))
        else:
            from api_agent.providers.llm import TextContent, ToolResult
            feedback = Message('tool', results=(ToolResult('wire-id', (TextContent('{"not":"validation"}'),)),))
            second = replace(second, payload=replace(second.payload, messages=(*second.payload.messages[:-1], feedback)))
        with pytest.raises(ContractError):
            await owner.complete(second)
        assert len(calls) == 1
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_omitted_raw_default_cannot_be_replaced_by_explicit_equivalent(selected):
    owner, calls, request = install_provider(selected, reasoning='high', cache='medium')
    try:
        first = await owner.complete(request)
        second = replay(request, first, first.structured_proposal.receipt)
        second = replace(second, payload=replace(second.payload,
            generation=replace(second.payload.generation, reasoning_effort='high')))
        with pytest.raises(ContractError):
            await owner.complete(second)
        assert len(calls) == 1
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_recorded_defaults_cannot_authorize_unscoped_or_foreign_owner():
    from api_agent.providers.cache_policy import configured_request
    from api_agent.providers.structured_proposals import StructuredReplayAuthority
    from test_structured_replay_authority import proposal
    raw = original()
    effective = configured_request(raw, 'medium', reasoning_effort='high')
    issuer, other = StructuredReplayAuthority(), StructuredReplayAuthority()
    receipt = await issuer.issue(effective, proposal(), source_request=raw)
    normalized = configured_request(replay(raw, proposal(), receipt), 'medium', reasoning_effort='high')
    with pytest.raises(ContractError):
        issuer.consume(normalized)
    with pytest.raises(ContractError):
        other.consume(replay(raw, proposal(), receipt))
    receipt.release()


def test_expired_copied_source_context_fails_closed():
    import contextvars
    from api_agent.providers.structured_proposals import bind_structured_source_request, current_structured_source_request
    with bind_structured_source_request(original()):
        copied = contextvars.copy_context()
        assert current_structured_source_request() == original()
    with pytest.raises(ContractError):
        copied.run(current_structured_source_request)


@pytest.mark.asyncio
async def test_learned_output_cap_remains_independent_of_raw_replay_origin(selected):
    owner, calls, request = install_provider(selected, reasoning='', cache='', cap=128000)
    try:
        await owner.ensure_ready()
        owner.shared._output_limits.remember('sample', 32)
        first = await owner.complete(request)
        await owner.complete(replay(request, first, first.structured_proposal.receipt))
        assert request.payload.generation.max_output_tokens is None
        assert [call.payload.generation.max_output_tokens for call in calls] == [32, 32]
    finally:
        await owner.aclose()
