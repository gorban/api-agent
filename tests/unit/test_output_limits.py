"""Output ceilings are negotiated only from explicit pre-inference facts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
from api_agent.providers.llm import AvailableSelection, CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.output_limits import OutputLimitCache, OutputLimitRejected, clamp_request
from api_agent.providers.shared_setup import SharedSetupOwner


def request(maximum=None, model='sample'):
    return LLMRequest(model, CompletionRequest(
        (Message('user', (TextContent('question'),)),),
        generation=GenerationOptions(max_output_tokens=maximum)))


@pytest.mark.parametrize('requested,catalog,learned,expected', [(None,None,None,200000),(300000,None,None,200000),(1000,900,800,800),(500,900,800,500)])
def test_clamp_uses_smallest_ceiling_and_preserves_original(requested, catalog, learned, expected):
    original = request(requested)
    selection = AvailableSelection('sample', features=('max_output_tokens',), max_output_tokens=catalog)
    effective = clamp_request(original, selection, learned_maximum=learned)
    assert effective.payload.generation.max_output_tokens == expected
    assert original.payload.generation.max_output_tokens == requested


def test_default_omitted_for_unsupported_transport():
    original = request()
    assert clamp_request(original, AvailableSelection('sample')) is original


@pytest.mark.parametrize('value', [True,0,-1,1048577,'100'])
def test_rejection_requires_bounded_integer(value):
    with pytest.raises(ContractError):
        OutputLimitRejected(value)


def test_cache_expiry_and_model_isolation(monkeypatch):
    now = [1.0]
    monkeypatch.setattr('api_agent.providers.output_limits.time.monotonic', lambda: now[0])
    cache = OutputLimitCache(10)
    cache.remember('a',100)
    cache.remember('a',200)
    assert cache.get('a') == 100
    assert cache.get('b') is None
    now[0] = 11
    assert cache.get('a') is None


def test_cache_has_bounded_owner_memory():
    cache = OutputLimitCache()
    for index in range(257):
        cache.remember(str(index), 100)
    assert len(cache._entries) == 256
    assert cache.get('0') is None


async def exercise(errors, *, attempts=1, calls=1):
    dispatched = []
    async def call(callback):
        return await callback(auth=object())
    async def describe(*, auth):
        return (AvailableSelection('sample', features=('max_output_tokens',), max_output_tokens=150000),)
    async def complete(effective, *, auth):
        dispatched.append(effective.payload.generation.max_output_tokens)
        if errors:
            error = errors.pop(0)
            if error is not None:
                raise error
        return LLMResult(Message('assistant', (TextContent('answer'),)), Usage(1,2))
    owner = SharedSetupOwner('op', 'identity', lambda credentials: SimpleNamespace(call=call, aclose=AsyncMock()),
                             describe=describe, complete=complete, max_attempts=attempts)
    ledgers = []
    try:
        for _ in range(calls):
            ledger = TransportAttemptLedger(attempts)
            with bind_transport_attempt_ledger(ledger):
                await owner.complete(request())
            ledgers.append(ledger.snapshot())
    finally:
        await owner.aclose()
    return dispatched, ledgers


def test_one_negotiation_is_extra_and_cached_for_same_owner():
    dispatched, ledgers = asyncio.run(exercise([OutputLimitRejected(64000)], calls=2))
    assert dispatched == [150000,64000,64000]
    failed, success = ledgers[0]['attempts']
    assert failed['execution'] == 'not_started'
    assert failed['retry_delay_seconds'] == 0
    assert failed['usage'] is None
    assert success['usage']['output_tokens'] == 2
    assert len(ledgers[1]['attempts']) == 1


@pytest.mark.parametrize('errors', [[OutputLimitRejected(150000)], [OutputLimitRejected(160000)],
    [ProviderAccessError('invalid_request', 'maximum output tokens: 64000', status=400)],
    [OutputLimitRejected(64000),OutputLimitRejected(32000)]])
def test_unconfirmed_or_second_rejection_never_replays(errors):
    with pytest.raises(ProviderAccessError):
        asyncio.run(exercise(errors))


def test_usage_on_typed_rejection_disables_negotiation():
    error = OutputLimitRejected(64000)
    error.usage = Usage(1,0)
    with pytest.raises(OutputLimitRejected):
        asyncio.run(exercise([error]))


def test_negotiation_does_not_consume_transport_retry_budget(monkeypatch):
    monkeypatch.setattr('api_agent.providers.inference_retry.retry_delay', lambda *args: 0)
    network = ProviderAccessError('network', 'Connection interrupted.')
    dispatched, ledgers = asyncio.run(exercise([network,OutputLimitRejected(64000)], attempts=2))
    assert dispatched == [150000,150000,64000]
    assert len(ledgers[0]['attempts']) == 3


def test_twenty_transport_attempts_and_one_negotiation_fit_wire_ledger(monkeypatch):
    monkeypatch.setattr('api_agent.providers.inference_retry.retry_delay', lambda *args: 0)
    errors = [ProviderAccessError('network', 'Connection interrupted.') for _ in range(19)]
    errors.append(OutputLimitRejected(64000))
    dispatched, ledgers = asyncio.run(exercise(errors, attempts=20))
    assert len(dispatched) == 21
    assert dispatched[-1] == 64000
    assert len(ledgers[0]['attempts']) == 21


def test_learned_limit_is_not_shared_between_owners():
    first, _ = asyncio.run(exercise([OutputLimitRejected(64000)]))
    second, _ = asyncio.run(exercise([]))
    assert first == [150000,64000]
    assert second == [150000]


@pytest.mark.parametrize('ttl', [True,-1,float('inf'),float('nan'),'1'])
def test_cache_rejects_invalid_expiry(ttl):
    with pytest.raises(ContractError):
        OutputLimitCache(ttl)


def test_disabled_cache_and_empty_cache():
    cache = OutputLimitCache(0)
    assert cache.get('a') is None
    cache.remember('a',100)
    assert cache.get('a') is None


@pytest.mark.parametrize('maximum', [True,0,1048577,'100'])
def test_owner_rejects_invalid_configured_maximum(maximum):
    with pytest.raises(ContractError):
        SharedSetupOwner('op','identity',lambda credentials: None,max_output_tokens=maximum)


def test_expired_deadline_does_not_authorize_negotiation(monkeypatch):
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def expired_budget(*args):
        yield SimpleNamespace(remaining=0)
    monkeypatch.setattr('api_agent.providers.progress.component_timeout', expired_budget)
    with pytest.raises(OutputLimitRejected):
        asyncio.run(exercise([OutputLimitRejected(64000)]))


def test_preinference_response_policy_metadata_is_preserved():
    from api_agent.providers.llm import ResponseEvidence
    from api_agent.providers.output_limits import rejected_limit
    evidence = ResponseEvidence('text','reject','text',None,None,None)
    error = OutputLimitRejected(64000)
    error.response = evidence
    assert rejected_limit(error, request(100000)) == 64000
    ledger = TransportAttemptLedger(1)
    token = ledger.begin(1)
    ledger.finish(token,error=error,retry_delay_seconds=0,response=evidence)
    assert ledger.snapshot()['attempts'][0]['response']['actual_mode'] == 'text'
    token = ledger.begin(2)
    ledger.finish(token,result=SimpleNamespace(usage=Usage(1,2),billing=None))
    assert len(ledger.snapshot()['attempts']) == 2


def test_omitted_limit_allows_empty_catalog_without_inventing_capabilities():
    from api_agent.providers.rich_inference import resolve_catalog_selection
    selection = asyncio.run(resolve_catalog_selection(AsyncMock(return_value=()), request(), clamp_output_limit=True))
    assert selection is None


def test_explicit_limit_still_requires_catalog_selection():
    from api_agent.providers.control import OperationalError
    from api_agent.providers.rich_inference import resolve_catalog_selection
    with pytest.raises(OperationalError):
        asyncio.run(resolve_catalog_selection(AsyncMock(return_value=()), request(100), clamp_output_limit=True))


def test_provider_preflight_rejects_before_authentication():
    from unittest.mock import Mock
    factory = Mock()
    def validate(original):
        raise ProviderAccessError('unsupported','Request option unsupported.')
    async def scenario():
        owner = SharedSetupOwner('op','identity',factory,validate_request=validate)
        try:
            with pytest.raises(ProviderAccessError):
                await owner.complete(request())
        finally:
            await owner.aclose()
    asyncio.run(scenario())
    factory.assert_not_called()


def test_unsupported_provider_omitted_limit_does_not_discover_catalog():
    async def scenario():
        describe = AsyncMock(side_effect=AssertionError('Discovery must not run.'))
        result = LLMResult(Message('assistant',(TextContent('answer'),)),Usage(1,2))
        complete = AsyncMock(return_value=result)
        async def call(callback):
            return await callback(auth=object())
        owner = SharedSetupOwner('op','identity',lambda credentials: SimpleNamespace(call=call,aclose=AsyncMock()),
                                 describe=describe,complete=complete,supports_output_limit=False)
        try:
            assert await owner.complete(request()) == result
            describe.assert_not_awaited()
            assert complete.call_args.args[0].payload.generation.max_output_tokens is None
        finally:
            await owner.aclose()
    asyncio.run(scenario())


def test_implicit_discovery_missing_selection_preserves_caller_request():
    from api_agent.providers.rich_inference import resolve_catalog_selection
    original = request(model='unlisted')
    selection = asyncio.run(resolve_catalog_selection(
        AsyncMock(return_value=(AvailableSelection('listed',features=('max_output_tokens',)),)),
        original,clamp_output_limit=True))
    assert selection is None
    assert clamp_request(original,selection) is original


def test_implicit_discovery_does_not_introduce_unrelated_feature_gates():
    from dataclasses import replace
    from api_agent.providers.rich_inference import resolve_catalog_selection
    original = request()
    original = replace(original,payload=replace(original.payload,
        generation=replace(original.payload.generation,reasoning_effort='high')))
    selected = AvailableSelection('sample',features=('max_output_tokens',),max_output_tokens=64000)
    actual = asyncio.run(resolve_catalog_selection(AsyncMock(return_value=(selected,)),original,clamp_output_limit=True))
    assert actual == selected
    assert clamp_request(original,actual).payload.generation.max_output_tokens == 64000


def test_ledger_rejects_two_negotiation_retries():
    ledger = TransportAttemptLedger(2)
    first = ledger.begin(1)
    ledger.finish(first,error=OutputLimitRejected(64000),retry_delay_seconds=0)
    second = ledger.begin(2)
    with pytest.raises(ContractError,match='Invalid output limit retry sequence'):
        ledger.finish(second,error=OutputLimitRejected(32000),retry_delay_seconds=0)


def test_twenty_one_rows_require_a_negotiation_retry():
    from copy import deepcopy
    from uuid import uuid4
    from api_agent.providers.generation_attempts import validate_transport_attempts
    ledger = TransportAttemptLedger(20)
    for index in range(1,21):
        token = ledger.begin(index)
        ledger.finish(token,error=ProviderAccessError('network','Connection interrupted.'),retry_delay_seconds=0)
    snapshot = ledger.snapshot()
    extra = deepcopy(snapshot['attempts'][-1])
    extra.update(id=str(uuid4()),index=21,started_ms=extra['stopped_ms'])
    snapshot['attempts'].append(extra)
    with pytest.raises(ContractError,match='Invalid output limit retry sequence'):
        validate_transport_attempts(snapshot)


def test_owner_rejects_nonboolean_output_capability():
    with pytest.raises(ContractError,match='Invalid output limit capability'):
        SharedSetupOwner('op','identity',lambda credentials: None,supports_output_limit=1)


def test_catalog_limit_validation_preserves_an_omitted_call_budget():
    from api_agent.providers.rich_inference import validate_catalog_selection
    original = request()
    selected = AvailableSelection('sample', features=('max_output_tokens',), max_output_tokens=64000)
    assert validate_catalog_selection((selected,), original, clamp_output_limit=True) is selected
    assert original.payload.generation.max_output_tokens is None


def test_pending_cancellation_prevents_limit_correction_dispatch():
    async def scenario():
        dispatched = []
        async def call(callback):
            return await callback(auth=object())
        async def describe(*, auth):
            return (AvailableSelection('sample', features=('max_output_tokens',)),)
        async def complete(effective, *, auth):
            dispatched.append(effective.payload.generation.max_output_tokens)
            if len(dispatched) == 1:
                asyncio.current_task().cancel()
                raise OutputLimitRejected(64000)
            return LLMResult(Message('assistant', (TextContent('answer'),)))
        owner = SharedSetupOwner('op', 'identity',
            lambda credentials: SimpleNamespace(call=call, aclose=AsyncMock()),
            describe=describe, complete=complete, max_attempts=1)
        ledger = TransportAttemptLedger(1)
        try:
            with bind_transport_attempt_ledger(ledger):
                with pytest.raises(asyncio.CancelledError):
                    await owner.complete(request())
            assert dispatched == [200000]
            rows = ledger.snapshot()['attempts']
            assert len(rows) == 1
            assert rows[0]['outcome'] == 'failed'
            assert rows[0]['execution'] == 'not_started'
        finally:
            await owner.aclose()
    asyncio.run(scenario())
