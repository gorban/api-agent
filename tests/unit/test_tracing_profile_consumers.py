"""Bundled profiles use identical inert selection across live and captured owners."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from api_agent.config.resolve import ConfigResult
from api_agent.runtime import tracing_collector_snapshot as snapshots
from api_agent.runtime.resource_pack import load_launch_context
from api_agent.runtime.tracing_services import custom_trace_prerequisites, tracing_requested
from api_agent.runtime.tracing_context import capture_trace_context, restore_trace_context
from api_agent.telemetry.tracing_providers import effective_selection
from test_pack_composition import pack

BASE = Path(__file__).resolve().parents[2]
PROVIDER = str(BASE / 'src/api_agent/telemetry/trace_adapters/provider.py') + ':create_tracing_provider'


@pytest.fixture
def profile_context(tmp_path):
    return load_launch_context(BASE, pack(tmp_path, 'application'))


def selected(mode='OTEL', **overrides):
    endpoint = ('http://localhost:4318/v1/traces' if mode == 'OTEL'
                else 'http://localhost:3000/api/public/otel/v1/traces')
    values = dict(TRACE_PROVIDER=PROVIDER, TRACE_MODE=mode, TRACE_EXPORT_ENDPOINT=endpoint,
                  JAEGER_VIEWER_PORT=16686, TRACE_METRICS=False,
                  LANGFUSE_ADMIN_EMAIL='viewer@example.com', LANGFUSE_ADMIN_PASSWORD='synthetic-login')
    values.update(overrides)
    return ConfigResult(values, {}, (), (), ())


@pytest.mark.parametrize('mode,profile,services', [
    ('OTEL', 'otel', {'jaeger'}),
    ('LangFuse', 'langfuse', {'langfuse-web', 'langfuse-worker', 'postgres', 'redis', 'clickhouse', 'minio'}),
])
def test_live_and_captured_profiles_reuse_exact_controlled_resources(profile_context, monkeypatch, mode, profile, services):
    config = selected(mode)
    canonical = effective_selection(config, owner_root=profile_context.owner_root)
    manager = custom_trace_prerequisites(profile_context, config)
    assert manager.plan.profiles == (profile,)
    assert set(manager.plan.services) == services
    packet = snapshots.capture_collector_plan(profile_context, config)
    snapshot = {'owner_root': str(profile_context.owner_root), 'trace_collector_plan': packet, 'trace_context':capture_trace_context(canonical)}
    def forbidden(*args, **kwargs):
        pytest.fail('Restoration read mutable source')
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    restored = snapshots.collector_from_snapshot(snapshot, canonical.values)
    assert restored.plan.profiles == manager.plan.profiles
    assert restored.plan.services == manager.plan.services
    assert restored.plan.assets == manager.plan.assets
    assert packet['identity'] == canonical.identity


def test_disabled_profile_allocates_no_dashboard_collector_or_capture(profile_context, monkeypatch):
    from api_agent.runtime.local_tracing import LocalTracing
    from api_agent.telemetry.privacy import EmissionBoundary
    config = selected('')
    monkeypatch.setattr('api_agent.tooling.admission.capture_tool_factory', lambda *a, **k: pytest.fail('Factory captured'))
    monkeypatch.setattr('api_agent.runtime.inventory.capture_inventory', lambda *a, **k: pytest.fail('Inventory read'))
    assert not tracing_requested(config, owner_root=profile_context.owner_root)
    assert custom_trace_prerequisites(profile_context, config) is None
    assert snapshots.capture_collector_plan(profile_context, config) is None
    dashboard = LocalTracing(config, emissions=EmissionBoundary(), owner_root=profile_context.owner_root)
    assert dashboard.state == 'disabled'
    assert dashboard._binding is None


@pytest.mark.parametrize('change', [
    {'TRACE_EXPORT_ENDPOINT': 'https://collector.example/v1/traces'},
    {'TRACE_VIEWER_URL': 'https://dashboard.example'},
    {'TRACE_VIEWER_URL': ''},
])
def test_remote_or_explicit_empty_viewer_never_plans_a_local_stack(profile_context, change, monkeypatch):
    from api_agent.config.resolve import ResolvedValue
    config = selected(**change)
    config = ConfigResult(config.values, {key: ResolvedValue(value, 'environment', True) for key, value in change.items()}, (), (), ())
    monkeypatch.setattr(snapshots, '_schema', lambda *a, **k: pytest.fail('Remote schema loaded'))
    manager = custom_trace_prerequisites(profile_context, config)
    assert manager is None
    packet = snapshots.capture_collector_plan(profile_context, config)
    canonical = effective_selection(config, owner_root=profile_context.owner_root)
    assert not packet['base_assets'] and packet['inventory'] is None
    assert snapshots.collector_from_snapshot({'owner_root': str(profile_context.owner_root), 'trace_collector_plan': packet, 'trace_context':capture_trace_context(canonical)}, canonical.values) is None


@pytest.mark.parametrize('change', [
    {'TRACE_MODE': 'LangFuse'},
    {'TRACE_EXPORT_ENDPOINT': 'https://other.example/v1/traces'},
    {'TRACE_VIEWER_URL': 'https://other.example'},
    {'JAEGER_VIEWER_PORT': 16687, 'TRACE_VIEWER_URL': 'http://localhost:16687'},
])
def test_captured_profile_or_destination_drift_rejects_before_manager(profile_context, change, monkeypatch):
    config = selected()
    canonical = effective_selection(config, owner_root=profile_context.owner_root)
    packet = snapshots.capture_collector_plan(profile_context, config)
    constructor = Mock()
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager', constructor)
    with pytest.raises(ValueError, match='profile or destinations'):
        snapshots.collector_from_snapshot({'owner_root': str(profile_context.owner_root), 'trace_collector_plan': packet, 'trace_context':capture_trace_context(canonical)},
                                         {**canonical.values, **change})
    constructor.assert_not_called()


def test_unknown_controlled_profile_rejects_before_manager(profile_context, monkeypatch):
    from dataclasses import replace
    from api_agent.telemetry.tracing_providers import read_declaration
    declaration = read_declaration(Path(PROVIDER.split(':')[0]).parent)
    profiles = tuple(replace(row, managed_compose_profile='missing-profile') for row in declaration.profiles)
    declaration = replace(declaration, profiles=profiles)
    monkeypatch.setattr('api_agent.telemetry.tracing_selection.read_declaration', lambda *a: declaration)
    constructor = Mock()
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager', constructor)
    with pytest.raises(ValueError, match='absent from the controlled'):
        custom_trace_prerequisites(profile_context, selected())
    constructor.assert_not_called()


def test_configured_jaeger_port_is_shared_by_viewer_live_and_capture(profile_context):
    config = selected(JAEGER_VIEWER_PORT=17686)
    selection = effective_selection(config, owner_root=profile_context.owner_root)
    assert selection.values['TRACE_VIEWER_URL'] == 'http://localhost:17686'
    manager = custom_trace_prerequisites(profile_context, config)
    assert manager.plan.environment['SA_JAEGER_VIEWER_PORT'] == '17686'
    packet = snapshots.capture_collector_plan(profile_context, config)
    restored = snapshots.collector_from_snapshot({'owner_root': str(profile_context.owner_root), 'trace_collector_plan': packet, 'trace_context':capture_trace_context(selection)}, selection.values)
    assert restored.plan.environment['SA_JAEGER_VIEWER_PORT'] == '17686'


def test_environment_trace_destination_controls_managed_selection_and_capture(profile_context, monkeypatch):
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', 'https://remote.example/v1/traces')
    config = selected()
    selection = effective_selection(config, owner_root=profile_context.owner_root)
    assert selection.values['TRACE_EXPORT_ENDPOINT'] == 'https://remote.example/v1/traces'
    assert selection.declaration.dashboard is None
    assert custom_trace_prerequisites(profile_context, config) is None
    packet = snapshots.capture_collector_plan(profile_context, config)
    assert not packet['base_assets']
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', 'https://changed.example/v1/traces')
    # Canonical captured values have explicit ownership and cannot drift with a worker environment.
    assert snapshots.collector_from_snapshot({'owner_root': str(profile_context.owner_root), 'trace_collector_plan': packet,
        'trace_context': capture_trace_context(selection)}, selection.values) is None


@pytest.mark.parametrize('trace,log', [(False,False),(False,True),(True,False),(True,True)])
@pytest.mark.asyncio
async def test_disabled_dashboard_start_never_probes_or_imports(profile_context, monkeypatch, trace, log):
    from api_agent.runtime.local_tracing import LocalTracing
    from api_agent.telemetry.privacy import EmissionBoundary
    forbidden = Mock(side_effect=AssertionError('Disabled dashboard allocated'))
    monkeypatch.setattr('api_agent.tooling.admission.capture_tool_factory', forbidden)
    dashboard = LocalTracing(selected('', TRACELOOP_TRACE_CONTENT=trace, LOG_CONTENT=log),
                             emissions=EmissionBoundary(), probe=forbidden, owner_root=profile_context.owner_root)
    assert await dashboard.start() is dashboard
    assert dashboard.viewer_url is None and dashboard.state == 'disabled'
    await dashboard.aclose()
    forbidden.assert_not_called()


def test_protected_writer_preserves_signal_headers_and_resource_across_worker_environment(profile_context, tmp_path, monkeypatch):
    import json
    from api_agent.testing.provider_setup import write_launch_snapshot
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.telemetry.tracing_providers import admit_binding
    token = 'synthetic-protected-trace-header'
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', 'https://remote.example/v1/traces')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_HEADERS', 'x-trace-token=' + token)
    monkeypatch.setenv('OTEL_RESOURCE_ATTRIBUTES', 'deployment.environment.name=synthetic-capture')
    config = selected(TRACE_EXPORT_HEADERS='')
    (tmp_path / 'protected').mkdir(mode=0o700)
    path = write_launch_snapshot(tmp_path / 'protected', profile_context, config.values, config_fields=config.fields)
    snapshot = json.loads(path.read_text())
    assert token in snapshot['trace_context']['environment']['OTEL_EXPORTER_OTLP_TRACES_HEADERS']
    assert token not in json.dumps(snapshot['trace_collector_plan'])
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_ENDPOINT', 'https://changed.example/v1/traces')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_TRACES_HEADERS', 'x-trace-token=changed-worker')
    monkeypatch.setenv('OTEL_RESOURCE_ATTRIBUTES', 'deployment.environment.name=changed-worker')
    options = restore_trace_context(snapshot)
    binding = admit_binding(snapshot['config'], owner_root=profile_context.owner_root,
                            boundary=EmissionBoundary(), **options)
    snapshots.verify_collector_binding(snapshot, binding)
    assert binding.values['TRACE_EXPORT_ENDPOINT'] == 'https://remote.example/v1/traces'
    assert binding._environment['OTEL_EXPORTER_OTLP_TRACES_HEADERS'] == 'x-trace-token=' + token
    assert snapshots.collector_from_snapshot(snapshot, snapshot['config']) is None


@pytest.mark.parametrize('context', [
    {}, {'version': True, 'environment': {}, 'explicit_fields': []},
    {'version': 1, 'environment': {'UNRELATED_SECRET':'value'}, 'explicit_fields': []},
    {'version': 1, 'environment': {'OTEL_SERVICE_NAME':3}, 'explicit_fields': []},
    {'version': 1, 'environment': {'OTEL_SERVICE_NAME':'bad\x00value'}, 'explicit_fields': []},
    {'version': 1, 'environment': {}, 'explicit_fields': ['lowercase']},
    {'version': 1, 'environment': {}, 'explicit_fields': ['TRACE_MODE','TRACE_MODE']},
    {'version': 1, 'environment': {}, 'explicit_fields': ['TRACE_MODE'] * 1025},
])
def test_invalid_protected_resolution_context_rejects_before_manager(profile_context, monkeypatch, context):
    config = selected()
    selection = effective_selection(config, owner_root=profile_context.owner_root)
    packet = snapshots.capture_collector_plan(profile_context, config)
    constructor = Mock()
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager', constructor)
    with pytest.raises(ValueError, match='tracing resolution context'):
        snapshots.collector_from_snapshot({'owner_root':str(profile_context.owner_root),
            'trace_collector_plan':packet, 'trace_context':context}, selection.values)
    constructor.assert_not_called()


def test_remote_destination_identity_is_required_even_without_infrastructure(profile_context, monkeypatch):
    config = selected(TRACE_EXPORT_ENDPOINT='https://remote.example/v1/traces')
    canonical = effective_selection(config, owner_root=profile_context.owner_root)
    packet = snapshots.capture_collector_plan(profile_context, config)
    assert not packet['base_assets']
    with pytest.raises(ValueError, match='profile or destinations'):
        snapshots.collector_from_snapshot({'owner_root': str(profile_context.owner_root), 'trace_collector_plan': packet, 'trace_context':capture_trace_context(canonical)},
            {**canonical.values, 'TRACE_EXPORT_ENDPOINT':'https://other.example/v1/traces'})


def test_missing_active_worker_context_cannot_fall_back_to_host_before_manager(profile_context, monkeypatch):
    config = selected()
    selection = effective_selection(config, owner_root=profile_context.owner_root)
    packet = snapshots.capture_collector_plan(profile_context, config)
    constructor = Mock()
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager', constructor)
    with pytest.raises(ValueError, match='requires its captured resolution context'):
        snapshots.collector_from_snapshot({'owner_root': str(profile_context.owner_root),
                                          'trace_collector_plan': packet}, selection.values)
    constructor.assert_not_called()
    # Configuration outside a worker remains allowed to use its ordinary inputs.
    assert restore_trace_context({}) == {}
    # An explicitly empty selector remains disabled without worker tracing inputs.
    assert snapshots.collector_from_snapshot({}, {'TRACE_PROVIDER':'', 'TRACE_MODE':'OTEL'}) is None


def test_captured_empty_environment_is_authoritative():
    assert restore_trace_context({'trace_context': {'version':1,'environment':{},'explicit_fields':[]}}, required=True) == {
        'environ':{},'explicit_fields':frozenset()}


def test_context_serialized_budget_rejects_individually_valid_environment_values():
    from api_agent.telemetry.tracing_selection import OTEL_KEYS
    context = {'version':1, 'environment':{key:'x' * 16384 for key in sorted(OTEL_KEYS)[:8]}, 'explicit_fields':[]}
    with pytest.raises(ValueError, match='exceeds its budget'):
        restore_trace_context({'trace_context':context}, required=True)


def test_controlled_collector_cannot_smuggle_custom_selected_services(profile_context):
    config = selected()
    selection = effective_selection(config, owner_root=profile_context.owner_root)
    packet = snapshots.capture_collector_plan(profile_context, config)
    packet['selected'] = ['synthetic/unrelated']
    with pytest.raises(ValueError, match='contains custom infrastructure'):
        snapshots.collector_from_snapshot({'owner_root':str(profile_context.owner_root), 'trace_collector_plan':packet,
                                          'trace_context':capture_trace_context(selection)}, selection.values)


def test_invalid_requested_provider_is_left_to_optional_tracing_owner():
    assert tracing_requested({'TRACE_PROVIDER':'missing'})


@pytest.mark.asyncio
async def test_pre_admitted_dashboard_binding_keeps_its_canonical_values():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.runtime.local_tracing import LocalTracing
    from api_agent.telemetry.privacy import EmissionBoundary
    binding = SimpleNamespace(values={'CANONICAL':'selected'}, declaration=SimpleNamespace(dashboard=None), aclose=AsyncMock())
    dashboard = LocalTracing({}, emissions=EmissionBoundary(), binding=binding)
    await dashboard.start()
    assert dashboard.config is binding.values
    assert dashboard.state == 'unavailable'
    await dashboard.aclose()
    binding.aclose.assert_awaited_once()


def test_declared_managed_destinations_cannot_redefine_controlled_local_resources(profile_context, monkeypatch):
    from dataclasses import replace
    from api_agent.telemetry.tracing_providers import read_declaration
    declaration = read_declaration(Path(PROVIDER.split(':')[0]).parent)
    profiles = tuple(replace(row, managed_endpoints=(
        ('trace_endpoint','https://remote.example/v1/traces'),
        ('dashboard_url','https://dashboard.example'),
    )) if row.profile == 'OTEL' else row for row in declaration.profiles)
    monkeypatch.setattr('api_agent.telemetry.tracing_selection.read_declaration', lambda *a:replace(declaration, profiles=profiles))
    constructor = Mock()
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager', constructor)
    with pytest.raises(ValueError, match='controlled local resource endpoints'):
        custom_trace_prerequisites(profile_context, selected(
            TRACE_EXPORT_ENDPOINT='https://remote.example/v1/traces',TRACE_VIEWER_URL='https://dashboard.example'))
    constructor.assert_not_called()


def test_captured_descriptor_cannot_redefine_controlled_local_endpoints(profile_context, monkeypatch):
    from hashlib import sha256
    from api_agent.runtime.infrastructure_contract import CapturedAsset
    from api_agent.telemetry.tracing_providers import read_declaration, parse_declaration
    original = read_declaration(Path(PROVIDER.split(':')[0]).parent).source
    body = original.body.replace(b'http://localhost:4318/v1/traces',b'https://remote.example/v1/traces').replace(
        b'http://localhost:16686',b'https://dashboard.example:16686')
    descriptor = parse_declaration(CapturedAsset(original.relative_path,body,sha256(body).hexdigest()))
    monkeypatch.setattr('api_agent.telemetry.tracing_selection.read_declaration',lambda *a:descriptor)
    with pytest.raises(ValueError, match='controlled local resource endpoints'):
        snapshots.capture_collector_plan(profile_context, selected(
            TRACE_EXPORT_ENDPOINT='https://remote.example/v1/traces',TRACE_VIEWER_URL='https://dashboard.example:16686'))


def test_captured_controlled_ports_must_match_ingestion_and_dashboard_contract(profile_context):
    import base64
    import json
    from hashlib import sha256
    config = selected('LangFuse')
    selection = effective_selection(config,owner_root=profile_context.owner_root)
    packet = snapshots.capture_collector_plan(profile_context,config)
    row = next(row for row in packet['base_assets'] if row['path']=='compose.json')
    document = json.loads(base64.b64decode(row['body']))
    document['services']['langfuse-web']['ports']=['127.0.0.1:3001:3000']
    body = json.dumps(document).encode()
    row.update(body=base64.b64encode(body).decode(),sha256=sha256(body).hexdigest())
    with pytest.raises(ValueError,match='published ports differ'):
        snapshots.collector_from_snapshot({'owner_root':str(profile_context.owner_root),'trace_collector_plan':packet,
            'trace_context':capture_trace_context(selection)},selection.values)


def test_protected_explicit_fields_must_belong_to_matching_configuration(profile_context,monkeypatch):
    config = selected()
    selection = effective_selection(config,owner_root=profile_context.owner_root)
    packet = snapshots.capture_collector_plan(profile_context,config)
    context = capture_trace_context(selection)
    context['explicit_fields'].append('ABSENT_CONFIG_FIELD')
    constructor = Mock()
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',constructor)
    with pytest.raises(ValueError,match='absent from matching configuration'):
        snapshots.collector_from_snapshot({'owner_root':str(profile_context.owner_root),'trace_collector_plan':packet,
            'trace_context':context},selection.values)
    constructor.assert_not_called()


def test_capture_rejects_explicit_fields_absent_from_its_configuration(profile_context):
    from api_agent.config.resolve import ResolvedValue
    config = selected()
    config = ConfigResult(config.values,{'ABSENT_CONFIG_FIELD':ResolvedValue('value','environment',True)},(),(),())
    with pytest.raises(ValueError,match='absent from matching configuration'):
        snapshots.capture_collector_plan(profile_context,config)
