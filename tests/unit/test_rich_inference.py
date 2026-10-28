"""Strict rich IPC contracts and synthetic native HTTP results."""
import copy
import json
import asyncio
import httpx
import pytest
from api_agent.providers import rich_inference as wire
from api_agent.providers.inference import request_to_wire, request_from_wire, result_to_wire, result_from_wire
from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, LLMResult, Message, TextContent, Tool, ToolCall, ToolResult, StructuredOutput, GenerationOptions, Usage
from api_agent.llm_providers.gpt import rich, inference

SCHEMA = json.dumps(dict(type='object', properties={'value': {'type': 'string'}}, required=['value'], additionalProperties=False))
TOKEN = 'synthetic-auth-secret'


def request(history=False, structured=False):
    messages = (Message('user', (TextContent('Question'),)),)
    if history:
        messages += (Message('assistant', calls=(ToolCall('call-1', 'lookup', '{"value":"x"}'),)), Message('tool', results=(ToolResult('call-1', (TextContent('answer'),)),)))
    return LLMRequest('gpt-synthetic', CompletionRequest(messages, (Tool('lookup', 'Lookup value', SCHEMA),), GenerationOptions(), StructuredOutput('answer', SCHEMA) if structured else None))


def test_roundtrip_preserves_history_native_definitions_usage_and_metadata():
    req = request(history=True, structured=True)
    assert request_from_wire(request_to_wire(req)) == req
    result = LLMResult(Message('assistant', (TextContent('Working'),), (ToolCall('call-2', 'lookup', '{"value":"x"}'),)), Usage(10, 4, 2, 1, 3), 'tool_calls', 'gpt-synthetic', 'default', 'revision')
    assert result_from_wire(result_to_wire(result)) == result
    for mode, subscription in [('responses', False), ('responses', True), ('completions', False)]:
        body = rich.payload(req, mode, subscription)
        assert body['parallel_tool_calls'] is False
        assert body['tools'][0]['type'] == 'function'
        assert (body['tools'][0] if mode == 'responses' else body['tools'][0]['function'])['strict'] is True
        assert body['stream'] is subscription
        assert ('text' in body) if mode == 'responses' else ('response_format' in body)


@pytest.mark.parametrize('mode', ['completions', 'responses'])
def test_openrouter_tool_requests_omit_parallel_control(mode):
    _, body = inference.validate_text_request(base_url='https://openrouter.ai/api/v1/',
        access_token=TOKEN, subscription=False, api_mode=mode, request=request())
    assert len(body['tools']) == 1
    assert 'parallel_tool_calls' not in body


@pytest.mark.parametrize('base,subscription,mode', [
    ('https://api.openai.com/v1', False, 'completions'),
    ('https://api.openai.com/v1', False, 'responses'),
    ('https://api.openai.com/v1', True, 'responses'),
    ('https://example.com/v1', False, 'completions'),
    ('https://openrouter.ai/other', False, 'completions'),
])
def test_other_routes_retain_serial_tool_request(base, subscription, mode):
    _, body = inference.validate_text_request(base_url=base, access_token=TOKEN,
        subscription=subscription, api_mode=mode, request=request())
    assert body['parallel_tool_calls'] is False


def test_each_returned_tool_call_is_validated_before_delivery():
    data = native(False)
    data['choices'][0]['message']['tool_calls'].append(dict(id='call-2', type='function',
        function=dict(name='lookup', arguments='{"value":4}')))
    result = rich.tool_result(data, False, TOKEN)
    with pytest.raises(ProviderAccessError, match='outside its declared contract'):
        rich.validate_result(request(), result)


def test_native_tool_call_batch_is_bounded():
    data = native(False)
    call = data['choices'][0]['message']['tool_calls'][0]
    data['choices'][0]['message']['tool_calls'] = [{**call, 'id': f'call-{i}'} for i in range(64)]
    assert len(rich.tool_result(data, False, TOKEN).payload.calls) == 64
    data['choices'][0]['message']['tool_calls'].append({**call, 'id': 'call-64'})
    with pytest.raises(ProviderAccessError, match='invalid function calls'):
        rich.tool_result(data, False, TOKEN)


def test_native_tool_call_limit_is_checked_before_wire_validation(monkeypatch):
    data = native(False)
    call = data['choices'][0]['message']['tool_calls'][0]
    data['choices'][0]['message']['tool_calls'] = [{**call, 'id': f'call-{i}'} for i in range(65)]
    monkeypatch.setattr(rich, 'result_to_wire', lambda result: None)
    with pytest.raises(ProviderAccessError, match='invalid function calls'):
        rich.tool_result(data, False, TOKEN)


@pytest.mark.parametrize('source', ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '[]', '{', 'null', '{"x":1e999}', 'x'*32769, 4])
def test_object_json_rejects_ambiguous_nonfinite_or_unbounded_values(source):
    with pytest.raises(ContractError):
        wire.object_json(source)


@pytest.mark.parametrize('definition', [None, [], {}, {'type':'string'}, {'type':'null'}, {'type':'object','properties':{},'required':[]}, {'type':'object','properties':{},'required':[], 'additionalProperties':False, '$ref':'x'}, {'type':'object','properties':{'x':{'type':'object'}},'required':['x'],'additionalProperties':False}, {'type':'object','properties':{'x':{'type':'array'}},'required':['x'],'additionalProperties':False}, {'type':'object','properties':{},'required':[],'additionalProperties':False,'anyOf':[{}]}])
def test_schema_rejects_native_unsupported_definitions(definition):
    with pytest.raises(ContractError):
        wire.schema(json.dumps(definition))


def test_nested_nullable_array_schema_is_strict_and_valid():
    child = dict(type=['object','null'], properties={}, required=[], additionalProperties=False)
    definition = dict(type='object', properties={'items':dict(type='array',items=child)},required=['items'],additionalProperties=False)
    assert wire.schema(json.dumps(definition)) == definition


@pytest.mark.parametrize('change', [
    lambda row: row.update(version=True), lambda row: row.update(messages=[]), lambda row: row.update(tools=[]),
    lambda row: row.update(max_output_tokens=True), lambda row: row.update(cache_policy=[]), lambda row: row.update(selected_llm='bad\n'),
    lambda row: row.update(request_id='bad\n'), lambda row: row.update(extra=1),
    lambda row: row['tools'][0].update(name='invalid.name'), lambda row: row['tools'].append(copy.deepcopy(row['tools'][0])),
    lambda row: row['messages'][0].update(role='tool'), lambda row: row['messages'][0].update(content=[]),
    lambda row: row['messages'][0].update(content=[1]), lambda row: row['messages'][0].update(calls={}),
    lambda row: row['messages'][1]['calls'][0].update(id=''), lambda row: row['messages'][1]['calls'][0].update(name='other'),
    lambda row: row['messages'][1]['calls'][0].update(arguments='{"value":4}'),
    lambda row: row['messages'][1]['calls'][0].update(arguments='{"value":"x","value":"y"}'),
    lambda row: row['messages'][1]['calls'].append(copy.deepcopy(row['messages'][1]['calls'][0])),
    lambda row: row['messages'][2]['results'][0].update(id='other'), lambda row: row['messages'][2].update(content=['x']),
    lambda row: row['messages'][2]['results'][0].update(content={}), lambda row: row['messages'].pop(),
    lambda row: row['messages'].reverse(), lambda row: row.update(structured={'name':'bad.name','schema':SCHEMA}),
])
def test_request_rejects_invalid_fields_and_tool_order(change):
    row = request_to_wire(request(history=True))
    change(row)
    with pytest.raises(ContractError):
        request_from_wire(row)


@pytest.mark.parametrize('change', [lambda row: row.update(version=1), lambda row: row.update(outcome='completed'), lambda row: row['message'].update(calls=[]), lambda row: row['message']['calls'].append(copy.deepcopy(row['message']['calls'][0])), lambda row: row.update(usage={}), lambda row: row.update(actual_model='bad\n')])
def test_result_rejects_invalid_native_tool_result(change):
    row = result_to_wire(LLMResult(Message('assistant', calls=(ToolCall('call-1', 'lookup', '{"value":"x"}'),)), outcome='tool_calls'))
    change(row)
    with pytest.raises(ContractError):
        result_from_wire(row)


def native(responses):
    call = dict(id='item-1', type='function_call', status='completed',call_id='call-1',name='lookup',arguments='{"value":"x"}')
    return dict(status='completed',output=[call],usage={'input_tokens':5,'output_tokens':3}) if responses else dict(choices=[dict(finish_reason='tool_calls',message=dict(role='assistant',content=None,tool_calls=[dict(id='call-1',type='function',function=dict(name='lookup',arguments='{"value":"x"}'))]))],usage={'prompt_tokens':5,'completion_tokens':3})


@pytest.mark.parametrize('mode,subscription',[('responses',False),('responses',True),('completions',False)])
def test_controlled_http_join_maps_native_calls_and_authentication(mode, subscription):
    data = native(mode == 'responses')
    sent=[]
    async def execute():
        def transport(req):
            sent.append(req)
            if subscription:
                return httpx.Response(200,text='data: '+json.dumps(dict(type='response.completed',response=data))+'\n\n',headers={'content-type':'text/event-stream'})
            return httpx.Response(200,json=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await inference.complete_text(client=client,base_url='https://api.openai.com/v1',access_token=TOKEN,subscription=subscription,api_mode=mode,request=request())
    result=asyncio.run(execute())
    assert result.payload.calls == (ToolCall('call-1','lookup','{"value":"x"}'),)
    assert result.usage == Usage(5,3)
    assert sent[0].headers['authorization'] == 'Bearer '+TOKEN
    assert TOKEN not in sent[0].content.decode()


@pytest.mark.parametrize('responses',[False,True])
def test_native_calls_validate_declared_schema_and_preserve_usage_on_rejection(responses):
    result=rich.tool_result(native(responses),responses,TOKEN)
    bad = LLMResult(Message('assistant', calls=(ToolCall('call-1','unknown','{}'),)),result.usage,'tool_calls')
    with pytest.raises(ProviderAccessError) as caught:
        rich.validate_result(request(),bad)
    assert caught.value.usage == Usage(5,3)


@pytest.mark.parametrize('data',[None,{}, {'status':'incomplete','output':[]}, {'status':'completed','output':[dict(type='function_call',status='completed',call_id='call-1',name='lookup',arguments='[]')]}, {'status':'completed','output':[dict(type='function_call',status='completed',call_id='call-1',name='lookup',arguments='{}'),dict(type='message',role='assistant',content=[dict(type='refusal',refusal='No')])]}, {'status':'completed','output':[dict(type='unknown')]}, {'status':'completed','output':[dict(type='reasoning')]}, {'status':'completed','output':[dict(type='function_call',status='completed',call_id='call-1',name='lookup',arguments='{}')], 'model':TOKEN}])
def test_native_calls_reject_invalid_output_and_authentication_content(data):
    with pytest.raises(ProviderAccessError):
        rich.tool_result(data,True,TOKEN)


@pytest.mark.parametrize('selected,maximum,temperature', [('other-model',None,None),('gpt-synthetic',1,None)])
def test_rich_payload_rejects_unsupported_selection_options(selected,maximum,temperature):
    original=request()
    req=LLMRequest(selected,CompletionRequest(original.payload.messages,original.payload.tools,GenerationOptions(maximum,temperature)))
    with pytest.raises(ProviderAccessError):
        rich.payload(req,'responses',True)


def test_request_requires_tool_result_before_next_user_message():
    row=request_to_wire(request(history=True))
    row['messages'].insert(2,dict(role='user',content=['next'],calls=[],results=[]))
    with pytest.raises(ContractError):
        request_from_wire(row)


@pytest.mark.parametrize('value',[{'x':'a'*2097153}, {'x':object()}, {'x':'\ud800'}])
def test_bounded_json_rejects_size_encoding_and_non_json_objects(value):
    with pytest.raises(ContractError):
        wire._bounded(value)


def test_rich_result_requires_tool_outcome():
    with pytest.raises(ContractError):
        wire.result_to_wire(LLMResult(Message('assistant',(TextContent('ok'),))))


def test_structured_only_native_mapping_and_result_validation():
    req=LLMRequest('gpt-synthetic',CompletionRequest(request().payload.messages,structured=StructuredOutput('answer',SCHEMA)))
    assert 'tools' not in rich.payload(req,'responses',False)
    valid=LLMResult(Message('assistant',(TextContent('{"value":"x"}'),)),Usage(2,1))
    assert rich.validate_result(req,valid) is valid
    for text in ['{}','[]','{"value":4}']:
        with pytest.raises(ProviderAccessError) as caught:
            rich.validate_result(req,LLMResult(Message('assistant',(TextContent(text),)),valid.usage))
        assert caught.value.usage == valid.usage
    refusal=LLMResult(Message('assistant',(TextContent('No'),)),outcome='refused')
    assert rich.validate_result(req,refusal) is refusal


@pytest.mark.parametrize('responses',[True,False])
def test_native_tool_calls_accept_assistant_text_and_reasoning(responses):
    data=native(responses)
    if responses:
        data['output'] += [dict(type='reasoning',id='reason-1',summary=[],encrypted_content='opaque-synthetic-encrypted-state'),dict(type='message',id='message-1',role='assistant',content=[dict(type='output_text',text='Working')])]
    else:
        data['choices'][0]['message']['content']='Working'
    assert rich.tool_result(data,responses,TOKEN).payload.content[0].text == 'Working'


@pytest.mark.parametrize('change', [lambda row: row['choices'][0].update(finish_reason='stop'),lambda row:row['choices'][0]['message'].update(refusal='No'),lambda row:row['choices'][0]['message']['tool_calls'][0].update(type='bad'), lambda row:row['choices'][0]['message']['tool_calls'].append(copy.deepcopy(row['choices'][0]['message']['tool_calls'][0]))])
def test_chat_tool_result_rejects_finish_refusal_type_and_duplicate_calls(change):
    data=native(False)
    change(data)
    with pytest.raises(ProviderAccessError):
        rich.tool_result(data,False,TOKEN)


def test_responses_rejects_unfinished_tool_call():
    data=native(True)
    data['output'][0]['status']='in_progress'
    with pytest.raises(ProviderAccessError):
        rich.tool_result(data,True,TOKEN)


def test_strict_schema_rejects_excessive_nesting():
    child={'type':'string'}
    for _ in range(18):
        child=dict(type='object',properties={'child':child},required=['child'],additionalProperties=False)
    with pytest.raises(ContractError):
        wire.schema(json.dumps(child))


def streamed_calls():
    data=native(True)
    call=data['output'][0]
    return [dict(type='response.output_item.added',output_index=0,item=dict(call,arguments='',status='in_progress')),
            dict(type='response.function_call_arguments.delta',output_index=0,item_id=call['id'],delta='{"value":'),
            dict(type='response.function_call_arguments.delta',output_index=0,item_id=call['id'],delta='"x"}'),
            dict(type='response.function_call_arguments.done',output_index=0,item_id=call['id'],arguments=call['arguments']),
            dict(type='response.output_item.done',output_index=0,item=call),
            dict(type='response.completed',response=data)]


def test_subscription_stream_correlates_function_identity_and_arguments():
    events=streamed_calls()
    raw=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
    assert inference._sse(raw,TOKEN,None).payload.calls[0].arguments == '{"value":"x"}'


@pytest.mark.parametrize('change',[lambda events:events[0]['item'].update(name=None),lambda events:events[0]['item'].update(arguments='{}'),lambda events:events[3].update(arguments='{}'),lambda events:events[4]['item'].update(name='other'),lambda events:events[4]['item'].update(arguments='{}'),lambda events:events[1].update(delta=1),lambda events:events.insert(4,copy.deepcopy(events[3])),lambda events:events[0]['item'].update(type='message')])
def test_subscription_stream_rejects_conflicting_function_arguments(change):
    events=copy.deepcopy(streamed_calls())
    change(events)
    raw=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
    with pytest.raises(ProviderAccessError):
        inference._sse(raw,TOKEN,None)


@pytest.mark.parametrize('text',[TOKEN, TOKEN[:10]+'\n'+TOKEN[10:], ''.join('\\u%04x'%ord(c) for c in TOKEN)])
def test_native_tool_arguments_reject_decoded_or_control_split_authentication(text):
    data=native(True)
    data['output'][0]['arguments']='{"value":"'+text.replace('\n','\\n')+'"}'
    with pytest.raises(ProviderAccessError):
        rich.tool_result(data,True,TOKEN)


def test_structured_result_rejects_encoded_authentication_with_usage():
    req=LLMRequest('gpt-synthetic',CompletionRequest(request().payload.messages,structured=StructuredOutput('answer',SCHEMA)))
    encoded=''.join('\\u%04x'%ord(c) for c in TOKEN)
    result=LLMResult(Message('assistant',(TextContent('{"value":"'+encoded+'"}'),)),Usage(4,3))
    with pytest.raises(ProviderAccessError) as caught:
        rich.validate_result(req,result,TOKEN)
    assert caught.value.usage == Usage(4,3)
    safe=LLMResult(Message('assistant',(TextContent('{"value":"safe"}'),)))
    assert rich.validate_result(req,safe,TOKEN) is safe


def test_privacy_recurses_through_arrays_keys_and_numeric_values():
    assert rich._protected([{'safe':[2,None,False]}, {'key':TOKEN}],TOKEN)
    assert rich._protected({TOKEN:'safe'},TOKEN)
    assert not rich._protected([{'safe':[2,None,False]}],TOKEN)


@pytest.mark.parametrize('reason',['max_output_tokens','content_filter'])
def test_responses_incomplete_length_keeps_safe_partial_text_and_usage(reason):
    data=dict(status='incomplete',incomplete_details={'reason':reason},output=[dict(type='message',role='assistant',content=[dict(type='output_text',text='partial')])],usage={'input_tokens':7,'output_tokens':2})
    if reason=='max_output_tokens':
        result=inference._result(data,True,TOKEN)
        assert result.outcome == 'length'
        assert result.payload.content[0].text == 'partial'
        assert result.usage == Usage(7,2)
    else:
        with pytest.raises(ProviderAccessError) as caught:
            inference._result(data,True,TOKEN)
        assert caught.value.usage == Usage(7,2)


def test_subscription_output_limit_is_length_outcome_with_partial_text_usage():
    response=dict(status='incomplete',incomplete_details={'reason':'max_output_tokens'},output=[dict(type='message',status='incomplete',role='assistant',content=[dict(type='output_text',text='partial')])],usage={'input_tokens':3,'output_tokens':2})
    raw=('data: '+json.dumps(dict(type='response.incomplete',response=response))+'\n\n').encode()
    result=inference._sse(raw,TOKEN,None)
    assert result.outcome == 'length'
    assert result.usage == Usage(3,2)


def test_strict_schema_rejects_valid_json_schema_composition():
    definition=json.loads(SCHEMA)
    definition['anyOf']=[{}]
    with pytest.raises(ContractError):
        wire.schema(json.dumps(definition))


def test_native_known_tool_rejects_schema_invalid_arguments():
    result=LLMResult(Message('assistant',calls=(ToolCall('call-1','lookup','{"value":4}'),)),Usage(3,2),'tool_calls')
    with pytest.raises(ProviderAccessError) as caught:
        rich.validate_result(request(),result)
    assert caught.value.usage == Usage(3,2)


def test_stream_argument_done_rejects_delta_conflict_when_terminal_agrees():
    events=copy.deepcopy(streamed_calls())
    events[3]['arguments']='{}'
    events[4]['item']['arguments']='{}'
    events[5]['response']['output'][0]['arguments']='{}'
    raw=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
    with pytest.raises(ProviderAccessError,match='changed function arguments'):
        inference._sse(raw,TOKEN,None)


def test_length_rejects_an_output_item_with_inconsistent_status():
    data=dict(status='incomplete',incomplete_details={'reason':'max_output_tokens'},output=[dict(type='message',status='in_progress',role='assistant',content=[dict(type='output_text',text='partial')])])
    with pytest.raises(ProviderAccessError,match='unfinished'):
        inference._result(data,True,TOKEN)


def test_subscription_length_terminal_cannot_change_response_identity():
    events=[dict(type='response.created',response={'id':'first'}),dict(type='response.incomplete',response=dict(id='other',status='incomplete',incomplete_details={'reason':'max_output_tokens'},output=[]))]
    raw=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
    with pytest.raises(ProviderAccessError,match='identity changed'):
        inference._sse(raw,TOKEN,None)


@pytest.mark.parametrize('field',['model','service_tier','serving_revision'])
def test_native_text_metadata_rejects_authentication_identity_with_usage(field):
    data=dict(status='completed',output=[dict(type='message',role='assistant',content=[dict(type='output_text',text='safe')])],usage={'input_tokens':3,'output_tokens':2})
    data[field]=TOKEN
    with pytest.raises(ProviderAccessError) as caught:
        inference._result(data,True,TOKEN)
    assert caught.value.usage == Usage(3,2)
    assert TOKEN not in str(caught.value)


def reasoning_native():
    data=native(True)
    data['output'].insert(0,dict(type='reasoning',id='reason-1',summary=[],encrypted_content='opaque-synthetic-encrypted-state'))
    return data


def test_responses_native_reasoning_roundtrip_replays_before_correlated_calls():
    data=reasoning_native()
    result=rich.tool_result(data,True,TOKEN)
    assert 'opaque-synthetic' not in repr(result.payload)
    recovered=result_from_wire(result_to_wire(result))
    from api_agent.providers.llm import ToolResult
    req=LLMRequest('gpt-synthetic',CompletionRequest(request().payload.messages+(recovered.payload,Message('tool',results=(ToolResult('call-1',(TextContent('Answer'),)),))),request().payload.tools))
    recovered_req=request_from_wire(request_to_wire(req))
    body=rich.payload(recovered_req,'responses',False)
    assert body['include']==['reasoning.encrypted_content'] and body['store'] is False
    assert body['input'][1:3]==data['output']
    assert body['input'][3]==dict(type='function_call_output',call_id='call-1',output='Answer')


@pytest.mark.parametrize('keyword',['dependentSchemas','unevaluatedProperties','contains','prefixItems','propertyNames','additionalItems','$defs','allOf'])
def test_schema_rejects_every_excluded_schema_valued_keyword_without_network(keyword,monkeypatch):
    import urllib.request
    calls=[]
    monkeypatch.setattr(urllib.request,'urlopen',lambda *args,**kwargs:calls.append(args))
    definition=json.loads(SCHEMA)
    definition[keyword]={'value':{'$ref':'https://example.invalid/private-schema'}} if keyword=='dependentSchemas' else {'$ref':'https://example.invalid/private-schema'}
    with pytest.raises(ContractError):
        wire.schema(json.dumps(definition))
    assert calls == []


def test_reference_keys_in_enum_instance_data_keep_their_ordinary_meaning():
    definition=json.loads(SCHEMA)
    definition['enum']=[{'nested':[{'$ref':'https://example.invalid/schema'}]}]
    assert wire.schema(json.dumps(definition)) == definition


def test_validation_defensively_refuses_remote_retrieval_even_without_schema_preflight(monkeypatch):
    import urllib.request
    calls=[]
    def network(*args,**kwargs):
        import io
        calls.append(args)
        return io.BytesIO(b'{}')
    monkeypatch.setattr(urllib.request,'urlopen',network)
    assert not wire.valid_schema({'$ref':'https://example.invalid/schema'}, {})
    assert calls==[]


@pytest.mark.parametrize('mode',['responses','completions'])
@pytest.mark.parametrize('cache',['medium','high'])
def test_openrouter_cached_tool_history_preflight_is_safe_and_never_keyerror(mode,cache):
    original=request(history=True)
    req=LLMRequest('openai/gpt-6-luna',CompletionRequest(original.payload.messages,original.payload.tools,GenerationOptions(cache_policy=cache)))
    if mode=='responses':
        with pytest.raises(ProviderAccessError) as caught:
            inference.validate_text_request(base_url='https://openrouter.ai/api/v1',access_token=TOKEN,subscription=False,api_mode=mode,request=req)
        assert caught.value.kind=='unsupported'
        assert 'nonempty final text' in str(caught.value)
        assert TOKEN not in str(caught.value)
    else:
        _,body=inference.validate_text_request(base_url='https://openrouter.ai/api/v1',access_token=TOKEN,subscription=False,api_mode=mode,request=req)
        assert body['messages'][-1]['content'][0]['text']=='answer'


@pytest.mark.parametrize('value',['{}','{"provider":"x","state":[]}','{"provider":"x","state":[1]}','{"provider":"x","state":{}}','{"provider":"x","state":[{}],"extra":1}'])
def test_opaque_continuation_rejects_malformed_envelopes(value):
    with pytest.raises(ContractError):
        wire.continuation(value)


def test_wire_continuation_rejects_non_assistant_role():
    row=wire.message_to_wire(rich.tool_result(reasoning_native(),True,TOKEN).payload)
    row['role']='user'
    with pytest.raises(ContractError,match='assistant'):
        wire.message_from_wire(row)


@pytest.mark.parametrize('mode,provider',[('completions','openai.responses'),('responses','other.provider')])
def test_provider_continuation_cannot_replay_on_another_protocol(mode,provider):
    from dataclasses import replace
    message=rich.tool_result(reasoning_native(),True,TOKEN).payload
    envelope=json.loads(message.continuation)
    envelope['provider']=provider
    message=replace(message,continuation=json.dumps(envelope))
    original=request(history=True)
    req=LLMRequest('gpt-synthetic',CompletionRequest((original.payload.messages[0],message,original.payload.messages[2]),original.payload.tools))
    with pytest.raises(ProviderAccessError) as caught:
        rich.payload(req,mode,False)
    assert caught.value.kind=='unsupported'


@pytest.mark.parametrize('change',[
    lambda items:items[0].update(encrypted_content=None),lambda items:items[0].update(encrypted_content=''),
    lambda items:items[0].update(encrypted_content='state\n'),lambda items:items[0].update(encrypted_content='x'*32769),
    lambda items:items[0].update(summary=[dict(type='summary_text',text='plaintext')]),
    lambda items:items[0].update(content=[dict(type='reasoning_text',text='hidden')]),
    lambda items:items[0].update(status='in_progress'),lambda items:items[0].update(id=items[1]['id']),
    lambda items:items[1].update(call_id=''),lambda items:items[1].update(arguments='[]'),
    lambda items:items.append(dict(type='other',id='other')),lambda items:items.append(None),
])
def test_native_continuation_rejects_plaintext_invalid_or_conflicting_state(change):
    items=reasoning_native()['output']
    change(items)
    with pytest.raises((ContractError,ValueError)):
        rich._native_state(items)


@pytest.mark.parametrize('items',[[],{},[dict(type='reasoning',id='reason-1',summary=[],encrypted_content='opaque')],[dict(type='function_call',id='item-1',call_id='call-1',name='lookup',arguments='{}')]])
def test_native_continuation_requires_bounded_reasoning_and_function_call_items(items):
    with pytest.raises(ContractError):
        rich._native_state(items)


def test_provider_continuation_must_match_public_assistant_calls_and_text():
    from dataclasses import replace
    original=request(history=True)
    message=rich.tool_result(reasoning_native(),True,TOKEN).payload
    envelope=json.loads(message.continuation)
    envelope['state'][1]['name']='other'
    message=replace(message,continuation=json.dumps(envelope))
    req=LLMRequest('gpt-synthetic',CompletionRequest((original.payload.messages[0],message,original.payload.messages[2]),original.payload.tools))
    with pytest.raises(ProviderAccessError) as caught:
        rich.payload(req,'responses',False)
    assert caught.value.kind=='invalid_request'


def test_continuation_rejects_credential_content_before_transport():
    from dataclasses import replace
    original=request(history=True)
    message=rich.tool_result(reasoning_native(),True,TOKEN).payload
    envelope=json.loads(message.continuation)
    envelope['state'][0]['encrypted_content']=TOKEN
    message=replace(message,continuation=json.dumps(envelope))
    req=LLMRequest('gpt-synthetic',CompletionRequest((original.payload.messages[0],message,original.payload.messages[2]),original.payload.tools))
    with pytest.raises(ProviderAccessError) as caught:
        inference.validate_text_request(base_url='https://api.openai.com/v1',access_token=TOKEN,subscription=False,api_mode='responses',request=req)
    assert caught.value.kind=='invalid_request'
    assert TOKEN not in str(caught.value)


def test_enabled_content_export_excludes_only_message_continuation():
    from api_agent.testing.inference_content import content_boundary,export_content
    from api_agent.providers.llm import ToolResult
    result=rich.tool_result(reasoning_native(),True,TOKEN)
    prompt=Message('user',(TextContent('Normal text with {"continuation":"user content"}'),))
    req=LLMRequest('gpt-synthetic',CompletionRequest((prompt,result.payload,Message('tool',results=(ToolResult('call-1',(TextContent('visible answer'),)),))),request().payload.tools))
    rows=export_content(content_boundary({'LOG_CONTENT':True}),req,result)
    serialized=json.dumps(rows)
    assert 'opaque-synthetic-encrypted-state' not in serialized
    assert 'encrypted_content' not in serialized
    assert 'Normal text' in serialized and 'visible answer' in serialized and 'user content' in serialized
    assert all('continuation' not in message for message in rows[0]['content']['messages'])
    assert 'continuation' not in rows[1]['content']['message']


@pytest.mark.parametrize('item',[
    dict(type='message',id='message-1',role='user',content=[]),
    dict(type='message',id='message-1',role='assistant',content={}),
    dict(type='message',id='message-1',role='assistant',content=[dict(type='refusal',refusal='no')]),
    dict(type='message',id='message-1',role='assistant',content=[dict(type='output_text',text='safe',annotations=[{}])]),
])
def test_native_replay_rejects_inconsistent_assistant_message_state(item):
    items=reasoning_native()['output']+[item]
    with pytest.raises(ContractError):
        rich._native_state(items)


def test_enabled_request_only_content_export_strips_opaque_continuation():
    from api_agent.testing.inference_content import content_boundary,export_content
    from api_agent.providers.llm import ToolResult
    result=rich.tool_result(reasoning_native(),True,TOKEN)
    req=LLMRequest('gpt-synthetic',CompletionRequest(request().payload.messages+(result.payload,Message('tool',results=(ToolResult('call-1',(TextContent('visible tool result'),)),))),request().payload.tools))
    rows=export_content(content_boundary({'LOG_CONTENT':True}),req)
    assert len(rows)==1
    assert 'opaque-synthetic' not in json.dumps(rows)
    assert 'visible tool result' in json.dumps(rows)


@pytest.mark.parametrize('value',[1,''])
def test_message_continuation_requires_a_nonempty_opaque_string(value):
    with pytest.raises(ValueError):
        Message('assistant',(TextContent('text'),),continuation=value)


def test_message_continuation_requires_assistant_role_and_is_not_in_repr():
    value=json.dumps(dict(provider='opaque.provider',state=[{}]))
    with pytest.raises(ValueError):
        Message('user',(TextContent('text'),),continuation=value)
    assert value not in repr(Message('assistant',(TextContent('text'),),continuation=value))


def test_strict_schema_rejects_dependent_schemas_even_without_references():
    definition=json.loads(SCHEMA)
    definition['dependentSchemas']={'value':{'type':'object'}}
    with pytest.raises(ContractError):
        wire.schema(json.dumps(definition))


def test_subscription_stream_rejects_terminal_encrypted_reasoning_changes():
    data=reasoning_native()
    added=copy.deepcopy(data['output'][0])
    terminal=copy.deepcopy(data)
    terminal['output'][0]['encrypted_content']='different-opaque-state'
    events=[dict(type='response.output_item.added',output_index=0,item=added),
            dict(type='response.output_item.done',output_index=0,item=copy.deepcopy(added)),
            dict(type='response.output_item.added',output_index=1,item=dict(data['output'][1],arguments='',status='in_progress')),
            dict(type='response.output_item.done',output_index=1,item=copy.deepcopy(data['output'][1])),
            dict(type='response.completed',response=terminal)]
    raw=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
    with pytest.raises(ProviderAccessError,match='conflicts'):
        inference._sse(raw,TOKEN,None)


@pytest.mark.parametrize('keyword', ['$ref', '$dynamicRef', '$recursiveRef'])
@pytest.mark.parametrize('operation', ['tool', 'structured'])
def test_reference_property_names_and_enum_data_survive_request_wire(keyword, operation):
    from jsonschema import Draft202012Validator
    instance = {keyword: 'literal instance value'}
    definition = {'type': 'object', 'properties': {keyword: {'type': 'string'}},
                  'required': [keyword], 'additionalProperties': False, 'enum': [instance],
                  'description': 'Ordinary schema annotation'}
    Draft202012Validator.check_schema(definition)
    assert Draft202012Validator(definition).is_valid(instance)
    schema_text = json.dumps(definition)
    messages = (Message('user', (TextContent('Question'),)),)
    payload = (CompletionRequest(messages, (Tool('lookup', 'Lookup', schema_text),))
               if operation == 'tool' else CompletionRequest(messages, structured=StructuredOutput('answer', schema_text)))
    selected = LLMRequest('gpt-synthetic', payload)
    recovered = request_from_wire(request_to_wire(selected))
    assert recovered == selected
    native = rich.payload(recovered, 'responses', False)
    actual = (native['tools'][0]['parameters'] if operation == 'tool'
              else native['text']['format']['schema'])
    assert actual == definition
    assert wire.valid_schema(actual, instance)


@pytest.mark.parametrize('keyword', ['$ref', '$dynamicRef', '$recursiveRef'])
@pytest.mark.parametrize('position', ['root', 'property', 'items'])
def test_actual_schema_references_reject_before_request_transport(keyword, position, monkeypatch):
    calls = []
    monkeypatch.setattr(wire, '_no_remote', lambda uri: calls.append(uri))
    referenced = {'type': 'string', keyword: 'https://example.invalid/schema'}
    definition = json.loads(SCHEMA)
    if position == 'root':
        definition[keyword] = 'https://example.invalid/schema'
    elif position == 'property':
        definition['properties']['value'] = referenced
    else:
        definition['properties']['value'] = {'type': 'array', 'items': referenced}
    selected = LLMRequest('gpt-synthetic', CompletionRequest(
        (Message('user', (TextContent('Question'),)),), structured=StructuredOutput('answer', json.dumps(definition))))
    with pytest.raises(ContractError):
        request_to_wire(selected)
    assert calls == []


@pytest.mark.parametrize('items', [True, False, None, []])
def test_non_object_array_item_schemas_remain_outside_strict_contract(items):
    definition = json.loads(SCHEMA)
    definition['properties']['value'] = {'type': 'array', 'items': items}
    with pytest.raises(ContractError):
        wire.schema(json.dumps(definition))
