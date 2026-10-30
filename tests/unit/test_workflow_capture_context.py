"""Real provenance and complete local inventory govern workflow publication."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from api_agent.config.resolve import ResolvedValue
from api_agent.providers.contracts import ContractError
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.testing.authority_scope import TestAuthorityScope, authority_scope
from api_agent.testing.workflow_capture import (capture_sources, capture_config,
    worker_capture, provider_capture, _union_boundary)
from api_agent.workflows.capture_authority import resolve_capture_services


def snapshot(log=True, trace=False, *, explicit=True, source='dotenv'):
    values={'LOG_CONTENT':log,'TRACELOOP_TRACE_CONTENT':trace,'API_KEY':'local-secret'}
    fields={name:ResolvedValue(values[name],source,explicit) for name in ('LOG_CONTENT','TRACELOOP_TRACE_CONTENT')}
    return {'config':values,'capture_sources':capture_sources(values,fields)}


@pytest.mark.parametrize('log,trace',[(False,False),(False,True),(True,False),(True,True)])
def test_parent_provenance_independently_admits_content_flags(log,trace):
    data=snapshot(log,trace)
    resolved=capture_config(data['config'],data['capture_sources'])
    assert ContentPolicy.from_config(resolved).allow_content is log
    assert ContentPolicy.from_config(resolved,field_name='TRACELOOP_TRACE_CONTENT').allow_content is trace


@pytest.mark.parametrize('source,explicit',[('default',False),('provider',True),('dotenv',False)])
def test_untrusted_provenance_does_not_enable_graph_content(source,explicit):
    data=snapshot(source=source,explicit=explicit)
    resolved=capture_config(data['config'],data['capture_sources'])
    assert not ContentPolicy.from_config(resolved).allow_content


def test_plain_configuration_mapping_does_not_supply_provenance():
    values={'LOG_CONTENT':True}
    assert capture_sources(values,None)=={}
    assert not ContentPolicy.from_config(capture_config(values,{})).allow_content


@pytest.mark.parametrize('sources',[
    [],{'OTHER':{}},{'LOG_CONTENT':{}},
    {'LOG_CONTENT':{'value':1,'source':'dotenv','explicit':True}},
    {'LOG_CONTENT':{'value':True,'source':None,'explicit':True}},
    {'LOG_CONTENT':{'value':True,'source':'dotenv','explicit':1}},
    {'LOG_CONTENT':{'value':False,'source':'dotenv','explicit':True}},
])
def test_malformed_or_changed_parent_records_reject(sources):
    with pytest.raises(ContractError,match='provenance'):
        capture_config({'LOG_CONTENT':True},sources)


def test_changed_resolved_value_cannot_inherit_old_source():
    with pytest.raises(ContractError,match='conflicts'):
        capture_sources({'LOG_CONTENT':False},{'LOG_CONTENT':ResolvedValue(True,'dotenv',True)})


def test_worker_default_is_scoped_and_has_registered_config_credentials():
    data=snapshot()
    with authority_scope(('workflow',)) as scope:
        with worker_capture(data,scope,('workflow',)):
            services=resolve_capture_services({})
            assert services['content_policy'].allow_content
            assert services['emissions'].content_export({'text':'local-secret'})=={'text':'[REDACTED]'}
            assert scope.authorities()==(services['emissions'],)
        assert not resolve_capture_services({})


def test_absent_workflow_manifest_does_not_inject_capture_permission():
    with authority_scope(('coordinator',)) as scope:
        with worker_capture(snapshot(),scope,('coordinator',)):
            assert not resolve_capture_services({})


def test_local_inventory_must_be_complete_even_without_provider():
    async def run():
        with authority_scope(('tools','workflow')) as scope:
            with worker_capture(snapshot(),scope,('tools','workflow')):
                services=resolve_capture_services({})
                with pytest.raises(ContractError,match='unavailable'):
                    async with services['capture_authority']():
                        pytest.fail('incomplete inventory admitted')
                tools=EmissionBoundary(ContentPolicy(False),secrets=('tool-secret',))
                scope.join('tools',tools)
                async with services['capture_authority']() as boundary:
                    assert boundary.content_export({'text':'tool-secret local-secret'})=={'text':'[REDACTED] [REDACTED]'}
    asyncio.run(run())


def test_remote_preparation_precedes_full_local_freeze_and_late_registration_waits():
    async def run():
        local=EmissionBoundary(ContentPolicy(False),secrets=('tool-secret',))
        remote=EmissionBoundary(ContentPolicy(False),secrets=('remote-secret',))
        order=[]
        class Client:
            async def prepare_graph_capture(self):
                assert not local._authority.evidence_leases
                order.append('prepare')
            @asynccontextmanager
            async def graph_capture_authority(self):
                assert local._authority.evidence_leases
                order.append('acquire')
                lease=remote.begin_evidence_lease()
                try:yield remote
                finally:
                    lease.release();order.append('release')
        provider=SimpleNamespace(client=Client())
        with authority_scope(('tools','workflow')) as scope:
            scope.join('tools',local)
            with worker_capture(snapshot(False,True),scope,('tools','workflow')):
                old=resolve_capture_services({})
                with provider_capture(provider):
                    services=resolve_capture_services({})
                    assert services['capture_authority'] is not old['capture_authority']
                    async with services['capture_authority']() as boundary:
                        assert boundary.content_export({'text':'remote-secret tool-secret local-secret'})=={'text':'[REDACTED] [REDACTED] [REDACTED]'}
                        waiting=asyncio.create_task(local.aregister_secret('later-secret'))
                        await asyncio.sleep(0)
                        assert not waiting.done()
                    await waiting
                assert resolve_capture_services({})['capture_authority'] is old['capture_authority']
        assert order==['prepare','acquire','release']
    asyncio.run(run())


def test_cancellation_releases_local_inventory_after_remote_acquisition():
    async def run():
        remote=EmissionBoundary(secrets=('remote-secret',));released=[]
        class Client:
            async def prepare_graph_capture(self):pass
            @asynccontextmanager
            async def graph_capture_authority(self):
                try:yield remote
                finally:released.append(True)
        with authority_scope(('workflow',)) as scope:
            with worker_capture(snapshot(),scope,('workflow',)):
                with provider_capture(SimpleNamespace(client=Client())):
                    services=resolve_capture_services({});boundary=services['emissions']
                    with pytest.raises(asyncio.CancelledError):
                        async with services['capture_authority']():raise asyncio.CancelledError()
                    assert boundary._authority.evidence_leases==0
        assert released==[True]
    asyncio.run(run())


def test_credential_only_lease_does_not_grant_logging_permission():
    with authority_scope(('workflow',)) as scope:
        boundary=EmissionBoundary(ContentPolicy(False),secrets=('local-secret',));scope.join('workflow',boundary)
        with pytest.raises(ContractError):scope.freeze()
        lease=scope.freeze(credential_only=True)
        assert lease.secrets==('local-secret',)
        assert not boundary.policy.allow_content
        lease.release()
    with pytest.raises(ContractError):TestAuthorityScope(None).freeze(credential_only=True)


@pytest.mark.parametrize('mode',[None,1,'yes'])
def test_credential_only_mode_requires_exact_boolean(mode):
    with pytest.raises(ContractError,match='mode'):
        TestAuthorityScope(None).freeze(credential_only=mode)


@pytest.mark.parametrize('values',[[''],[1],['x'*16385],[str(i) for i in range(65)]])
def test_union_authority_budgets_fail_closed(values):
    with pytest.raises(ContractError):_union_boundary(values)


def test_protected_launch_snapshot_preserves_explicit_content_provenance(tmp_path,monkeypatch):
    from api_agent.testing.provider_setup import write_launch_snapshot,_read_snapshot
    data=snapshot(True,False)
    fields={name:ResolvedValue(row['value'],row['source'],row['explicit']) for name,row in data['capture_sources'].items()}
    path=write_launch_snapshot(tmp_path,SimpleNamespace(owner_root=tmp_path),data['config'],
        password_manager_registry=SimpleNamespace(definitions=()),config_fields=fields)
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT',str(path))
    restored=_read_snapshot()
    assert restored['capture_sources']==data['capture_sources']
    assert ContentPolicy.from_config(capture_config(restored['config'],restored['capture_sources'])).allow_content
    import json
    packet=json.loads(path.read_text());packet['capture_sources']['LOG_CONTENT']['value']=False
    path.write_text(json.dumps(packet))
    with pytest.raises(ContractError,match='context is unavailable'):_read_snapshot()


def test_profile_cannot_reuse_changed_content_setting_provenance():
    data=snapshot()
    data['config']['LOG_CONTENT']=False
    with authority_scope(('workflow',)) as scope:
        with pytest.raises(ContractError,match='provenance'):
            with worker_capture(data,scope,('workflow',)):pass


def test_backend_and_lazy_provider_share_native_operation_authority(tmp_path):
    from api_agent.testing.setup_backend import SetupBackend,_SetupOwner
    from api_agent.runtime.tasks import offload
    boundary=EmissionBoundary(ContentPolicy(True))
    backend=SetupBackend({'API_KEY':'backend-config-secret','LOG_CONTENT':False},tmp_path,authority=boundary)
    async def run():
        try:
            descriptor=await offload(backend.start)
            from api_agent.providers.setup_transport import SetupClient
            client=SetupClient(descriptor)
            try:
                await client.prepare_graph_capture()
                async with client.graph_capture_authority() as snapshot_boundary:
                    assert snapshot_boundary.content_export({'text':'backend-config-secret'})=={'text':'[REDACTED]'}
                # No provider/authentication owner was needed to capture input.
                assert backend.registry is not None
            finally:await offload(client.close)
        finally:await offload(backend.close)
    asyncio.run(run())
    # The owning constructor uses the same native authority with its own policy.
    loop=asyncio.new_event_loop()
    try:
        async def make():
            owner=_SetupOwner(SimpleNamespace(config={'LOG_CONTENT':False},owner_root=tmp_path,
                unattended_index_source=None,password_manager_registry=SimpleNamespace(definitions=()),
                interaction=None,quota_observer=None,authority=boundary))
            assert owner.provider.emissions._authority is boundary._authority
            assert not owner.provider.emissions.policy.allow_content
            await owner.provider.emissions.aregister_secret('later-provider-secret')
            assert boundary.content_export({'text':'later-provider-secret'})=={'text':'[REDACTED]'}
            await owner.aclose()
        loop.run_until_complete(make())
    finally:loop.close()


def test_backend_initialization_waits_for_credential_registration_before_opening_ipc(tmp_path):
    from api_agent.testing.setup_backend import SetupBackend
    async def run():
        authority=EmissionBoundary(ContentPolicy(True))
        lease=authority.begin_evidence_lease()
        backend=SetupBackend({'API_KEY':'new-seed-secret'},tmp_path,authority=authority)
        try:
            task=asyncio.create_task(backend._initialize())
            await asyncio.sleep(0)
            assert not task.done()
            assert backend.server is None
            lease.release()
            await task
            assert authority.content_export({'text':'new-seed-secret'})=={'text':'[REDACTED]'}
        finally:
            lease.release()
            await backend._dispose()
    asyncio.run(run())


@pytest.mark.parametrize('log,trace',[(False,False),(False,True),(True,False),(True,True)])
def test_actual_observed_workflow_admits_native_owners_before_remote_completion(tmp_path,monkeypatch,log,trace):
    """Input capture precedes auth; graph outputs and provider calls share owners."""
    import json
    from api_agent.providers.llm import LLMResult,Message,TextContent
    from api_agent.providers.setup_registry import SetupRegistry
    from api_agent.providers.setup_transport import SetupServer
    from api_agent.testing.provider_setup import RemoteIntegrationLLM
    from api_agent.runtime.tasks import offload
    from api_agent.workflows.application import build
    from api_agent.workflows.inference_capture import bind_boundary,capture_inference
    from api_agent.workflows.resources import bundled_root
    root=bundled_root()
    events=tmp_path/'events.jsonl';events.touch()
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING','1')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    remote=EmissionBoundary(ContentPolicy(False),secrets=('remote-secret',))
    provider_boundary=remote.for_policy(ContentPolicy(log))
    additional=EmissionBoundary(ContentPolicy(not log),secrets=('additional-secret',))
    called=[]
    class Owner:
        emissions=provider_boundary
        async def ensure_ready(self):called.append('ready')
        async def describe(self):return {}
        async def complete(self,request):
            called.append('complete')
            bind_boundary(self.emissions)
            capture_inference('input',{'messages':[{'text':'provider visible local-secret remote-secret additional-secret'}]},request.request_id)
            return LLMResult(Message('assistant',(TextContent('Ok'),)))
        async def aclose(self):pass
    async def run():
        registry=SetupRegistry('op','selected',Owner)
        server=SetupServer(registry,'synthetic-graph-capability-00000000',timeout=10,authority=remote)
        descriptor=await server.start()
        provider=RemoteIntegrationLLM(descriptor,{'LOG_CONTENT':log,'TRACELOOP_TRACE_CONTENT':trace,
            'LLM_SETUP_REQUEST_TIMEOUT_SECONDS':10})
        try:
            with authority_scope(('workflow',)) as scope:
                scope.join('workflow',additional)
                with worker_capture(snapshot(log,trace),scope,('workflow',)):
                    with provider_capture(provider):
                        async def complete(request):return await offload(provider.complete,request)
                        graph=build(root,llm=complete,selection='model')
                        assert not called
                        result=await graph.arun({'messages':[{'role':'user','content':'visible local-secret remote-secret additional-secret'}]})
                        assert result['llm_call']['response_text']=='Ok'
                        assert called.count('complete')==1
                        assert scope._leases==0
        finally:
            await offload(provider.close)
            await server.aclose();await registry.aclose()
    asyncio.run(run())
    text=events.read_text()
    assert all(secret not in text for secret in ('remote-secret','local-secret','additional-secret'))
    packets=[json.loads(line) for line in text.splitlines()]
    graph_packets=[row for row in packets if row.get('kind')=='workflow_evidence']
    assert graph_packets
    graph_data=graph_packets[-1]['data']
    if type(graph_data) is str:graph_data=json.loads(graph_data)
    captures=[row for row in graph_data['events'] if row.get('event')=='capture']
    node_captures=[row for row in captures if row.get('cause_id') is None]
    assert len(node_captures)==2
    node_input,= [row for row in node_captures if row['direction']=='input']
    node_output,= [row for row in node_captures if row['direction']=='output']
    visits={row['scope_id'] for row in graph_data['events']
        if row.get('event')=='scope_start' and row.get('kind')=='node' and row.get('owner')=='llm_call'}
    assert node_input['visit_id']==node_output['visit_id']
    assert node_input['visit_id'] in visits
    if log:
        assert node_input['reason']==node_output['reason']=='captured'
        assert node_input['values']['messages']==[{
            'role':'user','content':'visible [REDACTED] [REDACTED] [REDACTED]'}]
        assert node_output['values']['response_text']=='Ok'
        assert node_output['values']['selected_model']=='model'
    else:
        assert all('values' not in row for row in captures)
        assert all(row.get('reason')=='withheld' for row in captures)
    assert remote.policy.allow_content is False
    assert additional.policy.allow_content is (not log)


def test_reused_compiled_workflow_keeps_concurrent_invocation_authorities_isolated(tmp_path,monkeypatch):
    import json
    from contextvars import ContextVar
    from api_agent.providers.llm import LLMResult,Message,TextContent
    from api_agent.workflows.application import build
    from api_agent.workflows.attribution import current_workflow_visit
    from api_agent.workflows.resources import bundled_root
    events=tmp_path/'concurrent-events.jsonl';events.touch()
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING','1')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    expected=ContextVar('expected_capture_owner')
    async def run():
        entered=0
        both_inside=asyncio.Event()
        observed=[]
        async def complete(request):
            nonlocal entered
            label,owner,enabled=expected.get()
            entered+=1
            if entered==2:both_inside.set()
            await both_inside.wait()
            adapter=current_workflow_visit()[0]
            owners=adapter.credential_authorities()
            assert len(owners)==1
            assert owners[0]._authority is owner._authority
            assert adapter.log_policy.allow_content is enabled
            observed.append(label)
            return LLMResult(Message('assistant',(TextContent(label),)))
        # Construct once before either caller binds its capture context.
        graph=build(bundled_root(),llm=complete,selection='model')
        async def invoke(label,enabled):
            with authority_scope(('workflow',)) as scope:
                with worker_capture(snapshot(enabled,not enabled),scope,('workflow',)):
                    owner=resolve_capture_services({})['emissions']
                    token=expected.set((label,owner,enabled))
                    try:
                        result=await graph.arun({'messages':[{'role':'user','content':label+' local-secret'}]})
                        assert result['llm_call']['response_text']==label
                    finally:expected.reset(token)
        await asyncio.wait_for(asyncio.gather(invoke('alpha',True),invoke('beta',False)),5)
        assert sorted(observed)==['alpha','beta']
    asyncio.run(run())
    text=events.read_text()
    assert 'local-secret' not in text
    packets=[json.loads(line) for line in text.splitlines()]
    graphs=[row['data'] for row in packets if row.get('kind')=='workflow_evidence']
    assert len(graphs)==2
    graphs=[json.loads(graph) if type(graph) is str else graph for graph in graphs]
    has_content=[any(row.get('values') for row in graph['events'] if row.get('event')=='capture') for graph in graphs]
    assert sorted(has_content)==[False,True]
    assert len({graph['invocation_id'] for graph in graphs})==2


@pytest.mark.parametrize('field', [ResolvedValue(True, None, True), ResolvedValue(True, 'dotenv', 1)])
def test_capture_sources_rejects_malformed_resolved_provenance(field):
    with pytest.raises(ContractError, match='provenance'):
        capture_sources({'LOG_CONTENT': True}, {'LOG_CONTENT': field})


def test_remote_capture_rejects_non_native_authority_and_releases_local_lease():
    class Client:
        async def prepare_graph_capture(self):
            pass

        @asynccontextmanager
        async def graph_capture_authority(self):
            yield object()

    async def run():
        with authority_scope(('workflow',)) as scope:
            with worker_capture(snapshot(), scope, ('workflow',)):
                with provider_capture(SimpleNamespace(client=Client())):
                    capability = resolve_capture_services({})['capture_authority']
                    with pytest.raises(ContractError, match='unavailable'):
                        async with capability():
                            pytest.fail('non-native authority admitted')
                    assert scope._leases == 0
    asyncio.run(run())


def test_direct_provider_capture_enrolls_original_live_authority():
    provider = SimpleNamespace(emissions=EmissionBoundary(ContentPolicy(False), secrets=('direct-secret',)))
    async def run():
        with authority_scope(('workflow',)) as scope:
            with worker_capture(snapshot(), scope, ('workflow',)):
                with provider_capture(provider):
                    assert provider.emissions in scope.authorities()
                    capability = resolve_capture_services({})['capture_authority']
                    async with capability() as boundary:
                        assert boundary.content_export({'value': 'direct-secret'}) == {'value': '[REDACTED]'}
                assert not provider.emissions.policy.allow_content
    asyncio.run(run())


@pytest.mark.parametrize('owners', [[], (object(),)])
def test_visit_authorities_rejects_non_native_or_non_tuple_owner_inventory(owners):
    from api_agent.testing.workflow_capture import visit_authorities
    journal = SimpleNamespace(credential_authorities=lambda: owners)
    with pytest.raises(ContractError, match='unavailable'):
        visit_authorities((journal, 'visit'))


@pytest.mark.parametrize('owner', ['provider', 'backend'])
def test_integration_owner_rejects_non_native_credential_authority(tmp_path, owner):
    from api_agent.testing.provider_setup import IntegrationLLM
    from api_agent.testing.setup_backend import SetupBackend
    factory = IntegrationLLM if owner == 'provider' else SetupBackend
    with pytest.raises(ContractError, match='Invalid integration credential authority'):
        factory({}, tmp_path, authority=object())


def test_real_worker_fixture_binds_snapshot_only_during_its_yield(monkeypatch):
    from api_agent.testing import provider_setup
    data = snapshot()
    marker = SimpleNamespace(args=('workflow',), kwargs={})
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda name: marker))
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', 'synthetic-context')
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda request: data)
    fixture = provider_setup._test_credential_authority_scope.__wrapped__(request)
    assert next(fixture) is None
    assert resolve_capture_services({})['content_policy'].allow_content
    with pytest.raises(StopIteration):
        next(fixture)
    assert not resolve_capture_services({})
