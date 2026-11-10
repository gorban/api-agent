"""Normal HTTP and retained manual tracing use distinct acknowledged owners."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from api_agent.testing.studio_tracing import StudioTraceOwner
from api_agent.testing.studio_retained import RetainedStudio
from api_agent.testing.workflow_capture import capture_sources
from test_studio_retained_owner import manual, Automated
from test_workflow_observations import collector, config
from test_operation_traces import spans_from, attrs


@pytest.mark.parametrize('trace,log', [(False,False),(False,True),(True,False),(True,True)])
def test_normal_http_retained_manual_has_separate_graph_and_frozen_evidence(manual, monkeypatch, tmp_path, trace, log):
    import httpx
    from api_agent.testing.studio_http import agent_endpoint
    from api_agent.workflows.definition import load_definition
    from api_agent.runtime.local_tools import selected_application
    from api_agent.workflows import evidence
    factory, seen, _ = manual
    frozen = tmp_path/'finalized-events'
    frozen.write_bytes(b'finalized suite\n')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(frozen))
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING','1')
    journals=[]
    monkeypatch.setattr(evidence,'publish',journals.append)
    root=Path(__file__).resolve().parents[2]
    application=selected_application(load_definition(root/'flow.md.html',base_root=root,pack_root=root))
    with collector() as (endpoint, received):
        settings=config(log,trace,TRACE_EXPORT_ENDPOINT=endpoint)
        async def run():
            retained=RetainedStudio(Automated(),factory,lambda:True)
            owner=StudioTraceOwner(settings.values,capture_sources(settings.values,settings.fields),retained)
            headers={'Origin':'http://localhost:9443','X-API-Agent-Session':'x'*32}
            async with agent_endpoint(retained,'sample','http://localhost:9443','x'*32,
                                      application=application,tracing=owner) as url:
                async with httpx.AsyncClient(timeout=60) as client:
                    denied=await client.get(url+'/app/agent/models')
                    assert denied.status_code==403 and not owner._sessions
                    models=await client.get(url+'/app/agent/models',headers=headers)
                    assert models.status_code==200
                    body={'model':'sample','session_id':str(uuid4()),'messages':[{'role':'user','content':'synthetic prompt'}]}
                    automated=await client.post(url+'/app/agent/chat',headers=headers,json=body)
                    assert automated.status_code==200
                    # One completed suite operation is now immutable.
                    baseline=frozen.read_bytes()
                    journal_count=len(journals)
                    body['session_id']=str(uuid4())
                    later=await client.post(url+'/app/agent/chat',headers=headers,json=body)
                    assert later.status_code==200 and later.json()['response_text']=='manual'
                    assert owner._sessions['automated'].closed
                    assert not owner._sessions['manual'].closed
                    assert frozen.read_bytes()==baseline and len(journals)==journal_count
                    assert retained.usage.report()['prompt_count']==1
            assert owner._closed and owner._sessions['manual'].closed
        asyncio.run(run())
    spans=spans_from(received)
    graphs=[span for span in spans if span.name=='graph GRAPH']
    # The selected root flow is parsed, with its own authored graph identifier.
    if not graphs:
        graphs=[span for span in spans if span.name.startswith('graph ')]
    assert len(graphs)==2
    assert graphs[0].trace_id!=graphs[1].trace_id
    for graph in graphs:
        request=next(span for span in spans if span.span_id==graph.parent_span_id)
        assert request.name=='POST /app/agent/chat'
        assert request.start_time_unix_nano<=graph.start_time_unix_nano<=graph.end_time_unix_nano<=request.end_time_unix_nano
    calls=[span for span in spans if attrs(span).get('gen_ai.operation.name')=={'stringValue':'chat'}]
    assert len(calls)==1  # Retained manual uses its existing accounting journal.
    assert calls[0].trace_id==graphs[1].trace_id
    assert 'x'*32 not in repr(received)


def test_trace_session_waits_for_active_request_before_flush_and_release():
    from api_agent.testing.studio_tracing import _TraceSession
    from types import SimpleNamespace
    actions=[]
    class Runtime:
        async def astart(self): actions.append('start')
        async def aclose(self): actions.append('flush')
    class Manager:
        timeout=2
        def now(self): return 0
        def acquire(self, **kwargs): actions.append('acquire')
        def release(self, **kwargs): actions.append('release')
    async def run():
        session=_TraceSession(Runtime(),Manager())
        await session.enter()
        close=asyncio.create_task(session.aclose())
        await asyncio.sleep(0)
        assert actions==['acquire','start'] and not close.done()
        await session.leave()
        await close
        assert actions==['acquire','start','flush','release']
        await session.aclose()
        assert actions.count('flush')==1
        with pytest.raises(RuntimeError): await session.enter()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['responses','completions','converse'])
@pytest.mark.parametrize('trace,log', [(False,False),(False,True),(True,False),(True,True)])
def test_actual_native_http_transport_maps_safe_messages_and_structural_fields(manual, mode, trace, log):
    import httpx
    from dataclasses import replace
    from api_agent.testing import provider_setup
    from api_agent.testing.studio_http import agent_endpoint
    from api_agent.llm_providers.gpt.inference import complete_text
    from api_agent.workflows.definition import load_definition
    from api_agent.runtime.local_tools import selected_application
    from test_gpt_optional_contracts import native
    factory, seen, _ = manual
    implementation=provider_setup.aload_provider.return_value.implementation
    implementation.captures_effective_payload=True
    implementation.telemetry_provider_name='independent.provider'
    sent=[]
    def respond(request):
        sent.append(request)
        body=native(mode,'structured','visible transport answer')
        body['id']='response-synthetic-1'
        return httpx.Response(200,json=body)
    async def complete(request, *, auth):
        if mode=='converse':
            from api_agent.llm_providers.bedrock.factory import factory as bedrock_factory
            provider=bedrock_factory({'BEDROCK_ENDPOINT':'runtime','BEDROCK_API_MODE':'converse'}, {})
            provider.bind_credential_authority(implementation.authority)
            async def dispatch(api,payload,**kwargs):
                sent.append(payload)
                kwargs['on_dispatch']()
                return {'output':{'message':{'role':'assistant','content':[{'text':'visible transport answer'}]}},
                        'stopReason':'end_turn', 'usage':{'inputTokens':9,'outputTokens':3,'totalTokens':12,
                                                        'cacheReadInputTokens':4}}
            provider.transport.call=dispatch
            try:
                result=await provider.acomplete(replace(request,
                    selected_llm='us.anthropic.claude-haiku-4-5-20251001-v1:0'),auth=auth)
                return replace(result,actual_model='served-alias')
            finally:
                await provider.aclose()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=await complete_text(client=client,base_url='https://api.openai.com/v1',
                access_token='synthetic-native-secret',subscription=False,api_mode=mode,
                request=replace(request,selected_llm='gpt-synthetic'),authority=implementation.authority)
            return replace(result,actual_model='served-alias')
    implementation.acomplete=complete
    root=Path(__file__).resolve().parents[2]
    application=selected_application(load_definition(root/'flow.md.html',base_root=root,pack_root=root))
    with collector() as (endpoint,received):
        settings=config(log,trace,TRACE_EXPORT_ENDPOINT=endpoint)
        async def run():
            retained=RetainedStudio(Automated(),factory,lambda:True)
            retained.retire_test()
            owner=StudioTraceOwner(settings.values,capture_sources(settings.values,settings.fields),retained)
            async with agent_endpoint(retained,'sample','http://localhost:9443','x'*32,
                                      application=application,tracing=owner) as url:
                async with httpx.AsyncClient(timeout=60) as client:
                    result=await client.post(url+'/app/agent/chat',
                        headers={'Origin':'http://localhost:9443','X-API-Agent-Session':'x'*32},json={
                          'model':'sample','session_id':str(uuid4()),'messages':[{'role':'user','content':'visible transport prompt'}]})
                    assert result.status_code==200, result.text
                    assert result.json()['response_text']=='visible transport answer'
        asyncio.run(run())
    assert len(sent)==1
    calls=[span for span in spans_from(received) if attrs(span).get('gen_ai.operation.name')=={'stringValue':'chat'}]
    assert len(calls)==1
    fields=attrs(calls[0])
    assert fields['gen_ai.provider.name']=={'stringValue':'independent.provider'}
    assert fields['gen_ai.response.model']=={'stringValue':'served-alias'}
    assert fields['gen_ai.usage.input_tokens']=={'intValue':'13' if mode=='converse' else '9'}
    assert fields['gen_ai.usage.cache_read.input_tokens']=={'intValue':'4'}
    reasons=fields.get('gen_ai.response.finish_reasons')
    if mode=='responses':
        assert reasons is None
    else:
        assert reasons=={'arrayValue':{'values':[{'stringValue':'end_turn' if mode=='converse' else 'stop'}]}}
    for direction in ('input','output'):
        key='gen_ai.'+direction+'.messages'
        assert (key in fields) is trace
        if trace:
            messages=__import__('json').loads(fields[key]['stringValue'])
            assert messages and messages[0]['parts']
            assert ('visible transport prompt' if direction=='input' else 'visible transport answer') in fields[key]['stringValue']
    assert 'synthetic-native-secret' not in repr(received)


@pytest.mark.parametrize('stage', ['sdk','collector'])
def test_session_cleanup_failure_retains_lease_for_acknowledged_retry(stage):
    from api_agent.testing.studio_tracing import _TraceSession,StudioTraceCleanupPending
    actions=[]
    class Runtime:
        calls=0
        async def aclose(self):
            self.calls+=1
            actions.append('sdk')
            if stage=='sdk' and self.calls==1:raise RuntimeError('synthetic-sensitive-sdk')
    class Manager:
        calls=0
        def release(self,**kwargs):
            self.calls+=1
            actions.append('release')
            if stage=='collector' and self.calls==1:raise RuntimeError('synthetic-sensitive-collector')
    async def run():
        session=_TraceSession(Runtime(),Manager())
        with pytest.raises(StudioTraceCleanupPending) as error:await session.aclose()
        assert 'synthetic-sensitive' not in str(error.value)
        assert not session.closed
        if stage=='sdk':assert actions==['sdk']
        await session.aclose()
        assert session.closed
    asyncio.run(run())
