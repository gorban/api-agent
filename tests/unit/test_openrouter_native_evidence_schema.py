"""Every known native carrier is schema-checked before transport evidence capture."""
import copy
import json

import httpx
import pytest

from api_agent.llm_providers.gpt.inference import complete_text
from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.inference_capture import collect_inference

BASE = 'https://openrouter.ai/api/v1'
MODEL = 'openai/gpt-5.6-luna'
OPAQUE = dict(type='reasoning.encrypted', data='opaque-sentinel', signature='signature-sentinel')
SHAPES = dict(object=OPAQUE, array=[OPAQUE], string=json.dumps(OPAQUE), number=7, null=None)


def partial_response(mode):
    if mode == 'responses':
        return dict(status='incomplete',incomplete_details=dict(reason='max_output_tokens'),output=[
            dict(type='reasoning',id='r',status='incomplete',summary=[],encrypted_content='opaque-sentinel'),
            dict(type='message',id='m',role='assistant',status='incomplete',content=[dict(type='output_text',text='Ok')])],
            usage=dict(input_tokens=5,output_tokens=7))
    return dict(choices=[dict(index=0,finish_reason='length',message=dict(role='assistant',content='Ok',reasoning_details=[
        dict(type='reasoning.encrypted',data='opaque-sentinel')]))],usage=dict(prompt_tokens=5,completion_tokens=7))


def malformed_carriers():
    """Invalid types follow each field's shared native parser, including nullable fields."""
    cases = []
    def add(mode,path,shapes,label):
        cases.extend(pytest.param(mode,path,SHAPES[shape],id=f'{label}-{shape}') for shape in shapes)
    for mode,field in [('responses','output'),('completions','choices')]:
        add(mode,(field,),('object','string','number','null'),field)
    add('responses',('output',0),('array','string','number','null'),'response-item')
    for field,shapes in [
        ('summary',('object','array','string','number','null')),
        ('content',('object','array','string','number')),
        ('id',('object','array','number','null')),
        ('status',('object','array','string','number')),
        ('encrypted_content',('object','array','number')),
        ('signature',('object','array','number')),
        ('format',('object','array','number')),
    ]:
        add('responses',('output',0,field),shapes,'response-reasoning-'+field)
    for field,shapes in [
        ('content',('object','string','number','null')),
        ('id',('object','array','number','null')),
        ('role',tuple(SHAPES)),('status',('object','array','string','number')),
        ('phase',('object','array','string','number')),
        ('reasoning',tuple(SHAPES)),('reasoning_content',tuple(SHAPES)),('reasoning_details',tuple(SHAPES)),
        ('signature',tuple(SHAPES)),('encrypted_content',tuple(SHAPES)),
    ]:
        add('responses',('output',1,field),shapes,'response-message-'+field)
    add('completions',('choices',0,'message'),('array','string','number','null'),'chat-message')
    for field,shapes in [
        ('reasoning',('object','array','number')),
        ('reasoning_content',('object','array','number')),
        ('reasoning_details',('object','string','number')),
        ('content',('object','array','number','null')),
        ('tool_calls',tuple(SHAPES)),('role',tuple(SHAPES)),
        ('signature',tuple(SHAPES)),('encrypted_content',tuple(SHAPES)),
    ]:
        add('completions',('choices',0,'message',field),shapes,'chat-message-'+field)
    for shape,value in SHAPES.items():
        cases.append(pytest.param('completions',('choices',0),dict(finish_reason='length',delta=value),id='chat-delta-'+shape))
    for field,shapes in [('type',tuple(SHAPES)),('data',('object','array','number','null')),
                         ('id',('object','array','number')),('format',('object','array','number')),
                         ('index',('object','array','string'))]:
        add('completions',('choices',0,'message','reasoning_details',0,field),shapes,'chat-detail-'+field)
    for kind,field in [('reasoning.text','text'),('reasoning.summary','summary')]:
        for shape in ('object','array','number','null'):
            cases.append(pytest.param('completions',('choices',0,'message','reasoning_details'),
                [dict(type=kind,**{field:SHAPES[shape]})],id=f'chat-{field}-{shape}'))
    return cases


@pytest.mark.parametrize('mode,path,value',malformed_carriers())
@pytest.mark.parametrize('status',[200,400],ids=['incomplete','rejected'])
@pytest.mark.asyncio
async def test_every_malformed_native_carrier_is_withheld_with_incurred_usage(mode,path,value,status):
    raw = partial_response(mode)
    target = raw
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = copy.deepcopy(value)
    if status == 400:
        raw['error'] = dict(message='Safe error')
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(status,json=raw))) as client:
            with pytest.raises(ProviderAccessError) as caught:
                await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode=mode,request=request)
    assert caught.value.usage.input_tokens == 5
    assert caught.value.usage.output_tokens == 7
    recorded = json.dumps(evidence['rows'])
    assert 'opaque-sentinel' not in recorded
    assert 'signature-sentinel' not in recorded


@pytest.mark.parametrize('field',['reasoning','reasoning_content'])
@pytest.mark.parametrize('text',['  Valid plaintext.  ',json.dumps(OPAQUE),None])
@pytest.mark.parametrize('status',[200,400])
@pytest.mark.asyncio
async def test_valid_nullable_plaintext_aliases_keep_their_schema_semantics(field,text,status):
    raw = partial_response('completions')
    message = raw['choices'][0]['message']
    message.pop('reasoning_details')
    message[field] = text
    if status == 400:
        raw['error'] = dict(message='Safe error')
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(status,json=raw))) as client:
            if status == 400:
                with pytest.raises(ProviderAccessError) as caught:
                    await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='completions',request=request)
                usage = caught.value.usage
            else:
                result = await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='completions',request=request)
                assert result.reasoning_summary == text
                assert result.payload.continuation is None
                usage = result.usage
    assert usage.output_tokens == 7
    native_message = next(row['content']['choices'][0]['message'] for row in evidence['rows'] if row['phase']=='response')
    assert native_message[field] == text


@pytest.mark.parametrize('wrapper',['input','messages'])
@pytest.mark.parametrize('alias,plaintext',[
    ('reasoning','Checked.'),('reasoning_content','Checked.'),
    ('reasoning_details',[dict(type='reasoning.text',text='Checked.')]),
])
@pytest.mark.parametrize('unknown',['signature','encrypted_content','unknown_native'])
@pytest.mark.parametrize('value',[OPAQUE,[OPAQUE],json.dumps(OPAQUE)],ids=['object','array','string'])
@pytest.mark.parametrize('mode',['responses','completions'])
@pytest.mark.parametrize('status',[200,400],ids=['incomplete','rejected'])
@pytest.mark.asyncio
async def test_echoed_native_assistants_require_complete_grammar(wrapper,alias,plaintext,unknown,value,mode,status):
    raw = partial_response(mode)
    raw[wrapper] = [dict(role='assistant',content='Ok',**{alias:plaintext,unknown:copy.deepcopy(value)})]
    if status == 400:
        raw['error'] = dict(message='Safe error')
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(status,json=raw))) as client:
            if status == 400:
                with pytest.raises(ProviderAccessError) as caught:
                    await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode=mode,request=request)
                usage = caught.value.usage
            else:
                result = await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode=mode,request=request)
                assert result.outcome == 'length'
                usage = result.usage
    assert usage.output_tokens == 7
    response = next(row['content'] for row in evidence['rows'] if row['phase']=='response')
    assert response[wrapper] == [{}]
    assert 'opaque-sentinel' not in json.dumps(evidence['rows'])
    assert 'signature-sentinel' not in json.dumps(evidence['rows'])


def request_grammar_cases():
    cache = [dict(type='input_text',text='Question',prompt_cache_breakpoint=dict(mode='explicit'))]
    calls = [dict(id='call-1',type='function',function=dict(name='lookup',arguments='{}'))]
    ordinary = [
        dict(role='user',content='Question'),dict(type='message',role='user',content='Question'),
        dict(role='system',content='Instructions'),dict(role='developer',content='Instructions'),
        dict(role='assistant',content='Ok'),dict(role='assistant',content=cache),
        dict(role='user',content=[dict(type='text',text='Question')]),
        dict(role='tool',tool_call_id='call-1',content='Found'),
        dict(role='assistant',content=None,tool_calls=calls),
        dict(type='function_call_output',call_id='call-1',output='Found'),
        dict(type='function_call_output',call_id='call-1',output=cache),
        dict(type='function_call',call_id='call-1',name='lookup',arguments='{}'),
        dict(type='function_call',id='fc-1',call_id='call-1',name='lookup',arguments='{}'),
        dict(type='message',id='m',role='assistant',content=[dict(type='output_text',text='Ok')]),
    ]
    cases = [pytest.param(item,item,id=f'valid-{number}') for number,item in enumerate(ordinary)]
    cases.extend([
        pytest.param(dict(role='assistant',content='Ok',reasoning_details=[dict(type='reasoning.text',text='Checked.',signature='signature-sentinel')]),
            dict(role='assistant',content='Ok',reasoning_details=[dict(type='reasoning.text',text='Checked.')]),id='native-chat'),
        pytest.param(dict(type='reasoning',id='r',summary=[],encrypted_content='opaque-sentinel'),
            dict(type='reasoning',id='r',summary=[]),id='native-responses'),
    ])
    malformed = [None,dict(role='unknown',content='Question'),dict(role='user',content=OPAQUE),
        dict(role='user',content=[OPAQUE]),dict(role='user',content=[dict(type='text',text='Question',prompt_cache_breakpoint=dict(mode='unknown'))]),
        dict(role='user',content=[dict(type='text',text=OPAQUE)]),
        dict(role='tool',tool_call_id=None,content='Found'),
        dict(type='function_call_output',call_id='call-1',output=OPAQUE),
        dict(type='function_call_output',call_id='call-1',output=[OPAQUE]),
        dict(type='function_call_output',call_id='call-1',output=[dict(type='text',text='Found')]),
        dict(type='function_call_output',call_id='call-1',output=[dict(type='input_text',text=OPAQUE)]),
        dict(type='function_call_output',call_id='call-1',output=[dict(type='input_text',text='Found',prompt_cache_breakpoint=dict(mode='unknown'))]),
        dict(type='function_call_output',call_id='call-1',output='Found',signature='signature-sentinel'),
        dict(type='function_call',call_id='call-1',name='lookup',arguments='{}',signature='signature-sentinel'),
    ]
    cases.extend(pytest.param(item,{},id=f'withheld-{number}') for number,item in enumerate(malformed))
    return cases


@pytest.mark.parametrize('wrapper',['input','messages'])
@pytest.mark.parametrize('item,expected',request_grammar_cases())
@pytest.mark.parametrize('status',[200,400],ids=['incomplete','rejected'])
@pytest.mark.asyncio
async def test_echoed_request_grammar_preserves_valid_items_and_withholds_invalid_items(wrapper,item,expected,status):
    raw = partial_response('completions')
    raw[wrapper] = [copy.deepcopy(item)]
    if status == 400:
        raw['error'] = dict(message='Safe error')
    request = LLMRequest(MODEL,CompletionRequest((Message('user',(TextContent('Question'),)),)))
    with collect_inference(EmissionBoundary(ContentPolicy(True)),retain_raw=True) as evidence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(status,json=raw))) as client:
            if status == 400:
                with pytest.raises(ProviderAccessError) as caught:
                    await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='completions',request=request)
                usage = caught.value.usage
            else:
                result = await complete_text(client=client,base_url=BASE,access_token='credential-sentinel',subscription=False,api_mode='completions',request=request)
                assert result.outcome == 'length'
                usage = result.usage
    assert usage.output_tokens == 7
    response = next(row['content'] for row in evidence['rows'] if row['phase']=='response')
    assert response[wrapper] == [expected]
    assert 'opaque-sentinel' not in json.dumps(evidence['rows'])
    assert 'signature-sentinel' not in json.dumps(evidence['rows'])
