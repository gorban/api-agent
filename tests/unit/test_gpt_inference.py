import asyncio
import json

import httpx
import pytest

from api_agent.llm_providers.gpt import inference as module
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent, StructuredOutput, Tool, ToolCall
from api_agent.providers.progress import report_timeout, progress_sink

TOKEN = 'synthetic-selected-token'
BASE = 'https://api.openai.com/v1'


def request(maximum=64, **kwargs):
    return LLMRequest('gpt-example', CompletionRequest((Message('user', (TextContent('Say Ok'),)),), generation=GenerationOptions(maximum), **kwargs))


def result(responses=True):
    return ({'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Ok'}]}],
             'usage': {'input_tokens': 3, 'output_tokens': 1, 'input_tokens_details': {'cached_tokens': 1}, 'output_tokens_details': {'reasoning_tokens': 0}}}
            if responses else {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Ok'}}],
             'usage': {'prompt_tokens': 3, 'completion_tokens': 1}})


def run(response=None, *, subscription=False, api_mode='responses', req=None, handler=None, base=BASE, token=TOKEN):
    calls = []
    async def transport(sent):
        calls.append(sent)
        return await handler(sent) if handler else response
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            value = await module.complete_text(client=client, base_url=base, access_token=token, subscription=subscription,
                                               api_mode=api_mode, request=req or request(maximum=None if subscription else 64))
            assert not client.is_closed
            return value
    return asyncio.run(execute()), calls


@pytest.mark.parametrize('subscription,mode', [(False, 'responses'), (False, 'completions'), (True, 'responses')])
def test_mode_payload_authorization_no_discovery_and_owned_client(subscription, mode):
    data = result(mode == 'responses')
    response = (httpx.Response(200, text='data: ' + json.dumps({'type': 'response.completed', 'response': data}) + '\n\n', headers={'content-type': 'text/event-stream'})
                if subscription else httpx.Response(200, json=data))
    value, calls = run(response, subscription=subscription, api_mode=mode)
    assert value.payload.content[0].text == 'Ok' and value.usage.input_tokens == 3
    assert value.outcome == 'completed' and len(calls) == 1
    sent = calls[0]
    assert sent.method == 'POST' and sent.headers['Authorization'] == 'Bearer ' + TOKEN
    assert sent.headers['Accept-Encoding'] == 'identity'
    assert str(sent.url) == BASE + ('/responses' if mode == 'responses' else '/chat/completions')
    body = json.loads(sent.content)
    assert body['stream'] is subscription
    if subscription:
        assert 'max_output_tokens' not in body and 'max_completion_tokens' not in body
    else:
        assert body.get('max_output_tokens', body.get('max_completion_tokens')) == 64
    if mode == 'responses':
        assert body['store'] is False and body['input'][0]['content'] == 'Say Ok'
    else:
        assert body['messages'][0]['content'] == 'Say Ok'


@pytest.mark.parametrize('status,kind', [(400,'invalid_request'), (401,'authentication'), (403,'authorization'), (429,'rate_limited'), (500,'unavailable'), (302,'unavailable')])
def test_http_rejection_preserves_safe_upstream(status, kind):
    with pytest.raises(ProviderAccessError) as caught:
        run(httpx.Response(status, json={'error': {'message': 'Actual permission denied ' + TOKEN + ' Bearer\nsecondary-secret', 'code': 123, 'type': 'permission'}}, headers={'x-request-id': 'req-1'}))
    error = caught.value
    assert error.kind == kind and error.status == status and error.code == '123' and error.type == 'permission' and error.request_id == 'req-1'
    assert 'Actual permission denied' in str(error)
    assert TOKEN not in str(vars(error)) and 'secondary-secret' not in str(vars(error))


@pytest.mark.parametrize('kwargs', [dict(api_mode='bad'), dict(api_mode='completions', subscription=True), dict(subscription=True,base='https://alternate.example/v1'), dict(token='bad key')])
def test_invalid_modes_auth_endpoint_before_transport(kwargs):
    with pytest.raises(ProviderAccessError):
        run(**kwargs)


@pytest.mark.parametrize('req', [request(tools=(Tool('tool', '', '{}'),)), request(structured=StructuredOutput('shape','{}')),
    LLMRequest('gpt-example', CompletionRequest((Message('user',(TextContent('x'),)),), generation=GenerationOptions(64,1))),
    LLMRequest('gpt-example', CompletionRequest((Message('assistant',calls=(ToolCall('id','tool','{}'),)),), generation=GenerationOptions(64))),
    LLMRequest('other-selection', request().payload), LLMRequest('gpt- x\n',request().payload),
    LLMRequest('gpt-example',CompletionRequest((Message('user'),),generation=GenerationOptions(4097))),
    LLMRequest('gpt-example',CompletionRequest((Message('user',(TextContent('x'*65537),)),),generation=GenerationOptions(64))),
    LLMRequest('gpt-example',CompletionRequest((Message('user'),)*65,generation=GenerationOptions(64))), object()])
def test_unsupported_or_unbounded_request_before_transport(req):
    with pytest.raises(ProviderAccessError):
        run(req=req)


@pytest.mark.parametrize('data', [None, [], {}, {'status':'incomplete','output':[]}, {'status':'completed','output':[None]},
    {'status':'completed','output':[{'type':'function_call'}]}, {'status':'completed','output':[{'type':'message','role':'assistant','content':[{}]}]},
    {'status':'completed','output':[]}, {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'x'*65537}]}]}])
def test_invalid_responses(data):
    with pytest.raises(ProviderAccessError):
        run(httpx.Response(200,json=data))


@pytest.mark.parametrize('data', [None, [], {}, {'choices':[None]}, {'choices':[]}, {'choices':[{},{}]},
    {'choices':[{'finish_reason':'tool_calls','message':{'role':'assistant','content':'x'}}]},
    {'choices':[{'finish_reason':'stop','message':{'role':'user','content':'x'}}]},
    {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'x','tool_calls':[{}]}}]},
    {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'x','refusal':'no'}}]}])
def test_invalid_completions(data):
    with pytest.raises(ProviderAccessError):
        run(httpx.Response(200,json=data),api_mode='completions')


@pytest.mark.parametrize('usage', [[], {'input_tokens':True}, {'input_tokens':1,'input_tokens_details':{'cached_tokens':2}}, {'output_tokens':1,'output_tokens_details':{'reasoning_tokens':2}}, {'input_tokens_details':[]}, {'output_tokens':-1}])
def test_invalid_usage(usage):
    data=result(); data['usage']=usage
    with pytest.raises(ProviderAccessError):
        run(httpx.Response(200,json=data))


def test_optional_usage_reasoning_rows_and_length():
    data=result(); data['output'].insert(0,{'type':'reasoning'}); data.pop('usage')
    value,_=run(httpx.Response(200,json=data)); assert value.usage.input_tokens is None
    data=result(False); data['choices'][0]['finish_reason']='length'
    value,_=run(httpx.Response(200,json=data),api_mode='completions'); assert value.outcome=='length'


@pytest.mark.parametrize('body', [b'\xff', b'not json', b'data: []\n\n', b'data: {}\n\n', b'data: {"type":"response.created"}\n\n',
    b'data: {"type":"response.failed","response":{"error":{"message":"rejected","code":"denied"}}}\n\n',
    b'data: {"type":"response.incomplete"}\n\n', b'data: {"type":"error","error":{"message":"bad"}}\n\n'])
def test_stream_failures_are_failures(body):
    with pytest.raises(ProviderAccessError):
        run(httpx.Response(200,content=body,headers={'content-type':'text/event-stream'}),subscription=True)


def test_duplicate_final_and_wrong_content_type_rejected():
    frame='data: '+json.dumps({'type':'response.completed','response':result()})+'\n\n'
    with pytest.raises(ProviderAccessError):
        run(httpx.Response(200,text=frame*2,headers={'content-type':'text/event-stream'}),subscription=True)
    with pytest.raises(ProviderAccessError):
        run(httpx.Response(200,json=result()),subscription=True)


@pytest.mark.parametrize('response', [httpx.Response(200,content=b'bad'), httpx.Response(403,content=b'bad'),
    httpx.Response(200,content=b'x'*(module._LIMIT+1)), httpx.Response(200,stream=httpx.ByteStream(b'bad'),headers={'content-encoding':'gzip'})])
def test_raw_bounds_bad_json_and_compression(response):
    with pytest.raises(ProviderAccessError):
        run(response)


@pytest.mark.parametrize('error', [httpx.ConnectError('secret '+TOKEN), httpx.ReadTimeout('secret '+TOKEN)])
def test_transport_errors_safe(error):
    async def handler(sent): raise error
    with pytest.raises(ProviderAccessError) as caught: run(handler=handler)
    assert TOKEN not in str(caught.value)


def test_nested_deadline_progress_and_cleanup():
    closed=[]; progress=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1)
            yield b'{}'
        async def aclose(self): closed.append(True)
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda sent:httpx.Response(200,stream=Stream()))) as client:
            with progress_sink(progress.append):
                async with report_timeout('outer',.01):
                    await module.complete_text(client=client,base_url=BASE,access_token=TOKEN,subscription=False,api_mode='responses',request=request())
    with pytest.raises(ProviderAccessError) as caught: asyncio.run(execute())
    assert caught.value.kind=='deadline' and closed==[True]
    assert any(row.name=='GPT text inference' and row.timeout<=.01 for row in progress)


def test_invalid_unicode_rejected_without_echo():
    bad=LLMRequest('gpt-example',CompletionRequest((Message('user',(TextContent('\ud800'),)),),generation=GenerationOptions(64)))
    with pytest.raises(ProviderAccessError,match='invalid Unicode'): run(req=bad)


def test_unterminated_sse_and_explicit_error_details():
    frame='data: '+json.dumps({'type':'response.completed','response':result()})
    with pytest.raises(ProviderAccessError,match='before completion'):
        run(httpx.Response(200,text=frame,headers={'content-type':'text/event-stream'}),subscription=True)
    frame='data: '+json.dumps({'type':'error','message':'Actual upstream error','code':'upstream'})+'\n\n'
    with pytest.raises(ProviderAccessError) as caught:
        run(httpx.Response(200,text=frame,headers={'content-type':'text/event-stream'}),subscription=True)
    assert caught.value.code=='upstream' and 'Actual upstream error' in str(caught.value)


@pytest.mark.parametrize('mode', ['subscription','api_key'])
def test_factory_completion_fixed_or_configured_endpoint_no_catalog(monkeypatch,mode):
    from api_agent.llm_providers.gpt import factory
    calls=[]; closed=[]
    class Client:
        def __init__(self,**kwargs): assert kwargs=={'follow_redirects':False,'trust_env':False}
        async def __aenter__(self): return self
        async def __aexit__(self,*args): closed.append(True)
    async def complete(**kwargs):
        calls.append(kwargs); return 'synthetic-result'
    monkeypatch.setattr(factory.httpx,'AsyncClient',Client)
    monkeypatch.setattr(module,'complete_text',complete)
    async def scenario():
        provider=factory.GPTProvider({'GPT_AUTH_MODE':mode,'GPT_BASE_URL':'https://alternate.example/v1'})
        assert await provider.acomplete(request(maximum=None if mode=='subscription' else 64),auth=TOKEN)=='synthetic-result'
        await provider.aclose()
        with pytest.raises(factory.ContractError,match='closed'): await provider.acomplete(request(),auth=TOKEN)
    asyncio.run(scenario())
    assert len(calls)==1 and closed==[True]
    assert calls[0]['base_url']==(BASE if mode=='subscription' else 'https://alternate.example/v1')
    assert calls[0]['access_token']==TOKEN and calls[0]['subscription'] is (mode=='subscription')


def test_cancelled_response_is_closed_and_client_retained():
    closed=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise asyncio.CancelledError
            yield b''
        async def aclose(self): closed.append(True)
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda sent:httpx.Response(200,stream=Stream()))) as client:
            with pytest.raises(asyncio.CancelledError):
                await module.complete_text(client=client,base_url=BASE,access_token=TOKEN,subscription=False,api_mode='responses',request=request())
            assert not client.is_closed
    asyncio.run(execute()); assert closed==[True]


def test_compression_rejected_before_decoder_or_body_iteration():
    touched=[]; closed=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            touched.append(True)
            yield b'not-a-gzip-body'
        async def aclose(self): closed.append(True)
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda sent:httpx.Response(200,stream=Stream(),headers={'content-encoding':'gzip'}))) as client:
            await module.complete_text(client=client,base_url=BASE,access_token=TOKEN,subscription=False,api_mode='responses',request=request())
    with pytest.raises(ProviderAccessError,match='unsupported content encoding'): asyncio.run(execute())
    assert not touched and closed==[True]


def test_raw_size_rejected_even_when_json_would_be_valid():
    body=json.dumps(result()).encode()+b' '*(module._LIMIT+1)
    with pytest.raises(ProviderAccessError,match='response exceeded its limit'): run(httpx.Response(200,content=body))


def test_stream_rejection_preserves_upstream_and_actual_http_status():
    frame={'type':'response.failed','response':{'error':{'message':'Actual subscription rejection Bearer\tsecondary-secret '+TOKEN,'code':'subscription_sharing_usage_unavailable','type':'usage_error'}}}
    with pytest.raises(ProviderAccessError) as caught:
        run(httpx.Response(200,text='data: '+json.dumps(frame)+'\n\n',headers={'content-type':'text/event-stream','x-request-id':'req-stream'}),subscription=True)
    error=caught.value
    assert error.status==200 and error.request_id=='req-stream' and error.code=='subscription_sharing_usage_unavailable' and error.type=='usage_error'
    assert 'Actual subscription rejection' in str(error) and TOKEN not in str(error) and 'secondary-secret' not in str(error)


def test_explicit_selection_does_not_list_and_results_are_not_cached():
    calls=[]
    def handler(sent):
        calls.append(sent)
        assert sent.url.path=='/v1/responses'
        data=result(); data['output'][0]['content'][0]['text']=str(len(calls))
        return httpx.Response(200,json=data)
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first=await module.complete_text(client=client,base_url=BASE,access_token=TOKEN,subscription=False,api_mode='responses',request=request())
            second=await module.complete_text(client=client,base_url=BASE,access_token=TOKEN,subscription=False,api_mode='responses',request=request())
            assert first.payload.content[0].text=='1' and second.payload.content[0].text=='2'
    asyncio.run(execute()); assert len(calls)==2


def test_subscription_rejects_explicit_token_limit_and_advertises_no_limit_feature():
    from api_agent.llm_providers.gpt.factory import GPTProvider
    with pytest.raises(ProviderAccessError,match='does not support max_output_tokens'):
        run(subscription=True,req=request())
    assert GPTProvider({'GPT_AUTH_MODE':'subscription'}).capabilities.features==()
    assert GPTProvider({'GPT_AUTH_MODE':'api_key'}).capabilities.features==('max_output_tokens',)


@pytest.mark.parametrize('mode,key', [('responses','max_output_tokens'),('completions','max_completion_tokens')])
def test_api_key_small_default_output_limit(mode,key):
    value,calls=run(httpx.Response(200,json=result(mode=='responses')),api_mode=mode,req=request(maximum=None))
    assert value.outcome=='completed' and json.loads(calls[0].content)[key]==128


@pytest.mark.parametrize('body', [b'{"status":"failed","status":"completed","output":[]}', b'{"x":NaN}', b'{"x":Infinity}',
    b'{"x":-Infinity}', b'{"usage":{"output_tokens":1,"output_tokens":2}}'])
def test_strict_json_rejects_duplicates_and_nonfinite_constants(body):
    with pytest.raises(ProviderAccessError,match='invalid result'): run(httpx.Response(200,content=body))


@pytest.mark.parametrize('body', ['data: {"type":"response.failed","type":"response.completed","response":{}}\n\n',
    'data: {"type":"response.completed","response":{},"response":{}}\n\n',
    'data: {"type":"response.created","sequence_number":NaN}\n\n'])
def test_strict_sse_json_rejects_duplicate_keys_and_nonfinite(body):
    with pytest.raises(ProviderAccessError,match='invalid result'):
        run(httpx.Response(200,text=body,headers={'content-type':'text/event-stream'}),subscription=True)


@pytest.mark.parametrize('detail', ['Actual account is blocked', 'Actual account is blocked '+TOKEN+' Bearer\r\nsecondary-secret https://private.example/rejected'])
def test_direct_subscription_error_detail_preserved_safely(detail):
    with pytest.raises(ProviderAccessError) as caught:
        run(httpx.Response(403,json={'detail':detail},headers={'x-request-id':'req-detail'}),subscription=True)
    error=caught.value
    assert error.status==403 and error.request_id=='req-detail' and 'Actual account is blocked' in str(error)
    assert TOKEN not in str(error) and 'secondary-secret' not in str(error) and 'private.example' not in str(error)


@pytest.mark.parametrize('subscription', [False,True])
def test_system_role_translation_only_subscription(subscription):
    req=LLMRequest('gpt-example',CompletionRequest((Message('system',(TextContent('Be concise'),)),Message('user',(TextContent('Say Ok'),))),generation=GenerationOptions(None if subscription else 64)))
    data=result()
    response=(httpx.Response(200,text='data: '+json.dumps({'type':'response.completed','response':data})+'\n\n',headers={'content-type':'text/event-stream'}) if subscription else httpx.Response(200,json=data))
    _,calls=run(response,subscription=subscription,req=req)
    assert json.loads(calls[0].content)['input'][0]['role']==('developer' if subscription else 'system')


@pytest.mark.parametrize('config,auth,req', [({'GPT_AUTH_MODE':[]},TOKEN,request()),({'GPT_AUTH_MODE':'bad'},TOKEN,request()),
    ({'GPT_AUTH_MODE':'api_key','GPT_BASE_URL':'http://bad.example'},TOKEN,request()),
    ({'GPT_AUTH_MODE':'subscription','GPT_API_MODE':'completions'},TOKEN,request(maximum=None)),
    ({'GPT_AUTH_MODE':'subscription'},TOKEN,request()),({'GPT_AUTH_MODE':'api_key'},'bad token',request()),
    ({'GPT_AUTH_MODE':'api_key'},TOKEN,request(tools=(Tool('tool','','{}'),)))])
def test_factory_preflight_before_client_allocation(monkeypatch,config,auth,req):
    from api_agent.llm_providers.gpt import factory
    def forbidden(**kwargs): pytest.fail('client allocated before pure preflight')
    monkeypatch.setattr(factory.httpx,'AsyncClient',forbidden)
    with pytest.raises((ProviderAccessError,factory.ContractError)):
        asyncio.run(factory.GPTProvider(config).acomplete(req,auth=auth))


@pytest.mark.parametrize('subscription,mode', [(False,'responses'),(False,'completions'),(True,'responses')])
@pytest.mark.parametrize('text', [TOKEN,TOKEN[:10]+'\r\n\t'+TOKEN[10:]])
def test_successful_output_cannot_export_selected_credential(subscription,mode,text):
    data=result(mode=='responses')
    if mode=='responses': data['output'][0]['content'][0]['text']='Reflected '+text
    else: data['choices'][0]['message']['content']='Reflected '+text
    response=(httpx.Response(200,text='data: '+json.dumps({'type':'response.completed','response':data})+'\n\n',headers={'content-type':'text/event-stream'}) if subscription else httpx.Response(200,json=data))
    with pytest.raises(ProviderAccessError,match='protected authentication content') as caught:
        run(response,subscription=subscription,api_mode=mode)
    assert TOKEN not in str(vars(caught.value)) and 'Reflected' not in str(caught.value)


def test_ordinary_output_controls_and_urls_are_preserved():
    text='Ordinary answer\n\tSee https://example.com/document for details.'
    data=result();data['output'][0]['content'][0]['text']=text
    value,_=run(httpx.Response(200,json=data))
    assert value.payload.content[0].text==text


@pytest.mark.parametrize('mutation', ['duplicate_status','duplicate_usage','nonfinite'])
def test_strict_json_guards_have_otherwise_valid_result(mutation):
    body=json.dumps(result())
    if mutation=='duplicate_status': body=body.replace('"status": "completed"','"status": "failed", "status": "completed"')
    elif mutation=='duplicate_usage': body=body.replace('"output_tokens": 1','"output_tokens": 999, "output_tokens": 1')
    else: body=body[:-1]+', "unused": NaN}'
    with pytest.raises(ProviderAccessError,match='invalid result'):
        run(httpx.Response(200,content=body))


@pytest.mark.parametrize('mutation', ['type','response'])
def test_strict_sse_duplicate_guards_have_otherwise_valid_final(mutation):
    response=json.dumps(result())
    if mutation=='type': frame='{"type":"response.failed","type":"response.completed","response":'+response+'}'
    else: frame='{"type":"response.completed","response":{"status":"failed"},"response":'+response+'}'
    with pytest.raises(ProviderAccessError,match='invalid result'):
        run(httpx.Response(200,text='data: '+frame+'\n\n',headers={'content-type':'text/event-stream'}),subscription=True)
