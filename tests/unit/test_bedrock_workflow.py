"""Parsed flow and LocalLLM joins use real Mantle codecs, with an offline wire boundary."""
import asyncio
from copy import deepcopy
from pathlib import Path
import re
import time
import pytest
from api_agent.providers.control import OperationContext, OperationalError
from api_agent.providers.costs import validate_quote
from api_agent.providers.llm import CompletionRequest, GenerationOptions, LLMRequest, Message, TextContent, Tool, ToolResult
from api_agent.runtime.local_llm import LocalLLM
from api_agent.workflows.definition import load_definition
from api_agent.workflows.local_application import LocalChatApplication

ROOT = Path(__file__).resolve().parents[2]
MODELS = {'completions': 'zai.glm-4.7-flash', 'responses': 'openai.gpt-oss-20b'}
SCHEMA = '{"type":"object","properties":{},"required":[],"additionalProperties":false}'

@pytest.fixture(autouse=True)
def no_real_sdk(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('Offline integration cannot launch an SDK worker')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden)


def configuration(mode='completions', auth='session'):
    return {'LLM_PROVIDER': str(ROOT / 'src/api_agent/llm_providers/bedrock/factory.py') + ':factory',
        'LLM_ALLOWED_MODELS': re.escape(MODELS[mode]), 'LLM_DEFAULT_MODEL': re.escape(MODELS[mode]),
        'BEDROCK_AUTH_MODE': auth, 'BEDROCK_API_MODE': mode, 'BEDROCK_REGION': 'us-east-1', 'NO_STD_IN': True}


def raw(mode, *, text='Ok', calls=(), usage=True):
    model = MODELS[mode]
    tokens = dict(input_tokens=7, output_tokens=2, total_tokens=9) if mode == 'responses' else dict(prompt_tokens=7, completion_tokens=2, total_tokens=9)
    if mode == 'completions':
        message = {'role': 'assistant', 'content': None if calls else text}
        if calls:
            message['tool_calls'] = [{'id': c[0], 'type': 'function', 'function': {'name': c[1], 'arguments': c[2]}} for c in calls]
        result = {'model': model, 'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}]}
    else:
        output = [{'id': 'fc'+str(i), 'type': 'function_call', 'call_id': c[0], 'name': c[1], 'arguments': c[2], 'status': 'completed'} for i,c in enumerate(calls)] if calls else [dict(id='msg', type='message', role='assistant', status='completed', content=[dict(type='output_text', text=text, annotations=[])])]
        result = dict(id='r', model=model, status='completed', output=output)
    if usage is True: result['usage'] = tokens
    elif usage is not None: result['usage'] = usage
    return result


def events(mode, *, text='Ok', calls=(), truncated=False, usage=True):
    final = raw(mode, text=text, calls=calls, usage=usage)
    if mode == 'completions':
        def chunk(delta, finish=None):
            return dict(id='r', object='chat.completion.chunk', model=MODELS[mode], choices=[dict(index=0, delta=delta, finish_reason=finish)])
        yield chunk({'role': 'assistant'})
        if calls:
            for i,c in enumerate(calls):
                yield chunk({'tool_calls': [dict(index=i, id=c[0], type='function', function=dict(name=c[1], arguments=c[2]))]})
        else: yield chunk({'content': text})
        if not truncated:
            yield chunk({}, 'tool_calls' if calls else 'stop')
            yield dict(id='r', object='chat.completion.chunk', model=MODELS[mode], choices=[], usage=final.get('usage'))
        return
    rows = [dict(type='response.created', response=dict(id='r', status='in_progress')),
            dict(type='response.in_progress', response=dict(id='r', status='in_progress'))]
    for i,item in enumerate(final['output']):
        start = deepcopy(item); start['status'] = 'in_progress'
        start['arguments' if calls else 'content'] = '' if calls else []
        rows.append(dict(type='response.output_item.added', output_index=i, item=start))
        base = dict(output_index=i, item_id=item['id'])
        if calls:
            rows.extend([base | dict(type='response.function_call_arguments.delta', delta=item['arguments']),
                         base | dict(type='response.function_call_arguments.done', arguments=item['arguments'])])
        else:
            base['content_index']=0; part=item['content'][0]
            rows.extend([base | dict(type='response.content_part.added', part=dict(part,text='')),
                base | dict(type='response.output_text.delta', delta=text),
                base | dict(type='response.output_text.done', text=text),
                base | dict(type='response.content_part.done', part=part)])
        rows.append(dict(type='response.output_item.done', output_index=i, item=item))
    if not truncated: rows.append(dict(type='response.completed', response=final))
    yield from (row | dict(sequence_number=i) for i,row in enumerate(rows))


def request(mode, messages=None, tools=()):
    return LLMRequest(MODELS[mode], CompletionRequest(messages or (Message('user',(TextContent('Return Ok.'),)),),
        tools=tools, generation=GenerationOptions(max_output_tokens=16)))

@pytest.mark.parametrize('mode', MODELS)
@pytest.mark.parametrize('auth_mode', ['session', 'access_key', 'api_key'])
def test_parsed_application_runs_real_auth_factory_codec_and_local_llm(monkeypatch, mode, auth_mode):
    from api_agent.providers.sessions import ScopedRoleCredentials
    from api_agent.runtime import local_llm
    reads=[]
    async def retrieve(role,index,kind,**kwargs):
        reads.append((role,index,kind)); return {'username':'synthetic-access', 'password':'synthetic-secret'}[kind]
    monkeypatch.setattr(local_llm, 'bind_role_credentials', lambda *a, **k: ScopedRoleCredentials(('llm',), 0, retrieve))
    async def run():
        config=configuration(mode,auth_mode)
        if auth_mode != 'session': config.update(UNATTENDED_USER_INDEX=0, LLM_CREDENTIAL_ROLE='llm')
        owner=LocalLLM(config,ROOT,registry={}); calls=[]
        try:
            await owner.ensure_ready(); implementation=owner.provider.implementation
            async def sdk(operation,payload,*,auth):
                values=auth.worker_config(); assert values['mode']==('access_key' if auth_mode=='session' else auth_mode) and values['region']=='us-east-1'
                calls.append((operation,payload))
                if operation=='models': return {'data':[{'id':MODELS[mode]}]}
                assert operation==mode and payload['prefix']=='/v1'
                assert payload['body']['model']==MODELS[mode]
                body=payload['body']
                assert body['max_output_tokens' if mode=='responses' else 'max_tokens']==16
                history=body['input' if mode=='responses' else 'messages']
                assert history[0]['role']=='user' and 'Return Ok.' in str(history[0]['content'])
                if mode=='responses': assert payload['body']['store'] is False
                return raw(mode)
            implementation.transport.call=sdk
            definition=load_definition(ROOT/'flow.md.html',base_root=ROOT,pack_root=ROOT)
            app=LocalChatApplication(definition,config=config)
            context=OperationContext('offline-mantle',time.monotonic()+30)
            try: result=await app.complete(request(mode),llm=owner,context=context)
            finally: await context.aclose()
            assert result.payload.content==(TextContent('Ok'),)
            assert calls[-1][0]==mode and len(owner.usage)==1
            quote=validate_quote(owner.usage[0]['quote'])
            assert quote['list_cost_usd'] == {'responses': pytest.approx(.00000109), 'completions': None}[mode]
            assert quote['payment'] is None
            assert quote['accounting']['rate_card']['models'][MODELS[mode]]['input'] == {'responses': .07 / 1000000, 'completions': None}[mode]
            assert owner.usage[0]['usage']['input_tokens']==7
        finally: await owner.aclose()
        assert implementation.closed and implementation.sessions==[]
    asyncio.run(run())
    assert [row[2] for row in reads]==({'session':[], 'access_key':['username','password'], 'api_key':['password']}[auth_mode])

@pytest.mark.parametrize('mode', MODELS)
def test_tool_turn_round_trip(mode):
    async def run():
        owner=LocalLLM(configuration(mode),ROOT,registry={}); calls=[]
        try:
            await owner.ensure_ready()
            async def sdk(operation,payload,*,auth):
                if operation=='models': return {'data':[{'id':MODELS[mode]}]}
                calls.append(payload['body'])
                if len(calls)==1: return raw(mode,calls=(('lookup-1','lookup','{}'),))
                history=payload['body']['input' if mode=='responses' else 'messages']
                assert any(row.get('type')=='function_call_output' and row['call_id']=='lookup-1' and row['output']=='found' for row in history) if mode=='responses' else history[-1]=={'role':'tool','tool_call_id':'lookup-1','content':'found'}
                return raw(mode,text='Done')
            owner.provider.implementation.transport.call=sdk
            tool=Tool('lookup','Lookup',SCHEMA); req=request(mode,tools=(tool,))
            first=await owner.complete(req); assert first.outcome=='tool_calls'
            second=await owner.complete(request(mode, req.payload.messages+(first.payload,Message('tool',results=(ToolResult('lookup-1',(TextContent('found'),)),))), (tool,)))
            assert second.payload.content==(TextContent('Done'),) and len(owner.usage)==2
        finally: await owner.aclose()
    asyncio.run(run())

@pytest.mark.parametrize('mode', MODELS)
@pytest.mark.parametrize('variant',['text','valid','undeclared','invalid_arguments','duplicate','truncated'])
def test_stream_buffers_tools_through_eof_and_result_validation(mode,variant):
    async def run():
        owner=LocalLLM(configuration(mode),ROOT,registry={}); ended=[]; delivered=[]
        try:
            await owner.ensure_ready(); implementation=owner.provider.implementation
            calls=() if variant=='text' else (('id','unrequested' if variant=='undeclared' else 'lookup','{"wrong":1}' if variant=='invalid_arguments' else '{}'),)
            if variant=='duplicate': calls=calls+calls
            async def sdk_stream(*a,**k):
                for row in events(mode,calls=calls,truncated=variant=='truncated'): yield row
                ended.append(True)
            async def models(*a,**k): return {'data':[{'id':MODELS[mode]}]}
            implementation.transport.call=models
            implementation.transport.stream=sdk_stream
            auth=await owner.session._auth(); stream=owner.provider.require('llm','stream').astream(request(mode,tools=(Tool('lookup','Lookup',SCHEMA),)),auth=auth)
            try:
                if variant in ('text','valid'):
                    first=await anext(stream); delivered.append(first)
                    assert first.kind==('text' if variant=='text' else 'tool_call')
                    if variant=='valid': assert ended==[True]
                    delivered.extend([row async for row in stream]); assert delivered[-1].kind=='completed'
                    if variant=='text': assert first.payload.text=='Ok'
                else:
                    with pytest.raises(OperationalError): delivered.append(await anext(stream))
                    assert delivered==[] and owner.usage==()
            finally: await stream.aclose()
        finally: await owner.aclose()
    asyncio.run(run())

@pytest.mark.parametrize('mode', MODELS)
@pytest.mark.parametrize('usage',[None,{}, {'total_tokens':True}])
def test_missing_usage_fails_without_completed_accounting(mode,usage):
    async def run():
        owner=LocalLLM(configuration(mode),ROOT,registry={})
        try:
            await owner.ensure_ready()
            async def sdk(operation,payload,*,auth):
                return {'data':[{'id':MODELS[mode]}]} if operation=='models' else raw(mode,usage=usage)
            owner.provider.implementation.transport.call=sdk
            with pytest.raises(OperationalError): await owner.complete(request(mode))
            assert owner.usage and all(row['outcome']=='failed' and row['usage'] is None and 'quote' not in row for row in owner.usage)
        finally: await owner.aclose()
    asyncio.run(run())

@pytest.mark.parametrize('mode',MODELS)
def test_stream_invalid_usage_has_no_completed_or_cost_event(mode):
    async def run():
        owner=LocalLLM(configuration(mode),ROOT,registry={}); delivered=[]
        try:
            await owner.ensure_ready()
            async def sdk_stream(*a,**k):
                for row in events(mode,usage={'total_tokens':True}): yield row
            async def models(*a,**k): return {'data':[{'id':MODELS[mode]}]}
            owner.provider.implementation.transport.call=models
            owner.provider.implementation.transport.stream=sdk_stream
            auth=await owner.session._auth(); stream=owner.provider.require('llm','stream').astream(request(mode),auth=auth)
            with pytest.raises(OperationalError):
                async for row in stream: delivered.append(row)
            assert [row.kind for row in delivered]==['text'] and owner.usage==()
        finally: await owner.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['completions', 'responses'])
def test_bootstrapped_provider_default_selects_oss20b_from_real_local_catalog(mode):
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.providers.loading import bootstrap_schema
    overrides={'LLM_PROVIDER':configuration()['LLM_PROVIDER'], 'BEDROCK_API_MODE':mode, 'NO_STD_IN':'True', 'TUI_MODE':'run-local'}
    schema=bootstrap_schema(load_shared_schema(),['--help'],{},{},overrides,owner_root=ROOT)
    config=resolve_config(schema,argv=[],environ={},dotenv={},overrides=overrides)
    assert not config.errors
    async def run():
        owner=LocalLLM(config.values,ROOT,registry={})
        try:
            await owner.ensure_ready()
            async def sdk(operation,payload,*,auth):
                assert operation=='models'
                return {'data':[{'id':'zai.glm-5'},{'id':'openai.gpt-oss-20b'},{'id':'zai.glm-4.7-flash'}]}
            owner.provider.implementation.transport.call=sdk
            chosen=owner.policy.select_default(await owner.describe())
            assert chosen=='openai.gpt-oss-20b'
        finally: await owner.aclose()
    asyncio.run(run())


@pytest.fixture(autouse=True)
def frozen_ambient(monkeypatch):
    from api_agent.llm_providers.bedrock.ambient import AmbientSession
    from api_agent.llm_providers.bedrock.acquisition import SessionMaterial
    async def acquire(self, request, **kwargs):
        return SessionMaterial('frozen', access_key='synthetic-key', secret_key='synthetic-secret'), {}
    monkeypatch.setattr(AmbientSession, 'acquire', acquire)
