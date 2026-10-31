"""Implicit output ceilings do not require catalog-list permission."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderAccessError, SetupPrerequisiteError
from api_agent.providers.control import OperationalError
from api_agent.providers.generation_attempts import TransportAttemptLedger, bind_transport_attempt_ledger
from api_agent.providers.llm import CacheBoundary, CompletionRequest, GenerationOptions, LLMRequest, LLMResult, Message, TextContent, Usage
from api_agent.providers.output_limits import OutputLimitRejected
from api_agent.providers.shared_setup import SharedSetupOwner
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.inference_capture import collect_inference


def request(*, maximum=None, cache_boundary=False):
    message = Message('user',(TextContent('question'),),cache_boundary=CacheBoundary() if cache_boundary else None)
    return LLMRequest('selected',CompletionRequest((message,),generation=GenerationOptions(max_output_tokens=maximum)))


@pytest.fixture
def make_owner():
    def create(*, catalog_error=None, capability=True, configured_maximum=200000, inference_errors=(), catalog=(), auth_error=None):
        dispatched = []
        failures = iter(inference_errors)
        async def call(callback):
            if auth_error is not None:
                raise auth_error
            return await callback(auth=object())
        session = SimpleNamespace(call=call,aclose=AsyncMock())
        describe = AsyncMock(side_effect=catalog_error,return_value=catalog)
        async def complete(value, *, auth):
            dispatched.append(value.payload.generation.max_output_tokens)
            error = next(failures,None)
            if error is not None:
                raise error
            return LLMResult(Message('assistant',(TextContent('answer'),)),Usage(1,2))
        owner = SharedSetupOwner('operation','fixed-identity',lambda credentials: session,
                                 describe=describe,complete=complete,supports_output_limit=capability,
                                 max_output_tokens=configured_maximum,max_attempts=1)
        return SimpleNamespace(owner=owner,dispatched=dispatched,describe=describe)
    return create


def test_optional_catalog_denial_keeps_cap_correction_capture_and_owner_cache(make_owner,monkeypatch):
    from api_agent.providers import rich_inference
    original_validate = rich_inference.async_validate_result
    selections = []
    async def validate(value,result,*,selection=None):
        selections.append(selection)
        return await original_validate(value,result,selection=selection)
    monkeypatch.setattr(rich_inference,'async_validate_result',validate)
    binding = make_owner(catalog_error=ProviderAccessError('authorization','Catalog listing denied.',status=403),
                         inference_errors=(OutputLimitRejected(64000),))
    original = request()
    async def scenario():
        ledgers, captured = [], []
        try:
            for _ in range(2):
                ledger = TransportAttemptLedger(1)
                with bind_transport_attempt_ledger(ledger), collect_inference(EmissionBoundary(ContentPolicy(True))) as collector:
                    result = await binding.owner.complete(original)
                assert result.payload.content == (TextContent('answer'),)
                ledgers.append(ledger.snapshot())
                captured.extend(row for row in collector['rows'] if row['phase']=='request')
        finally:
            await binding.owner.aclose()
        assert [row['content']['max_output_tokens'] for row in captured] == [200000,64000,64000]
        assert [row['transport_attempt_id'] for row in captured] == [
            *(row['id'] for row in ledgers[0]['attempts']),ledgers[1]['attempts'][0]['id']]
        assert ledgers[0]['attempts'][0]['execution'] == 'not_started'
        assert ledgers[0]['attempts'][0]['usage'] is None
        assert ledgers[0]['attempts'][1]['usage']['output_tokens'] == 2
        assert len(ledgers[1]['attempts']) == 1
    asyncio.run(scenario())
    assert binding.dispatched == [200000,64000,64000]
    assert original.payload.generation.max_output_tokens is None
    assert binding.describe.await_count == 2
    assert selections == [None,None]


@pytest.mark.parametrize('error',[
    ProviderAccessError('authentication','Authentication denied.',status=401),
    ProviderAccessError('authorization','Authentication denied.',status=401),
    ProviderAccessError('authorization','Local permission denied.',status=403,origin='local_output_validation'),
    ProviderAccessError('authorization','Catalog failure.',status=500),
    OperationalError('authorization'),
])
def test_other_authentication_or_authorization_failures_remain_terminal(make_owner,error):
    binding = make_owner(catalog_error=error)
    async def scenario():
        try:
            with pytest.raises(type(error)) as caught:
                await binding.owner.complete(request())
            assert caught.value is error
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == []


@pytest.mark.parametrize('value,error',[
    (request(maximum=100000),ProviderAccessError('authorization','Catalog listing denied.',status=403)),
    (request(cache_boundary=True),ProviderAccessError('authorization','Catalog listing denied.',status=403)),
    (request(maximum=100000),OperationalError('unsupported')),
    (request(maximum=100000),OperationalError('unavailable')),
    (request(maximum=100000),SetupPrerequisiteError('Catalog unavailable.')),
])
def test_caller_catalog_dependent_constraints_remain_strict(make_owner,value,error):
    binding = make_owner(catalog_error=error)
    async def scenario():
        try:
            with pytest.raises(type(error)) as caught:
                await binding.owner.complete(value)
            assert caught.value is error
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == []


@pytest.mark.parametrize('error',[OperationalError('unsupported'),OperationalError('unavailable'),SetupPrerequisiteError('Catalog unavailable.')])
def test_existing_optional_metadata_fallback_sends_declared_cap(make_owner,error):
    binding = make_owner(catalog_error=error,configured_maximum=12000)
    async def scenario():
        try:
            await binding.owner.complete(request())
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == [12000]


@pytest.mark.parametrize('capability,expected',[(None,None),(True,12000),(False,None)])
def test_empty_catalog_fallback_requires_declared_capability(make_owner,capability,expected):
    binding = make_owner(capability=capability,configured_maximum=12000)
    async def scenario():
        try:
            await binding.owner.complete(request())
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == [expected]
    assert binding.describe.await_count == (0 if capability is False else 1)


def test_declared_capability_without_catalog_still_sends_configured_ceiling(make_owner):
    binding = make_owner(configured_maximum=12000)
    binding.owner._describe = None
    async def scenario():
        try:
            await binding.owner.complete(request())
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == [12000]


def test_catalog_permission_fallback_does_not_swallow_inference_permission_denial(make_owner):
    failure = ProviderAccessError('authorization','Inference denied.',status=403)
    binding = make_owner(catalog_error=ProviderAccessError('authorization','Catalog denied.',status=403),inference_errors=(failure,))
    async def scenario():
        try:
            with pytest.raises(ProviderAccessError) as caught:
                await binding.owner.complete(request())
            assert caught.value is failure
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == [200000]


def test_failed_owner_authentication_never_reaches_optional_catalog(make_owner):
    error = ProviderAccessError('authentication','Authentication denied.',status=401)
    binding = make_owner(auth_error=error)
    async def scenario():
        try:
            with pytest.raises(ProviderAccessError) as caught:
                await binding.owner.complete(request())
            assert caught.value is error
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    binding.describe.assert_not_awaited()
    assert binding.dispatched == []


def test_catalog_error_with_inference_usage_is_not_optional(make_owner):
    failure = ProviderAccessError('authorization','Catalog denied.',status=403,usage=Usage(1,0))
    binding = make_owner(catalog_error=failure)
    async def scenario():
        try:
            with pytest.raises(ProviderAccessError) as caught:
                await binding.owner.complete(request())
            assert caught.value is failure
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == []


def test_optional_denial_preserves_lower_configured_ceiling(make_owner):
    binding = make_owner(configured_maximum=12000,
                         catalog_error=ProviderAccessError('authorization','Catalog listing denied.',status=403))
    async def scenario():
        try:
            await binding.owner.complete(request())
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == [12000]


def test_arbitrary_operational_error_cannot_impersonate_provider_catalog_denial(make_owner):
    error = OperationalError('authorization')
    error.status,error.origin,error.usage,error.response = 403,'provider',None,None
    binding = make_owner(catalog_error=error)
    async def scenario():
        try:
            with pytest.raises(OperationalError) as caught:
                await binding.owner.complete(request())
            assert caught.value is error
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == []


def test_catalog_error_with_response_evidence_is_not_optional(make_owner):
    from api_agent.providers.llm import ResponseEvidence
    evidence = ResponseEvidence('text','reject','text',None,None,None)
    error = ProviderAccessError('authorization','Catalog denied.',status=403,response=evidence)
    binding = make_owner(catalog_error=error)
    async def scenario():
        try:
            with pytest.raises(ProviderAccessError) as caught:
                await binding.owner.complete(request())
            assert caught.value is error
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == []


def test_permission_fallback_requires_implicit_ceiling_discovery():
    from api_agent.providers.rich_inference import resolve_catalog_selection
    error = ProviderAccessError('authorization','Catalog listing denied.',status=403)
    with pytest.raises(ProviderAccessError) as caught:
        asyncio.run(resolve_catalog_selection(AsyncMock(side_effect=error),request()))
    assert caught.value is error


def test_absent_catalog_does_not_bypass_explicit_caller_budget(make_owner):
    value = request(maximum=100000)
    binding = make_owner()
    binding.owner._describe = None
    async def scenario():
        try:
            with pytest.raises(OperationalError,match='unsupported'):
                await binding.owner.complete(value)
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == []


@pytest.mark.parametrize('error',[OperationalError('unsupported'),OperationalError('unavailable'),SetupPrerequisiteError('Catalog unavailable.')])
def test_unavailable_optional_cache_hints_preserve_baseline_behavior(make_owner,error):
    binding = make_owner(catalog_error=error)
    async def scenario():
        try:
            await binding.owner.complete(request(cache_boundary=True))
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == [None]


def test_optional_cache_hint_without_catalog_remains_unapplied(make_owner):
    binding = make_owner()
    binding.owner._describe = None
    async def scenario():
        try:
            result = await binding.owner.complete(request(cache_boundary=True))
            assert result.applied_cache_boundaries == ()
        finally:
            await binding.owner.aclose()
    asyncio.run(scenario())
    assert binding.dispatched == [None]
