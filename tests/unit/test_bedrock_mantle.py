from copy import deepcopy
import pytest
from api_agent.llm_providers.bedrock import mantle as m
from api_agent.providers.contracts import ProviderAccessError, ContractError
from api_agent.providers.llm import (LLMRequest, CompletionRequest, Message, TextContent, Tool, ToolCall, ToolResult,
    GenerationOptions, StructuredOutput, CacheBoundary)

MODEL = 'openai.gpt-oss-20b'
SCHEMA = '{"type":"object","properties":{},"required":[],"additionalProperties":false}'


def request(**changes):
    return LLMRequest(MODEL, CompletionRequest((Message('user', (TextContent('hello'),)),), **changes))


def usage(response=False):
    return dict(input_tokens=2,output_tokens=3,total_tokens=5) if response else dict(prompt_tokens=2,completion_tokens=3,total_tokens=5)


def completion():
    return {'model':MODEL,'choices':[{'index':0,'message':{'role':'assistant','content':'hi'},'finish_reason':'stop'}], 'usage':usage()}


def response(output=None):
    return {'id':'r','model':MODEL,'status':'completed','output': output or [], 'usage':usage(True)}


def message(text='hi'):
    return {'id':'msg','type':'message','role':'assistant','status':'completed', 'content':[{'type':'output_text','text':text,'annotations':[]}]}


def call(response=False):
    spec = {'name':'lookup','arguments':'{}'}
    return dict(type='function_call',id='fc',call_id='c',status='completed',**spec) if response else {'id':'c','type':'function','function':spec}


@pytest.mark.parametrize('mode', ['completions','responses'])
def test_encode_text_tools_and_history(mode):
    req = LLMRequest(MODEL, CompletionRequest((Message('system',(TextContent('system'),)),
        Message('assistant',calls=(ToolCall('c','lookup','{}'),)), Message('tool',results=(ToolResult('c',(TextContent('result'),)),)),
        Message('user',(TextContent('hello'),))), tools=(Tool('lookup','desc',SCHEMA),),
        generation=GenerationOptions(max_output_tokens=10,temperature=.5)))
    body = m.encode(req,mode)
    assert body['model']==MODEL and body['tools'][0]['type']=='function'
    assert body['temperature']==.5
    assert body['max_output_tokens' if mode=='responses' else 'max_tokens']==10
    if mode=='responses':
        assert body['store'] is False
        assert [row.get('type') for row in body['input']]==[None,'function_call','function_call_output',None]
    else:
        assert body['messages'][1]['tool_calls'][0]['id']=='c'
        assert body['messages'][2]['tool_call_id']=='c'


@pytest.mark.parametrize('mode',['completions','responses'])
def test_encode_minimal(mode):
    assert 'tools' not in m.encode(request(),mode)


@pytest.mark.parametrize('req', [None, request(structured=StructuredOutput('s',SCHEMA)),
    request(generation=GenerationOptions(cache_policy='off')),request(generation=GenerationOptions(reasoning_effort='off')),
    LLMRequest(MODEL,CompletionRequest((Message('assistant',continuation='opaque'),))),
    LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('a'),),cache_boundary=CacheBoundary()),)))])
def test_request_unsupported(req):
    with pytest.raises(ProviderAccessError):
        m.validate_request(req)


def test_request_common_contract_error(monkeypatch):
    def fail(request):
        raise ContractError('invalid')
    monkeypatch.setattr(m,'request_to_wire',fail)
    with pytest.raises(ProviderAccessError):
        m.validate_request(request())


@pytest.mark.parametrize('mode',['completions','responses'])
def test_decode_text_and_usage_subsets(mode):
    raw = completion() if mode=='completions' else response([message()])
    raw['usage'].update({'prompt_tokens_details' if mode=='completions' else 'input_tokens_details':{'cached_tokens':1},
                         'completion_tokens_details' if mode=='completions' else 'output_tokens_details':{'reasoning_tokens':2}})
    result=m.decode(raw,mode,selected_llm=MODEL)
    assert result.payload.content==(TextContent('hi'),)
    assert result.usage.cached_input_tokens==1 and result.usage.reasoning_tokens==2


@pytest.mark.parametrize('mode',['completions','responses'])
def test_decode_tools(mode):
    raw=completion() if mode=='completions' else response([call(True)])
    if mode=='completions':
        raw['choices'][0].update(message={'role':'assistant','content':None,'tool_calls':[call()]},finish_reason='tool_calls')
    result=m.decode(raw,mode,selected_llm=MODEL)
    assert result.payload.calls==(ToolCall('c','lookup','{}'),) and result.outcome=='tool_calls'


def test_response_incomplete():
    raw=response([message()])
    raw.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
    assert m.decode(raw,'responses',selected_llm=MODEL).outcome=='length'


@pytest.mark.parametrize('raw', [None, {}, {'error':{}}, dict(completion(),model='wrong'),
    dict(completion(),choices=[]),dict(completion(),usage=None),dict(completion(),usage=usage()|{'total_tokens':6}),
    dict(completion(),usage=usage()|{'prompt_tokens':True}),dict(completion(),usage=usage()|{'prompt_tokens_details':[]}),
    dict(completion(),usage=usage()|{'completion_tokens_details':{'reasoning_tokens':4}})])
def test_decode_bad_completion(raw):
    with pytest.raises(ProviderAccessError):
        m.decode(raw,'completions',selected_llm=MODEL)


@pytest.mark.parametrize('change', [dict(role='user'),dict(refusal='no'),dict(content=1),dict(content='\ud800'),
    dict(content='x'*(m._LIMIT+1)),dict(tool_calls=1),dict(tool_calls=[call(),call()]),
    dict(tool_calls=[{'id':'c','type':'bad'}]),dict(tool_calls=[call()|{'function':None}]),
    dict(tool_calls=[call()|{'function':{'name':'lookup','arguments':'[]'}}])])
def test_decode_bad_message(change):
    raw=completion()
    raw['choices'][0]['message'].update(change)
    with pytest.raises(ProviderAccessError):
        m.decode(raw,'completions',selected_llm=MODEL)


@pytest.mark.parametrize('reason',['bad',True,'tool_calls'])
def test_decode_bad_finish(reason):
    raw=completion();raw['choices'][0]['finish_reason']=reason
    with pytest.raises(ProviderAccessError):
        m.decode(raw,'completions',selected_llm=MODEL)


@pytest.mark.parametrize('raw',[response([{'type':'reasoning'}]), response([message()|{'role':'user'}]),
    response([message()|{'content':[{'type':'refusal'}]}]),response([call(True)|{'status':'in_progress'}]),
    response()|{'status':'failed'},response()|{'incomplete_details':{}},
    response()|{'status':'incomplete','incomplete_details':{'reason':'other'}}])
def test_decode_bad_response(raw):
    with pytest.raises(ProviderAccessError):
        m.decode(raw,'responses',selected_llm=MODEL)


def chunk(delta=None,finish=None,**changes):
    return {'id':'r','model':MODEL,'object':'chat.completion.chunk',
            'choices':[{'index':0,'delta':{} if delta is None else delta,'finish_reason':finish}]}|changes


def completion_stream(tools=False):
    yield chunk({'role':'assistant'})
    yield chunk({'content':'hi'})
    if tools:
        yield chunk({'tool_calls':[{'index':0,**call()|{'function':{'name':'lookup','arguments':'{'}}}]})
        yield chunk({'tool_calls':[{'index':0,'function':{'arguments':'}'}}]})
    yield chunk(finish='tool_calls' if tools else 'stop')
    yield chunk(choices=[],usage=usage())


@pytest.mark.parametrize('tools',[False,True])
def test_completion_stream_buffers_tools_and_finishes_only_at_eof(tools):
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    emitted=[]
    for event in completion_stream(tools):
        emitted.extend(decoder.feed(event))
    assert [event.payload.text for event in emitted]==['hi']
    result=decoder.finish()
    assert bool(result.payload.calls)==tools
    with pytest.raises(ProviderAccessError):decoder.finish()
    with pytest.raises(ProviderAccessError):decoder.feed(chunk())


@pytest.mark.parametrize('events', [[],[chunk({'role':'assistant'})], [chunk({'content':'x'})],
    [chunk({'role':'assistant'}),chunk({'role':'assistant'})], [chunk({'role':'assistant'},object='bad')],
    [chunk({'role':'assistant'}),chunk(id='other')], [chunk({'role':'assistant'}),chunk(choices=[])],
    [chunk({'role':'assistant'}),chunk({'unknown':1})], [chunk({'role':'assistant'}),chunk(finish='other')],
    [chunk({'role':'assistant'}),chunk(finish='stop'),chunk()],
    [chunk({'role':'assistant'}),chunk({'tool_calls':None})],
    [chunk({'role':'assistant'}),chunk({'tool_calls':[{'index':1,'function':{}}]})],
    [chunk({'role':'assistant'}),chunk({'tool_calls':[{'index':0,'function':None}]})]])
def test_bad_completion_stream(events):
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        for event in events:decoder.feed(event)
        decoder.finish()


def response_stream(tools=False):
    events=[{'type':'response.created','response':{'id':'r','status':'in_progress'}},
            {'type':'response.in_progress','response':{'id':'r','status':'in_progress'}}]
    item=call(True) if tools else message()
    start=deepcopy(item);start['status']='in_progress'
    if tools:start['arguments']=''
    else:start['content']=[]
    events.append({'type':'response.output_item.added','output_index':0,'item':start})
    base={'output_index':0,'item_id':item['id']}
    if tools:
        events.extend([base|{'type':'response.function_call_arguments.delta','delta':'{}'},
                       base|{'type':'response.function_call_arguments.done','arguments':'{}'}])
    else:
        base['content_index']=0
        part=item['content'][0]
        events.extend([base|{'type':'response.content_part.added','part':dict(part,text='')},
                       base|{'type':'response.output_text.delta','delta':'hi'},
                       base|{'type':'response.output_text.done','text':'hi'},
                       base|{'type':'response.content_part.done','part':part}])
    events.extend([{'type':'response.output_item.done','output_index':0,'item':item},
                   {'type':'response.completed','response':response([item])}])
    return [event|{'sequence_number':index} for index,event in enumerate(events)]


@pytest.mark.parametrize('tools',[False,True])
def test_response_stream_terminal_snapshot_and_tools(tools):
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    emitted=[]
    for event in response_stream(tools):emitted.extend(decoder.feed(event))
    assert [event.payload.text for event in emitted]==([] if tools else ['hi'])
    assert bool(decoder.finish().payload.calls)==tools


@pytest.mark.parametrize('index,changes', [(0,{'sequence_number':1}), (0,{'response':{}}),
    (1,{'response':{'id':'other','status':'in_progress'}}), (2,{'output_index':True}),
    (2,{'item':{'type':'reasoning'}}), (3,{'item_id':'wrong'}),(3,{'part':{'type':'output_text','text':'nonempty'}}),
    (4,{'content_index':1}), (5,{'text':'wrong'}),(6,{'type':'unknown'}), (7,{'item':{}}),
    (8,{'response':response()}), (8,{'response':response([message('wrong')])})])
def test_response_stream_rejects_malformed_order_or_mismatch(index,changes):
    events=response_stream();events[index].update(changes)
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        for event in events:decoder.feed(event)
        decoder.finish()


@pytest.mark.parametrize('index',[1,3,4,5,6,7])
def test_response_stream_missing_lifecycle_event(index):
    events=response_stream();del events[index]
    events=[event|{'sequence_number':i} for i,event in enumerate(events)]
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    if index==1:
        for event in events:decoder.feed(event)
        assert decoder.finish().outcome=='completed'
    else:
        with pytest.raises(ProviderAccessError):
            for event in events:decoder.feed(event)
            decoder.finish()


def reasoning():
    return {'type':'reasoning','id':'think','status':'completed','summary':[{'type':'summary_text','text':'plan'}],
            'content':[{'type':'reasoning_text','text':'private thought'}]}


def test_native_reasoning_is_validated_but_never_exposed_or_continued():
    result=m.decode(response([reasoning(),message()]),'responses',selected_llm=MODEL)
    assert result.payload.content==(TextContent('hi'),) and result.payload.continuation is None


@pytest.mark.parametrize('change',[{'encrypted_content':'opaque'},{'summary':None},{'summary':[{'type':'unknown','text':'bad'}]},
    {'content':[{'type':'reasoning_text','text':1}]},{'status':'in_progress'}])
def test_invalid_native_reasoning_rejected(change):
    with pytest.raises(ProviderAccessError):
        m.decode(response([reasoning()|change]),'responses',selected_llm=MODEL)


@pytest.mark.parametrize('summary',[True,False])
def test_reasoning_stream_is_bounded_checked_and_silent(summary):
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    field='summary' if summary else 'content'
    part_type='summary_text' if summary else 'reasoning_text'
    event_prefix='response.reasoning_summary' if summary else 'response'
    text_prefix='response.reasoning_summary_text' if summary else 'response.reasoning_text'
    item={'type':'reasoning','id':'think','status':'in_progress','summary':[]}
    base={'output_index':0,'item_id':'think','summary_index' if summary else 'content_index':0}
    part={'type':part_type,'text':'plan'}
    done=dict(item,status='completed',**{field:[part]})
    events=[{'type':'response.created','response':{'id':'r','status':'in_progress'}},
        {'type':'response.output_item.added','output_index':0,'item':item},
        base|{'type':event_prefix+('_part.added' if summary else '.content_part.added'),'part':dict(part,text='')},
        base|{'type':text_prefix+'.delta','delta':'plan'},base|{'type':text_prefix+'.done','text':'plan'},
        base|{'type':event_prefix+('_part.done' if summary else '.content_part.done'),'part':part},
        {'type':'response.output_item.done','output_index':0,'item':done},
        {'type':'response.completed','response':response([done])}]
    for index,event in enumerate(events):
        assert decoder.feed(event|{'sequence_number':index})==()
    assert decoder.finish().payload==Message('assistant',(TextContent(''),))


def test_response_without_content_is_valid():
    assert m.decode(response(),'responses',selected_llm=MODEL).payload.content==(TextContent(''),)


def test_decode_response_size_bound_and_nonfinite():
    with pytest.raises(ProviderAccessError):
        m.decode(response([message('x'*m._LIMIT)]),'responses',selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        m.decode(completion()|{'unknown':float('nan')},'completions',selected_llm=MODEL)


def test_stream_aggregate_size_bound(monkeypatch):
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    decoder.feed(chunk({'role':'assistant'}))
    monkeypatch.setattr(m,'_LIMIT',300)
    with pytest.raises(ProviderAccessError):
        decoder.feed(chunk({'content':'x'*50}))


def test_duplicate_tool_header_rejected():
    events=list(completion_stream(True))
    events.insert(3,events[2])
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        for event in events:decoder.feed(event)


def test_default_token_bound_and_explicit_override():
    assert m.encode(request(),'completions')['max_tokens']==1024
    assert m.encode(request(),'responses')['max_output_tokens']==1024
    assert m.encode(request(generation=GenerationOptions(max_output_tokens=40)),'completions')['max_tokens']==40


def test_chat_native_reasoning_is_suppressed_in_result_and_stream():
    raw=completion();raw['choices'][0]['message']['reasoning_content']='private thought'
    assert m.decode(raw,'completions',selected_llm=MODEL).payload.content==(TextContent('hi'),)
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    events=list(completion_stream())
    events.insert(1,chunk({'reasoning_content':'private thought'}))
    emitted=[]
    for event in events:emitted.extend(decoder.feed(event))
    assert [event.payload.text for event in emitted]==['hi']
    assert decoder.finish().payload.continuation is None


@pytest.mark.parametrize('value',[1,'x'*(m._LIMIT+1)])
def test_chat_native_reasoning_validated(value):
    raw=completion();raw['choices'][0]['message']['reasoning_content']=value
    with pytest.raises(ProviderAccessError):m.decode(raw,'completions',selected_llm=MODEL)
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    decoder.feed(chunk({'role':'assistant'}))
    with pytest.raises(ProviderAccessError):decoder.feed(chunk({'reasoning_content':value}))


def test_maintained_sdk_retains_native_reasoning_extra_without_network():
    import httpx
    from openai import OpenAI
    raw=completion()
    raw.update(id='r',object='chat.completion',created=0)
    raw['choices'][0]['message']['reasoning_content']='private thought'
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,json=raw))) as http:
        with OpenAI(api_key='synthetic',base_url='https://example.invalid/v1',http_client=http,max_retries=0) as client:
            parsed=client.chat.completions.create(model=MODEL,messages=[{'role':'user','content':'hello'}])
    dumped=parsed.model_dump(exclude_unset=True)
    assert dumped['choices'][0]['message']['reasoning_content']=='private thought'
    assert m.decode(dumped,'completions',selected_llm=MODEL).payload.content==(TextContent('hi'),)


def test_glm_temperature_bound():
    req=LLMRequest('zai.glm-5',CompletionRequest((Message('user',(TextContent('hi'),)),),
        generation=GenerationOptions(temperature=1.1)))
    with pytest.raises(ProviderAccessError):m.encode(req,'completions')
    assert m.encode(request(generation=GenerationOptions(temperature=1.1)),'completions')['temperature']==1.1


def test_tool_argument_validation_is_independent_of_finish_reason():
    raw=completion()
    raw['choices'][0].update(message={'role':'assistant','tool_calls':[call()|{'function':{'name':'lookup','arguments':'[]'}}]},
                             finish_reason='tool_calls')
    with pytest.raises(ProviderAccessError):m.decode(raw,'completions',selected_llm=MODEL)


def test_duplicate_tool_header_with_otherwise_valid_arguments_is_rejected():
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    decoder.feed(chunk({'role':'assistant'}))
    decoder.feed(chunk({'tool_calls':[{'index':0,**call()}]}))
    with pytest.raises(ProviderAccessError):
        decoder.feed(chunk({'tool_calls':[{'index':0,**call()|{'function':{'name':'lookup','arguments':''}}}]}))


def test_sdk_stream_retains_native_reasoning_extra_without_network():
    import json
    import httpx
    from openai import OpenAI
    events=list(completion_stream())
    events.insert(1,chunk({'reasoning_content':'private thought'}))
    wire=''.join('data: '+json.dumps(event)+'\n\n' for event in events)+'data: [DONE]\n\n'
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    retained=[]
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,headers={'content-type':'text/event-stream'},text=wire))) as http:
        with OpenAI(api_key='synthetic',base_url='https://example.invalid/v1',http_client=http,max_retries=0) as client:
            with client.chat.completions.create(model=MODEL,messages=[{'role':'user','content':'hello'}],stream=True) as stream:
                for event in stream:
                    dumped=event.model_dump(exclude_unset=True)
                    retained.append(dumped)
                    decoder.feed(dumped)
    assert retained[1]['choices'][0]['delta']['reasoning_content']=='private thought'
    assert decoder.finish().payload.content==(TextContent('hi'),)


@pytest.mark.parametrize('model,temperature,allowed',[('zai.glm-5',1,True),('zai.glm-5',1.01,False),
    (MODEL,2,True),(MODEL,2.01,False)])
def test_temperature_admission_before_encoding(model,temperature,allowed):
    req=LLMRequest(model,CompletionRequest((Message('user',(TextContent('hi'),)),),generation=GenerationOptions(temperature=temperature)))
    if allowed:m.validate_request(req)
    else:
        with pytest.raises(ProviderAccessError):m.validate_request(req)


def test_parallel_response_tool_items_close_in_any_order():
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    calls=[call(True),call(True)|{'id':'fc2','call_id':'c2'}]
    events=[{'type':'response.created','response':{'id':'r','status':'in_progress'}}]
    events.extend({'type':'response.output_item.added','output_index':i,'item':dict(item,status='in_progress',arguments='')}
                  for i,item in enumerate(calls))
    for i in (1,0):
        base={'output_index':i,'item_id':calls[i]['id']}
        events.extend([base|{'type':'response.function_call_arguments.delta','delta':'{}'},
            base|{'type':'response.function_call_arguments.done','arguments':'{}'},
            {'type':'response.output_item.done','output_index':i,'item':calls[i]}])
    events.append({'type':'response.completed','response':response(calls)})
    for index,event in enumerate(events):assert decoder.feed(event|{'sequence_number':index})==()
    assert tuple(call.call_id for call in decoder.finish().payload.calls)==('c','c2')


def test_incomplete_response_item_status_preserves_length_outcome():
    raw=response([message()|{'status':'incomplete'}])|{'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'}}
    assert m.decode(raw,'responses',selected_llm=MODEL).outcome=='length'
    events=response_stream()
    events[-2]['item']['status']='incomplete'
    events[-1].update(type='response.incomplete',response=raw)
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    for event in events:decoder.feed(event)
    assert decoder.finish().outcome=='length'


def test_multiple_response_messages_preserve_emitted_and_final_text_order():
    first=response_stream()
    second=response_stream()
    item2=message('hi')|{'id':'msg2'}
    events=first[:-1]
    for event in second[2:-1]:
        event=deepcopy(event);event['output_index']=1
        if 'item_id' in event:event['item_id']='msg2'
        if 'item' in event:event['item']['id']='msg2'
        events.append(event)
    events.append({'type':'response.completed','response':response([message(),item2])})
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    emitted=[]
    for index,event in enumerate(events):emitted.extend(decoder.feed(event|{'sequence_number':index}))
    assert ''.join(event.payload.text for event in emitted)=='hihi'
    assert decoder.finish().payload.content==(TextContent('hihi'),)


def test_terminal_tool_snapshot_cannot_change_completed_call_name():
    events=response_stream(True)
    events[-1]=deepcopy(events[-1])
    events[-1]['response']['output'][0]['name']='different'
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        for event in events:decoder.feed(event)
        decoder.finish()


def test_sdk_nullable_chat_fields_are_absence_in_results_and_streams():
    import json
    import httpx
    from openai import OpenAI
    raw=completion();raw.update(id='r',object='chat.completion',created=0)
    raw['choices'][0]['message'].update(tool_calls=None,refusal=None,audio=None,function_call=None)
    events=list(completion_stream(True))
    events[1]['choices'][0]['delta'].update(role=None,refusal=None,tool_calls=None,function_call=None,audio=None)
    events[3]['choices'][0]['delta']['tool_calls'][0].update(id=None,type=None)
    events[3]['choices'][0]['delta']['tool_calls'][0]['function']['name']=None
    # SDK nullable no-op continuation carries no function delta at all.
    events.insert(4,chunk({'tool_calls':[{'index':0,'function':None}]}))
    events.insert(5,chunk({'tool_calls':[{'index':0,'function':{'arguments':None}}]}))
    wire=''.join('data: '+json.dumps(event)+'\n\n' for event in events)+'data: [DONE]\n\n'
    def transport(request):
        body=json.loads(request.content)
        if body.get('stream'):
            return httpx.Response(200,headers={'content-type':'text/event-stream'},text=wire)
        return httpx.Response(200,json=raw)
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        with OpenAI(api_key='synthetic',base_url='https://example.invalid/v1',http_client=http,max_retries=0) as client:
            parsed=client.chat.completions.create(model=MODEL,messages=[{'role':'user','content':'hello'}])
            assert m.decode(parsed.model_dump(exclude_unset=True),'completions',selected_llm=MODEL).payload.content==(TextContent('hi'),)
            with client.chat.completions.create(model=MODEL,messages=[{'role':'user','content':'hello'}],stream=True) as stream:
                for event in stream:decoder.feed(event.model_dump(exclude_unset=True))
    assert decoder.finish().payload.calls==(ToolCall('c','lookup','{}'),)


@pytest.mark.parametrize('field,value',[('audio',{'data':'x'}),('function_call',{'name':'lookup','arguments':'{}'}),
    ('refusal','refused'),('future_execution',True)])
def test_nonnull_unsupported_chat_fields_rejected(field,value):
    raw=completion();raw['choices'][0]['message'][field]=value
    with pytest.raises(ProviderAccessError):m.decode(raw,'completions',selected_llm=MODEL)
    decoder=m.StreamDecoder('completions',selected_llm=MODEL)
    decoder.feed(chunk({'role':'assistant'}))
    with pytest.raises(ProviderAccessError):decoder.feed(chunk({field:value}))


@pytest.mark.parametrize('snapshot',[0,1])
@pytest.mark.parametrize('field,value',[('error',{'code':'server_error'}),('model','wrong-model')])
def test_early_response_snapshot_failure_blocks_tools(snapshot,field,value):
    events=response_stream(True);events[snapshot]['response'][field]=value
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        for event in events:decoder.feed(event)
        decoder.finish()


@pytest.mark.parametrize('field,value',[('namespace','other_tools'),('async',True),('async',False),
    ('caller',{'type':'program','caller_id':'other'}),('future_execution',True)])
def test_unsupported_execution_context_cannot_become_local_function(field,value):
    with pytest.raises(ProviderAccessError):m.decode(response([call(True)|{field:value}]),'responses',selected_llm=MODEL)
    events=response_stream(True);events[2]['item'][field]=value
    decoder=m.StreamDecoder('responses',selected_llm=MODEL)
    with pytest.raises(ProviderAccessError):
        for event in events:decoder.feed(event)
        decoder.finish()


@pytest.mark.parametrize('field',['namespace','async','caller','future_execution'])
def test_nullable_execution_context_is_absence(field):
    assert m.decode(response([call(True)|{field:None}]),'responses',selected_llm=MODEL).payload.calls==(ToolCall('c','lookup','{}'),)


@pytest.mark.parametrize('field',['content','encrypted_content','status'])
def test_nullable_reasoning_metadata_is_absence(field):
    result=m.decode(response([reasoning()|{field:None},message()]),'responses',selected_llm=MODEL)
    assert result.payload.content==(TextContent('hi'),) and result.payload.continuation is None


@pytest.mark.parametrize('mode', ['completions', 'responses'])
def test_resolved_shared_reasoning_default_omits_wire_control(mode):
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.cache_policy import configured_request

    config = resolve_config(load_shared_schema(), environ={})
    assert not config.errors
    assert config.values['LLM_REASONING_EFFORT'] == ''
    configured = configured_request(request(), config.values['LLM_CACHE_POLICY'],
        reasoning_effort=config.values['LLM_REASONING_EFFORT'])
    assert configured.payload.generation.reasoning_effort == ''
    assert configured.payload.generation.cache_policy is None

    body = m.encode(configured, mode)

    assert body == m.encode(request(), mode)
    assert 'reasoning' not in body
    assert 'reasoning_effort' not in body


@pytest.mark.parametrize('effort', ['off'])
@pytest.mark.parametrize('mode', ['completions', 'responses'])
def test_reasoning_off_remains_unsupported(effort, mode):
    req = request(generation=GenerationOptions(reasoning_effort=effort))

    with pytest.raises(ProviderAccessError) as error:
        m.encode(req, mode)

    assert error.value.kind == 'unsupported'
