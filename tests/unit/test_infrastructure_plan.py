"""Selected infrastructure admission is pure and fails before engine effects."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType

import pytest

from api_agent.config.declarations import ConfigSchema, FieldDeclaration
from api_agent.runtime.compose import ComposeError, snapshot_compose
from api_agent.runtime.infrastructure_contract import (
    Activation, CapturedAsset, EndpointOutput, InfrastructureService, Readiness, Seed,
)
from api_agent.runtime.infrastructure_plan import MissingEngineTransport, plan_infrastructure
import api_agent.runtime.infrastructure_plan as planner
from api_agent.runtime.ownership import StackScope


IMAGE = 'example/fixture@sha256:' + 'a' * 64
SCOPE = StackScope('a' * 16)
SCHEMA = ConfigSchema({'USE_DATA': FieldDeclaration('USE_DATA', 'bool')})


def captured(name, body=b'fixed'):
    return CapturedAsset(name, body, sha256(body).hexdigest())


def service(**changes):
    fragment = {'image': IMAGE, 'ports': ('127.0.0.1:58090:8090',),
        'volumes': (MappingProxyType({'asset': 'server.py', 'target': '/opt/fixture/server.py'}),
                    MappingProxyType({'volume': 'data', 'target': '/data'})),
        'command': ('python', '/opt/fixture/server.py')}
    base = InfrastructureService('pack/server', 'pack', Path('/pack/infra.toml'),
        captured('infra.toml'), Activation('USE_DATA', True), (), MappingProxyType(fragment),
        captured('fragment.json'), (captured('server.py'),),
        Readiness('http', 'api/ready', identity_field='service', identity_value='fixture'),
        (EndpointOutput('api', 'http://127.0.0.1:58090', 'http://self:8090'),),
        Seed('server.py', Readiness('http', 'api/seed', identity_field='seed', identity_value='done')))
    return replace(base, **changes)


def plan(contributions=None, values=None, **kwargs):
    return plan_infrastructure({'USE_DATA': True} if values is None else values, SCHEMA,
        (service(),) if contributions is None else contributions, SCOPE,
        engine_environment=kwargs.pop('engine_environment', {'DOCKER_HOST': 'unix:///run/test/engine.sock'}),
        **kwargs)


def test_active_plan_snapshot_and_resolved_contract(tmp_path):
    result = plan()
    metadata, = result.conditional
    assert metadata.id == 'pack/server'
    assert metadata.compose_name in result.services
    assert metadata.profile in result.profiles
    assert metadata.readiness.url == 'http://127.0.0.1:58090/ready'
    assert metadata.seed_completion.url == 'http://127.0.0.1:58090/seed'
    assert metadata.endpoints[0].in_project == f'http://{metadata.compose_name}:8090'
    document = json.loads(result.assets['compose.json'])
    spec = document['services'][metadata.compose_name]
    assert spec['command'] == ['python', '/opt/fixture/server.py']
    assert spec['volumes'][0].endswith(':/opt/fixture/server.py:ro,z')
    assert spec['labels']['api-agent.declaration-sha256'] == service().declaration_source.sha256
    command, env = snapshot_compose(result, tmp_path)
    key, digest = next(iter(metadata.asset_digests.items()))
    assert key in env and Path(env[key]).read_bytes() == b'fixed'
    assert digest == sha256(b'fixed').hexdigest()
    assert Path(command[command.index('-f') + 1]).read_bytes() == result.assets['compose.json']


def test_inactive_and_empty_preserve_fixed_plan():
    assert not plan(values={'USE_DATA': False}).conditional
    assert not plan(contributions=()).conditional
    assert plan(values={'USE_DATA': False}).profiles == ()


@pytest.mark.parametrize('transport', [{}, {'DOCKER_HOST': 'tcp://127.0.0.1:2375'},
    {'DOCKER_HOST': 'unix:///run/a.sock', 'CONTAINER_HOST': 'unix:///run/b.sock'},
    {'DOCKER_HOST': 'unix://remote/run/a.sock'}, {'DOCKER_HOST': 'unix:///run/../a.sock'}])
def test_active_plan_requires_one_explicit_unix_transport(transport):
    with pytest.raises(ComposeError, match='Unix engine transport'):
        plan(engine_environment=transport)


def test_only_absent_active_transport_uses_typed_missing_signal():
    with pytest.raises(MissingEngineTransport):
        plan(engine_environment={})
    with pytest.raises(ComposeError) as invalid:
        plan(engine_environment={'DOCKER_HOST': 'tcp://127.0.0.1:2375'})
    assert type(invalid.value) is ComposeError
    assert not plan(values={'USE_DATA': False}, engine_environment={}).conditional


@pytest.mark.parametrize('fragment', [
    {'build': '.'}, {'privileged': True}, {'network_mode': 'host'},
    {'image': 'example/fixture:latest'}, {'image': IMAGE, 'ports': ('0.0.0.0:58090:8090',)},
    {'image': IMAGE, 'ports': ('127.0.0.1:70000:8090',)},
    {'image': IMAGE, 'environment': {'BAD': '${HOST_SECRET}'}},
    {'image': IMAGE, 'volumes': ('/tmp:/data',)},
    {'image': IMAGE, 'volumes': (MappingProxyType({'asset': 'missing', 'target': '/data'}),)},
    {'image': IMAGE, 'volumes': (MappingProxyType({'volume': 'data', 'target': '/../data'}),)},
    {'image': IMAGE, 'command': ('sh', '-c', 'true')},
])
def test_unsafe_fragments_rejected_before_engine(fragment):
    with pytest.raises(ComposeError):
        plan((service(compose=MappingProxyType(fragment)),))


def test_predicate_schema_and_capture_must_match():
    with pytest.raises(ComposeError, match='activation'):
        plan((service(activation=Activation('USE_DATA', 'yes')),))
    with pytest.raises(ComposeError, match='activation'):
        plan((service(activation=Activation('MISSING', True)),))
    with pytest.raises(ComposeError, match='captured bytes'):
        plan((service(assets=(replace(captured('server.py'), sha256='0' * 64),)),))


def test_missing_dependency_and_cycle_rejected():
    with pytest.raises(ComposeError, match='dependency'):
        plan((service(dependencies=('pack/missing',)),))
    left = service(dependencies=('pack/other',))
    right = replace(service(), id='pack/other', dependencies=('pack/server',))
    with pytest.raises(ComposeError, match='cycle'):
        plan((left, right))


def test_endpoint_and_seed_must_match_declared_ports_and_mounts():
    with pytest.raises(ComposeError, match='endpoint'):
        plan((service(endpoints=(EndpointOutput('api', 'http://other:58090', 'http://self:8090'),)),))
    with pytest.raises(ComposeError, match='endpoint'):
        plan((service(endpoints=(EndpointOutput('api', 'http://127.0.0.1:58090', 'http://self:9000'),)),))
    fragment = dict(service().compose)
    fragment['volumes'] = ()
    fragment.pop('command')
    with pytest.raises(ComposeError, match='Seed asset'):
        plan((service(compose=MappingProxyType(fragment)),))


def test_dependency_order_and_profile_closure():
    first = service()
    second_fragment = dict(first.compose)
    second_fragment['ports'] = ('127.0.0.1:58091:8090',)
    second_fragment['environment'] = MappingProxyType({'UPSTREAM': MappingProxyType({
        'endpoint': 'pack/server/api', 'address': 'in_project'})})
    second = replace(first, id='pack/other', dependencies=(first.id,),
        compose=MappingProxyType(second_fragment),
        endpoints=(EndpointOutput('api', 'http://127.0.0.1:58091', 'http://self:8090'),))
    result = plan((second, first))
    assert tuple(item.id for item in result.conditional) == (first.id, second.id)
    assert result.conditional[1].dependencies == (result.conditional[0].compose_name,)
    spec = json.loads(result.assets['compose.json'])['services'][result.conditional[1].compose_name]
    assert spec['depends_on'][result.conditional[0].compose_name] == {'condition': 'service_started'}
    assert spec['environment']['UPSTREAM'] == result.conditional[0].endpoints[0].in_project
    assert result.command.count('--profile') == 1


@pytest.mark.parametrize('part', [
    {'ports': ('127.0.0.1:58090:8090', '127.0.0.1:58090:8091')},
    {'ports': ('127.0.0.1:58090:8090', '127.0.0.1:58091:8091'),
     'environment': {'FIXTURE': 'ok'}, 'volumes': ()},
    {'volumes': (MappingProxyType({'volume': 'data', 'target': '/data'}),
                 MappingProxyType({'volume': 'other', 'target': '/data'}))},
    {'volumes': (MappingProxyType({'asset': 'server.py', 'target': '/opt/fixture/server.py'}),
                 MappingProxyType({'asset': 'server.py', 'target': '/opt/fixture/other.py'}))},
])
def test_port_and_mount_collisions(part):
    fragment = dict(service().compose)
    fragment.update(part)
    if fragment.get('volumes') == ():
        fragment.pop('command')
    with pytest.raises(ComposeError):
        plan((service(compose=MappingProxyType(fragment)),))


def test_active_fixed_profile_port_collision():
    fragment = dict(service().compose)
    fragment['ports'] = ('127.0.0.1:4318:8090',)
    item = service(compose=MappingProxyType(fragment),
        endpoints=(EndpointOutput('api', 'http://127.0.0.1:4318', 'http://self:8090'),))
    with pytest.raises(ComposeError, match='port'):
        plan((item,), values={'USE_DATA': True, 'TRACE_MODE': 'OTEL'})


def test_health_and_unconditional_activation():
    item = service(activation=None, readiness=Readiness('health'), seed=None)
    result = plan((item,), values={})
    assert result.conditional[0].readiness.kind == 'health'
    assert result.conditional[0].seed_completion is None


def test_bad_http_rule_and_bad_snapshot_path_rejected(tmp_path):
    with pytest.raises(ComposeError, match='readiness'):
        plan((service(readiness=Readiness('http', 'nopath')),))
    with pytest.raises(ComposeError, match='target'):
        plan((service(readiness=Readiness('http', 'api/../escape')),))
    bad = replace(plan(), assets=MappingProxyType({'../escape': b'bad'}))
    with pytest.raises(ComposeError, match='asset path'):
        snapshot_compose(bad, tmp_path)


@pytest.mark.parametrize('address', ['http://127.0.0.1:58090/path',
    'http://127.0.0.1:58090?q=1', 'http://user@127.0.0.1:58090',
    'https://127.0.0.1:58090', 'http://127.0.0.1:bad', 'http://127.0.0.1',
    'http://127.0.0.1:58090#x', 'http://foreign:58090'])
def test_bad_endpoint_addresses(address):
    with pytest.raises(ComposeError):
        plan((service(endpoints=(EndpointOutput('api', address, 'http://self:8090'),)),))


def test_wrong_crossed_port_pair_rejected():
    fragment = dict(service().compose)
    fragment['ports'] = ('127.0.0.1:58090:8090', '127.0.0.1:58091:8091')
    with pytest.raises(ComposeError, match='endpoint'):
        plan((service(compose=MappingProxyType(fragment),
            endpoints=(EndpointOutput('api', 'http://127.0.0.1:58090', 'http://self:8091'),)),))


def test_wildcard_published_port_rejected_independently():
    fragment = dict(service().compose)
    fragment['ports'] = ('0.0.0.0:58090:8090',)
    with pytest.raises(ComposeError, match='loopback'):
        plan((service(compose=MappingProxyType(fragment)),))


@pytest.mark.parametrize('reference', [
    {'endpoint': 'pack/server/api', 'address': 'in_project'},
    {'endpoint': 'pack/missing/api', 'address': 'in_project'},
    {'endpoint': 'pack/server/api', 'address': 'host'},
    {'endpoint': 'pack/server/api'},
])
def test_endpoint_reference_requires_declared_active_dependency(reference):
    fragment = dict(service().compose)
    fragment['environment'] = MappingProxyType({'UPSTREAM': MappingProxyType(reference)})
    with pytest.raises(ComposeError, match='environment|endpoint reference'):
        plan((service(compose=MappingProxyType(fragment)),))


@pytest.mark.parametrize('literal', ['$NAME', '$$NAME', '${NAME}', 'prefix$NAME'])
def test_literal_environment_rejects_compose_interpolation(literal):
    fragment = dict(service().compose)
    fragment['environment'] = MappingProxyType({'VALUE': literal})
    with pytest.raises(ComposeError, match='environment'):
        plan((service(compose=MappingProxyType(fragment)),))


@pytest.mark.parametrize('volume', [
    {'asset': 'server.py', 'target': '/opt/$SA_SCOPE/server.py'},
    {'volume': 'data', 'target': '/data/$SA_SCOPE'},
])
def test_mount_target_rejects_compose_interpolation(volume):
    fragment = dict(service().compose)
    mounts = list(fragment['volumes'])
    if 'asset' in volume:
        mounts[0] = MappingProxyType(volume)
        fragment['command'] = ('python', volume['target'])
    else:
        mounts[1] = MappingProxyType(volume)
    fragment['volumes'] = tuple(mounts)
    with pytest.raises(ComposeError, match='volume target'):
        plan((service(compose=MappingProxyType(fragment)),))


@pytest.mark.parametrize('bad', ['\r\n', '\x00', '\t', ' ', '\u00e9'])
def test_raw_host_endpoint_controls_rejected(bad):
    with pytest.raises(ComposeError, match='endpoint'):
        plan((service(endpoints=(EndpointOutput('api',
            'http://127.0.0.1:58090' + bad, 'http://self:8090'),)),))


@pytest.mark.parametrize('bad', ['\r\n', '\x00', '\t', ' ', '\u00e9'])
def test_raw_in_project_endpoint_controls_rejected(bad):
    with pytest.raises(ComposeError, match='endpoint'):
        plan((service(endpoints=(EndpointOutput('api',
            'http://127.0.0.1:58090', 'http://self:8090' + bad),)),))


@pytest.mark.parametrize('path', ['api/ready\r\n', 'api/ready\x00',
    'api/ready path', 'api/ready\\part', 'api/ready%ZZ'])
def test_raw_readiness_path_controls_rejected(path):
    with pytest.raises(ComposeError, match='readiness|target'):
        plan((service(readiness=Readiness('http', path)),))


def test_percent_encoded_readiness_path_remains_admitted():
    result = plan((service(readiness=Readiness('http', 'api/ready%20now')) ,))
    assert result.conditional[0].readiness.url == 'http://127.0.0.1:58090/ready%20now'


@pytest.mark.parametrize('delimiter', ['?', '#'])
@pytest.mark.parametrize('address', ['host', 'in_project'])
def test_empty_url_delimiters_rejected_before_readiness_join(delimiter, address):
    output = EndpointOutput('api',
        'http://127.0.0.1:58090' + (delimiter if address == 'host' else ''),
        'http://self:8090' + (delimiter if address == 'in_project' else ''))
    with pytest.raises(ComposeError, match='endpoint'):
        plan((service(endpoints=(output,)),))


@pytest.mark.parametrize('slash', ['', '/'])
def test_bare_and_trailing_slash_endpoint_bases_join_readiness(slash):
    output = EndpointOutput('api', 'http://127.0.0.1:58090' + slash,
                            'http://self:8090' + slash)
    result = plan((service(endpoints=(output,)),))
    assert result.conditional[0].readiness.url == 'http://127.0.0.1:58090/ready'


@pytest.mark.parametrize('userinfo', ['@', ':@', 'user:@', ':password@'])
@pytest.mark.parametrize('address', ['host', 'in_project'])
def test_empty_or_partial_userinfo_rejected(address, userinfo):
    host = ('http://' + userinfo + '127.0.0.1:58090' if address == 'host'
            else 'http://127.0.0.1:58090')
    internal = ('http://' + userinfo + 'self:8090' if address == 'in_project'
                else 'http://self:8090')
    with pytest.raises(ComposeError, match='endpoint'):
        plan((service(endpoints=(EndpointOutput('api', host, internal),)),))


def test_plain_host_and_in_project_endpoints_have_no_userinfo():
    result = plan()
    output = result.conditional[0].endpoints[0]
    assert output.host == 'http://127.0.0.1:58090'
    assert output.in_project.startswith('http://infra-')


def test_untyped_or_incomplete_config_rejected():
    with pytest.raises(ComposeError, match='typed configuration'):
        plan_infrastructure(None, SCHEMA, (service(),), SCOPE)
    with pytest.raises(ComposeError, match='typed configuration'):
        plan_infrastructure({}, None, (service(),), SCOPE)
    with pytest.raises(ComposeError, match='completed configuration'):
        plan_infrastructure(type('Incomplete', (), {'values': {'USE_DATA': True},
            'errors': ('missing',), 'help_requested': False})(), SCHEMA, (service(),), SCOPE)
    with pytest.raises(ComposeError, match='fresh typed'):
        plan(values={'USE_DATA': 'true'})


def test_duplicate_and_unsafe_captures_rejected():
    with pytest.raises(ComposeError, match='unique'):
        plan((service(), service()))
    with pytest.raises(ComposeError, match='captured bytes'):
        plan((service(assets=(captured('../escape'),)),))


@pytest.mark.parametrize('fragment', [
    {'image': IMAGE, 'ports': '127.0.0.1:58090:8090'},
    {'image': IMAGE, 'volumes': 'data:/data'},
    {'image': IMAGE, 'volumes': (MappingProxyType({'asset': 5, 'target': '/data'}),)},
    {'image': IMAGE, 'volumes': (MappingProxyType({'volume': 'Bad', 'target': '/data'}),)},
    {'image': IMAGE, 'environment': {'lowercase': 'bad'}},
])
def test_wrong_fragment_types_rejected(fragment):
    with pytest.raises(ComposeError):
        plan((service(compose=MappingProxyType(fragment)),))


def test_nonstring_endpoint_and_fixed_variable_ports():
    with pytest.raises(ComposeError, match='endpoint'):
        plan((service(endpoints=(EndpointOutput('api', None, 'http://self:8090'),)),))
    item = service(compose=MappingProxyType({'image': IMAGE, 'ports': ('127.0.0.1:58090:8090',)}),
        readiness=Readiness('health'), seed=None)
    both = plan((item,), values={'USE_DATA': True, 'TRACE_MODE': 'OTEL',
        'ALLURE_S3_USE_LOCALSTACK': True})
    assert set(both.profiles) >= {'otel', 'localstack'}


def test_multi_dependency_dag_visits_shared_predecessor_once():
    root = service()
    def other(identifier, host, dependencies):
        fragment = dict(root.compose)
        fragment['ports'] = (f'127.0.0.1:{host}:8090',)
        return replace(root, id=identifier, compose=MappingProxyType(fragment),
            dependencies=dependencies,
            endpoints=(EndpointOutput('api', f'http://127.0.0.1:{host}', 'http://self:8090'),))
    left = other('pack/left', 58091, (root.id,))
    right = other('pack/right', 58092, (root.id,))
    leaf = other('pack/leaf', 58093, (left.id, right.id))
    result = plan((leaf, right, left, root))
    assert tuple(item.id for item in result.conditional) == (
        root.id, left.id, right.id, leaf.id)


def test_collision_guards_are_causal(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(planner, '_name', lambda prefix, identifier: 'infra-collision')
        with pytest.raises(ComposeError, match='name collision'):
            plan((service(), replace(service(), id='pack/other')))
    with monkeypatch.context() as patch:
        original = planner._name
        patch.setattr(planner, '_name', lambda prefix, identifier:
            'jaeger-badger' if ':data' in identifier else original(prefix, identifier))
        with pytest.raises(ComposeError, match='volume collision'):
            plan(values={'USE_DATA': True, 'TRACE_MODE': 'OTEL'})
