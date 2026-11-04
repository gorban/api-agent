"""Custom collectors retain exact inputs across the protected Studio boundary."""
import asyncio
import base64
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api_agent.runtime import tracing_collector_snapshot as snapshots
from test_pack_composition import pack
from test_tracing_provider_extension import synthetic_provider, values, BASE


@pytest.fixture
def selected(tmp_path, synthetic_provider):
    from api_agent.runtime.resource_pack import load_launch_context
    root=pack(tmp_path,'application')
    (synthetic_provider/'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["traces"]\ninfrastructure=["synthetic/collector"]\n')
    (synthetic_provider/'infrastructure.toml').write_text('''version=1
owner="synthetic"
[[services]]
id="database"
compose="database.json"
readiness={kind="health"}
[[services]]
id="collector"
compose="collector.json"
dependencies=["synthetic/database"]
when={field="COLLECTOR_ENABLED",equals=true}
readiness={kind="health"}
[[services]]
id="neighbor"
compose="neighbor.json"
readiness={kind="health"}
''')
    for name in ('database','collector','neighbor'):
        (synthetic_provider/f'{name}.json').write_text('{"image":"example/service@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
    cfg=synthetic_provider/'cfg';cfg.mkdir()
    (cfg/'config.toml').write_text('version=1\n[fields.COLLECTOR_ENABLED]\ntype="bool"\ndefault=true\n'
        '[fields.TRACE_EXPORT_ENDPOINT]\ntype="str"\ndefault="https://collector.example.test/v1/traces"\n')
    config=values(synthetic_provider,COLLECTOR_ENABLED=True)
    context=load_launch_context(BASE,root)
    packet=snapshots.capture_collector_plan(context,config)
    return context, config, packet, synthetic_provider


def worker(context, packet):
    return {'owner_root':str(context.owner_root),'trace_collector_plan':packet,
            'trace_context':{'version':1,'environment':{},'explicit_fields':[]}}


def test_restore_consumes_selected_capture_without_any_source_reads(selected,monkeypatch):
    context,config,packet,provider=selected
    constructor=Mock()
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',constructor)
    monkeypatch.setenv('DOCKER_HOST','unix:///tmp/synthetic.sock')
    def forbidden(*args,**kwargs): raise AssertionError('worker reread captured source')
    monkeypatch.setattr(Path,'read_bytes',forbidden)
    monkeypatch.setattr('api_agent.runtime.infrastructure_contract._capture',forbidden)
    manager=snapshots.collector_from_snapshot(worker(context,packet),config)
    assert manager is constructor.return_value
    plan=constructor.call_args.args[0]
    assert {item.id for item in plan.conditional}=={'synthetic/database','synthetic/collector'}
    assert 'neighbor.json' not in {row['path'] for row in packet['assets']}
    assert set(plan.assets)>={'compose.json','localstack-ready.sh','jaeger-config.json'}
    assert packet['schema']=={'version':1,'fields':{'COLLECTOR_ENABLED':{'type':'bool'}}}
    assert 'COLLECTOR_ENABLED' not in json.dumps(packet['assets'])
    manager.acquire.assert_not_called()


@pytest.mark.parametrize('change', ['missing','digest','extra','inventory','schema','selection','base','descriptor','source','activation'])
def test_tampered_or_missing_capture_rejects_before_manager(selected,monkeypatch,change):
    context,config,packet,provider=selected
    packet=deepcopy(packet);config=dict(config)
    if change=='missing': packet['assets'].pop()
    if change=='digest': packet['assets'][0]['sha256']='0'*64
    if change=='extra': packet['assets'].append(packet['assets'][0])
    if change=='inventory': packet['inventory']['declarations'][0]['body']+='\n'
    if change=='schema': packet['schema']['fields']={}
    if change=='selection': config['TRACE_PROVIDER']+='changed'
    if change=='base': packet['base_assets'].pop()
    if change=='descriptor': packet['descriptor']['path']='other.toml'
    if change=='source': packet['sources'][0]['owner']='unselected'
    if change=='activation': config['COLLECTOR_ENABLED']=False
    constructor=Mock();monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',constructor)
    monkeypatch.setenv('DOCKER_HOST','unix:///tmp/synthetic.sock')
    with pytest.raises(ValueError): snapshots.collector_from_snapshot(worker(context,packet),config)
    constructor.assert_not_called()


def test_remote_positive_receipt_is_inert_and_missing_managed_packet_rejects(selected,monkeypatch):
    context,config,_,provider=selected
    (provider/'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["traces"]\n')
    monkeypatch.setattr(snapshots,'_schema',lambda *a:pytest.fail('remote schema read'))
    monkeypatch.setattr('api_agent.runtime.tracing_services.selected_tracing_infrastructure',lambda *a:pytest.fail('remote infrastructure read'))
    monkeypatch.delenv('DOCKER_HOST',raising=False)
    packet=snapshots.capture_collector_plan(context,config)
    assert packet['selected']==[] and snapshots.collector_from_snapshot(worker(context,packet),config) is None
    with pytest.raises(ValueError): snapshots.collector_from_snapshot({'owner_root':str(context.owner_root)},config)
    packet['assets']=[{}]
    with pytest.raises(ValueError,match='Remote'): snapshots.collector_from_snapshot(worker(context,packet),config)


def test_source_replacement_does_not_change_admitted_collectors(selected,monkeypatch):
    context,config,packet,provider=selected
    for path in provider.rglob('*'):
        if path.is_file(): path.write_text('invalid replacement')
    monkeypatch.setenv('DOCKER_HOST','unix:///tmp/synthetic.sock')
    constructor=Mock();monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',constructor)
    snapshots.collector_from_snapshot(worker(context,packet),config)
    assert {item.id for item in constructor.call_args.args[0].conditional}=={'synthetic/database','synthetic/collector'}


def test_binding_descriptor_identity_is_checked_without_source_read(selected):
    context,config,packet,provider=selected
    binding=SimpleNamespace(values=config,owner_root=provider,_environment={},explicit_fields=frozenset(config),
        declaration=SimpleNamespace(source=SimpleNamespace(sha256=packet['descriptor']['sha256'])))
    snapshots.verify_collector_binding(worker(context,packet),binding)
    binding.declaration.source.sha256='0'*64
    with pytest.raises(ValueError,match='differs'):snapshots.verify_collector_binding(worker(context,packet),binding)
    with pytest.raises(ValueError,match='absent'):snapshots.verify_collector_binding(worker(context,packet),None)
    snapshots.verify_collector_binding({},None)


def test_capture_does_not_copy_configured_credentials(selected):
    context,config,packet,provider=selected
    path=provider/'tracing-provider.toml'
    path.write_text(path.read_text()+'secret_fields=["EXPORT_TOKEN"]\n# synthetic-private-value\n')
    config['EXPORT_TOKEN']='synthetic-private-value'
    with pytest.raises(ValueError,match='credentials'):snapshots.capture_collector_plan(context,config)


@pytest.mark.asyncio
async def test_normal_studio_protected_snapshot_starts_selected_closure_before_sdk(selected,monkeypatch,tmp_path):
    from api_agent.testing import studio_tracing,provider_setup
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.config import ConfigResult
    context,config,packet,provider=selected
    events=[]
    class Manager:
        timeout=5
        def __init__(self,plan,*args,**kwargs):
            assert {item.id for item in plan.conditional}=={'synthetic/database','synthetic/collector'}
        def now(self):return 0
        def acquire(self,**kwargs):events.append('collector-ready')
        def release(self,**kwargs):events.append('collector-release')
    class Runtime:
        def __init__(self,config,*,boundary,binding):self.boundary=boundary;self.binding=binding
        async def astart(self):events.append('sdk-start')
        async def aclose(self):events.append('sdk-close');await self.binding.aclose()
    monkeypatch.setenv('DOCKER_HOST','unix:///tmp/synthetic.sock')
    monkeypatch.setattr(studio_tracing,'capture_collector_plan',snapshots.capture_collector_plan)
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',Manager)
    directory=tmp_path/'snapshot';directory.mkdir()
    path=provider_setup.write_launch_snapshot(directory,context,config,password_manager_registry=SimpleNamespace(definitions=()))
    snapshot=json.loads(path.read_text())
    assert snapshot['trace_collector_plan']['version']==2
    owner=studio_tracing.StudioTraceOwner(config,{},object(),owner_root=context.owner_root,
        runtime_factory=Runtime,collector_factory=lambda values:snapshots.collector_from_snapshot(snapshot,values))
    owner.bind_http('https://studio.example.test','synthetic-session-nonce')
    scope={'client':('127.0.0.1',1),'headers':[(b'origin',b'https://studio.example.test'),(b'x-api-agent-session',b'synthetic-session-nonce')]}
    async with owner.request(scope) as runtime: assert runtime is not None
    assert events==['collector-ready','sdk-start']
    await owner.aclose()
    assert events==['collector-ready','sdk-start','sdk-close','collector-release']

@pytest.mark.parametrize('row', [None,{}, {'root':'relative','path':'x','body':'','sha256':''},
    {'root':'/tmp','path':'../x','body':'','sha256':''},
    {'root':'/tmp','path':'x','body':3,'sha256':''},
    {'root':'/tmp','path':'x','body':'%%%','sha256':''},
    {'root':'/tmp','path':'x','body':'','sha256':'0'*64}])
def test_asset_envelopes_fail_closed(row):
    with pytest.raises(ValueError): snapshots._asset(row)


@pytest.mark.parametrize('value',[None,'',3,'relative','/tmp/../other','/tmp/'])
def test_owner_roots_are_lexically_canonical(value):
    with pytest.raises(ValueError):snapshots._root(value)


def test_packet_budgets_and_invalid_json_fail_closed():
    with pytest.raises(ValueError,match='packet'):snapshots._budget({'unsupported':set()})
    with pytest.raises(ValueError,match='budget'):snapshots._budget({'body':'x'*snapshots.MAX_PACKET_BYTES})


@pytest.mark.parametrize('field,value', [('version',True),('schema',None),('schema',{'version':1,'fields':[]}),
    ('selected','synthetic/collector'),('selected',['synthetic/collector','synthetic/collector']),
    ('sources',{}),('assets',{}),('base_assets',{}),('inventory',{}),('base_root','relative')])
def test_packet_envelope_rejects_malformed_inputs(selected,field,value):
    context,config,packet,_=selected
    packet=deepcopy(packet);packet[field]=value
    with pytest.raises(ValueError):snapshots.collector_from_snapshot(worker(context,packet),config)


@pytest.mark.parametrize('change',['source_shape','duplicate_source','unused_asset','wrong_base_owner','duplicate_base','wrong_schema_type','extra_schema_metadata'])
def test_captured_authority_and_schema_reject_extra_inputs(selected,change):
    context,config,packet,provider=selected
    packet=deepcopy(packet)
    if change=='source_shape':packet['sources'][0]['extra']=True
    if change=='duplicate_source':packet['sources'].append(packet['sources'][0])
    if change=='unused_asset':packet['assets'].append({'root':str(provider),'path':'unused.json','body':'e30=','sha256':sha256(b'{}').hexdigest()})
    if change=='wrong_base_owner':packet['base_assets'][0]['root']='/tmp/unselected'
    if change=='duplicate_base':packet['base_assets'].append(packet['base_assets'][0])
    if change=='wrong_schema_type':packet['schema']['fields']['COLLECTOR_ENABLED']['type']='float'
    if change=='extra_schema_metadata':packet['schema']['fields']['COLLECTOR_ENABLED']['help']='extra metadata'
    with pytest.raises(ValueError):snapshots.collector_from_snapshot(worker(context,packet),config)


def test_mode_without_selected_provider_remains_disabled(selected,monkeypatch):
    context,_,_,_=selected
    assert snapshots.capture_collector_plan(context,{}) is None
    assert snapshots.collector_from_snapshot({}, {}) is None
    config={'TRACE_MODE':'OTEL','TRACE_EXPORT_ENDPOINT':'http://localhost:4318/v1/traces'}
    assert snapshots.capture_collector_plan(context,config) is None
    assert snapshots.collector_from_snapshot(worker(context,None),config) is None


def test_controlled_compose_requires_exact_captured_assets(selected):
    from api_agent.runtime.compose import plan_compose
    from api_agent.runtime.ownership import StackScope
    context,_,packet,_=selected
    scope=StackScope.from_roots(context.base_root,context.pack_root)
    with pytest.raises(ValueError,match='captured controlled'):plan_compose({},scope,captured_assets={})
    values={row['path']:base64.b64decode(row['body']) for row in packet['base_assets']}
    values['empty.env']='not bytes'
    with pytest.raises(ValueError,match='captured controlled'):plan_compose({},scope,captured_assets=values)


def test_capture_rejects_conflicting_bytes_for_one_owned_asset(selected,monkeypatch):
    from dataclasses import replace
    from api_agent.runtime.tracing_services import selected_tracing_infrastructure
    from api_agent.runtime.infrastructure_contract import CapturedAsset
    context,config,_,_=selected
    contributions,admitted=selected_tracing_infrastructure(context,config)
    changed=replace(contributions[1],compose_source=CapturedAsset(contributions[0].compose_source.relative_path,b'{}',sha256(b'{}').hexdigest()))
    monkeypatch.setattr('api_agent.runtime.tracing_services.selected_tracing_infrastructure',lambda *a, **kw:((contributions[0],changed),admitted))
    with pytest.raises(ValueError,match='Conflicting captured'):snapshots.capture_collector_plan(context,config)


def test_capture_rejects_credentials_embedded_in_selected_asset(selected):
    context,config,_,provider=selected
    path=provider/'tracing-provider.toml';path.write_text(path.read_text()+'secret_fields=["EXPORT_TOKEN"]\n')
    path=provider/'collector.json';document=json.loads(path.read_text());document['labels']={'configured':'synthetic-private-value'};path.write_text(json.dumps(document))
    config['EXPORT_TOKEN']='synthetic-private-value'
    with pytest.raises(ValueError,match='credentials'):snapshots.capture_collector_plan(context,config)


def test_restore_rejects_conflicting_owner_roots(selected):
    context,config,packet,provider=selected
    packet=deepcopy(packet)
    body='version=1\nowner="other"\nservices=[]\n'
    packet['inventory']['authority'].append({'root':str(provider),'owner':'other','path':'other.toml'})
    packet['inventory']['declarations'].append({'body':body,'sha256':sha256(body.encode()).hexdigest()})
    packet['sources'].append({'root':str(provider),'owner':'other','path':'other.toml'})
    with pytest.raises(ValueError,match='conflicting owners'):snapshots.collector_from_snapshot(worker(context,packet),config)


def test_restore_requires_declaration_bytes_to_match_inventory(selected):
    context,config,packet,_=selected
    packet=deepcopy(packet)
    row=next(row for row in packet['assets'] if row['path']=='infrastructure.toml')
    body=base64.b64decode(row['body'])+b'\n# changed after capture\n'
    row.update(body=base64.b64encode(body).decode(),sha256=sha256(body).hexdigest())
    with pytest.raises(ValueError,match='inventory bytes'):snapshots.collector_from_snapshot(worker(context,packet),config)


def test_restore_requires_provider_descriptor_owner_to_match_services(selected):
    context,config,packet,_=selected
    packet=deepcopy(packet);row=packet['descriptor']
    body=base64.b64decode(row['body']).replace(b'id="synthetic"',b'id="other"')
    row.update(body=base64.b64encode(body).decode(),sha256=sha256(body).hexdigest())
    from api_agent.telemetry.tracing_providers import effective_selection, parse_declaration
    descriptor=parse_declaration(snapshots._asset(row)[1])
    packet['identity']=effective_selection(config,owner_root=context.owner_root,declaration=descriptor).identity
    with pytest.raises(ValueError,match='provider owner'):snapshots.collector_from_snapshot(worker(context,packet),config)

@pytest.mark.parametrize('body',[b'\xff',b'version = ['])
def test_captured_descriptor_parser_rejects_invalid_bytes(body):
    from api_agent.runtime.infrastructure_contract import CapturedAsset
    from api_agent.telemetry.tracing_providers import parse_declaration
    with pytest.raises(ValueError,match='cannot be admitted'):
        parse_declaration(CapturedAsset('tracing-provider.toml',body,sha256(body).hexdigest()))


def test_activation_projection_is_rejected_before_planning(selected):
    _,config,packet,_=selected
    packet=deepcopy(packet);packet['schema']['fields']={}
    with pytest.raises(ValueError,match='activation schema differs'):snapshots._decode(packet,config)


def inventory_neighbor(context, body):
    from api_agent.runtime.resource_pack import load_launch_context
    root=context.pack_root/'neighbor';root.mkdir()
    (root/'inventory.toml').write_text('version=1\nowner="neighbor"\nservices=[]\n'+body)
    path=context.pack_root/'pack.toml'
    path.write_text(path.read_text()+'''\n[infrastructure]
version=1
declarations=[]
inventory=[{owner="neighbor",root="pack",directory="neighbor",declarations=["inventory.toml"]}]
''')
    return load_launch_context(context.base_root,context.pack_root)


def test_inventory_only_neighbor_comment_rejects_configured_credential_before_handoff(selected):
    context,config,_,provider=selected
    token='synthetic-neighbor-sensitive-value'
    path=provider/'tracing-provider.toml';path.write_text(path.read_text()+'secret_fields=["EXPORT_TOKEN"]\n')
    config={**config,'EXPORT_TOKEN':token}
    context=inventory_neighbor(context,'# '+token+'\n')
    with pytest.raises(ValueError,match='configured credentials'):
        snapshots.capture_collector_plan(context,config)


def test_safe_inventory_neighbor_retains_exact_source_and_complete_packet_is_credential_free(selected):
    from api_agent.telemetry.redaction import registered_byte_ranges
    context,config,_,provider=selected
    token='synthetic-neighbor-sensitive-value'
    path=provider/'tracing-provider.toml';path.write_text(path.read_text()+'secret_fields=["EXPORT_TOKEN"]\n')
    config={**config,'EXPORT_TOKEN':token}
    context=inventory_neighbor(context,'# safe public placeholder ${EXAMPLE_PASSWORD}\n')
    path=context.pack_root/'neighbor/inventory.toml';before=path.read_bytes()
    packet=snapshots.capture_collector_plan(context,config)
    retained=next(row for authority,row in zip(packet['inventory']['authority'],packet['inventory']['declarations'],strict=True)
                  if authority['owner']=='neighbor')
    assert retained=={'body':before.decode(),'sha256':sha256(before).hexdigest()}
    assert not registered_byte_ranges(json.dumps(packet).encode(),(token,))
    assert packet['schema']=={'version':1,'fields':{'COLLECTOR_ENABLED':{'type':'bool'}}}


def test_inventory_screen_uses_schema_sensitive_fields_without_descriptor_secret_names(selected):
    context,config,_,provider=selected
    token='synthetic-schema-sensitive-value'
    path=provider/'cfg/config.toml';path.write_text(path.read_text()+'\n[fields.EXAMPLE_PASSWORD]\ntype="str"\nsensitive=true\n')
    config={**config,'EXAMPLE_PASSWORD':token}
    context=inventory_neighbor(context,'# '+token+'\n')
    with pytest.raises(ValueError,match='configured credentials'):
        snapshots.capture_collector_plan(context,config)


@pytest.mark.parametrize('channel',['key','context','inventory','decoded_asset'])
def test_packet_screen_checks_every_serialized_channel_and_decoded_bytes(selected,channel):
    _,_,packet,_=selected
    packet=deepcopy(packet);token='synthetic-channel-sensitive-value'
    if channel=='key':packet[token]='harmless'
    if channel=='context':packet['base_root']='/tmp/'+token
    if channel=='inventory':packet['inventory']['declarations'][0]['body']+='\n# '+token
    if channel=='decoded_asset':
        body=b'\xff\x00'+token.encode()
        packet['assets'][0].update(body=base64.b64encode(body).decode(),sha256=sha256(body).hexdigest())
    with pytest.raises(ValueError,match='configured credentials'):
        snapshots._screen_packet(packet,(token,))


def test_safe_source_templates_are_preserved_even_when_ordinary_json_projection_drops_keys(selected):
    _,_,packet,_=selected
    packet=deepcopy(packet)
    body=b'{"password":"${EXAMPLE_PASSWORD}","secret_fields":["EXPORT_TOKEN"]}'
    packet['assets'][0].update(body=base64.b64encode(body).decode(),sha256=sha256(body).hexdigest())
    before=deepcopy(packet)
    snapshots._screen_packet(packet,('synthetic-unrelated-sensitive-value',))
    assert packet==before


def test_shared_emission_authority_can_reject_when_byte_detector_has_no_range(selected,monkeypatch):
    _,_,packet,_=selected
    packet=deepcopy(packet);token='synthetic-authority-sensitive-value'
    packet['context_metadata']=token
    monkeypatch.setattr('api_agent.telemetry.redaction.registered_byte_ranges',lambda *a,**kw:())
    with pytest.raises(ValueError,match='configured credentials'):
        snapshots._screen_packet(packet,(token,))


def test_complete_serialization_cannot_reassemble_registered_secret_across_leaves(selected):
    _,_,packet,_=selected
    token='"schema": {"version": 1'
    assert token in json.dumps(packet)
    with pytest.raises(ValueError,match='configured credentials'):
        snapshots._screen_packet(packet,(token,))

@pytest.mark.parametrize('token',['schema','provider'])
def test_capture_rejects_registered_literal_collisions_in_packet_field_names_or_context(selected,token):
    context,config,_,provider=selected
    path=provider/'tracing-provider.toml';path.write_text(path.read_text()+'secret_fields=["EXPORT_TOKEN"]\n')
    with pytest.raises(ValueError,match='configured credentials'):
        snapshots.capture_collector_plan(context,{**config,'EXPORT_TOKEN':token})


@pytest.mark.parametrize('token', ['value', 'wrapped', 'harmless', '{"": "version"}', '{"value": "version"}'])
def test_remote_packet_accepts_absent_short_credentials_without_wrapper_collisions(selected, token):
    from api_agent.telemetry.redaction import registered_byte_ranges
    context, config, _, provider = selected
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text('version=1\nid="synthetic"\nsignals=["traces"]\nsecret_fields=["EXPORT_TOKEN"]\n')
    packet = snapshots.capture_collector_plan(context, {**config, 'EXPORT_TOKEN': token})
    assert packet['selected'] == []
    assert not registered_byte_ranges(json.dumps(packet).encode(), (token,))
    assert not registered_byte_ranges(base64.b64decode(packet['descriptor']['body']), (token,))
    assert snapshots.collector_from_snapshot(worker(context, packet), config) is None


def test_remote_packet_rejects_real_short_credential_collision(selected):
    context, config, _, provider = selected
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text('version=1\nid="synthetic"\nsignals=["traces"]\nsecret_fields=["EXPORT_TOKEN"]\n# value\n')
    with pytest.raises(ValueError, match='configured credentials'):
        snapshots.capture_collector_plan(context, {**config, 'EXPORT_TOKEN': 'value'})
