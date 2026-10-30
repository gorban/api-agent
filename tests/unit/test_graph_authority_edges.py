"""Purpose-bound graph credential enrollment rejects authenticated corruption."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from api_agent.providers.authority_channel import AuthorityChannel, _encode, _mac, _read, leased_snapshot
from api_agent.providers.contracts import ContractError
from api_agent.telemetry.privacy import EmissionBoundary

KEY=b'g'*32
BINDING={'worker_id':'graph-worker'}
NONCE='a'*64


@pytest.mark.parametrize('stage',['hello','confirm','receipt','release','owner_before_transfer','owner_after_transfer'])
def test_reverse_source_rejects_only_corrupted_stage_and_releases_lease(stage):
    async def run():
        original=EmissionBoundary(secrets=['synthetic-source-secret'])
        replacement=EmissionBoundary()
        calls=[]
        def source(binding):
            assert binding==BINDING
            calls.append(True)
            drift=3 if stage=='owner_before_transfer' else 4 if stage=='owner_after_transfer' else None
            return replacement if len(calls)==drift else original
        channel=AuthorityChannel(KEY,lambda *_:None,snapshot_source=source)
        hello=dict(version=1,purpose='graph-capture',binding=BINDING,nonce=NONCE,
            proof=_mac(KEY,'graph-client-hello',BINDING,NONCE))
        if stage=='hello':hello['proof']=_mac(KEY,'different-purpose',BINDING,NONCE)
        class Writer:
            frames=[]
            def write(self,raw):self.frames.append(json.loads(raw))
            async def drain(self):pass
        writer=Writer()
        class Reader:
            sequence=0
            async def readline(self):
                self.sequence+=1
                identity=(BINDING,NONCE,writer.frames[0]['nonce'])
                if self.sequence==1:
                    role='graph-client-confirm'
                    args=identity
                    target='confirm'
                else:
                    digest=hashlib.sha256(_encode(writer.frames[1]['secrets'])).hexdigest()
                    role='graph-client-receipt' if self.sequence==2 else 'graph-client-release'
                    args=(*identity,digest)
                    target='receipt' if self.sequence==2 else 'release'
                proof=_mac(KEY,'different-purpose' if stage==target else role,*args)
                return _encode(dict(version=1,proof=proof))
        with pytest.raises(ContractError):
            await channel._serve_snapshot(Reader(),writer,hello,SimpleNamespace(reschedule=lambda _:None))
        assert original._authority.evidence_leases==0
        if stage in ('hello','confirm','owner_before_transfer'):
            assert not any('secrets' in frame for frame in writer.frames)
    asyncio.run(run())


@pytest.mark.parametrize('stage',['challenge_shape','challenge_proof','snapshot_shape','snapshot_proof','ready','released'])
def test_reverse_consumer_rejects_authenticated_wrong_purpose_or_shape(tmp_path,stage):
    async def run():
        done=asyncio.Event()
        received=[]
        async def fake(reader,writer):
            try:
                hello=await _read(reader);received.append(hello)
                identity=(hello['binding'],hello['nonce'],NONCE)
                challenge=dict(version=1,nonce=NONCE,proof=_mac(KEY,'graph-server-challenge',*identity))
                if stage=='challenge_shape':challenge.pop('nonce')
                if stage=='challenge_proof':challenge['proof']=_mac(KEY,'different-purpose',*identity)
                writer.write(_encode(challenge));await writer.drain()
                received.append(await _read(reader))
                values=['synthetic-source-secret'];digest=hashlib.sha256(_encode(values)).hexdigest()
                snapshot=dict(version=1,secrets=values,proof=_mac(KEY,'graph-server-snapshot',*identity,digest))
                if stage=='snapshot_shape':snapshot.pop('secrets')
                if stage=='snapshot_proof':snapshot['proof']=_mac(KEY,'different-purpose',*identity,digest)
                writer.write(_encode(snapshot));await writer.drain()
                received.append(await _read(reader))
                ready=dict(version=1,proof=_mac(KEY,'different-purpose' if stage=='ready' else 'graph-server-ready',*identity,digest))
                writer.write(_encode(ready));await writer.drain()
                received.append(await _read(reader))
                released=dict(version=1,proof=_mac(KEY,'different-purpose' if stage=='released' else 'graph-server-released',*identity,digest))
                writer.write(_encode(released));await writer.drain()
            except ContractError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                done.set()
        path=str(tmp_path/'channel')
        server=await asyncio.start_unix_server(fake,path)
        try:
            with pytest.raises(ContractError):
                async with leased_snapshot(path,KEY,BINDING):
                    assert stage=='released'
            await asyncio.wait_for(done.wait(),1)
            assert 'synthetic-source-secret' not in json.dumps(received)
        finally:
            server.close();await server.wait_closed()
    asyncio.run(run())


def test_reverse_source_configuration_requires_callable():
    with pytest.raises(ContractError):
        AuthorityChannel(KEY,lambda *_:None,snapshot_source=object())


def test_capture_service_contract_rejects_unknown_and_nonmapping_bundles():
    from api_agent.workflows.capture_authority import bind_capture_services,resolve_capture_services
    with pytest.raises(ValueError),bind_capture_services({'ordinary':True}):
        pass
    with pytest.raises(TypeError):
        resolve_capture_services(None)
    with pytest.raises(TypeError):
        resolve_capture_services({},inherited=object())


def test_credential_accessor_deduplicates_shared_wrappers_and_rejects_empty_union():
    from api_agent.telemetry.privacy import ContentPolicy
    from api_agent.workflows.journal import _JournalBoundary,_credential_authorities
    owner=EmissionBoundary()
    child=_JournalBoundary(ContentPolicy(False),owner)
    assert _credential_authorities(_JournalBoundary(ContentPolicy(False),child,child))==(owner,)
    with pytest.raises(ValueError):
        _credential_authorities(_JournalBoundary(ContentPolicy(False)))


def test_graph_capture_rejects_non_native_authority_without_forwarding_raw_values():
    from contextlib import asynccontextmanager
    from api_agent.workflows.capture_authority import capture_node
    rows=[]
    @asynccontextmanager
    async def capability():
        yield object()
    class Journal:
        def capture_json(self,**kwargs):rows.append(kwargs)
    asyncio.run(capture_node(Journal(),{'capture_authority':capability},visit_id='visit',direction='input',
        values={'text':'raw-sentinel'},schema={'type':'object'}))
    assert rows[0]['values']=={}
    assert rows[0]['capture_reason']=='unavailable'
    assert 'raw-sentinel' not in repr(rows)


def test_graph_capture_preserves_plain_contract_after_credential_admission():
    from contextlib import asynccontextmanager
    from api_agent.workflows.capture_authority import capture_node
    rows=[]
    owner=EmissionBoundary(secrets=['synthetic-source-secret'])
    @asynccontextmanager
    async def capability():
        yield owner
    class Journal:
        def capture(self,**kwargs):rows.append(kwargs)
        def capture_json(self,**kwargs):pytest.fail('Plain capture contract was lost')
    asyncio.run(capture_node(Journal(),{'capture_authority':capability},visit_id='visit',direction='input',
        values={'text':'visible synthetic-source-secret'},schema={'type':'object'},plain_fields=['text']))
    assert rows[0]['plain_fields']==['text']
    assert rows[0]['values']=={'text':'visible [REDACTED]'}


def test_reverse_release_tolerates_local_close_error_after_remote_ack(tmp_path,monkeypatch):
    import api_agent.providers.authority_channel as protocol
    original_open=asyncio.open_unix_connection
    async def open_with_close_error(*args,**kwargs):
        reader,writer=await original_open(*args,**kwargs)
        class Writer:
            def __getattr__(self,name):return getattr(writer,name)
            async def wait_closed(self):
                await writer.wait_closed()
                raise OSError('Synthetic local close failure')
        return reader,Writer()
    monkeypatch.setattr(protocol.asyncio,'open_unix_connection',open_with_close_error)
    async def run():
        owner=EmissionBoundary(secrets=['synthetic-source-secret'])
        channel=AuthorityChannel(KEY,lambda *_:None,snapshot_source=lambda _:owner)
        path=await channel.start()
        try:
            async with leased_snapshot(path,KEY,BINDING) as boundary:
                assert boundary.content_export({'text':'synthetic-source-secret'})=={'text':'[REDACTED]'}
            assert owner._authority.evidence_leases==0
        finally:
            await channel.close()
    asyncio.run(run())


@pytest.mark.parametrize('invalid',['revoked','disconnected','missing_authority'])
def test_graph_snapshot_source_requires_live_binding_and_operation_authority(invalid):
    from api_agent.providers.setup_transport import SetupServer
    async def run():
        binding=dict(version=1,operation_id='op',selected_identity='selected',worker_id='worker',attempt=1)
        registry=SimpleNamespace(_lease=lambda value:{'revoked':invalid=='revoked'})
        authority=None if invalid=='missing_authority' else EmissionBoundary()
        server=SetupServer(registry,'synthetic-capability-0000000000000',authority=authority)
        server._peers=[] if invalid=='disconnected' else [SimpleNamespace(binding=binding,revoked=False)]
        with pytest.raises(ContractError):
            server._graph_authority_source(binding)
    asyncio.run(run())


def test_unregistered_client_cannot_borrow_graph_credentials():
    from api_agent.providers.setup_transport import SetupClient
    client=SetupClient(dict(version=1,host='127.0.0.1',port=12345,capability='synthetic-capability-0000000000000'))
    with pytest.raises(ContractError):
        client.graph_capture_authority()
