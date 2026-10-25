"""Passive declarations authorize exact neighbors without acquiring them."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.runtime.inventory import InventoryCatalog, capture_inventory, inventory_authority
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.resource_pack import load_launch_context, ResourcePackError
from api_agent.runtime.infrastructure_plan import _name, plan_infrastructure
from api_agent.config.declarations import ConfigSchema
from api_agent.runtime.prerequisites import PrerequisiteManager, PrerequisiteError
from test_prerequisites import setup


@pytest.fixture
def source(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'base/demo'
    pack.mkdir(parents=True)
    fixture = base / 'fixture'
    fixture.mkdir()
    (pack / 'config.toml').write_text('version=1\n')
    (pack / 'normal.toml').write_text('version=1\nowner="demo"\n[[services]]\nid="api"\ncompose="ABSENT"\nwhen={field="ABSENT",equals=true}\n')
    (fixture / 'inventory.toml').write_text('version=1\nowner="fixture"\n[[services]]\nid="backing"\nassets=["ABSENT"]\n[[services]]\nid="primary"\nreadiness="NOT PARSED"\n')
    (pack / 'pack.toml').write_text('version=1\nid="demo"\nconfig="config.toml"\n[infrastructure]\nversion=1\ndeclarations=["normal.toml"]\ninventory=[{owner="fixture",root="base",declarations=["fixture/inventory.toml"]}]\n')
    context = load_launch_context(base, pack)
    return SimpleNamespace(base=base, pack=pack, fixture=fixture, context=context,
        scope=StackScope.from_roots(base, pack))


def test_identity_only_explicit_sources_survive_both_operation_directions(source):
    empty = SimpleNamespace(tools=(), captured_assets={})
    ordinary = capture_inventory(source.context, empty)
    tool = SimpleNamespace(infrastructure={'owner': 'fixture', 'owner_root': source.fixture,
        'declarations': (source.fixture / 'inventory.toml',)})
    plan = SimpleNamespace(tools=(tool,), captured_assets={})
    selected = capture_inventory(source.context, plan)
    assert ordinary.pairs == selected.pairs
    assert len(ordinary.pairs) == 3
    assert not (source.fixture / 'ABSENT').exists()
    assert not hasattr(ordinary, 'endpoints')
    with pytest.raises(TypeError):
        ordinary.authority[0]['root'] = '/arbitrary'
    restored = InventoryCatalog.from_wire(ordinary.to_wire(), scope=source.scope,
                                         authority=inventory_authority(source.context, empty))
    assert restored.pairs == ordinary.pairs
    # A source change cannot broaden an already captured operation.
    (source.pack / 'normal.toml').write_text('invalid after capture')
    assert ordinary.verify(source.scope) == ordinary.pairs


def test_already_admitted_declaration_bytes_are_reused(source, monkeypatch):
    from api_agent.runtime import inventory
    authorities = inventory_authority(source.context)
    captured = {Path(row['root']) / row['path']: (Path(row['root']) / row['path']).read_bytes()
                for row in authorities}
    monkeypatch.setattr(inventory, '_capture', lambda *args: (_ for _ in ()).throw(AssertionError('reread')))
    assert len(capture_inventory(source.context, SimpleNamespace(tools=(), captured_assets=captured)).pairs) == 3


@pytest.mark.parametrize('damage', ['scope', 'authority', 'digest', 'pairs', 'nested', 'owner', 'duplicate', 'version', 'service', 'envelope'])
def test_malformed_catalog_metadata_and_unbound_source_authority_reject(source, damage):
    catalog = capture_inventory(source.context)
    wire = catalog.to_wire()
    authority = inventory_authority(source.context)
    if damage == 'scope': wire['scope'] = 'f' * 16
    elif damage == 'authority': wire['authority'][0]['root'] = '/forged'
    elif damage == 'digest': wire['declarations'][0]['sha256'] = '0' * 64
    elif damage == 'pairs':
        forged = replace(catalog, pairs=(('unadmitted', 'otel'),))
        with pytest.raises(ValueError, match='pairs differ'): forged.verify(source.scope)
        return
    elif damage == 'nested': wire['declarations'][0]['extra'] = True
    else:
        body = wire['declarations'][0]['body']
        if damage == 'owner': body = body.replace('owner="demo"', 'owner="other"')
        elif damage == 'duplicate': body += '\n[[services]]\nid="api"\n'
        elif damage == 'version': body = body.replace('version=1', 'version=true')
        elif damage == 'service': body = body.replace('id="api"', 'id="../bad"')
        else: body += '\nextra="bad"\n'  # service fields remain inert; use top-level envelope instead
        if damage == 'envelope': body = 'unexpected=1\n' + body
        wire['declarations'][0] = {'body': body, 'sha256': sha256(body.encode()).hexdigest()}
    with pytest.raises(ValueError):
        InventoryCatalog.from_wire(wire, scope=source.scope, authority=authority)


def test_owner_root_and_declaration_owner_conflicts_reject(source):
    other = source.base / 'other'
    other.mkdir()
    declaration = other / 'inventory.toml'
    declaration.write_text('version=1\nowner="fixture"\nservices=[]\n')
    tool = SimpleNamespace(infrastructure={'owner': 'fixture', 'owner_root': other, 'declarations': (declaration,)})
    with pytest.raises(ValueError, match='conflicting roots'):
        inventory_authority(source.context, SimpleNamespace(tools=(tool,)))
    tool.infrastructure = {'owner': 'other', 'owner_root': source.fixture,
                           'declarations': (source.fixture / 'inventory.toml',)}
    with pytest.raises(ValueError, match='conflicting owners'):
        inventory_authority(source.context, SimpleNamespace(tools=(tool,)))


@pytest.mark.parametrize('damage', ['profile', 'service', 'cross-profile', 'managed', 'scope', 'project'])
def test_exact_passive_pair_rejects_foreign_neighbors_before_effects(setup, source, damage):
    create, engine, _, scope, _ = setup
    catalog = capture_inventory(source.context)
    # Bind independently captured source authority to the manager's physical scope.
    catalog = InventoryCatalog.from_wire({**catalog.to_wire(), 'scope': scope.id}, scope=scope,
                                        authority=list(map(dict, catalog.authority)))
    normal = create()
    service, profile = catalog.pairs[0]
    identifier = 'a' * 64
    labels = {**scope.labels(profile, profiles={profile}), 'com.docker.compose.project': scope.project,
              'com.docker.compose.service': service}
    keys = {'profile': 'api-agent.profile', 'service': 'com.docker.compose.service',
            'managed': 'api-agent.managed', 'scope': 'api-agent.scope', 'project': 'com.docker.compose.project'}
    if damage == 'cross-profile': labels['api-agent.profile'] = next(other for _, other in catalog.pairs if other != profile)
    else: labels[keys[damage]] = 'foreign'
    engine.records[identifier] = {'Id': identifier, 'Config': {'Image': 'neighbor', 'Labels': labels}, 'State': {'Running': True}}
    manager = PrerequisiteManager(replace(normal.plan, inventory=catalog), scope, normal.directory,
        command_runner=engine.run, identity=normal.identity, now=normal.now, sleep=normal.sleep,
        readiness_probe=lambda *args: True)
    with pytest.raises(PrerequisiteError, match='foreign ownership'):
        manager.acquire()
    assert all('up' not in command and command[1] != 'stop' for command, _ in engine.commands)


def test_passive_neighbor_never_enters_selected_lease_or_cleanup(setup, source):
    create, engine, _, scope, _ = setup
    normal = create()
    raw = capture_inventory(source.context)
    catalog = InventoryCatalog.from_wire({**raw.to_wire(), 'scope': scope.id}, scope=scope,
        authority=list(map(dict, raw.authority)))
    service, profile = catalog.pairs[0]
    identifier = 'a' * 64
    engine.records[identifier] = {'Id': identifier, 'Config': {'Image': 'neighbor', 'Labels': {
        **scope.labels(profile, profiles={profile}), 'com.docker.compose.project': scope.project,
        'com.docker.compose.service': service}}, 'State': {'Running': True}}
    manager = PrerequisiteManager(replace(normal.plan, inventory=catalog), scope, normal.directory,
        command_runner=engine.run, identity=normal.identity, now=normal.now, sleep=normal.sleep,
        readiness_probe=lambda *args: True)
    engine.owner = manager
    lease = manager.acquire()
    assert identifier not in lease.container_ids
    manager.release()
    assert engine.records[identifier]['State']['Running']
    assert all(identifier not in command[2:] for command, _ in engine.commands if command[1] in {'stop','start','exec'})


def test_planner_rejects_catalog_for_other_scope(source):
    catalog = capture_inventory(source.context)
    with pytest.raises(ValueError, match='scope differs'):
        plan_infrastructure({}, ConfigSchema({}), (), StackScope('0' * 16), inventory=catalog)


def test_changed_valid_catalog_cannot_replace_trusted_worker_packet(packet):
    from api_agent.testing.tool_runtime import read_tool_packet
    from api_agent.providers.contracts import ContractError
    path = packet.directory / 'tool-plan.json'
    data = json.loads(path.read_text())
    catalog = data['inventory']
    # New valid bytes and their matching digest still lack the launch descriptor.
    body = 'version=1\nowner="invented"\n[[services]]\nid="extra"\n'
    catalog['authority'].append({'owner':'invented','root':str(packet.root),'path':'extra.toml'})
    catalog['declarations'].append({'body':body,'sha256':sha256(body.encode()).hexdigest()})
    path.write_text(json.dumps(data))
    with pytest.raises(ContractError, match='changed after launch'):
        read_tool_packet(packet.path, packet.data['tool_plan'], inventory_context=packet.data)


from test_tool_test_runtime import packet


@pytest.mark.parametrize('change', ['root-alias', 'extension', 'count'])
def test_capture_rejects_unbounded_or_noncanonical_authority(source, change):
    if change == 'root-alias':
        root = source.base / 'alias'
        root.symlink_to(source.fixture, target_is_directory=True)
        sources = (('fixture', root, (root / 'inventory.toml',)),)
    elif change == 'extension':
        sources = (('fixture', source.fixture, (source.fixture / 'declaration.json',)),)
    else:
        sources = (('fixture', source.fixture, tuple(source.fixture / f'd{i}.toml' for i in range(17))),)
    context = SimpleNamespace(inventory_sources=sources)
    with pytest.raises(ValueError): inventory_authority(context)


@pytest.mark.parametrize('change', ['count', 'root', 'root-conflict', 'extension', 'service-type', 'service-budget', 'bytes'])
def test_bounded_catalog_decoder_rejects_invalid_identity_sources(source, change):
    catalog = capture_inventory(source.context)
    wire = catalog.to_wire()
    if change == 'count':
        wire['declarations'] = []
    elif change == 'root': wire['authority'][0]['root'] = 'relative'
    elif change == 'root-conflict':
        wire['authority'][1]['owner'] = wire['authority'][0]['owner']
    elif change == 'extension': wire['authority'][0]['path'] = 'catalog.json'
    else:
        if change == 'service-type': body = 'version=1\nowner="demo"\nservices=[42]\n'
        elif change == 'service-budget': body = 'version=1\nowner="demo"\n' + ''.join(f'[[services]]\nid="s{i}"\n' for i in range(33))
        else: body = '#' * (64 * 1024 + 1)
        wire['declarations'][0] = {'body':body,'sha256':sha256(body.encode()).hexdigest()}
    with pytest.raises(ValueError):
        InventoryCatalog.from_wire(wire, scope=source.scope, authority=deepcopy(wire['authority']))


@pytest.mark.parametrize('kind', ['service', 'profile'])
def test_generated_name_collisions_do_not_merge_authority(source, monkeypatch, kind):
    from api_agent.runtime import infrastructure_plan
    original = infrastructure_plan._name
    def collide(prefix, identity):
        if (kind == 'profile' and '/' not in identity) or (kind == 'service' and '/' in identity):
            return 'infra-collision'
        return original(prefix, identity)
    monkeypatch.setattr(infrastructure_plan, '_name', collide)
    with pytest.raises(ValueError, match='name collision'):
        capture_inventory(source.context)


def test_admitted_tool_owner_or_bytes_cannot_disagree_with_passive_snapshot(source):
    catalog = capture_inventory(source.context)
    path = source.fixture / 'inventory.toml'
    tool = SimpleNamespace(infrastructure={'owner':'fixture', 'declarations':(path,)})
    catalog.verify_plan(SimpleNamespace(tools=(tool,), captured_assets={path:path.read_bytes()}))
    for changed in (b'different bytes',):
        with pytest.raises(ValueError, match='declaration bytes'):
            catalog.verify_plan(SimpleNamespace(tools=(tool,), captured_assets={path:changed}))
    tool.infrastructure['declarations'] = (source.fixture / 'unknown.toml',)
    with pytest.raises(ValueError, match='excludes an admitted'):
        catalog.verify_plan(SimpleNamespace(tools=(tool,), captured_assets={}))


@pytest.mark.parametrize('row', [
    '"invalid"',
    '{owner="fixture",root="outside",declarations=["fixture/inventory.toml"]}',
    '{owner="fixture",root="base",declarations=[]}',
    '{owner="bad/owner",root="base",declarations=["fixture/inventory.toml"]}',
    '{owner="fixture",root="base",declarations=["../inventory.toml"]}',
    '{owner="fixture",root="base",declarations=["fixture/inventory.json"]}',
    '{owner="fixture",root="base",declarations=["fixture/inventory.toml","fixture/inventory.toml"]}',
])
def test_pack_explicit_inventory_grammar_is_bounded_and_inert(source, row):
    manifest = source.pack / 'pack.toml'
    prefix = manifest.read_text().split('inventory=')[0]
    manifest.write_text(prefix + 'inventory=[' + row + ']\n')
    with pytest.raises(ResourcePackError, match='inventory'):
        load_launch_context(source.base, source.pack)


def test_pack_explicit_inventory_rejects_symlinked_declaration(source):
    path = source.fixture / 'inventory.toml'
    original = source.fixture / 'original.toml'
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(ResourcePackError, match='inventory declaration'):
        load_launch_context(source.base, source.pack)


@pytest.mark.parametrize('labels', [None, [], 'invalid'])
def test_malformed_engine_label_object_is_a_bounded_ownership_failure(setup, labels):
    create, engine, _, _, _ = setup
    manager = create()
    identifier = engine.add('jaeger')
    engine.records[identifier]['Config']['Labels'] = labels
    with pytest.raises(PrerequisiteError, match='foreign ownership'):
        manager.acquire()
    assert all('up' not in command and command[1] != 'stop' for command, _ in engine.commands)


@pytest.mark.parametrize('kind', ['wrong-scope', 'wrong-type'])
def test_manager_requires_scope_bound_catalog_before_engine_access(setup, source, kind):
    create, engine, _, scope, _ = setup
    normal = create()
    catalog = capture_inventory(source.context)
    if kind == 'wrong-scope':
        assert catalog.scope_id != scope.id
    else:
        catalog = SimpleNamespace(scope_id=scope.id, pairs=catalog.pairs)
    before = list(engine.commands)
    with pytest.raises(PrerequisiteError, match='scope differs'):
        PrerequisiteManager(replace(normal.plan, inventory=catalog), scope, normal.directory,
            command_runner=engine.run)
    assert engine.commands == before


def test_valid_catalog_cannot_override_selected_service_profile(setup, source):
    create, engine, _, scope, _ = setup
    normal = create()
    captured = capture_inventory(source.context)
    catalog = InventoryCatalog.from_wire({**captured.to_wire(), 'scope':scope.id}, scope=scope,
        authority=[dict(row) for row in captured.authority])
    name, profile = catalog.pairs[0]
    assert profile != 'otel' and catalog.verify(scope) == catalog.pairs
    document = json.loads(normal.plan.assets['compose.json'])
    document['services'][name] = deepcopy(document['services']['jaeger'])
    assert document['services'][name]['profiles'] == ['otel']
    plan = replace(normal.plan, inventory=catalog, services=(*normal.plan.services, name),
        assets={**normal.plan.assets, 'compose.json':json.dumps(document).encode()})
    before = list(engine.commands)
    with pytest.raises(PrerequisiteError, match='conflicts with selected service ownership'):
        PrerequisiteManager(plan, scope, normal.directory, command_runner=engine.run)
    assert engine.commands == before


@pytest.mark.parametrize('rows', ['42', '[' + ','.join('{}' for _ in range(17)) + ']'])
def test_explicit_inventory_source_array_rejects_type_and_count(source, rows):
    manifest = source.pack / 'pack.toml'
    manifest.write_text(manifest.read_text().split('inventory=')[0] + 'inventory=' + rows + '\n')
    with pytest.raises(ResourcePackError, match='inventory must be bounded explicit sources'):
        load_launch_context(source.base, source.pack)


def test_plan_only_packet_reader_does_not_consume_passive_authority(packet):
    from api_agent.testing.tool_runtime import read_tool_packet
    plan, sources = read_tool_packet(packet.path, packet.data['tool_plan'])
    assert [tool.id for tool in plan.tools] == [tool.id for tool in packet.plan.tools]
    assert len(plan.tools) == 2
    assert sources['ONE_LABEL'] == {'source':'shared_default','explicit':False}
