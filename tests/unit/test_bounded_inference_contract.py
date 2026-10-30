"""Synthetic bounded inference contracts independent of provider transports."""
import json
import pytest
from api_agent.providers.llm import (AvailableSelection, CacheBoundary, CompletionRequest,
    GenerationOptions, LLMRequest, LLMResult, Message, StructuredOutput, TextContent,
    Tool, ToolCall, ToolResult, Usage)
from api_agent.providers.inference import request_to_wire, request_from_wire, result_to_wire, result_from_wire
from api_agent.providers.rich_inference import schema, valid_schema, validate_result
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.control import OperationalError
from api_agent.providers.catalog import catalog_to_wire, catalog_from_wire
from api_agent.providers.setup_transport import _encode, _decode

SELECTION = AvailableSelection('synthetic',features=('tools','structured','temperature','max_output_tokens'),max_output_tokens=131072)

OPTIONAL = json.dumps({'type':'object','$defs':{'number':{'type':'integer'}},'properties':{'count':{'$ref':'#/$defs/number','default':7},'label':{'anyOf':[{'type':'string'},{'type':'null'}]}}})


def request(*, maximum=131072, boundary=None):
    return LLMRequest('synthetic', CompletionRequest((Message('system',(TextContent('🌍'*34000),), cache_boundary=boundary),
        Message('assistant', calls=(ToolCall('call','lookup','{}'),)),
        Message('tool',results=(ToolResult('call',(TextContent('🧪'*120000),)),))),
        (Tool('lookup','Synthetic lookup',OPTIONAL,strict=False),),
        GenerationOptions(maximum,temperature=0.5),StructuredOutput('answer',OPTIONAL,strict=False)))


def joined_test_provider(provider):
    # Synthetic lifecycle fakes do not acquire credentials or send network requests.
    provider.credential_protection_version = 1
    provider.bind_credential_authority = lambda authority: setattr(provider, 'authority', authority)
    return provider

@pytest.mark.parametrize('maximum',[16384,32768,131072])
def test_large_unicode_history_and_generation_roundtrip_in_real_frame_codec(maximum):
    original=request(maximum=maximum,boundary=CacheBoundary(3600))
    frame={'request':request_to_wire(original)}
    assert request_from_wire(_decode(_encode(frame))['request']) == original
    assert frame['request']['tools'][0]['schema'] == OPTIONAL


def test_optional_composed_local_reference_schema_preserves_missing_defaults():
    definition=schema(OPTIONAL,strict=False)
    assert valid_schema(definition,{})
    assert valid_schema(definition,{'count':1,'label':None})
    assert not valid_schema(definition,{'count':'bad'})
    assert definition['properties']['count']['default'] == 7
    with pytest.raises(ContractError):
        schema(OPTIONAL)


@pytest.mark.parametrize('definition',[
    {'type':'object','$ref':'https://example.invalid/schema'},
    {'type':'object','$ref':'#/$defs/missing'},
    {'type':'object','$defs':{'self':{'$ref':'#/$defs/self'}},'$ref':'#/$defs/self'},
])
def test_schema_rejects_remote_missing_and_recursive_references(definition):
    with pytest.raises(ContractError):
        schema(json.dumps(definition),strict=False)


@pytest.mark.parametrize('text',['{"count":"bad"}','{"count":1,"count":2}','{','[]'])
def test_actual_structured_output_rejection_retains_usage(text):
    result=LLMResult(Message('assistant',(TextContent(text),)),Usage(10,2))
    with pytest.raises(ProviderAccessError) as raised:
        validate_result(request(),result)
    assert raised.value.usage == result.usage


def test_actual_optional_output_and_tool_arguments_use_originating_schema():
    original=request()
    assert validate_result(original,LLMResult(Message('assistant',(TextContent('{}'),)))).payload.content[0].text == '{}'
    invalid=LLMResult(Message('assistant',calls=(ToolCall('new','lookup','{"count":"bad"}'),)),outcome='tool_calls')
    with pytest.raises(ProviderAccessError):
        validate_result(original,invalid)


def test_advertised_output_limit_rejects_without_clipping():
    selection=AvailableSelection('synthetic',features=('tools','structured','temperature','max_output_tokens'),max_output_tokens=32768)
    selection.validate(request(maximum=32768))
    with pytest.raises(OperationalError,match='unsupported'):
        selection.validate(request(maximum=131072))


def test_cache_lifetimes_catalog_and_applied_facts_are_independent_of_usage():
    selection=AvailableSelection('synthetic',features=('cache_boundaries',),cache_boundary_lifetimes=(300,3600))
    assert catalog_from_wire(catalog_to_wire((selection,))) == (selection,)
    original=request(boundary=CacheBoundary(300))
    result=LLMResult(Message('assistant',(TextContent('{}'),)),Usage(12,3,0),applied_cache_boundaries=(0,))
    assert result_from_wire(result_to_wire(result)) == result
    assert validate_result(original,result,selection=selection) == result
    with pytest.raises(ProviderAccessError):
        validate_result(request(),result)


@pytest.mark.parametrize('lifetime',[True,0,301,'300'])
def test_invalid_cache_lifetimes(lifetime):
    with pytest.raises(ValueError):
        CacheBoundary(lifetime)


def test_too_many_and_empty_cache_boundaries_rejected():
    with pytest.raises(ValueError):
        Message('user',cache_boundary=CacheBoundary())
    with pytest.raises(ValueError):
        CompletionRequest(tuple(Message('user',(TextContent('x'),),cache_boundary=CacheBoundary()) for _ in range(5)))


def test_large_history_and_applied_boundary_facts_cross_real_local_ipc():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    async def scenario():
        original=request(boundary=CacheBoundary(3600))
        result=LLMResult(Message('assistant',(TextContent('{}'),)),Usage(11,2,0),applied_cache_boundaries=(0,))
        complete=AsyncMock(return_value=result)
        owner=SimpleNamespace(ensure_ready=AsyncMock(),complete=complete,aclose=AsyncMock())
        registry=SetupRegistry('synthetic-operation','synthetic-identity',lambda:owner)
        server=SetupServer(registry,'synthetic-bounded-inference-capability-000')
        client=RemoteIntegrationLLM(await server.start())
        try:
            assert await asyncio.to_thread(client.complete,original) == result
            assert await asyncio.to_thread(client.complete,original) == result
            assert [call.args[0] for call in complete.await_args_list] == [original,original]
        finally:
            await asyncio.to_thread(client.close)
            await server.aclose()
            await registry.aclose()
    asyncio.run(scenario())


def test_shared_completion_rejects_invalid_actual_schema_before_success():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.sessions import LLMSessionOwner
    async def scenario():
        provider=SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='synthetic-secret')))
        invalid=LLMResult(Message('assistant',(TextContent('{"count":"bad"}'),)),Usage(13,4))
        owner=SharedSetupOwner('operation','identity',lambda credentials:LLMSessionOwner(provider),complete=AsyncMock(return_value=invalid),describe=AsyncMock(return_value=(SELECTION,)))
        try:
            with pytest.raises(ProviderAccessError) as raised:
                await owner.complete(request())
            assert raised.value.usage == Usage(13,4)
        finally:
            await owner.aclose()
    asyncio.run(scenario())


def test_loaded_complete_and_terminal_stream_validate_before_publication():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import _StreamView
    from api_agent.providers.llm import StreamEvent
    async def scenario():
        invalid=LLMResult(Message('assistant',(TextContent('{"count":"bad"}'),)),Usage(13,4))
        disposed=[]
        async def events(request,*,auth):
            try:
                yield StreamEvent(TextContent('partial'))
                yield StreamEvent(invalid,'completed')
            finally:
                disposed.append(True)
        view=_StreamView(joined_test_provider(SimpleNamespace(astream=events,acomplete=AsyncMock(return_value=invalid),adescribe=AsyncMock(return_value=(SELECTION,)))))
        with pytest.raises(ProviderAccessError):
            await view.acomplete(request())
        stream=view.astream(request())
        assert aiter(stream) is stream
        assert (await anext(stream)).kind == 'text'
        with pytest.raises(ProviderAccessError) as raised:
            await anext(stream)
        assert raised.value.usage == Usage(13,4)
        await stream.aclose()
        assert disposed == [True]
    asyncio.run(scenario())


def test_cache_helper_ignores_unsupported_feature_or_lifetime_without_changing_prompt():
    from api_agent.providers.llm import cache_boundary_indexes
    original=request(boundary=CacheBoundary(3600))
    assert cache_boundary_indexes(original,AvailableSelection('synthetic')) == ()
    short=AvailableSelection('synthetic',features=('cache_boundaries',),cache_boundary_lifetimes=(300,))
    assert cache_boundary_indexes(original,short) == ()
    both=AvailableSelection('synthetic',features=('cache_boundaries',),cache_boundary_lifetimes=(300,3600))
    assert cache_boundary_indexes(original,both) == (0,)
    assert original.payload.messages[0].content[0].text == '🌍'*34000


@pytest.mark.parametrize('indexes',[None,'0',{},[True],[0,0],[64],[-1],[0,1,2,3,4]])
def test_applied_cache_wire_facts_reject_invalid_shape(indexes):
    row=result_to_wire(LLMResult(Message('assistant',(TextContent('{}'),))))
    row['applied_cache_boundaries']=indexes
    with pytest.raises(ContractError):
        result_from_wire(row)


def test_schema_root_composition_and_json_pointer_escaping():
    definition={'$defs':{'a/b~c':{'type':'object','properties':{'x':{'type':'integer'}}}},'allOf':[{'$ref':'#/$defs/a~1b~0c'}]}
    checked=schema(json.dumps(definition),strict=False)
    assert checked == definition
    assert valid_schema(checked,{'x':1})
    assert not valid_schema(checked,{'x':'bad'})


@pytest.mark.parametrize('keyword',['pattern','patternProperties'])
def test_executable_regex_schema_preserves_constraints(keyword):
    child={'type':'string','pattern':'^[a-z]+$'}
    definition={'type':'object','properties':{'x':child}} if keyword == 'pattern' else {'type':'object','patternProperties':{'^x$':child}}
    checked=schema(json.dumps(definition),strict=False)
    assert valid_schema(checked,{'x':'abc'})
    assert not valid_schema(checked,{'x':'123'})


def test_deep_large_and_expanding_schemas_are_bounded():
    from api_agent.providers.rich_inference import object_json
    nested={}
    for _ in range(34):
        nested={'x':nested}
    with pytest.raises(ContractError):
        object_json(json.dumps(nested))
    with pytest.raises(ContractError):
        object_json(json.dumps({'items':[0]*1025}))
    definition={'type':'object','$defs':{'bad':'literal'},'$ref':'#/$defs/bad'}
    with pytest.raises(ContractError):
        schema(json.dumps(definition),strict=False)
    assert not valid_schema({'$ref':'https://example.invalid/remote'}, {})


@pytest.mark.parametrize('method',['complete','stream'])
def test_advertised_maximum_rejects_before_provider_side_effects(method):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from api_agent.providers.loading import _StreamView
    selection=AvailableSelection('synthetic',features=('tools','structured','temperature','max_output_tokens'),max_output_tokens=32768)
    provider=SimpleNamespace(validate_request=selection.validate,acomplete=AsyncMock(),astream=Mock())
    view=_StreamView(joined_test_provider(provider))
    with pytest.raises(OperationalError,match='unsupported'):
        asyncio.run(view.acomplete(request())) if method == 'complete' else asyncio.run(anext(view.astream(request())))
    provider.acomplete.assert_not_awaited()
    provider.astream.assert_not_called()


def test_pathological_regex_times_out_and_preserves_actual_usage():
    import time
    from dataclasses import replace
    definition=json.dumps({'type':'object','properties':{'x':{'type':'string','pattern':'(a+)+$'}}})
    original=request()
    original=replace(original,payload=replace(original.payload,structured=StructuredOutput('answer',definition,strict=False)))
    result=LLMResult(Message('assistant',(TextContent(json.dumps({'x':'a'*64+'!'})),)),Usage(17,3))
    started=time.monotonic()
    with pytest.raises(ProviderAccessError) as raised:
        validate_result(original,result)
    assert raised.value.usage == Usage(17,3)
    assert time.monotonic()-started < 6


def test_strict_array_fast_path_and_valid_tool_result_with_applied_fact():
    definition=json.dumps({'type':'object','properties':{'items':{'type':'array','items':{'type':'integer'}}},'required':['items'],'additionalProperties':False})
    assert valid_schema(schema(definition),{'items':[1,2]})
    original=request(boundary=CacheBoundary())
    result=LLMResult(Message('assistant',calls=(ToolCall('new','lookup','{}'),)),outcome='tool_calls',applied_cache_boundaries=(0,))
    selection=AvailableSelection('synthetic',features=('cache_boundaries',),cache_boundary_lifetimes=(300,))
    assert validate_result(original,result,selection=selection) == result
    assert result_from_wire(result_to_wire(result)) == result


@pytest.mark.parametrize('definition,strict',[
    ({},'false'),
    ({'type':'object','properties':[]},False),
])
def test_invalid_schema_mode_and_schema_map_are_rejected(definition,strict):
    with pytest.raises(ContractError):
        schema(json.dumps(definition),strict=strict)


def test_reference_expansion_budget_rejects_combinatorial_schema_before_effects():
    definitions={'leaf':{'type':'integer'}}
    for index in range(12):
        previous='leaf' if index == 0 else str(index-1)
        definitions[str(index)]={'anyOf':[{'$ref':'#/$defs/'+previous}]*2}
    definition={'type':'object','$defs':definitions,'properties':{'x':{'$ref':'#/$defs/11'}}}
    with pytest.raises(ContractError):
        schema(json.dumps(definition),strict=False)


def test_unknown_tool_output_rejects_with_usage():
    result=LLMResult(Message('assistant',calls=(ToolCall('new','unknown','{}'),)),Usage(3,2),'tool_calls')
    with pytest.raises(ProviderAccessError) as raised:
        validate_result(request(),result)
    assert raised.value.usage == Usage(3,2)


def test_invalid_unicode_text_request_rejects_before_encoding():
    original=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('\ud800'),)),)))
    with pytest.raises(ContractError):
        request_to_wire(original)


def test_catalog_cache_lifetime_wire_field_requires_a_list():
    row=catalog_to_wire((AvailableSelection('synthetic'),))[0]
    with pytest.raises(ContractError):
        catalog_from_wire([row|{'cache_boundary_lifetimes':()}])


@pytest.mark.parametrize('kind,operation',[('llm','complete'),('llm','stream'),('llm','describe'),('api','execute')])
def test_loaded_provider_views_only_wrap_llm_generation(kind,operation):
    from types import SimpleNamespace
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.providers.loading import LoadedProvider, _StreamView
    provider=SimpleNamespace(capabilities=ProviderCapabilities(kind,(operation,)))
    selected=LoadedProvider(provider,'synthetic').require(kind,operation)
    assert isinstance(selected,_StreamView) == (kind == 'llm')


def test_async_validators_preserve_replay_schema_and_reject_invalid_actual_bounds():
    import asyncio
    from dataclasses import replace
    from api_agent.providers.inference import async_request_to_wire, async_request_from_wire
    from api_agent.providers.rich_inference import async_valid_schema, async_validate_result
    async def scenario():
        original=request()
        row=await async_request_to_wire(original)
        assert await async_request_from_wire(row) == original
        strict=schema(json.dumps({'type':'object','properties':{},'required':[],'additionalProperties':False}))
        assert await async_valid_schema(strict,{})
        assert not await async_valid_schema({'$ref':'https://example.invalid/schema'},{})
        oversized=LLMResult(Message('assistant',(TextContent('x'*524289),)),Usage(9,3))
        plain=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),)))
        with pytest.raises(ProviderAccessError) as raised:
            await async_validate_result(plain,oversized)
        assert raised.value.usage == Usage(9,3)
        messages=list(original.payload.messages)
        messages[1]=Message('assistant',calls=(ToolCall('call','lookup','{"count":"bad"}'),))
        invalid=replace(original,payload=replace(original.payload,messages=tuple(messages)))
        with pytest.raises(ContractError):
            await async_request_to_wire(invalid)
    asyncio.run(scenario())


def test_local_json_pointer_array_indexes_preserve_schema_semantics():
    from api_agent.providers.rich_inference import schema,valid_schema
    definition={'type':'object','allOf':[{'properties':{'x':{'type':'integer'}}}],'properties':{'y':{'$ref':'#/allOf/0/properties/x'}}}
    checked=schema(json.dumps(definition),strict=False)
    assert valid_schema(checked,{'y':1})
    assert not valid_schema(checked,{'y':'bad'})
    for token in ('01','-1','x','99','١'):
        definition['properties']['y']['$ref']='#/allOf/'+token+'/properties/x'
        with pytest.raises(ContractError):
            schema(json.dumps(definition),strict=False)


@pytest.mark.parametrize('kind',['complete','stream'])
@pytest.mark.parametrize('catalog',[None,(),(AvailableSelection('other',features=('max_output_tokens',),max_output_tokens=10),),(AvailableSelection('synthetic',features=('tools','structured','temperature','max_output_tokens'),max_output_tokens=8),)])
def test_ordinary_provider_catalog_limits_reject_before_generation(kind,catalog):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock,Mock
    from api_agent.providers.loading import _StreamView
    provider=SimpleNamespace(acomplete=AsyncMock(),astream=Mock())
    if catalog is not None:
        provider.adescribe=AsyncMock(return_value=catalog)
    async def scenario():
        view=_StreamView(joined_test_provider(provider))
        with pytest.raises(OperationalError,match='unsupported'):
            await view.acomplete(request()) if kind == 'complete' else await anext(view.astream(request()))
        provider.acomplete.assert_not_awaited()
        provider.astream.assert_not_called()
    asyncio.run(scenario())


def test_async_malformed_structured_output_preserves_usage():
    import asyncio
    from api_agent.providers.rich_inference import async_validate_result
    result=LLMResult(Message('assistant',(TextContent('{'),)),Usage(3,2))
    with pytest.raises(ProviderAccessError) as raised:
        asyncio.run(async_validate_result(request(),result))
    assert raised.value.usage == Usage(3,2)


def test_loaded_owner_retains_retryable_cancellation_resistant_generation():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import LoadedProvider,ProviderError
    from api_agent.providers.contracts import ProviderCapabilities
    plain=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),)))
    result=LLMResult(Message('assistant',(TextContent('Answer'),)))
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        async def complete(request,*,auth):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return result
        provider=SimpleNamespace(capabilities=ProviderCapabilities('llm',('complete',)),acomplete=complete,aclose=AsyncMock())
        owner=LoadedProvider(joined_test_provider(provider),'synthetic-retained-owner',timeout=.02)
        view=owner.require('llm','complete')
        task=asyncio.create_task(view.acomplete(plain))
        await entered.wait()
        with pytest.raises(ProviderError,match='cleanup_pending') as raised:
            await owner.aclose()
        assert raised.value.retained_owners == (owner,)
        assert not owner.disposed
        release.set()
        with pytest.raises(ProviderError,match='closed'):
            await task
        await owner.aclose()
        assert owner.disposed
        provider.aclose.assert_awaited_once()
        with pytest.raises(ProviderError,match='closed'):
            await view.acomplete(plain)
        with pytest.raises(ProviderError,match='closed'):
            view.astream(plain)
    asyncio.run(scenario())


def test_loaded_owner_retains_stream_close_failure_and_retries():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import LoadedProvider,ProviderError
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.providers.llm import StreamEvent
    plain=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),)))
    async def scenario():
        class Iterator:
            def __aiter__(self): return self
            async def __anext__(self): return StreamEvent(TextContent('partial'))
            aclose=AsyncMock(side_effect=[RuntimeError('synthetic close failure'),None])
        iterator=Iterator()
        provider=SimpleNamespace(capabilities=ProviderCapabilities('llm',('stream',)),astream=lambda *args,**kwargs:iterator,aclose=AsyncMock())
        owner=LoadedProvider(joined_test_provider(provider),'synthetic-stream-retention')
        stream=owner.require('llm','stream').astream(plain)
        assert (await anext(stream)).kind == 'text'
        with pytest.raises(ProviderError,match='cleanup_pending') as raised:
            await owner.aclose()
        assert raised.value.retained_owners == (owner,)
        await owner.aclose()
        assert owner.disposed
        assert iterator.aclose.await_count == 2
    asyncio.run(scenario())


def test_async_valid_tool_output_and_closed_unstarted_stream():
    import asyncio
    from types import SimpleNamespace
    from api_agent.providers.rich_inference import async_validate_result
    from api_agent.providers.loading import _StreamView
    async def scenario():
        original=request()
        result=LLMResult(Message('assistant',calls=(ToolCall('new','lookup','{}'),)),outcome='tool_calls')
        assert await async_validate_result(original,result) == result
        stream=_StreamView(SimpleNamespace()).astream(original)
        await stream.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    asyncio.run(scenario())


def test_shared_plain_legacy_request_and_missing_explicit_maximum_catalog():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.sessions import LLMSessionOwner
    plain=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),)))
    result=LLMResult(Message('assistant',(TextContent('Answer'),)))
    async def scenario():
        provider=SimpleNamespace(asetup=AsyncMock(return_value=AsyncMock(return_value='synthetic-secret')))
        complete=AsyncMock(return_value=result)
        owner=SharedSetupOwner('operation','identity',lambda credentials:LLMSessionOwner(provider),complete=complete)
        try:
            assert await owner.complete(plain) == result
            with pytest.raises(OperationalError,match='unsupported'):
                await owner.complete(request())
            complete.assert_awaited_once()
        finally:
            await owner.aclose()
    asyncio.run(scenario())


def test_loaded_stream_cleanup_cancellation_can_be_retried():
    import asyncio
    from types import SimpleNamespace
    from api_agent.providers.loading import LoadedProvider
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.providers.llm import StreamEvent
    plain=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),)))
    async def scenario():
        entered,release=asyncio.Event(),asyncio.Event()
        class Iterator:
            def __aiter__(self): return self
            async def __anext__(self): return StreamEvent(TextContent('partial'))
            async def aclose(self):
                entered.set()
                await release.wait()
        owner=LoadedProvider(joined_test_provider(SimpleNamespace(capabilities=ProviderCapabilities('llm',('stream',)),astream=lambda *args,**kwargs:Iterator())),'synthetic-close-cancellation')
        stream=owner.require('llm','stream').astream(plain)
        await anext(stream)
        assert aiter(stream.iterator) is stream.iterator
        close=asyncio.create_task(owner.aclose())
        await entered.wait()
        owner._closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close
        release.set()
        await owner.aclose()
        assert owner.disposed
    asyncio.run(scenario())


@pytest.mark.parametrize('consumer',['loaded_complete','loaded_stream','shared'])
@pytest.mark.parametrize('support',['absent','short','full','unknown'])
@pytest.mark.parametrize('applied',[(),(0,)])
def test_actual_cache_claims_require_admitted_feature_and_lifetime(consumer,support,applied):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import LoadedProvider
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.sessions import LLMSessionOwner
    from api_agent.providers.contracts import ProviderCapabilities
    from api_agent.providers.llm import StreamEvent
    original=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Unchanged prompt'),),cache_boundary=CacheBoundary(3600)),)))
    selection=AvailableSelection('synthetic',features=() if support == 'absent' else ('cache_boundaries',),cache_boundary_lifetimes=() if support == 'absent' else (300,) if support == 'short' else (300,3600))
    result=LLMResult(Message('assistant',(TextContent('Answer'),)),Usage(11,3,0),applied_cache_boundaries=applied)
    async def scenario():
        sent=[]
        async def events(request,*,auth):
            sent.append(request)
            yield StreamEvent(result,'completed')
        provider=SimpleNamespace(capabilities=ProviderCapabilities('llm',('complete','stream')),acomplete=AsyncMock(return_value=result),astream=events,aclose=AsyncMock(),asetup=AsyncMock(return_value=AsyncMock(return_value='synthetic-secret')))
        if support != 'unknown':
            provider.adescribe=AsyncMock(return_value=(selection,))
        if consumer == 'shared':
            owner=SharedSetupOwner('operation','identity',lambda credentials:LLMSessionOwner(provider),complete=provider.acomplete,describe=getattr(provider,'adescribe',None))
            operation=owner.complete(original)
        else:
            owner=LoadedProvider(joined_test_provider(provider),'synthetic-cache-owner')
            view=owner.require('llm','stream' if consumer == 'loaded_stream' else 'complete')
            operation=anext(view.astream(original)) if consumer == 'loaded_stream' else view.acomplete(original)
        try:
            if applied and support != 'full':
                with pytest.raises(ProviderAccessError) as raised:
                    await operation
                assert raised.value.usage == result.usage
            else:
                observed=await operation
                assert (observed.payload if consumer == 'loaded_stream' else observed) == result
            assert (sent if consumer == 'loaded_stream' else [call.args[0] for call in provider.acomplete.await_args_list]) == [original]
            if support != 'unknown':
                provider.adescribe.assert_awaited_once()
        finally:
            await owner.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure',[OperationalError('unsupported'),OperationalError('unavailable'),OperationalError('authentication'),ContractError('malformed metadata')])
def test_optional_cache_catalog_unavailability_preserves_primary_failures(failure):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import _StreamView
    original=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Unchanged prompt'),),cache_boundary=CacheBoundary()),)))
    result=LLMResult(Message('assistant',(TextContent('Answer'),)))
    provider=SimpleNamespace(adescribe=AsyncMock(side_effect=failure),acomplete=AsyncMock(return_value=result))
    async def scenario():
        view=_StreamView(joined_test_provider(provider))
        if isinstance(failure,OperationalError) and failure.kind in ('unsupported','unavailable'):
            assert await view.acomplete(original) == result
        else:
            with pytest.raises(type(failure)) as raised:
                await view.acomplete(original)
            assert raised.value is failure
            provider.acomplete.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize('maximum',[None,16])
def test_missing_optional_catalog_capability_is_ignored_only_without_explicit_maximum(maximum):
    import asyncio
    from api_agent.providers.rich_inference import resolve_catalog_selection
    from api_agent.providers.contracts import SetupPrerequisiteError
    original=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),),generation=GenerationOptions(maximum)))
    error=SetupPrerequisiteError('Synthetic catalog unavailable')
    async def unavailable(): raise error
    if maximum is None:
        assert asyncio.run(resolve_catalog_selection(unavailable,original)) is None
    else:
        with pytest.raises(SetupPrerequisiteError) as raised:
            asyncio.run(resolve_catalog_selection(unavailable,original))
        assert raised.value is error


@pytest.mark.parametrize('kind',['loaded','shared'])
def test_cache_catalog_cancellation_preserves_failure_and_owner_cleanup(kind):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.loading import LoadedProvider
    from api_agent.providers.shared_setup import SharedSetupOwner
    from api_agent.providers.sessions import LLMSessionOwner
    from api_agent.providers.contracts import ProviderCapabilities
    original=LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Question'),),cache_boundary=CacheBoundary()),)))
    async def scenario():
        entered,stopped=asyncio.Event(),asyncio.Event()
        async def describe(*,auth):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        provider=SimpleNamespace(capabilities=ProviderCapabilities('llm',('complete',)),adescribe=describe,acomplete=AsyncMock(),aclose=AsyncMock(),asetup=AsyncMock(return_value=AsyncMock(return_value='synthetic-secret')))
        if kind == 'shared':
            owner=SharedSetupOwner('operation','identity',lambda credentials:LLMSessionOwner(provider),complete=provider.acomplete,describe=describe)
            operation=owner.complete(original)
        else:
            owner=LoadedProvider(joined_test_provider(provider),'synthetic-catalog-cancellation')
            operation=owner.require('llm','complete').acomplete(original)
        task=asyncio.create_task(operation)
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await owner.aclose()
        assert stopped.is_set()
        provider.acomplete.assert_not_awaited()
    asyncio.run(scenario())
