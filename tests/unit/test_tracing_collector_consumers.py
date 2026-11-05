"""Protected launch receipts govern normal Studio and shared setup collectors."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock

import pytest

from test_tracing_collector_snapshot import selected
from test_tracing_provider_extension import synthetic_provider
from test_workflow_observations import collector
from test_operation_traces import spans_from,attrs


@pytest.fixture
def default_selection(selected,tmp_path,request):
    from api_agent.config import load_shared_schema,resolve_config
    from api_agent.providers.loading import bootstrap_schema
    from api_agent.runtime.pack_composition import load_context_schema
    from api_agent.runtime.pack_admission import configuration_inputs
    parameters=getattr(request.node,'callspec',SimpleNamespace(params={})).params
    context,values,_,provider=selected
    values={**values,'UI_PROVIDER':str(context.base_root/'src/api_agent/ui/provider.py')+':create_provider'}
    with (provider/'cfg/config.toml').open('a') as stream:
        stream.write('\n[fields.SYNTHETIC_TRACE_EVENTS]\ntype="path"\nanchor="owner_root"\n')
    (provider/'provider.py').write_text('''import json
from pathlib import Path
from api_agent.telemetry.tracing_providers import ExportHandles
class Adapter:
    version=1
    def __init__(self,config,services):
        self.path=Path(config['SYNTHETIC_TRACE_EVENTS'])
        self.own=services['own_exporter']
    def event(self,name):
        with self.path.open('a') as stream:stream.write(json.dumps(name)+'\\n')
    def create_exporter(self,plan):
        from api_agent.telemetry.tracing import _sdk_factory
        self.event('sdk-open')
        provider,tracer=_sdk_factory(plan.trace_endpoint,dict(plan.headers.get('traces',{})),dict(plan.resource),own=self.own)
        return ExportHandles(provider,tracer)
    async def aclose(self):self.event('adapter-close')
def create_tracing_provider(config,services):
    adapter=Adapter(config,services)
    adapter.event('factory')
    return adapter
''')
    schema=load_context_schema(context,load_shared_schema(context.base_root))
    initial=resolve_config(schema,environ={},dotenv={},anchors=context.anchors,interactive=True,
        overrides={name:str(value) if type(value) is bool else value for name,value in values.items()})
    assert not initial.errors
    schema=bootstrap_schema(schema,(),{},{},initial.values,owner_root=context.owner_root,
        configuration_paths=context.provider_config_paths,**configuration_inputs(context))
    config=resolve_config(schema,environ={'TRACELOOP_TRACE_CONTENT':str(parameters.get('trace',False)),
        'LOG_CONTENT':str(parameters.get('log',False))},dotenv={},anchors=context.anchors,interactive=True,operation_intents={'ui_session'},
        overrides={**{name:str(value) if type(value) is bool else value for name,value in values.items()},'SYNTHETIC_TRACE_EVENTS':str(tmp_path/'events.jsonl'),
                   'LLM_ALLOWED_MODELS':'sample','LLM_DEFAULT_MODEL':'sample','DEBUG':'False'})
    assert not config.errors
    assert {'HELP', 'WORKFLOW_MAX_PASSES', 'WORKFLOW_COMPONENT_TIMEOUT_SECONDS'} <= config.values.keys()
    return context,config,provider


class Manager:
    timeout=2
    def __init__(self,plan,scope,directory,**kwargs):
        self.plan=plan
        self.events=Manager.events
        self.path=Manager.event_file
        self.held=False
        assert {item.id for item in plan.conditional}=={'synthetic/database','synthetic/collector'}
    def now(self):return 0
    def event(self,value):
        self.events.append(value)
        with self.path.open('a') as stream:stream.write(json.dumps(value)+'\n')
    def acquire(self,**kwargs):
        if not self.held:self.event('collector-acquire');self.held=True
    def release(self,**kwargs):
        if self.held:self.event('collector-release');self.held=False


def snapshot(directory,context,config,**kwargs):
    from api_agent.testing.provider_setup import write_launch_snapshot
    directory.mkdir()
    return write_launch_snapshot(directory,context,config.values,
        config_fields=config.fields,password_manager_registry=SimpleNamespace(definitions=()),**kwargs)


@pytest.mark.parametrize('trace,log',[(False,False),(False,True),(True,False),(True,True)])
def test_default_resolver_protected_writer_normal_studio_and_shared_host(default_selection,tmp_path,monkeypatch,trace,log):
    import httpx
    from api_agent.providers.llm import AvailableSelection,LLMResult,Message,TextContent,Usage
    from api_agent.testing import provider_setup
    from api_agent.testing.setup_backend import SetupBackend
    from api_agent.testing.studio_http import studio_backend_factory
    from api_agent.testing.workflow_capture import capture_sources
    from api_agent.telemetry.privacy import EmissionBoundary,ContentPolicy
    from api_agent.workflows.inference_capture import capture_inference
    from api_agent.testing.prerequisites import local_trace_prerequisites
    context,config,provider=default_selection
    events=[];Manager.events=events
    Manager.event_file=Path(config.values['SYNTHETIC_TRACE_EVENTS'])
    monkeypatch.setenv('DOCKER_HOST','unix:///tmp/synthetic-collector.sock')
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',Manager)
    class LLM:
        def __init__(self,*args,**kwargs):
            self.emissions=EmissionBoundary(ContentPolicy(log));self.closed=False
            self.provider=SimpleNamespace(implementation=SimpleNamespace())
        async def _ensure(self):pass
        async def _describe(self):return (AvailableSelection('sample'),)
        async def _preflight(self,value):return value
        async def _complete(self,value):
            capture_inference('output',{'text':'safe answer'},'synthetic')
            return LLMResult(Message('assistant',(TextContent('safe answer'),)),Usage(2,1))
        async def _close(self):self.closed=True
        async def _finish_execution(self):pass
    monkeypatch.setattr(provider_setup,'IntegrationLLM',LLM)
    with collector() as (endpoint,received):
        config.values['TRACE_EXPORT_ENDPOINT']=endpoint
        backend=SetupBackend(config.values,context.owner_root,capture_sources=capture_sources(config.values,config.fields),
            trace_prerequisites_factory=lambda values:local_trace_prerequisites(context,values))
        descriptor=backend.start()
        path=snapshot(tmp_path/'protected',context,config,setup_backend=descriptor)
        monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT',str(path))
        protected=provider_setup._read_snapshot()
        assert protected['trace_collector_plan']['version']==2
        assert path.stat().st_mode&0o777==0o600
        remote=provider_setup.RemoteIntegrationLLM(descriptor,config.values)
        factory=studio_backend_factory(remote,'sample')
        async def run():
            async with factory(config.values) as binding:
                headers={'Origin':config.values['UI_LISTENER_URL'],'X-API-Agent-Session':binding['access_token']}
                async with httpx.AsyncClient() as client:
                    response=await client.get(binding['agent_url']+'/app/agent/models',headers=headers)
                    assert response.status_code==200
                    response=await client.post(binding['agent_url']+'/app/agent/chat',headers=headers,json={
                        'session_id':None,'model':'sample','messages':[{'role':'user','content':'question'}]})
                    assert response.status_code==200,response.text
                    assert response.json()['response_text']=='safe answer'
        try:asyncio.run(run())
        finally:remote.close();backend.close()
    assert events.count('collector-acquire')==2 and events.count('collector-release')==2
    factory_events=[json.loads(line) for line in Path(config.values['SYNTHETIC_TRACE_EVENTS']).read_text().splitlines()]
    assert factory_events.count('sdk-open')==2 and factory_events.count('adapter-close')==2
    assert factory_events[:3]==['collector-acquire','factory','sdk-open']
    for index,event in enumerate(factory_events):
        if event=='collector-release':
            assert factory_events[:index].count('adapter-close')>factory_events[:index].count('collector-release')
    spans=spans_from(received)
    assert {'GET /app/agent/models', 'POST /app/agent/chat'} <= {span.name for span in spans}
    assert len([span for span in spans if attrs(span).get('gen_ai.operation.name')=={'stringValue':'chat'}])==1
    assert ('safe answer' in str(received)) is trace



def owner_from_protected(context,config,path,monkeypatch,*,runtime_factory=None):
    from api_agent.testing import provider_setup
    from api_agent.testing.studio_tracing import StudioTraceOwner,collector_from_snapshot
    from test_studio_retained_owner import Automated
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT',str(path))
    data=provider_setup._read_snapshot()
    owner=StudioTraceOwner(config.values,data['capture_sources'],Automated(),owner_root=context.owner_root,
        collector_snapshot=data,collector_factory=lambda values:collector_from_snapshot(data,values),
        **({'runtime_factory':runtime_factory} if runtime_factory is not None else {}))
    owner.bind_http('http://localhost:9443','synthetic-nonce')
    scope={'client':('127.0.0.1',1),'headers':[(b'origin',b'http://localhost:9443'),(b'x-api-agent-session',b'synthetic-nonce')]}
    return owner,scope,data


@pytest.mark.parametrize('change',['digest','missing','descriptor_replaced','assets_replaced'])
def test_protected_receipt_or_factory_identity_failure_is_optional_before_effects(default_selection,tmp_path,monkeypatch,caplog,change):
    context,config,provider=default_selection
    path=snapshot(tmp_path/'protected',context,config)
    data=json.loads(path.read_text())
    if change=='digest':data['trace_collector_plan']['descriptor']['sha256']='0'*64
    elif change=='missing':data.pop('trace_collector_plan')
    elif change=='descriptor_replaced':
        with (provider/'tracing-provider.toml').open('a') as stream:stream.write('\n# replacement after protected capture\n')
    else:data['trace_collector_plan']['assets'][0]['body']=''
    path.write_text(json.dumps(data))
    manager=Mock(side_effect=AssertionError('manager effect'))
    from api_agent.telemetry.tracing import TraceRuntime
    runtime=Mock(wraps=TraceRuntime)
    sdk=Mock(side_effect=AssertionError('SDK effect'))
    monkeypatch.setattr('api_agent.telemetry.tracing._sdk_factory',sdk)
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',manager)
    owner,scope,_=owner_from_protected(context,config,path,monkeypatch,runtime_factory=runtime)
    async def run():
        async with owner.request(scope) as selected:assert selected is None
        await owner.aclose()
    asyncio.run(run())
    manager.assert_not_called();sdk.assert_not_called()
    if change!='missing':runtime.assert_not_called()
    assert not Path(config.values['SYNTHETIC_TRACE_EVENTS']).exists()
    assert 'Studio tracing setup failed' in caplog.text


@pytest.mark.parametrize('mode',['custom','builtin_remote'])
@pytest.mark.parametrize('logging_fails',[False,True])
def test_protected_capture_failure_retains_unavailable_marker_and_logger_policy(default_selection,tmp_path,monkeypatch,caplog,mode,logging_fails):
    import logging
    from api_agent.testing import studio_tracing
    context,config,provider=default_selection
    if mode=='builtin_remote':config.values.update(TRACE_PROVIDER='',TRACE_MODE='OTEL',TRACE_EXPORT_ENDPOINT='https://collector.example.test/v1/traces')
    monkeypatch.setattr(studio_tracing,'capture_collector_plan',Mock(side_effect=ValueError('unpublishable source detail')))
    if logging_fails:
        monkeypatch.setattr(logging.getLogger('api_agent.testing.provider_setup'),'error',Mock(side_effect=OSError('diagnostic unavailable')))
        with pytest.raises(OSError,match='diagnostic unavailable'):snapshot(tmp_path/'protected',context,config)
        assert not (tmp_path/'protected/launch.json').exists()
        return
    path=snapshot(tmp_path/'protected',context,config)
    runtime=Mock(side_effect=AssertionError('SDK effect'))
    owner,scope,data=owner_from_protected(context,config,path,monkeypatch,runtime_factory=runtime)
    assert data['trace_collector_plan']=={}
    with pytest.raises(ValueError,match='unavailable'):
        studio_tracing.collector_from_snapshot(data,config.values)
    async def run():
        async with owner.request(scope) as selected:assert selected is None
        await owner.aclose()
    asyncio.run(run())
    runtime.assert_not_called()
    assert 'Tracing collector capture failed' in caplog.text
    assert 'unpublishable source detail' not in caplog.text


@pytest.mark.parametrize('trace,log',[(False,False),(False,True),(True,False),(True,True)])
def test_remote_custom_positive_receipt_never_touches_engine_or_schema(default_selection,tmp_path,monkeypatch,trace,log):
    from api_agent.runtime import tracing_collector_snapshot as helper
    context,config,provider=default_selection
    (provider/'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["traces"]\n')
    forbidden=Mock(side_effect=AssertionError('remote infrastructure effect'))
    monkeypatch.setattr(helper,'_schema',forbidden)
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',forbidden)
    monkeypatch.setenv('DOCKER_HOST','invalid transport must not be inspected')
    with collector() as (endpoint,received):
        config.values['TRACE_EXPORT_ENDPOINT']=endpoint
        path=snapshot(tmp_path/'protected',context,config)
        owner,scope,data=owner_from_protected(context,config,path,monkeypatch)
        assert data['trace_collector_plan']['selected']==[]
        async def run():
            async with owner.request(scope) as runtime:
                assert runtime is not None
                runtime.begin('node remote-owned',{}).end()
            await owner.aclose()
        asyncio.run(run())
    forbidden.assert_not_called()
    assert any(span.name=='node remote-owned' for span in spans_from(received))


@pytest.mark.parametrize('trace,log',[(False,False),(False,True),(True,False),(True,True)])
def test_disabled_resolved_selection_has_no_receipt_manager_factory_or_sdk(default_selection,tmp_path,monkeypatch,trace,log):
    from api_agent.testing import provider_setup
    from api_agent.testing.setup_backend import _SetupOwner
    from api_agent.telemetry.privacy import EmissionBoundary
    context,config,provider=default_selection
    config.values.update(TRACE_PROVIDER='',TRACE_MODE='')
    forbidden=Mock(side_effect=AssertionError('disabled tracing effect'))
    monkeypatch.setattr('api_agent.telemetry.tracing_providers.admit_binding',forbidden)
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',forbidden)
    monkeypatch.setattr(provider_setup,'IntegrationLLM',lambda *args,**kwargs:SimpleNamespace(emissions=EmissionBoundary(),_close=AsyncMock()))
    path=snapshot(tmp_path/'protected',context,config)
    owner,scope,data=owner_from_protected(context,config,path,monkeypatch,runtime_factory=forbidden)
    assert 'trace_collector_plan' not in data
    async def run():
        async with owner.request(scope) as runtime:assert runtime is None
        host=_SetupOwner(SimpleNamespace(config=config.values,owner_root=context.owner_root,
            unattended_index_source=None,password_manager_registry=None,interaction=None,quota_observer=None,
            authority=None,trace_prerequisites_factory=forbidden))
        assert host.trace is None and host.trace_prerequisites is None
        await host.aclose();await owner.aclose()
    asyncio.run(run())
    forbidden.assert_not_called()


@pytest.mark.parametrize('stage',['planning','admission'])
@pytest.mark.parametrize('logger_fails',[False,True])
def test_shared_host_optional_setup_failure_preserves_provider_and_fatal_diagnostic(default_selection,monkeypatch,stage,logger_fails):
    import sys
    from api_agent.testing import provider_setup
    from api_agent.testing.setup_backend import _SetupOwner
    from api_agent.telemetry.privacy import EmissionBoundary
    context,config,provider=default_selection
    llm=SimpleNamespace(emissions=EmissionBoundary(),_ensure=AsyncMock(),_close=AsyncMock())
    monkeypatch.setattr(provider_setup,'IntegrationLLM',lambda *args,**kwargs:llm)
    if stage=='planning':factory=Mock(side_effect=ValueError('unpublishable collector'))
    else:
        factory=Mock(return_value=None)
        monkeypatch.setattr('api_agent.telemetry.remote_inference.RemoteInferenceTrace',Mock(side_effect=ValueError('unpublishable exporter')))
    stderr=[]
    class Writer:
        def write(self,text):
            if logger_fails:raise OSError('diagnostic unavailable')
            stderr.append(text);return len(text)
        def flush(self):pass
    monkeypatch.setattr(sys,'stderr',Writer())
    async def run():
        args=SimpleNamespace(config=config.values,owner_root=context.owner_root,unattended_index_source=None,
            password_manager_registry=None,interaction=None,quota_observer=None,authority=None,
            capture_sources={},trace_prerequisites_factory=factory)
        if logger_fails:
            with pytest.raises(OSError,match='diagnostic unavailable'):_SetupOwner(args)
        else:
            host=_SetupOwner(args)
            await host.ensure_ready();await host.aclose()
            assert host._trace_unavailable and host.trace is None
            llm._ensure.assert_awaited_once()
            assert 'Continuing without tracing' in ''.join(stderr)
            assert 'unpublishable' not in ''.join(stderr)
    asyncio.run(run())


def test_captured_managed_sources_can_be_replaced_without_worker_rereads(default_selection,tmp_path,monkeypatch):
    context,config,provider=default_selection
    monkeypatch.setenv('DOCKER_HOST','unix:///tmp/synthetic-collector.sock')
    monkeypatch.delenv('CONTAINER_HOST',raising=False)
    Manager.events=[];Manager.event_file=Path(config.values['SYNTHETIC_TRACE_EVENTS'])
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',Manager)
    original={name:(provider/f'{name}.json').read_bytes() for name in ('database','collector')}
    with collector() as (endpoint,received):
        config.values['TRACE_EXPORT_ENDPOINT']=endpoint
        path=snapshot(tmp_path/'protected',context,config)
        for name in ('infrastructure.toml','database.json','collector.json'):
            (provider/name).write_text('invalid live replacement')
        owner,scope,_=owner_from_protected(context,config,path,monkeypatch)
        async def run():
            async with owner.request(scope) as runtime:
                assert runtime is not None
                plan=owner._sessions['automated'].manager.plan
                restored=json.loads(plan.assets['compose.json'])['services']
                assert {item.id.split('/')[1]:restored[item.compose_name]['image'] for item in plan.conditional}=={
                    name:json.loads(body)['image'] for name,body in original.items()}
                runtime.begin('node captured-source',{}).end()
            await owner.aclose()
        asyncio.run(run())
    assert Manager.events==['collector-acquire','collector-release']
    assert any(span.name=='node captured-source' for span in spans_from(received))


def test_worker_identity_failure_cannot_swallow_diagnostic_failure(default_selection,tmp_path,monkeypatch):
    import logging
    context,config,provider=default_selection
    path=snapshot(tmp_path/'protected',context,config)
    with (provider/'tracing-provider.toml').open('a') as stream:stream.write('\n# changed identity\n')
    monkeypatch.setattr(logging.getLogger('api_agent.testing.studio_tracing'),'error',Mock(side_effect=OSError('diagnostic unavailable')))
    runtime=Mock(side_effect=AssertionError('SDK effect'))
    owner,scope,_=owner_from_protected(context,config,path,monkeypatch,runtime_factory=runtime)
    async def run():
        with pytest.raises(OSError,match='diagnostic unavailable'):
            async with owner.request(scope):pytest.fail('Tracing diagnostic failure was hidden')
        await owner.aclose()
    asyncio.run(run())
    runtime.assert_not_called()


def test_shared_host_planning_cancellation_keeps_original_interruption(default_selection,monkeypatch):
    from api_agent.runtime.prerequisites import PrerequisiteCancelled
    from api_agent.testing import provider_setup
    from api_agent.testing.setup_backend import _SetupOwner
    from api_agent.telemetry.privacy import EmissionBoundary
    context,config,provider=default_selection
    llm=SimpleNamespace(emissions=EmissionBoundary())
    monkeypatch.setattr(provider_setup,'IntegrationLLM',lambda *args,**kwargs:llm)
    cancellation=PrerequisiteCancelled('intentional cancellation')
    monkeypatch.setattr(_SetupOwner,'_trace_failure',Mock(side_effect=AssertionError('Cancellation was converted into optional failure')))
    async def run():
        args=SimpleNamespace(config=config.values,owner_root=context.owner_root,unattended_index_source=None,
            password_manager_registry=None,interaction=None,quota_observer=None,authority=None,
            trace_prerequisites_factory=Mock(side_effect=cancellation))
        with pytest.raises(PrerequisiteCancelled) as caught:_SetupOwner(args)
        assert caught.value is cancellation
    asyncio.run(run())


def test_shared_host_without_managed_prerequisites_keeps_optional_trace_owner(default_selection,monkeypatch):
    from api_agent.testing import provider_setup
    from api_agent.testing.setup_backend import _SetupOwner
    from api_agent.telemetry.privacy import EmissionBoundary
    context,config,provider=default_selection
    llm=SimpleNamespace(emissions=EmissionBoundary(),_ensure=AsyncMock(),_close=AsyncMock())
    monkeypatch.setattr(provider_setup,'IntegrationLLM',lambda *args,**kwargs:llm)
    async def run():
        args=SimpleNamespace(config=config.values,owner_root=context.owner_root,unattended_index_source=None,
            password_manager_registry=None,interaction=None,quota_observer=None,authority=None,capture_sources={})
        host=_SetupOwner(args)
        assert host.trace_prerequisites is None and host.trace is not None
        await host.aclose()
        llm._close.assert_awaited_once()
    asyncio.run(run())


def test_protected_capture_cancellation_preserves_identity_without_warning_or_snapshot(default_selection,tmp_path,monkeypatch):
    from api_agent.runtime.prerequisites import PrerequisiteCancelled
    from api_agent.testing import studio_tracing
    context,config,provider=default_selection
    cancellation=PrerequisiteCancelled('operator interrupted capture')
    capture=Mock(side_effect=cancellation)
    diagnostic=Mock(side_effect=AssertionError('cancellation must not log optional failure'))
    monkeypatch.setattr(studio_tracing,'capture_collector_plan',capture)
    monkeypatch.setattr('logging.Logger.error',diagnostic)
    directory=tmp_path/'cancelled-protected'
    with pytest.raises(PrerequisiteCancelled) as caught:snapshot(directory,context,config)
    assert caught.value is cancellation
    capture.assert_called_once()
    diagnostic.assert_not_called()
    assert not (directory/'launch.json').exists()
