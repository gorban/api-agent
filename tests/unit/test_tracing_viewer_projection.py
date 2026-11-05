"""Standalone viewer retains externally owned descriptor inputs and controllers."""
from dataclasses import replace

import pytest

from api_agent.config import ConfigSchema, load_shared_schema, resolve_config, schema_from_dict
from api_agent.config.declarations import UNSET
from api_agent.config.resolve import ConfigError
from api_agent.providers.loading import bootstrap_schema
from api_agent.runtime.trace_viewer import resolve_viewer_config
from api_agent.telemetry.tracing_providers import effective_selection


@pytest.fixture
def external_profile(tmp_path):
    provider = tmp_path / 'provider'
    provider.mkdir()
    (provider / 'provider.py').write_text("raise AssertionError('viewer imported executable factory')\n")
    (provider / 'tracing-provider.toml').write_text('''version=2
id="external-profile"
selector="MODE"
disabled_value=""
[[profiles]]
value="local"
signals=["traces"]
infrastructure=["fixture/collector"]
managed_endpoints={trace_endpoint="http://localhost:4318/v1/traces",dashboard_url="http://localhost:16686",dashboard_port_field="VIEWER_PORT"}
[profiles.dashboard]
url_field="DASHBOARD_URL"
[[profiles]]
value="other"
signals=[]
infrastructure=["fixture/other"]
managed_endpoints={trace_endpoint="http://localhost:4318/v1/traces",dashboard_url="http://localhost:16686",dashboard_port_field="OTHER_PORT"}
[profiles.dashboard]
url_field="OTHER_DASHBOARD_URL"
''')
    shared = load_shared_schema()
    fields = dict(shared.fields)
    fields['TRACE_PROVIDER'] = replace(fields['TRACE_PROVIDER'], default=str(provider / 'provider.py') + ':create')
    extension = schema_from_dict({'version': 1, 'fields': {
        'FEATURE': {'type': 'bool', 'default': True},
        'SECONDARY': {'type': 'bool', 'conditional_defaults': [{'when': {'FEATURE': True}, 'value': True}]},
        'MODE': {'type': 'str', 'default': 'local', 'applies_when': {'FEATURE': True}},
        'OTHER_PORT': {'type': 'int', 'default': 18686},
        'VIEWER_PORT': {'type': 'int', 'conditional_defaults': [{'when': {'SECONDARY': True}, 'value': 17686}]},
        'TRACE_EXPORT_ENDPOINT': {'type': 'str', 'default': 'http://localhost:4318/v1/traces'},
        'DASHBOARD_URL': {'type': 'str', 'default': ''},
        'OTHER_DASHBOARD_URL': {'type': 'str', 'default': 'https://other.example'},
    }})
    fields.update(extension.fields)
    return ConfigSchema(fields), {'owner_root': tmp_path}, provider


def test_external_selector_and_port_match_full_bootstrap_without_factory_import(external_profile):
    schema, anchors, provider = external_profile
    discovered = bootstrap_schema(schema, (), {}, {}, owner_root=anchors['owner_root'])
    full = resolve_config(discovered, environ={}, anchors=anchors, interactive=True)
    selection = effective_selection(full, owner_root=anchors['owner_root'], environ={})

    viewer = resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)

    assert selection.values['MODE'] == viewer['MODE'] == 'local'
    assert selection.values['VIEWER_PORT'] == viewer['VIEWER_PORT'] == 17686
    assert selection.values['DASHBOARD_URL'] == viewer['DASHBOARD_URL'] == 'http://localhost:17686'
    assert viewer['FEATURE'] is True
    assert 'SECONDARY' not in viewer
    assert 'OTHER_PORT' not in viewer
    assert 'OTHER_DASHBOARD_URL' not in viewer
    assert not (provider / '__pycache__').exists()


@pytest.mark.parametrize('missing', ['MODE', 'VIEWER_PORT', 'OTHER_PORT', 'OTHER_DASHBOARD_URL'])
def test_undefined_descriptor_fields_are_rejected_before_factory_import(external_profile, missing):
    schema, anchors, provider = external_profile
    fields = {name: field for name, field in schema.fields.items() if name != missing}
    with pytest.raises(ConfigError, match='no dashboard capability' if missing.endswith('PORT') else 'undeclared configuration fields'):
        resolve_viewer_config(ConfigSchema(fields), argv=(),
            environ={'MODE': 'other'} if missing.startswith('OTHER_') else {}, dotenv={}, anchors=anchors)
    assert not (provider / '__pycache__').exists()


def test_external_controller_keeps_inactive_selector_unavailable(external_profile):
    schema, anchors, _ = external_profile
    with pytest.raises(ConfigError, match='Invalid trace destination configuration'):
        resolve_viewer_config(schema, argv=(), environ={'FEATURE': 'False'}, dotenv={}, anchors=anchors)


def test_external_endpoint_matches_full_selection_and_keeps_managed_viewer(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('signals=["traces"]',
        'signals=["traces"]\nendpoint_field="DESTINATION"'))
    endpoint = schema_from_dict({'version': 1, 'fields': {
        'DESTINATION': {'type': 'str', 'conditional_defaults': [
            {'when': {'FEATURE': True}, 'value': 'http://localhost:4318/v1/traces'}]},
    }}, base_schema=schema)
    schema = ConfigSchema({**schema.fields, **endpoint.fields})
    full = resolve_config(bootstrap_schema(schema, (), {}, {}, owner_root=anchors['owner_root']),
        environ={}, anchors=anchors, interactive=True)
    selected = effective_selection(full, owner_root=anchors['owner_root'], environ={})

    viewer = resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)

    assert viewer['DESTINATION'] == selected.values['DESTINATION'] == 'http://localhost:4318/v1/traces'
    assert viewer['DASHBOARD_URL'] == selected.values['DASHBOARD_URL'] == 'http://localhost:17686'
    assert not (provider / '__pycache__').exists()


@pytest.mark.parametrize('source', ['environment', 'dotenv', 'cli'])
def test_viewer_ignores_typed_unused_protocol_while_execution_rejects(source):
    schema = load_shared_schema()
    argv = ('--trace-export-protocol=grpc',) if source == 'cli' else ()
    environ = {'TRACE_MODE': 'OTEL', **({'TRACE_EXPORT_PROTOCOL': 'grpc'} if source == 'environment' else {})}
    dotenv = {'TRACE_EXPORT_PROTOCOL': 'grpc'} if source == 'dotenv' else {}
    anchors = {'owner_root': schema.fields['TRACE_PROVIDER'].default.rsplit('/telemetry/', 1)[0]}

    viewer = resolve_viewer_config(schema, argv=argv, environ=environ, dotenv=dotenv, anchors=anchors)

    assert viewer['TRACE_VIEWER_URL'] == 'http://localhost:16686'
    assert 'TRACE_EXPORT_PROTOCOL' not in viewer
    discovered = bootstrap_schema(schema, argv, environ, dotenv, owner_root=anchors['owner_root'])
    execution = resolve_config(discovered, argv=argv, environ=environ, dotenv=dotenv,
        anchors=anchors, interactive=True)
    with pytest.raises(ConfigError, match='TRACE_EXPORT_PROTOCOL.*http/protobuf'):
        execution.raise_for_errors()


def test_viewer_ignores_required_exporter_secret_while_execution_requires_it(external_profile):
    schema, anchors, provider = external_profile
    (provider / 'cfg').mkdir()
    (provider / 'cfg/config.toml').write_text(
        'version=1\n[fields.EXPORT_SECRET]\ntype="str"\nsensitive=true\nrequired=true\n')

    viewer = resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)

    assert viewer['DASHBOARD_URL'] == 'http://localhost:17686'
    assert 'EXPORT_SECRET' not in viewer
    discovered = bootstrap_schema(schema, (), {}, {}, owner_root=anchors['owner_root'])
    execution = resolve_config(discovered, environ={}, anchors=anchors, interactive=True)
    assert 'EXPORT_SECRET' in execution.missing
    assert not (provider / '__pycache__').exists()


@pytest.mark.parametrize('literal', ['7', '"FIELD"', '["SAME", "SAME"]',
    '["bad field"]', '[' + ','.join('"FIELD_' + str(i) + '"' for i in range(33)) + ']'],
    ids=['integer', 'string', 'duplicate', 'invalid', 'oversized'])
def test_dashboard_configuration_dependencies_require_bounded_unique_names(external_profile, literal):
    from api_agent.telemetry.tracing_providers import read_declaration, TracingProviderError
    _, _, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=' + literal, 1))
    with pytest.raises(TracingProviderError, match='configuration field|bounded unique'):
        read_declaration(provider)


def test_declared_dashboard_dependency_is_required_and_retains_its_controllers(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=["VIEWER_TOKEN"]', 1))
    with pytest.raises(ConfigError, match='undeclared configuration fields'):
        resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)
    dependency = schema_from_dict({'version': 1, 'fields': {
        'VIEWER_TOKEN': {'type': 'str', 'sensitive': True, 'required_when': {'FEATURE': True}},
    }}, base_schema=schema)
    schema = ConfigSchema({**schema.fields, **dependency.fields})
    with pytest.raises(ConfigError, match='valid selected dashboard'):
        resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)

    viewer = resolve_viewer_config(schema, argv=(), environ={'VIEWER_TOKEN': 'synthetic-viewer-token'},
        dotenv={}, anchors=anchors)

    assert viewer['VIEWER_TOKEN'] == 'synthetic-viewer-token'
    assert viewer['FEATURE'] is True
    assert not (provider / '__pycache__').exists()


def test_managed_langfuse_viewer_keeps_ordinary_provisioning_and_readiness_keys(tmp_path):
    from api_agent.runtime.compose import plan_compose
    from api_agent.runtime.ownership import StackScope
    schema = load_shared_schema()
    environ = {'TUI_MODE': 'run-local', 'TRACE_MODE': 'LangFuse', 'LANGFUSE_PUBLIC_KEY': 'synthetic-project-public',
        'LANGFUSE_SECRET_KEY': 'synthetic-project-secret', 'ALLURE_S3_USE_LOCALSTACK': 'False'}
    anchors = {'owner_root': tmp_path}
    full = resolve_config(bootstrap_schema(schema, (), environ, {}, owner_root=tmp_path),
        environ=environ, anchors=anchors, interactive=True)
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / 'pack')
    ordinary_plan = plan_compose(full, scope)

    viewer = resolve_viewer_config(schema, argv=(), environ=environ, dotenv={}, anchors=anchors)
    viewer_plan = plan_compose(viewer, scope)

    assert viewer_plan.profiles == ordinary_plan.profiles == ('langfuse',)
    for key in ('SA_LANGFUSE_PUBLIC_KEY', 'SA_LANGFUSE_SECRET_KEY', 'SA_USER_EMAIL', 'SA_USER_PASSWORD'):
        assert viewer_plan.environment[key] == ordinary_plan.environment[key]
    assert viewer['LANGFUSE_PUBLIC_KEY'] == environ['LANGFUSE_PUBLIC_KEY']
    assert viewer['LANGFUSE_SECRET_KEY'] == environ['LANGFUSE_SECRET_KEY']


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('count', [0, 32])
def test_dashboard_dependency_list_is_immutable_for_static_and_profiled_descriptors(external_profile, version, count):
    from api_agent.telemetry.tracing_providers import read_declaration
    _, _, provider = external_profile
    header = 'version=' + str(version) + '\nid="dependencies"\n'
    if version == 2:
        header += 'selector="MODE"\ndisabled_value=""\n[[profiles]]\nvalue="viewer"\n'
    fields = ['SETTING_' + str(i) for i in range(count)]
    import json
    (provider / 'tracing-provider.toml').write_text(header + 'signals=[]\n' +
        ('[profiles.dashboard]\n' if version == 2 else '[dashboard]\n') +
        'url_field="DASHBOARD_URL"\nconfig_fields=' + json.dumps(fields) + '\n')

    declaration = read_declaration(provider)

    assert declaration.dashboard.config_fields == tuple(fields)


def test_unselected_profile_requirements_do_not_block_selected_dashboard(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    text = descriptor.read_text()
    index = text.rindex('[profiles.dashboard]')
    descriptor.write_text(text[:index] + text[index:].replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=["OTHER_REQUIRED"]', 1))
    required = schema_from_dict({'version': 1, 'fields': {
        'OTHER_REQUIRED': {'type': 'str', 'required': True, 'sensitive': True},
    }})
    schema = ConfigSchema({**schema.fields, **required.fields})

    viewer = resolve_viewer_config(schema, argv=(), environ={'OTHER_PORT': 'invalid'}, dotenv={}, anchors=anchors)

    assert viewer['MODE'] == 'local'
    assert viewer['DASHBOARD_URL'] == 'http://localhost:17686'
    assert 'OTHER_REQUIRED' not in viewer and 'OTHER_PORT' not in viewer
    with pytest.raises(ConfigError, match='no dashboard capability') as error:
        resolve_viewer_config(schema, argv=(), environ={'MODE': 'other'}, dotenv={}, anchors=anchors)
    assert "MODE='other'" in str(error.value)
    assert 'Actual TRACE_EXPORT_ENDPOINT=http://localhost:4318/v1/traces' in str(error.value)
    assert 'OTHER_DASHBOARD_URL' in str(error.value)
    with pytest.raises(ConfigError, match='valid selected dashboard'):
        resolve_viewer_config(schema, argv=(), environ={
            'MODE': 'other', 'OTHER_DASHBOARD_URL': 'https://dashboard.example'}, dotenv={}, anchors=anchors)
    assert not (provider / '__pycache__').exists()


@pytest.mark.parametrize('version', [1, 2])
def test_unmanaged_dashboard_does_not_require_declared_export_endpoint(external_profile, version):
    schema, anchors, provider = external_profile
    header = 'version=' + str(version) + '\nid="unused-endpoint"\n'
    if version == 2:
        header += 'selector="MODE"\ndisabled_value=""\n[[profiles]]\nvalue="local"\n'
    (provider / 'tracing-provider.toml').write_text(header + 'signals=["traces"]\nendpoint_field="DESTINATION"\n' +
        ('[profiles.dashboard]\n' if version == 2 else '[dashboard]\n') + 'url_field="DASHBOARD_URL"\n')
    endpoint = schema_from_dict({'version': 1, 'fields': {'DESTINATION': {'type': 'str', 'required': True}}})
    schema = ConfigSchema({**schema.fields, **endpoint.fields,
        'DASHBOARD_URL': replace(schema.fields['DASHBOARD_URL'], default='https://dashboard.example')})

    viewer = resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)

    assert viewer['DASHBOARD_URL'] == 'https://dashboard.example'
    assert 'DESTINATION' not in viewer
    ordinary = resolve_config(bootstrap_schema(schema, (), {}, {}, owner_root=anchors['owner_root']),
        environ={}, anchors=anchors, interactive=True)
    assert 'DESTINATION' in ordinary.missing
    assert not (provider / '__pycache__').exists()


def test_managed_dashboard_can_omit_required_export_endpoint_without_local_ownership(external_profile):
    schema, anchors, _ = external_profile
    schema = ConfigSchema({**schema.fields,
        'TRACE_EXPORT_ENDPOINT': replace(schema.fields['TRACE_EXPORT_ENDPOINT'],
            default=UNSET, required=True),
        'DASHBOARD_URL': replace(schema.fields['DASHBOARD_URL'], default='https://dashboard.example')})

    viewer = resolve_viewer_config(schema, argv=(), environ={'DASHBOARD_URL': 'https://dashboard.example'}, dotenv={}, anchors=anchors)

    assert viewer['DASHBOARD_URL'] == 'https://dashboard.example'
    assert viewer.get('TRACE_EXPORT_ENDPOINT', '') == ''
    selection = effective_selection(viewer, owner_root=anchors['owner_root'], environ={}, require_export=False)
    assert selection.declaration.managed_compose_profile is None
    assert not selection.declaration.infrastructure


def test_explicit_dashboard_dependency_keeps_endpoint_required(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=["TRACE_EXPORT_ENDPOINT"]', 1))
    schema = ConfigSchema({**schema.fields,
        'TRACE_EXPORT_ENDPOINT': replace(schema.fields['TRACE_EXPORT_ENDPOINT'], default=UNSET, required=True),
        'DASHBOARD_URL': replace(schema.fields['DASHBOARD_URL'], default='https://dashboard.example')})
    with pytest.raises(ConfigError, match='valid selected dashboard'):
        resolve_viewer_config(schema, argv=(), environ={'DASHBOARD_URL': 'https://dashboard.example'}, dotenv={}, anchors=anchors)


def test_optional_endpoint_drops_export_only_requirement_controller(external_profile):
    schema, anchors, _ = external_profile
    extra = schema_from_dict({'version': 1, 'fields': {
        'EXPORT_ENABLED': {'type': 'bool', 'required': True},
    }})
    schema = ConfigSchema({**schema.fields, **extra.fields,
        'TRACE_EXPORT_ENDPOINT': replace(schema.fields['TRACE_EXPORT_ENDPOINT'], default=UNSET,
            required_when={'EXPORT_ENABLED': True})})

    viewer = resolve_viewer_config(schema, argv=(),
        environ={'DASHBOARD_URL': 'https://dashboard.example'}, dotenv={}, anchors=anchors)

    assert viewer['DASHBOARD_URL'] == 'https://dashboard.example'
    assert 'EXPORT_ENABLED' not in viewer
    assert viewer.get('TRACE_EXPORT_ENDPOINT', '') == ''


def test_typed_invalid_managed_dashboard_port_is_rejected_before_factory_import(external_profile):
    schema, anchors, provider = external_profile
    with pytest.raises(ConfigError, match='no dashboard capability'):
        resolve_viewer_config(schema, argv=(), environ={'VIEWER_PORT': '0'}, dotenv={}, anchors=anchors)
    assert not (provider / '__pycache__').exists()


@pytest.mark.parametrize('endpoint', [None, 'https://collector.example/traces'])
def test_selected_export_only_profile_has_no_standalone_dashboard(external_profile, endpoint):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    text = descriptor.read_text()
    index = text.rindex('[[profiles]]')
    descriptor.write_text(text[:index] + '[[profiles]]\nvalue="export"\nsignals=["traces"]\n')
    schema = ConfigSchema({**schema.fields,
        'TRACE_EXPORT_ENDPOINT': replace(schema.fields['TRACE_EXPORT_ENDPOINT'], default=UNSET, required=True)})
    environment = {'MODE': 'export'}
    if endpoint is not None:
        environment['TRACE_EXPORT_ENDPOINT'] = endpoint
    with pytest.raises(ConfigError, match='no dashboard capability') as error:
        resolve_viewer_config(schema, argv=(), environ=environment, dotenv={}, anchors=anchors)
    assert ('Actual TRACE_EXPORT_ENDPOINT=' + (endpoint or '<unset or unsafe URL; redacted>')) in str(error.value)
    assert not (provider / '__pycache__').exists()


@pytest.mark.parametrize('predicate', ['applies_when', 'conditional_defaults', 'required_when'])
@pytest.mark.parametrize('value', [None, 'invalid', 'True'])
def test_auxiliary_controllers_are_best_effort_for_explicit_dashboard(external_profile, predicate, value):
    schema, anchors, _ = external_profile
    controls = schema_from_dict({'version': 1, 'fields': {
        'AUXILIARY': {'type': 'bool', 'required': True},
    }})
    endpoint = schema.fields['TRACE_EXPORT_ENDPOINT']
    changes = ({'conditional_defaults': ({'when': {'AUXILIARY': True}, 'value': endpoint.default},), 'default': UNSET}
        if predicate == 'conditional_defaults' else {predicate: {'AUXILIARY': True}})
    schema = ConfigSchema({**schema.fields, **controls.fields,
        'TRACE_EXPORT_ENDPOINT': replace(endpoint, **changes),
        'DASHBOARD_URL': replace(schema.fields['DASHBOARD_URL'], conditional_defaults=(
            {'when': {'TRACE_EXPORT_ENDPOINT': 'http://localhost:4318/v1/traces'},
             'value': 'http://localhost:17686'},))})
    environ = {'DASHBOARD_URL': 'https://dashboard.example'}
    if value is not None:
        environ['AUXILIARY'] = value

    viewer = resolve_viewer_config(schema, argv=(), environ=environ, dotenv={}, anchors=anchors)

    assert viewer['DASHBOARD_URL'] == 'https://dashboard.example'
    selection = effective_selection(viewer, owner_root=anchors['owner_root'], environ={}, require_export=False)
    assert not selection.declaration.infrastructure
    assert 'AUXILIARY' not in viewer
    if value == 'True' or predicate == 'required_when':
        assert viewer['TRACE_EXPORT_ENDPOINT'] == 'http://localhost:4318/v1/traces'
    else:
        assert viewer['TRACE_EXPORT_ENDPOINT'] == ''


def test_remote_langfuse_dashboard_needs_no_managed_project_keys():
    schema = load_shared_schema()
    viewer = resolve_viewer_config(schema, argv=(), environ={
        'TRACE_MODE': 'LangFuse', 'TRACE_EXPORT_ENDPOINT': 'https://collector.example/api/public/otel/v1/traces',
        'TRACE_VIEWER_URL': 'https://dashboard.example', 'LANGFUSE_ADMIN_EMAIL': 'viewer@example.test',
        'LANGFUSE_ADMIN_PASSWORD': 'synthetic-dashboard-password'}, dotenv={}, anchors={})
    assert viewer['TRACE_VIEWER_URL'] == 'https://dashboard.example'
    assert 'LANGFUSE_PUBLIC_KEY' not in viewer and 'LANGFUSE_SECRET_KEY' not in viewer


@pytest.mark.parametrize('source', ['environment', 'dotenv', 'cli'])
def test_unavailable_dashboard_redacts_export_secret_from_endpoint_path(source):
    secret = 'synthetic-project-secret'
    environment = {'TRACE_MODE': 'LangFuse',
        'TRACE_EXPORT_ENDPOINT': 'https://collector.example/' + secret + '/traces',
        'LANGFUSE_ADMIN_EMAIL': 'viewer@example.test', 'LANGFUSE_ADMIN_PASSWORD': 'synthetic-dashboard-password'}
    argv = ('--langfuse-secret-key=' + secret,) if source == 'cli' else ()
    dotenv = {'LANGFUSE_SECRET_KEY': secret} if source == 'dotenv' else {}
    if source == 'environment':
        environment['LANGFUSE_SECRET_KEY'] = secret

    with pytest.raises(ConfigError) as error:
        resolve_viewer_config(load_shared_schema(), argv=argv, environ=environment, dotenv=dotenv, anchors={})

    assert 'no dashboard capability' in str(error.value)
    assert '<unset or unsafe URL; redacted>' in str(error.value)
    assert secret not in str(error.value)


def test_unavailable_dashboard_redacts_secret_from_provider_location(external_profile):
    schema, anchors, provider = external_profile
    secret = 'synthetic-location-secret'
    moved = provider.with_name(secret)
    provider.rename(moved)
    schema = ConfigSchema({**schema.fields,
        'TRACE_PROVIDER': replace(schema.fields['TRACE_PROVIDER'], default=str(moved / 'provider.py') + ':create'),
        **schema_from_dict({'version': 1, 'fields': {'EXPORT_SECRET': {'type': 'str'}}}).fields})
    descriptor = moved / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('signals=["traces"]',
        'signals=["traces"]\nsecret_fields=["EXPORT_SECRET"]', 1))

    with pytest.raises(ConfigError) as error:
        resolve_viewer_config(schema, argv=(), environ={'EXPORT_SECRET': secret,
            'TRACE_EXPORT_ENDPOINT': 'https://collector.example/traces'}, dotenv={}, anchors=anchors)

    assert 'no dashboard capability' in str(error.value)
    assert 'TRACE_PROVIDER=' in str(error.value)
    assert secret not in str(error.value)
    assert '[redacted]/provider.py:create' in str(error.value)


@pytest.mark.parametrize('source', ['environment', 'dotenv', 'cli'])
@pytest.mark.parametrize('credential', ['declared', 'dashboard', 'sensitive'])
def test_disabled_profile_redacts_known_raw_secret_in_provider_path_without_requiring_credentials(
        external_profile, source, credential):
    schema, anchors, provider = external_profile
    secret = 'synthetic-disabled-secret'
    moved = provider.with_name(secret)
    provider.rename(moved)
    fields = schema_from_dict({'version': 1, 'fields': {
        'EXPORT_SECRET': {'type': 'str', 'required': True, 'sensitive': credential == 'sensitive'},
        'VIEWER_USERNAME': {'type': 'str', 'required': True},
    }}).fields
    schema = ConfigSchema({**schema.fields, **fields,
        'TRACE_PROVIDER': replace(schema.fields['TRACE_PROVIDER'], default=str(moved / 'provider.py') + ':create')})
    descriptor = moved / 'tracing-provider.toml'
    if credential == 'declared':
        descriptor.write_text(descriptor.read_text().replace('signals=["traces"]',
            'signals=["traces"]\nsecret_fields=["EXPORT_SECRET"]', 1))
    if credential == 'dashboard':
        descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
            '[profiles.dashboard]\nusername_field="VIEWER_USERNAME"\npassword_field="EXPORT_SECRET"\nlogin="provider"', 1))
    environment = {'MODE': ''}
    argv = ('--export-secret=' + secret,) if source == 'cli' else ()
    dotenv = {'EXPORT_SECRET': secret} if source == 'dotenv' else {}
    if source == 'environment':
        environment['EXPORT_SECRET'] = secret

    with pytest.raises(ConfigError) as error:
        resolve_viewer_config(schema, argv=argv, environ=environment, dotenv=dotenv, anchors=anchors)

    message = str(error.value)
    assert 'enabled tracing provider profile' in message
    assert 'MODE is disabled' in message
    assert '[redacted]/provider.py:create' in message
    assert secret not in message
    assert not (moved / '__pycache__').exists()


@pytest.mark.parametrize('credential', ['required', 'missing-controller', 'undeclared'])
def test_unavailable_dashboard_diagnostic_does_not_require_export_secrets(external_profile, credential, monkeypatch):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('signals=["traces"]',
        'signals=["traces"]\nsecret_fields=["EXPORT_SECRET"]', 1))
    field = schema_from_dict({'version': 1, 'fields': {
        'EXPORT_SECRET': {'type': 'str', 'required': True}}}).fields['EXPORT_SECRET']
    if credential == 'missing-controller':
        field = replace(field, applies_when={'UNDECLARED': True})
    if credential != 'undeclared':
        schema = ConfigSchema({**schema.fields, 'EXPORT_SECRET': field})
    if credential == 'missing-controller':
        monkeypatch.setattr('api_agent.providers.loading.bootstrap_schema', lambda *args, **kwargs: schema)

    with pytest.raises(ConfigError) as error:
        resolve_viewer_config(schema, argv=(), environ={
            'TRACE_EXPORT_ENDPOINT': 'https://collector.example/traces'}, dotenv={}, anchors=anchors)

    assert 'no dashboard capability' in str(error.value)
    assert 'Actual TRACE_EXPORT_ENDPOINT=https://collector.example/traces' in str(error.value)


def test_unavailable_dashboard_redacts_explicit_secret_with_unavailable_controller(external_profile, monkeypatch):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('signals=["traces"]',
        'signals=["traces"]\nsecret_fields=["EXPORT_SECRET"]', 1))
    secret_field = schema_from_dict({'version': 1, 'fields': {'EXPORT_SECRET': {'type': 'str'}}}).fields['EXPORT_SECRET']
    schema = ConfigSchema({**schema.fields,
        'EXPORT_SECRET': replace(secret_field, applies_when={'UNDECLARED': True})})
    monkeypatch.setattr('api_agent.providers.loading.bootstrap_schema', lambda *args, **kwargs: schema)
    secret = 'synthetic-unresolved-secret'

    with pytest.raises(ConfigError) as error:
        resolve_viewer_config(schema, argv=(), environ={'EXPORT_SECRET': secret,
            'TRACE_EXPORT_ENDPOINT': 'https://collector.example/' + secret + '/traces'}, dotenv={}, anchors=anchors)

    assert 'no dashboard capability' in str(error.value)
    assert secret not in str(error.value)


def test_managed_only_required_dependency_is_strict_only_for_local_admission(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nmanaged_config_fields=["LOCAL_KEY"]', 1))
    required = schema_from_dict({'version': 1, 'fields': {'LOCAL_KEY': {'type': 'str', 'required': True}}})
    schema = ConfigSchema({**schema.fields, **required.fields})
    with pytest.raises(ConfigError, match='valid selected dashboard'):
        resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)
    local = resolve_viewer_config(schema, argv=(), environ={'LOCAL_KEY': 'synthetic'}, dotenv={}, anchors=anchors)
    assert local['LOCAL_KEY'] == 'synthetic'
    remote = resolve_viewer_config(schema, argv=(), environ={'DASHBOARD_URL': 'https://dashboard.example'},
        dotenv={}, anchors=anchors)
    assert 'LOCAL_KEY' not in remote


def test_endpoint_directly_named_as_dashboard_url_remains_strict(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('signals=["traces"]',
        'signals=["traces"]\nendpoint_field="DASHBOARD_URL"'))
    schema = ConfigSchema({**schema.fields,
        'DASHBOARD_URL': replace(schema.fields['DASHBOARD_URL'], default=UNSET, required=True)})
    with pytest.raises(ConfigError, match='valid selected dashboard'):
        resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)


@pytest.mark.parametrize('member', ['config_fields', 'managed_config_fields'])
@pytest.mark.parametrize('literal', ['7', '["SAME", "SAME"]', '["bad name"]',
    '[' + ','.join('"FIELD_' + str(i) + '"' for i in range(33)) + ']'])
def test_role_dependency_lists_are_strictly_bounded(external_profile, member, literal):
    from api_agent.telemetry.tracing_providers import read_declaration, TracingProviderError
    _, _, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\n' + member + '=' + literal, 1))
    with pytest.raises(TracingProviderError, match='configuration field|bounded unique'):
        read_declaration(provider)


def test_auxiliary_default_cycle_cannot_block_explicit_remote_dashboard(external_profile):
    schema, anchors, _ = external_profile
    cyclic = schema_from_dict({'version': 1, 'fields': {
        'TRACE_EXPORT_ENDPOINT': {'type': 'str', 'conditional_defaults': [
            {'when': {'AUXILIARY': True}, 'value': 'http://localhost:4318/v1/traces'}]},
        'AUXILIARY': {'type': 'bool', 'conditional_defaults': [
            {'when': {'TRACE_EXPORT_ENDPOINT': 'http://localhost:4318/v1/traces'}, 'value': True}]},
    }})
    schema = ConfigSchema({**schema.fields, **cyclic.fields})
    viewer = resolve_viewer_config(schema, argv=(), environ={'DASHBOARD_URL': 'https://dashboard.example'},
        dotenv={}, anchors=anchors)
    assert viewer['TRACE_EXPORT_ENDPOINT'] == ''
    assert 'AUXILIARY' not in viewer


def test_missing_optional_endpoint_declaration_cannot_reenable_invalid_auxiliary_ownership(external_profile):
    schema, anchors, _ = external_profile
    schema = ConfigSchema({name: field for name, field in schema.fields.items() if name != 'TRACE_EXPORT_ENDPOINT'})
    viewer = resolve_viewer_config(schema, argv=(), environ={'VIEWER_PORT': '0',
        'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': 'http://localhost:4318/v1/traces',
        'DASHBOARD_URL': 'https://dashboard.example'}, dotenv={}, anchors=anchors)
    assert viewer['TRACE_EXPORT_ENDPOINT'] == 'http://localhost:4318/v1/traces'
    assert viewer['VIEWER_PORT'] == 0
    selection = effective_selection(viewer, owner_root=anchors['owner_root'], environ={}, require_export=False)
    assert not selection.declaration.infrastructure


@pytest.mark.parametrize('change', [{'MODE': ''}, {'DASHBOARD_URL': 'https://changed.example'}])
def test_managed_dependency_join_rejects_configuration_changes_between_admissions(external_profile, monkeypatch, change):
    from api_agent.runtime import tracing_viewer_config
    schema, anchors, _ = external_profile
    environ = {}
    original = tracing_viewer_config.effective_selection
    calls = []
    def admit_then_change_input(*args, **kwargs):
        selection = original(*args, **kwargs)
        calls.append(selection)
        if len(calls) == 1:
            environ.update(change)
        return selection
    monkeypatch.setattr(tracing_viewer_config, 'effective_selection', admit_then_change_input)
    with pytest.raises(ConfigError, match='changed admitted ownership'):
        resolve_viewer_config(schema, argv=(), environ=environ, dotenv={}, anchors=anchors)
    assert len(calls) == 2


@pytest.mark.parametrize('member', ['TRACE_EXPORT_ENDPOINT', 'VIEWER_PORT'])
def test_managed_named_dependency_uses_original_strict_auxiliary_declaration(external_profile, member):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nmanaged_config_fields=["' + member + '"]', 1))
    viewer = resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)
    assert viewer[member] == ('http://localhost:4318/v1/traces' if member == 'TRACE_EXPORT_ENDPOINT' else 17686)


def test_undefined_managed_dependency_rejects_local_admission(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nmanaged_config_fields=["UNDECLARED"]', 1))
    with pytest.raises(ConfigError, match='undeclared configuration fields'):
        resolve_viewer_config(schema, argv=(), environ={}, dotenv={}, anchors=anchors)


def test_strict_named_endpoint_does_not_bypass_invalid_managed_port(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=["TRACE_EXPORT_ENDPOINT"]', 1))
    with pytest.raises(ConfigError, match='Invalid trace destination configuration'):
        resolve_viewer_config(schema, argv=(), environ={'VIEWER_PORT': '0'}, dotenv={}, anchors=anchors)


def test_strict_named_endpoint_with_invalid_auxiliary_port_keeps_explicit_manual_viewer(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=["TRACE_EXPORT_ENDPOINT"]', 1))
    viewer = resolve_viewer_config(schema, argv=(), environ={'VIEWER_PORT': '0',
        'DASHBOARD_URL': 'https://dashboard.example'}, dotenv={}, anchors=anchors)
    assert viewer['TRACE_EXPORT_ENDPOINT'] == 'http://localhost:4318/v1/traces'
    assert viewer['DASHBOARD_URL'] == 'https://dashboard.example'
    selection = effective_selection(viewer, owner_root=anchors['owner_root'], environ={}, require_export=False)
    assert not selection.declaration.infrastructure


def test_required_auxiliary_port_controller_does_not_weaken_strict_endpoint_or_manual_viewer(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=["TRACE_EXPORT_ENDPOINT"]', 1))
    controller = schema_from_dict({'version': 1, 'fields': {
        'PORT_ENABLED': {'type': 'bool', 'required': True},
    }})
    schema = ConfigSchema({**schema.fields, **controller.fields,
        'VIEWER_PORT': replace(schema.fields['VIEWER_PORT'], conditional_defaults=(
            {'when': {'PORT_ENABLED': True}, 'value': 17686},))})
    viewer = resolve_viewer_config(schema, argv=(), environ={'DASHBOARD_URL': 'https://dashboard.example'},
        dotenv={}, anchors=anchors)
    assert viewer['TRACE_EXPORT_ENDPOINT'] == 'http://localhost:4318/v1/traces'
    assert viewer['DASHBOARD_URL'] == 'https://dashboard.example'
    assert viewer['VIEWER_PORT'] == 0
    assert 'PORT_ENABLED' not in viewer
    selection = effective_selection(viewer, owner_root=anchors['owner_root'], environ={}, require_export=False)
    assert not selection.declaration.infrastructure


@pytest.mark.parametrize('version', [1, 2])
@pytest.mark.parametrize('endpoint', ['invalid', ''])
def test_unused_endpoint_enum_cannot_block_manual_dashboard(external_profile, version, endpoint):
    schema, anchors, provider = external_profile
    if version == 1:
        (provider / 'tracing-provider.toml').write_text('version=1\nid="manual"\nsignals=["traces"]\n'
            '[dashboard]\nurl_field="DASHBOARD_URL"\n')
    schema = ConfigSchema({**schema.fields,
        'TRACE_EXPORT_ENDPOINT': replace(schema.fields['TRACE_EXPORT_ENDPOINT'],
            enum=('http://localhost:4318/v1/traces',))})
    environ = {'TRACE_EXPORT_ENDPOINT': endpoint, 'DASHBOARD_URL': 'https://dashboard.example'}

    viewer = resolve_viewer_config(schema, argv=(), environ=environ, dotenv={}, anchors=anchors)

    assert viewer['DASHBOARD_URL'] == 'https://dashboard.example'
    selection = effective_selection(viewer, owner_root=anchors['owner_root'], environ={}, require_export=False)
    assert not selection.declaration.infrastructure
    ordinary = resolve_config(bootstrap_schema(schema, (), environ, {}, owner_root=anchors['owner_root']),
        environ=environ, anchors=anchors, interactive=True)
    with pytest.raises(ConfigError, match='TRACE_EXPORT_ENDPOINT.*expected one of'):
        ordinary.raise_for_errors()


@pytest.mark.parametrize('controller', [None, 'invalid'])
def test_auxiliary_enum_with_missing_or_invalid_controller_cannot_block_manual_dashboard(external_profile, controller):
    schema, anchors, _ = external_profile
    added = schema_from_dict({'version': 1, 'fields': {'AUX_ENUM': {'type': 'bool', 'required': True}}})
    schema = ConfigSchema({**schema.fields, **added.fields,
        'TRACE_EXPORT_ENDPOINT': replace(schema.fields['TRACE_EXPORT_ENDPOINT'],
            enum=('http://localhost:4318/v1/traces',), applies_when={'AUX_ENUM': True})})
    environ = {'DASHBOARD_URL': 'https://dashboard.example'}
    if controller is not None:
        environ['AUX_ENUM'] = controller

    viewer = resolve_viewer_config(schema, argv=(), environ=environ, dotenv={}, anchors=anchors)

    assert viewer['TRACE_EXPORT_ENDPOINT'] == ''
    assert viewer['DASHBOARD_URL'] == 'https://dashboard.example'
    assert 'AUX_ENUM' not in viewer


def test_effective_standard_endpoint_outside_raw_enum_retains_admitted_route(external_profile):
    schema, anchors, _ = external_profile
    schema = ConfigSchema({**schema.fields,
        'TRACE_EXPORT_ENDPOINT': replace(schema.fields['TRACE_EXPORT_ENDPOINT'],
            enum=('http://localhost:4318/v1/traces',))})
    environ = {'DASHBOARD_URL': 'https://dashboard.example',
        'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': 'https://collector.example/v1/traces'}

    viewer = resolve_viewer_config(schema, argv=(), environ=environ, dotenv={}, anchors=anchors)

    assert viewer['TRACE_EXPORT_ENDPOINT'] == environ['OTEL_EXPORTER_OTLP_TRACES_ENDPOINT']
    assert viewer['DASHBOARD_URL'] == 'https://dashboard.example'
    ordinary = resolve_config(bootstrap_schema(schema, (), environ, {}, owner_root=anchors['owner_root']),
        environ=environ, anchors=anchors, interactive=True)
    assert not ordinary.errors
    selected = effective_selection(ordinary, owner_root=anchors['owner_root'], environ=environ)
    assert selected.values['TRACE_EXPORT_ENDPOINT'] == viewer['TRACE_EXPORT_ENDPOINT']


@pytest.mark.parametrize(('environment', 'reason'), [
    ({'TRACE_EXPORT_ENDPOINT': 'https://collector.example/v1/traces'}, 'saved endpoint from a different profile'),
    ({'DASHBOARD_URL': ''}, 'DASHBOARD_URL is explicitly empty'),
])
def test_custom_provider_dashboard_diagnostic_uses_its_own_fields(external_profile, environment, reason):
    schema, anchors, provider = external_profile
    with pytest.raises(ConfigError) as error:
        resolve_viewer_config(schema, argv=(), environ=environment, dotenv={}, anchors=anchors)
    message = str(error.value)
    assert reason in message
    assert "MODE='local'" in message
    assert str(provider/'provider.py') + ':create' in message
    assert 'http://localhost:4318/v1/traces' in message


@pytest.mark.parametrize('conditional', [False, True])
@pytest.mark.parametrize('destination', ['provider-path', 'endpoint-path'])
def test_diagnostic_redacts_sensitive_schema_defaults(external_profile, conditional, destination):
    schema, anchors, provider = external_profile
    secret = 'synthetic-sensitive-default'
    declaration = {'type':'str', 'sensitive':True}
    declaration.update({'conditional_defaults':[{'when':{'FEATURE':True},'value':secret}]}
                       if conditional else {'default':secret})
    extension = schema_from_dict({'version':1,'fields':{'DEFAULT_SECRET':declaration}}, base_schema=schema)
    fields = {**schema.fields, **extension.fields}
    if destination == 'provider-path':
        moved = provider.with_name(secret)
        provider.rename(moved)
        fields['TRACE_PROVIDER'] = replace(fields['TRACE_PROVIDER'], default=str(moved / 'provider.py') + ':create')
        environ = {'MODE':''}
    else:
        fields['TRACE_EXPORT_ENDPOINT'] = replace(fields['TRACE_EXPORT_ENDPOINT'],
            default='https://collector.example/' + secret + '/traces')
        environ = {}
    with pytest.raises(ConfigError) as error:
        resolve_viewer_config(ConfigSchema(fields), argv=(), environ=environ, dotenv={}, anchors=anchors)
    message = str(error.value)
    assert 'TRACE_PROVIDER=' in message
    assert secret not in message
    assert ('[redacted]' in message if destination == 'provider-path' else
            '<unset or unsafe URL; redacted>' in message)


@pytest.mark.parametrize('source', ['environment', 'dotenv', 'cli'])
def test_stale_profile_endpoint_diagnostic_precedes_missing_dashboard_credentials(source):
    values = {'TRACE_MODE': 'LangFuse', 'TRACE_EXPORT_ENDPOINT': 'http://localhost:4318/v1/traces'}
    argv = tuple('--' + name.lower().replace('_', '-') + '=' + value for name, value in values.items()) if source == 'cli' else ()
    environment = values if source == 'environment' else {}
    dotenv = values if source == 'dotenv' else {}

    with pytest.raises(ConfigError) as error:
        resolve_viewer_config(load_shared_schema(), argv=argv, environ=environment, dotenv=dotenv, anchors={})

    message = str(error.value)
    assert 'no dashboard capability' in message
    assert "TRACE_MODE='LangFuse'" in message
    assert 'Actual TRACE_EXPORT_ENDPOINT=http://localhost:4318/v1/traces' in message
    assert 'http://localhost:3000/api/public/otel/v1/traces' in message
    assert 'remove the saved override' in message and 'TRACE_VIEWER_URL' in message


def test_explicit_empty_viewer_is_explained_without_dashboard_credentials():
    with pytest.raises(ConfigError, match='explicitly empty'):
        resolve_viewer_config(load_shared_schema(), argv=(), environ={
            'TRACE_MODE': 'LangFuse', 'TRACE_EXPORT_ENDPOINT': 'http://localhost:4318/v1/traces',
            'TRACE_VIEWER_URL': ''}, dotenv={}, anchors={})


def test_explicit_remote_dashboard_still_requires_selected_login_credentials():
    with pytest.raises(ConfigError, match='valid selected dashboard configuration'):
        resolve_viewer_config(load_shared_schema(), argv=(), environ={
            'TRACE_MODE': 'LangFuse', 'TRACE_EXPORT_ENDPOINT': 'https://collector.example/traces',
            'TRACE_VIEWER_URL': 'https://dashboard.example'}, dotenv={}, anchors={})


@pytest.mark.parametrize('named_route', [False, True])
def test_unavailable_external_dashboard_precedes_missing_declared_dependency(external_profile, named_route):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    fields = '["TRACE_EXPORT_ENDPOINT","VIEWER_TOKEN"]' if named_route else '["VIEWER_TOKEN"]'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=' + fields, 1))
    dependency = schema_from_dict({'version': 1, 'fields': {
        'VIEWER_TOKEN': {'type': 'str', 'sensitive': True, 'required_when': {'FEATURE': True}},
    }}, base_schema=schema)
    schema = ConfigSchema({**schema.fields, **dependency.fields})

    with pytest.raises(ConfigError, match='no dashboard capability') as error:
        resolve_viewer_config(schema, argv=(), environ={
            'TRACE_EXPORT_ENDPOINT': 'https://collector.example/traces'}, dotenv={}, anchors=anchors)

    assert "MODE='local'" in str(error.value)
    assert 'DASHBOARD_URL' in str(error.value)
    assert not (provider / '__pycache__').exists()


def test_unavailable_dashboard_does_not_defer_typed_dependency_errors(external_profile):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=["VIEWER_COUNT"]', 1))
    dependency = schema_from_dict({'version': 1, 'fields': {
        'VIEWER_COUNT': {'type': 'int', 'required': True},
    }}, base_schema=schema)
    schema = ConfigSchema({**schema.fields, **dependency.fields})

    with pytest.raises(ConfigError, match='VIEWER_COUNT'):
        resolve_viewer_config(schema, argv=(), environ={
            'TRACE_EXPORT_ENDPOINT': 'https://collector.example/traces', 'VIEWER_COUNT': 'invalid'},
            dotenv={}, anchors=anchors)

    assert not (provider / '__pycache__').exists()


@pytest.mark.parametrize('custom', [False, True])
def test_passwordless_dashboard_diagnostic_uses_only_string_environment_names(external_profile, custom):
    import os
    if custom:
        schema, anchors, _ = external_profile
        values = {'MODE': 'local', 'TRACE_EXPORT_ENDPOINT': 'https://collector.example/traces'}
        selector = "MODE='local'"
    else:
        schema, anchors = load_shared_schema(), {}
        values = {'TRACE_MODE': 'OTEL', 'TRACE_EXPORT_ENDPOINT': 'https://collector.example/traces'}
        selector = "TRACE_MODE='OTEL'"
    template = os.environ
    backing = {template.encodekey(name): template.encodevalue(value) for name, value in values.items()}
    environment = type(template)(backing, template.encodekey, template.decodekey,
                                 template.encodevalue, template.decodevalue)

    with pytest.raises(ConfigError, match='no dashboard capability') as error:
        resolve_viewer_config(schema, argv=(), environ=environment, dotenv={}, anchors=anchors)

    assert selector in str(error.value)
    assert 'Actual TRACE_EXPORT_ENDPOINT=https://collector.example/traces' in str(error.value)


@pytest.mark.parametrize('controller,match', [({}, 'valid selected dashboard'),
                                            ({'ROUTE_ENABLED': 'invalid'}, 'ROUTE_ENABLED')])
def test_named_route_keeps_required_controller_strict(external_profile, controller, match):
    schema, anchors, provider = external_profile
    descriptor = provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]',
        '[profiles.dashboard]\nconfig_fields=["TRACE_EXPORT_ENDPOINT"]', 1))
    dependency = schema_from_dict({'version': 1, 'fields': {
        'ROUTE_ENABLED': {'type': 'bool', 'required': True},
    }}, base_schema=schema)
    schema = ConfigSchema({**schema.fields, **dependency.fields,
        'TRACE_EXPORT_ENDPOINT': replace(schema.fields['TRACE_EXPORT_ENDPOINT'], default=UNSET,
                                        required_when={'ROUTE_ENABLED': True})})

    with pytest.raises(ConfigError, match=match):
        resolve_viewer_config(schema, argv=(), environ={**controller,
            'TRACE_EXPORT_ENDPOINT': 'https://collector.example/traces'}, dotenv={}, anchors=anchors)

    assert not (provider / '__pycache__').exists()
