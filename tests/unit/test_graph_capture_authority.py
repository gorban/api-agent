import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from api_agent.providers.authority_channel import AuthorityChannel, leased_snapshot
from api_agent.providers.contracts import ContractError
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.capture_authority import bind_capture_services, resolve_capture_services, capture_node
from api_agent.workflows.journal import WorkflowJournal

KEY=b'k'*32
BINDING={'worker_id':'example-worker'}


def definition():
    node=SimpleNamespace(id='Make',kind='node',parent=None)
    layout=SimpleNamespace(steps=[node],tables={'Edges':[], 'Functions':[{'ID':'Make','Kind':'deterministic'}]})
    resource=SimpleNamespace(kind='declaration',logical_path='flows/main.md.html',sha256='a'*64,content_bytes=b'# Workflow v2')
    return SimpleNamespace(graph_id='Example',definition_id='b'*64,entry_root='base',layout=layout,resources=[resource],replacements=[])


def journal(boundary):
    return WorkflowJournal(definition(),log_content=True,content_policy=ContentPolicy(True),boundary=boundary)


def test_explicit_partial_bundle_does_not_inherit_permission_or_remote_authority():
    remote=object()
    with bind_capture_services({'content_policy':ContentPolicy(True),'capture_authority':remote}):
        assert resolve_capture_services({})['capture_authority'] is remote
        selected=resolve_capture_services({'emissions':None})
        assert 'content_policy' not in selected
        assert 'capture_authority' not in selected
        assert selected['emissions'] is None


def test_reverse_lease_scrubs_and_blocks_registration_until_release():
    async def run():
        boundary=EmissionBoundary(secrets=['synthetic-credential'])
        channel=AuthorityChannel(KEY,lambda *_:None,snapshot_source=lambda binding: boundary)
        path=await channel.start()
        try:
            async with leased_snapshot(path,KEY,BINDING) as snapshot:
                assert snapshot.content_export({'text':'visible synthetic-credential'})=={'text':'visible [REDACTED]'}
                pending=asyncio.create_task(boundary.aregister_secret('late-credential'))
                await asyncio.sleep(0)
                assert not pending.done()
            await asyncio.wait_for(pending,1)
            assert 'late-credential' in boundary._secrets
        finally:
            await channel.close()
    asyncio.run(run())


def test_stale_binding_after_snapshot_receipt_never_admits_capture():
    async def run():
        boundary=EmissionBoundary(secrets=['synthetic-credential'])
        calls=[]
        def source(binding):
            calls.append(binding)
            if len(calls)==4:
                raise ContractError('Binding revoked')
            return boundary
        channel=AuthorityChannel(KEY,lambda *_:None,snapshot_source=source)
        path=await channel.start()
        try:
            with pytest.raises(ContractError):
                async with leased_snapshot(path,KEY,BINDING):
                    pytest.fail('Revoked binding admitted')
            assert boundary._authority.evidence_leases==0
        finally:
            await channel.close()
    asyncio.run(run())


def test_graph_commit_releases_authority_before_reentrant_subscriber():
    async def run():
        local=EmissionBoundary(ContentPolicy(True))
        remote=EmissionBoundary(secrets=['synthetic-credential'])
        observed=[]
        record=journal(local)
        def observer(row):
            assert remote._authority.evidence_leases==0
            remote.register_secret('subscriber-credential')
            observed.append(row)
        record.subscribe(observer)
        @asynccontextmanager
        async def capability():
            lease=remote.begin_evidence_lease()
            try:
                yield remote.snapshot_content()[1]
            finally:
                lease.release()
        result=await capture_node(record,{'capture_authority':capability},visit_id='visit',direction='input',
            values={'text':'visible synthetic-credential'},schema={'type':'object'})
        assert result['values']=={'text':'visible [REDACTED]'}
        assert observed==[result]
        assert record.finalize()['events'][0]['values']==result['values']
    asyncio.run(run())


def test_unavailable_authority_never_reaches_journal():
    async def run():
        record=journal(EmissionBoundary(ContentPolicy(True)))
        @asynccontextmanager
        async def unavailable():
            raise ContractError('Unavailable')
            yield
        result=await capture_node(record,{'capture_authority':unavailable},visit_id='visit',direction='input',
            values={'text':'must-not-be-retained'},schema={'type':'object'})
        assert result['reason']=='unavailable'
        assert result.get('values')=={}
        assert 'must-not-be-retained' not in str(record.finalize())
    asyncio.run(run())


@pytest.mark.parametrize('log,trace',[(False,False),(False,True),(True,False),(True,True)])
def test_graph_admission_preserves_independent_destination_flags(log,trace):
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    from api_agent.workflows.observations import WorkflowObservationFactory
    async def run():
        boundary=EmissionBoundary(ContentPolicy(log))
        remote=EmissionBoundary(secrets=['synthetic-credential'])
        rows=[]
        events=[]
        class Span:
            def event(self,row): events.append(row)
            def attributes(self,row): pass
            def end(self,**kwargs): pass
        trace_runtime=SimpleNamespace(boundary=boundary.for_policy(ContentPolicy(trace)),
            is_model_identity=lambda _:False,begin=lambda *args,**kwargs:Span())
        values={'LOG_CONTENT':log,'TRACELOOP_TRACE_CONTENT':trace}
        config=ConfigResult(values,{key:ResolvedValue(value,'cli',True) for key,value in values.items()},(),(),())
        record=WorkflowObservationFactory(config,boundary=boundary,sink=rows.append,trace=trace_runtime).create(definition())
        record.observe(dict(event='scope_start',scope_id='visit',owner='Make',kind='node'))
        @asynccontextmanager
        async def capability():
            lease=remote.begin_evidence_lease()
            try:
                yield remote.snapshot_content()[1]
            finally:
                lease.release()
        await capture_node(record,{'capture_authority':capability},visit_id='visit',direction='input',
            values={'text':'visible synthetic-credential'},schema={'type':'object'})
        retained=next(row for row in record.finalize()['events'] if row['event']=='capture')
        live=next(row for row in rows if row['event']=='capture')
        assert ('values' in retained) is log
        assert ('values' in live) is log
        assert len(events) == 1
        if not trace:
            assert events[0] == {'values': {}}
        assert 'synthetic-credential' not in str((retained,rows,events))
        if trace:
            assert events[0]['values']['text']=='visible [REDACTED]'
    asyncio.run(run())


def test_cancelled_reverse_capture_releases_registration_waiter():
    async def run():
        boundary=EmissionBoundary(secrets=['synthetic-credential'])
        channel=AuthorityChannel(KEY,lambda *_:None,snapshot_source=lambda binding:boundary)
        path=await channel.start()
        entered=asyncio.Event()
        async def capture():
            async with leased_snapshot(path,KEY,BINDING):
                entered.set()
                await asyncio.Future()
        task=asyncio.create_task(capture())
        try:
            await entered.wait()
            pending=asyncio.create_task(boundary.aregister_secret('late-credential'))
            await asyncio.sleep(0)
            assert not pending.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(pending,1)
            assert boundary._authority.evidence_leases==0
        finally:
            await channel.close()
    asyncio.run(run())


def test_graph_authority_keeps_long_payload_and_truthful_overflow():
    async def run():
        boundary=EmissionBoundary(ContentPolicy(True))
        record=journal(boundary)
        @asynccontextmanager
        async def capability():
            yield EmissionBoundary(ContentPolicy(True))
        complete=await capture_node(record,{'capture_authority':capability},visit_id='visit',direction='input',
            values={'text':'v'*1024},schema={'type':'object'})
        overflow=await capture_node(record,{'capture_authority':capability},visit_id='visit',direction='output',
            values={'text':'v'*65537},schema={'type':'object'})
        assert complete['reason']=='captured'
        assert complete['values']['text']=='v'*1024
        assert overflow['reason']=='truncated'
        assert overflow['values']=={}
    asyncio.run(run())


def test_invocation_partial_bundle_never_inherits_constructor_authority():
    constructor={'tools':'constructor-tools','content_policy':ContentPolicy(True),
        'emissions':EmissionBoundary(ContentPolicy(True)),'capture_authority':object()}
    selected=resolve_capture_services({'emissions':None,'ordinary':'runtime'},inherited=constructor)
    assert selected['tools']=='constructor-tools'
    assert selected['ordinary']=='runtime'
    assert selected['emissions'] is None
    assert 'content_policy' not in selected
    assert 'capture_authority' not in selected


def test_real_reverse_release_is_confirmed_before_reentrant_subscriber(monkeypatch):
    import api_agent.providers.authority_channel as protocol
    original=protocol._read
    server_readers={}
    async def delayed_release(reader):
        row=await original(reader)
        identity=id(reader)
        if row.get('purpose')=='graph-capture':
            server_readers[identity]=1
        elif identity in server_readers:
            server_readers[identity]+=1
            if server_readers[identity]==4:
                # A local writer.wait_closed alone cannot prove remote release.
                await asyncio.sleep(.02)
        return row
    monkeypatch.setattr(protocol,'_read',delayed_release)
    async def run():
        remote=EmissionBoundary(secrets=['synthetic-credential'])
        channel=AuthorityChannel(KEY,lambda *_:None,snapshot_source=lambda _:remote)
        path=await channel.start()
        record=journal(EmissionBoundary(ContentPolicy(True)))
        observed=[]
        def observer(row):
            remote.register_secret('subscriber-credential')
            observed.append(row)
        record.subscribe(observer)
        try:
            await capture_node(record,{'capture_authority':lambda:leased_snapshot(path,KEY,BINDING)},
                visit_id='visit',direction='input',values={'text':'visible synthetic-credential'},
                schema={'type':'object'})
            assert observed[0]['values']['text']=='visible [REDACTED]'
            assert remote._authority.evidence_leases==0
            assert 'subscriber-credential' in remote._secrets
        finally:
            await channel.close()
    asyncio.run(run())


def test_nested_partial_binding_does_not_inherit_outer_authority():
    with bind_capture_services({'content_policy':ContentPolicy(True),'capture_authority':object()}):
        with bind_capture_services({'emissions':None}):
            selected=resolve_capture_services({})
            assert selected['emissions'] is None
            assert 'content_policy' not in selected
            assert 'capture_authority' not in selected
        assert resolve_capture_services({})['content_policy'].allow_content


def test_stateful_invocation_partial_bundle_never_inherits_constructor_authority(tmp_path,monkeypatch):
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_catalog, load_stateful_definition
    monkeypatch.delenv('API_AGENT_ALLURE_REPORTING',raising=False)
    (tmp_path/'native.py').write_text('''from typing import TypedDict

class State(TypedDict):
    inherited: bool

def inspect_services(state, context):
    return {'inherited': 'content_policy' in context.services or 'capture_authority' in context.services}
''')
    (tmp_path/'inspect.md.html').write_text('''# Node v3: inspect

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| inspect_services | deterministic | [Python](native.py) |
''')
    flow=tmp_path/'flow.md.html'
    flow.write_text('''# Workflow v3

## Graph ID

| ID |
| --- |
| capture_services |

## State

| Registry | Source |
| --- | --- |
| State | [Python](native.py) |

## Flow

1.  [node inspect](inspect.md.html)

## Edges

| From | To | Router | Source |
| --- | --- | --- | --- |
| inspect | END | - | - |
''')
    definition=load_stateful_definition(flow,base_root=tmp_path,pack_root=tmp_path)
    catalog=load_stateful_catalog(definition)
    workflow=StatefulWorkflow(definition,catalog=catalog,reducers={},services={
        'content_policy':ContentPolicy(True),'capture_authority':object()})
    result=asyncio.run(workflow.arun({'inherited':False},thread_id='capture-thread',services={'emissions':None}))
    assert result['inherited'] is False


def test_real_observation_wrapper_enrolls_live_native_owners_and_late_credentials():
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    from api_agent.testing.authority_scope import TestAuthorityScope
    from api_agent.workflows.observations import WorkflowObservationFactory
    async def run():
        local=EmissionBoundary(ContentPolicy(True))
        trace_owner=EmissionBoundary(ContentPolicy(False))
        resolved=ConfigResult({'LOG_CONTENT':True},
            {'LOG_CONTENT':ResolvedValue(True,'cli',True)},(),(),())
        trace=SimpleNamespace(boundary=trace_owner,is_model_identity=lambda _:False)
        record=WorkflowObservationFactory(resolved,boundary=local,trace=trace).create(definition())
        owners=record.credential_authorities()
        assert len(owners)==2
        assert owners[0]._authority is local._authority
        assert owners[1] is trace_owner
        assert all(type(owner) is EmissionBoundary for owner in owners)
        # The logging policy of a live owner is preserved, never elevated.
        assert owners[1].policy.allow_content is False
        # Enrollment consumes the real accessor, rather than the wrapper boundary.
        scope=TestAuthorityScope(['workflow'])
        for owner in owners:
            await scope.ajoin('workflow',owner)
        local.register_secret('late-local-credential')
        trace_owner.register_secret('late-trace-credential')
        admitted=scope.authorities()
        assert any('late-local-credential' in owner._secrets for owner in admitted)
        assert any('late-trace-credential' in owner._secrets for owner in admitted)
        assert len(admitted)==2
        scope.close()
    asyncio.run(run())


@pytest.mark.parametrize('invalid',['cycle','unknown','deep','wide'])
def test_journal_credential_authority_accessor_rejects_invalid_wrapper_graph(invalid):
    from api_agent.workflows.journal import _JournalBoundary
    record=journal(EmissionBoundary(ContentPolicy(True)))
    wrapper=_JournalBoundary(ContentPolicy(True),record._boundary)
    if invalid=='cycle':
        wrapper.include(wrapper)
    elif invalid=='unknown':
        wrapper.include(object())
    elif invalid=='wide':
        wrapper=_JournalBoundary(ContentPolicy(True),*([record._boundary]*2049))
    else:
        for _ in range(65):
            wrapper=_JournalBoundary(ContentPolicy(True),wrapper)
    record._boundary=wrapper
    with pytest.raises((TypeError,ValueError)):
        record.credential_authorities()


@pytest.mark.parametrize('source_enabled,owner_enabled',[(False,False),(False,True),(True,False),(True,True)])
def test_credential_union_does_not_derive_permission_from_owner_policies(source_enabled,owner_enabled):
    from api_agent.providers.evidence_authority import EvidenceAuthority
    source=EmissionBoundary(ContentPolicy(source_enabled),secrets=['source-credential'])
    owner=EmissionBoundary(ContentPolicy(owner_enabled),secrets=['owner-credential'])
    authority=EvidenceAuthority((source,owner),['caller-credential'])
    generation,snapshot=authority.snapshot()
    assert snapshot.export({'text':'source-credential owner-credential caller-credential visible'})=={
        'text':'[REDACTED] [REDACTED] [REDACTED] visible'}
    assert authority.commit_if_current(generation,lambda:None)
    assert source.policy.allow_content is source_enabled
    assert owner.policy.allow_content is owner_enabled


@pytest.mark.parametrize('enabled',[False,True])
def test_stateful_native_writes_are_invocation_local_and_cannot_rebind_capture(tmp_path,monkeypatch,enabled):
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_catalog, load_stateful_definition
    monkeypatch.delenv('API_AGENT_ALLURE_REPORTING',raising=False)
    (tmp_path/'native.py').write_text('''from typing import TypedDict

class State(TypedDict):
    value: int

def write(state, context):
    context.services['counter'] = context.services.get('counter', 0) + 1
    context.services['capture_authority'] = None
    context.services['content_policy'] = None
    return {'value': context.services['counter']}

def read(state, context):
    assert context.services['counter'] == state['value']
    return {}
''')
    for name in ('write','read'):
        (tmp_path/(name+'.md.html')).write_text('''# Node v3: '''+name+'''

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| '''+name+''' | deterministic | [Python](native.py) |
''')
    flow=tmp_path/'flow.md.html'
    flow.write_text('''# Workflow v3

## Graph ID

| ID |
| --- |
| mutable_services |

## State

| Registry | Source |
| --- | --- |
| State | [Python](native.py) |

## Flow

1.  [node write](write.md.html)
2.  [node read](read.md.html)

## Edges

| From | To | Router | Source |
| --- | --- | --- | --- |
| write | read | - | - |
| read | END | - | - |
''')
    definition=load_stateful_definition(flow,base_root=tmp_path,pack_root=tmp_path)
    owner=EmissionBoundary(ContentPolicy(enabled))
    admitted=[]
    @asynccontextmanager
    async def capability():
        admitted.append(True)
        yield owner
    workflow=StatefulWorkflow(definition,catalog=load_stateful_catalog(definition),reducers={},
        services={'emissions':owner,'content_policy':ContentPolicy(enabled),'capture_authority':capability})
    async def run():
        for label in ('first','second'):
            record=WorkflowJournal(definition,log_content=enabled,content_policy=ContentPolicy(enabled),boundary=owner)
            result=await workflow.arun({'value':0},thread_id=label,journal=record)
            assert result['value']==1
            rows=[row for row in record.finalize()['events'] if row.get('event')=='capture']
            assert len(rows)==4
            if enabled:
                assert rows[1]['values']=={'value':1}
            else:
                assert all('values' not in row and row['reason']=='withheld' for row in rows)
    asyncio.run(run())
    assert len(admitted)==8
    assert 'counter' not in workflow.services
    assert workflow.services['capture_authority'] is capability
