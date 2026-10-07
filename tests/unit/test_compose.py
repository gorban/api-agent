from dataclasses import dataclass
import itertools
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from api_agent.runtime.compose import (ComposeError, local_development_environment,
    plan_compose, selected_profiles, validate_compose, validate_document)


@dataclass
class Scope:
    id: str = '0123456789abcdef'
    project: str = 'sa-0123456789abcdef'

    def labels(self, profile):
        return {'api-agent.managed': 'true',
                'api-agent.scope': self.id,
                'api-agent.profile': profile}


@pytest.mark.parametrize('mode,target,expected', [('', '', ()),
    ('LangFuse', None, ('langfuse',)), ('OTEL', None, ('otel',)),
    ('OTEL', 'http://127.0.0.1:4318/v1/traces', ('otel',)),
    ('LangFuse', 'http://[::1]:3000/', ()),
    ('LangFuse', 'https://remote.example:3000', ()),
    ('OTEL', 'http://localhost:4319/v1/traces', ()),
    ('OTEL', 'http://localhost:4318/other', ()),
    ('OTEL', 'http://localhost:4318/v1/traces?x=1', ())])
def test_profile_selection(mode, target, expected):
    assert selected_profiles({'TRACE_MODE': mode, 'TRACE_TARGET': target}) == expected


@pytest.mark.parametrize('mode,localstack', list(itertools.product(('', 'LangFuse', 'OTEL'), (False, True))))
def test_every_profile_combination_has_dependencies_and_scope(mode, localstack):
    scope = Scope()
    plan = plan_compose({'TRACE_MODE': mode, 'ALLURE_S3_ENDPOINT': 'http://localhost:4566' if localstack else ''}, scope)
    doc = json.loads((plan.directory / 'compose.json').read_text())
    active = set(plan.services)
    assert ('localstack' in active) == localstack
    assert ('langfuse-web' in active) == (mode == 'LangFuse')
    assert ('jaeger' in active) == (mode == 'OTEL')
    for name in active:
        assert set(doc['services'][name].get('depends_on', {})) <= active
    assert not any('ports' in doc['services'][name] for name in ('postgres', 'clickhouse', 'redis', 'langfuse-worker'))
    assert plan.environment['SA_SCOPE'] == scope.id


def test_command_is_explicit_and_environment_is_isolated(monkeypatch):
    monkeypatch.setenv('SA_POSTGRES_PASSWORD', 'private-env-value')
    monkeypatch.setenv('COMPOSE_FILE', '/private/compose.yml')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'private-key')
    plan = plan_compose({'TRACE_MODE': 'LangFuse', 'ALLURE_S3_ENDPOINT': 'http://localhost:4566'}, Scope())
    assert plan.command == ('docker', 'compose', '--project-name', Scope().project,
        '--project-directory', str(plan.directory), '-f', str(plan.directory / 'compose.json'),
        '--env-file', str(plan.directory / 'empty.env'), '--profile', 'langfuse', '--profile', 'localstack')
    assert plan.validation_command == (*plan.command, 'config', '--quiet')
    assert 'private-env-value' not in plan.environment.values()
    assert 'COMPOSE_FILE' not in plan.environment and 'AWS_SECRET_ACCESS_KEY' not in plan.environment
    assert 'SA_POSTGRES_PASSWORD' not in repr(plan)
    assert plan.environment == plan_compose({'TRACE_MODE': 'LangFuse'}, Scope()).environment
    assert local_development_environment(Scope('fedcba9876543210', 'sa-fedcba9876543210')) != plan.environment


@pytest.mark.parametrize('config', [{'TRACE_MODE': 'invalid'}, {'TRACE_MODE': []}, {'TRACE_MODE': 'LangFuse', 'TRACE_TARGET': ''}, {'TRACE_MODE': 'OTEL', 'TRACE_TARGET': 7},
    {'TRACE_MODE': 'OTEL', 'TRACE_TARGET': 'file:///tmp/a'},
    {'ALLURE_S3_ENDPOINT': 'http://user:password@localhost:4566'},
    {'ALLURE_S3_ENDPOINT': 'http://localhost:invalid'}, {'ALLURE_S3_ENDPOINT': 'http://localhost:4566/#x'}])
def test_invalid_config(config):
    with pytest.raises(ComposeError):
        plan_compose(config, Scope())


def test_remote_s3_does_not_request_stack():
    assert selected_profiles({'ALLURE_S3_ENDPOINT': 'https://s3.example:4566'}) == ()


@pytest.mark.parametrize('mutation', ['labels', 'port', 'dependency', 'profile', 'image', 'override', 'volume'])
def test_document_rejects_unowned_or_unsafe_configuration(mutation):
    plan = plan_compose({}, Scope())
    doc = json.loads((plan.directory / 'compose.json').read_text())
    service = doc['services']['langfuse-web']
    if mutation == 'labels': service['labels']['api-agent.managed'] = 'false'
    if mutation == 'port': service['ports'] = ['3000:3000']
    if mutation == 'dependency': service['depends_on'] = {'jaeger': {'condition': 'service_started'}}
    if mutation == 'profile': service['profiles'] = []
    if mutation == 'image': service['image'] = 'langfuse/langfuse:latest'
    if mutation == 'override': service['env_file'] = '.env'
    if mutation == 'volume': service['volumes'] = ['/private:/data']
    with pytest.raises(ComposeError):
        validate_document(doc, Scope(), ('langfuse',))


def test_missing_services():
    with pytest.raises(ComposeError): validate_document({}, Scope(), ())


@pytest.mark.parametrize('scope,executable', [(Scope('bad', 'sa-bad'), 'docker'), (Scope(), '-docker'), (Scope(), '')])
def test_invalid_identity(scope, executable):
    with pytest.raises(ComposeError): plan_compose({}, scope, executable=executable)


def test_missing_and_dirty_controlled_files(tmp_path):
    with pytest.raises(ComposeError): plan_compose({}, Scope(), compose_directory=tmp_path)
    plan = plan_compose({}, Scope())
    (tmp_path / 'compose.json').write_text((plan.directory / 'compose.json').read_text())
    (tmp_path / 'empty.env').write_text('SA_POSTGRES_PASSWORD=private\n')
    with pytest.raises(ComposeError, match='must be empty'): plan_compose({}, Scope(), compose_directory=tmp_path)


def test_validation_never_starts_engine_and_discards_secrets():
    plan = plan_compose({'TRACE_MODE': 'OTEL'}, Scope())
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='private', stderr='private')
    assert validate_compose(plan, runner=run)
    assert calls[0][0][-2:] == ('config', '--quiet')
    assert calls[0][1]['env'] == plan.environment
    assert calls[0][1]['capture_output'] is True
    assert not validate_compose(plan_compose({}, Scope()), runner=run)
    assert len(calls) == 1
    with pytest.raises(ComposeError, match='exit 2') as error:
        validate_compose(plan, runner=lambda *args, **kwargs: SimpleNamespace(returncode=2, stderr='private'))
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('failure', [OSError('private'), subprocess.TimeoutExpired('private', 15)])
def test_validation_unavailable(failure):
    def run(*args, **kwargs): raise failure
    with pytest.raises(ComposeError, match='unavailable'):
        validate_compose(plan_compose({'TRACE_MODE': 'OTEL'}, Scope()), runner=run)


def test_installed_compose_assets_are_found_without_cwd(tmp_path, monkeypatch):
    import api_agent.runtime.compose as compose
    source_plan = plan_compose({}, Scope())
    package = tmp_path / 'site-packages' / 'api_agent'
    module = package / 'runtime' / 'compose.py'
    assets = package / 'infra' / 'compose'
    module.parent.mkdir(parents=True)
    assets.mkdir(parents=True)
    module.write_text('# installed module')
    for name in ('compose.json', 'empty.env', 'localstack-ready.sh'):
        (assets / name).write_text((source_plan.directory / name).read_text())
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(compose, '__file__', str(module))
    plan = compose.plan_compose({'TRACE_MODE': 'OTEL'}, Scope())
    assert plan.directory == assets
    assert plan.services == ('jaeger',)
    assert str(assets / 'compose.json') in plan.command


def test_incomplete_source_assets_fall_back_to_package(tmp_path, monkeypatch):
    import api_agent.runtime.compose as compose
    module = tmp_path / 'src' / 'api_agent' / 'runtime' / 'compose.py'
    module.parent.mkdir(parents=True)
    module.write_text('# source module')
    source_assets = tmp_path / 'infra' / 'compose'
    source_assets.mkdir(parents=True)
    (source_assets / 'compose.json').write_text('{}')
    monkeypatch.setattr(compose, '__file__', str(module))
    assert compose.default_compose_directory() == tmp_path / 'src' / 'api_agent' / 'infra' / 'compose'


def test_langfuse_v3_storage_and_headless_contract():
    """Assert upstream-consumed names and usable values, not just valid Compose."""
    plan = plan_compose({'TRACE_MODE': 'LangFuse'}, Scope())
    document = json.loads((plan.directory / 'compose.json').read_text())
    required_storage_suffixes = ('BUCKET', 'REGION', 'ACCESS_KEY_ID',
        'SECRET_ACCESS_KEY', 'ENDPOINT', 'FORCE_PATH_STYLE', 'PREFIX')
    for name in ('langfuse-web', 'langfuse-worker'):
        env = document['services'][name]['environment']
        for kind in ('EVENT', 'MEDIA'):
            prefix = f'LANGFUSE_S3_{kind}_UPLOAD_'
            assert {prefix + suffix for suffix in required_storage_suffixes} <= env.keys()
            assert env[prefix + 'BUCKET'] == 'langfuse'
            assert env[prefix + 'REGION'] == 'us-east-1'
            assert env[prefix + 'ACCESS_KEY_ID'] == document['services']['minio']['environment']['MINIO_ROOT_USER']
            assert env[prefix + 'SECRET_ACCESS_KEY'] == document['services']['minio']['environment']['MINIO_ROOT_PASSWORD']
            assert env[prefix + 'FORCE_PATH_STYLE'] == 'true'
            assert env[prefix + 'PREFIX'] == ('events/' if kind == 'EVENT' else 'media/')
            expected_endpoint = ('http://localhost:9090' if kind == 'MEDIA' and name == 'langfuse-web'
                                 else 'http://minio:9000')
            assert env[prefix + 'ENDPOINT'] == expected_endpoint
            assert not any(key.startswith(f'LANGFUSE_S3_{kind}_') and not key.startswith(prefix) for key in env)
        assert env['LANGFUSE_S3_BATCH_EXPORT_BUCKET'] == 'langfuse'
        assert env['LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY'] == document['services']['minio']['environment']['MINIO_ROOT_PASSWORD']
    web_env = document['services']['langfuse-web']['environment']
    for suffix in ('ORG_ID', 'ORG_NAME', 'PROJECT_ID', 'PROJECT_NAME',
                   'PROJECT_PUBLIC_KEY', 'PROJECT_SECRET_KEY', 'USER_EMAIL', 'USER_NAME', 'USER_PASSWORD'):
        assert web_env['LANGFUSE_INIT_' + suffix]
    assert web_env['LANGFUSE_INIT_PROJECT_PUBLIC_KEY'] == '${SA_LANGFUSE_PUBLIC_KEY:?controlled local development value required}'
    assert web_env['LANGFUSE_INIT_PROJECT_SECRET_KEY'] == '${SA_LANGFUSE_SECRET_KEY:?controlled local development value required}'
    assert plan.environment['SA_LANGFUSE_PUBLIC_KEY'].startswith('pk-lf-local-')
    assert plan.environment['SA_LANGFUSE_SECRET_KEY'].startswith('sk-lf-local-')
    assert plan.environment['SA_USER_PASSWORD']
    assert 'LANGFUSE_INIT_USER_PASSWORD' not in document['services']['langfuse-worker']['environment']


def test_ipv6_s3_requires_user_managed_endpoint():
    assert selected_profiles({'ALLURE_S3_ENDPOINT': 'http://[::1]:4566'}) == ()


def test_explicit_engine_transport_paths_are_preserved_without_user_environment(monkeypatch):
    monkeypatch.setenv('DOCKER_HOST', 'unix:///not-selected.sock')
    plan = plan_compose({'TRACE_MODE': 'OTEL'}, Scope(), engine_environment={
        'PATH': '/usr/local/bin:/usr/bin', 'DOCKER_HOST': 'unix:///chosen.sock',
        'HOME': '/tmp/public-home', 'DOCKER_CONFIG': '/tmp/public-engine-config',
        'CONTAINER_HOST': 'unix:///chosen-podman.sock', 'XDG_RUNTIME_DIR': '/run/user/1000'})
    assert plan.environment['DOCKER_HOST'] == 'unix:///chosen.sock'
    assert plan.environment['PATH'].startswith('/usr/local/bin')
    assert plan.environment['SA_SCOPE'] == Scope().id


@pytest.mark.parametrize('environment', [{'AWS_SECRET_ACCESS_KEY': 'private'}, {'PATH': 7}, {'HOME': 'bad\x00path'}])
def test_engine_environment_rejects_unapproved_keys_or_values(environment):
    with pytest.raises(ComposeError, match='environment override'):
        plan_compose({}, Scope(), engine_environment=environment)


def test_local_headless_keys_match_explicit_reviewed_configuration():
    plan = plan_compose({'TRACE_MODE': 'LangFuse', 'LANGFUSE_PUBLIC_KEY': 'pk-lf-reviewed-local',
                         'LANGFUSE_SECRET_KEY': 'sk-lf-reviewed-local'}, Scope())
    assert plan.environment['SA_LANGFUSE_PUBLIC_KEY'] == 'pk-lf-reviewed-local'
    assert plan.environment['SA_LANGFUSE_SECRET_KEY'] == 'sk-lf-reviewed-local'
    assert 'sk-lf-reviewed-local' not in repr(plan)


@pytest.mark.parametrize('key', [None, '', ' ', 7, 'bad\x00key', ' key '])
def test_local_project_key_validation(key):
    if key is None:
        assert plan_compose({'TRACE_MODE': 'LangFuse', 'LANGFUSE_PUBLIC_KEY': key}, Scope()).environment['SA_LANGFUSE_PUBLIC_KEY'].startswith('pk-lf-local-')
    else:
        with pytest.raises(ComposeError, match='project keys'):
            plan_compose({'TRACE_MODE': 'LangFuse', 'LANGFUSE_PUBLIC_KEY': key}, Scope())


@pytest.mark.parametrize('explicit_keys', [False, True])
def test_resolved_configuration_headless_readiness_and_tracing_keys_match_across_scopes(tmp_path, monkeypatch, explicit_keys):
    import asyncio
    import base64
    from types import SimpleNamespace
    from api_agent.config import load_shared_schema, resolve_config
    from api_agent.runtime.ownership import StackScope
    from api_agent.runtime import prerequisites
    from api_agent.telemetry.tracing import TraceRuntime
    environment = {'TUI_MODE': 'run-local', 'TRACE_MODE': 'LangFuse'}
    if explicit_keys:
        environment.update(LANGFUSE_PUBLIC_KEY='pk-lf-explicit-local', LANGFUSE_SECRET_KEY='sk-lf-explicit-local')
    result = resolve_config(load_shared_schema(), environ=environment, dotenv={})
    assert not result.errors and not result.missing
    expected = (result.values['LANGFUSE_PUBLIC_KEY'] + ':' + result.values['LANGFUSE_SECRET_KEY']).encode()
    scopes = [StackScope.from_roots(tmp_path / 'base', tmp_path / name) for name in ('one', 'two')]
    assert scopes[0].id != scopes[1].id
    for scope in scopes:
        plan = plan_compose(result.values, scope)
        assert (plan.environment['SA_LANGFUSE_PUBLIC_KEY'] + ':' + plan.environment['SA_LANGFUSE_SECRET_KEY']).encode() == expected
        document = json.loads((plan.directory / 'compose.json').read_text())
        headless = prerequisites._interpolate(document['services']['langfuse-web']['environment'], plan.environment)
        assert (headless['LANGFUSE_INIT_PROJECT_PUBLIC_KEY'] + ':' + headless['LANGFUSE_INIT_PROJECT_SECRET_KEY']).encode() == expected
        requests = []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return json.dumps({'data': [{'id': 'api-agent-' + scope.id}]}).encode()
        def open_local(request, **kwargs):
            requests.append(request)
            return Response()
        monkeypatch.setattr(prerequisites, 'urlopen', open_local)
        assert prerequisites._readiness('langfuse', .1, plan.environment)
        assert base64.b64decode(requests[0].headers['Authorization'].split()[1]) == expected
        exports = []
        def factory(endpoint, headers, resource):
            exports.append((endpoint, headers))
            return SimpleNamespace(), SimpleNamespace()
        runtime = TraceRuntime(result, factory=factory)
        asyncio.run(runtime.astart())
        assert exports[0][0] == 'http://localhost:3000/api/public/otel/v1/traces'
        assert base64.b64decode(exports[0][1]['Authorization'].split()[1]) == expected


def test_wrapper_host_uid_is_only_accepted_as_explicit_public_transport_value(monkeypatch):
    monkeypatch.setenv('API_AGENT_HOST_UID', '999')
    assert 'API_AGENT_HOST_UID' not in plan_compose({}, Scope()).environment
    plan = plan_compose({}, Scope(), engine_environment={'API_AGENT_HOST_UID': '1000', 'PATH': '/usr/local/bin:/usr/bin'})
    assert plan.environment['API_AGENT_HOST_UID'] == '1000'


@pytest.mark.parametrize('uid', ['', '-1', 'one', '1.5', '١', ' 1000'])
def test_wrapper_host_uid_rejects_non_numeric_transport_value(uid):
    with pytest.raises(ComposeError, match='ASCII digits'):
        plan_compose({}, Scope(), engine_environment={'API_AGENT_HOST_UID': uid})


def test_localstack_toggle_defaults_and_explicit_selected_bucket_region():
    default = plan_compose({'ALLURE_S3_USE_LOCALSTACK': True}, Scope())
    assert default.profiles == ('localstack',)
    assert default.environment['SA_ALLURE_S3_BUCKET'] == 'allure-results'
    assert default.environment['SA_ALLURE_S3_REGION'] == 'us-east-1'
    assert Path(default.environment['SA_LOCALSTACK_READY_HOOK']).is_absolute()
    explicit = plan_compose({'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': 'selected-bucket',
        'ALLURE_S3_REGION': 'eu-west-1', 'ALLURE_S3_ENDPOINT': 'http://127.0.0.1:4566/'}, Scope())
    assert explicit.environment['SA_ALLURE_S3_BUCKET'] == 'selected-bucket'
    assert explicit.environment['SA_ALLURE_S3_REGION'] == 'eu-west-1'
    assert len(explicit.environment['SA_LOCALSTACK_READY_DIGEST']) == 64


@pytest.mark.parametrize('endpoint', ['https://s3.example', '', 'http://[::1]:4566',
    'http://localhost:4567', 'http://localhost:4566/bucket', 'http://localhost:4566?x=1'])
def test_localstack_toggle_refuses_explicit_conflicting_endpoint(endpoint):
    with pytest.raises(ComposeError):
        plan_compose({'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_ENDPOINT': endpoint}, Scope())


@pytest.mark.parametrize('config', [{'ALLURE_S3_USE_LOCALSTACK': 'True'},
    {'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': ''},
    {'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': 7},
    {'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': 'bad..bucket'},
    {'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': '127.0.0.1'},
    {'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_REGION': ''},
    {'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_REGION': 7}])
def test_localstack_selection_validates_bucket_region_and_typed_toggle(config):
    with pytest.raises(ComposeError): plan_compose(config, Scope())


def test_localstack_readonly_hook_mount_and_configuration_contract():
    from api_agent.runtime.prerequisites import _interpolate
    plan = plan_compose({'ALLURE_S3_USE_LOCALSTACK': True, 'ALLURE_S3_BUCKET': 'chosen-results'}, Scope())
    document = json.loads((plan.directory / 'compose.json').read_text())
    spec = _interpolate(document['services']['localstack'], plan.environment)
    assert spec['environment']['SA_ALLURE_S3_BUCKET'] == 'chosen-results'
    assert spec['environment']['AWS_DEFAULT_REGION'] == plan.environment['SA_ALLURE_S3_REGION']
    assert spec['environment']['SA_LOCALSTACK_READY_DIGEST'] == plan.environment['SA_LOCALSTACK_READY_DIGEST']
    assert spec['volumes'][1] == {'type': 'bind', 'source': str(plan.directory / 'localstack-ready.sh'),
        'target': '/etc/localstack/init/ready.d/10-api-agent-s3.sh', 'read_only': True,
        'bind': {'create_host_path': False, 'selinux': 'z'}}
    for name, change in [('localstack', {'read_only': False}), ('localstack', {'source': '/private'}),
                         ('jaeger', {})]:
        altered = json.loads((plan.directory / 'compose.json').read_text())
        mount = {**document['services']['localstack']['volumes'][1], **change}
        altered['services'][name]['volumes'] = [mount]
        with pytest.raises(ComposeError, match='controlled LocalStack hook'):
            validate_document(altered, Scope(), plan.profiles)


@pytest.mark.parametrize('case', ['missing', 'symlink', 'unreadable'])
def test_controlled_hook_asset_must_be_real_readable_file(tmp_path, monkeypatch, case):
    import shutil
    source = plan_compose({}, Scope()).directory
    for name in ('compose.json', 'empty.env'):
        shutil.copyfile(source / name, tmp_path / name)
    hook = tmp_path / 'localstack-ready.sh'
    if case == 'symlink': hook.symlink_to(source / 'localstack-ready.sh')
    if case == 'unreadable':
        shutil.copyfile(source / 'localstack-ready.sh', hook)
        original = Path.read_bytes
        def read(path):
            if path == hook: raise PermissionError('private')
            return original(path)
        monkeypatch.setattr(Path, 'read_bytes', read)
    with pytest.raises(ComposeError, match='hook'):
        plan_compose({}, Scope(), compose_directory=tmp_path)


@pytest.mark.parametrize('explicit', [False, True])
def test_resolved_localstack_toggle_and_selected_bucket_feed_provisioning_plan(explicit, tmp_path):
    from api_agent.config import load_shared_schema, resolve_config
    environment = {'TUI_MODE': 'integration', 'ALLURE_S3_USE_LOCALSTACK': 'True'}
    if explicit:
        environment.update(ALLURE_S3_BUCKET='explicit-results', ALLURE_S3_REGION='eu-west-1')
    result = resolve_config(load_shared_schema(), environ=environment, dotenv={}, anchors={'owner_root': tmp_path})
    assert not result.errors and not result.missing
    plan = plan_compose(result.values, Scope())
    assert plan.profiles == ('localstack',)
    assert plan.environment['SA_ALLURE_S3_BUCKET'] == ('explicit-results' if explicit else 'allure-results')
    assert plan.environment['SA_ALLURE_S3_REGION'] == ('eu-west-1' if explicit else 'us-east-1')
    assert result.values['ALLURE_S3_ENDPOINT'] == 'http://localhost:4566'
