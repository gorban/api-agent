"""Effective tracing capabilities and destinations are selected before allocation."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest

from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.telemetry.tracing_providers import (TracingProviderError, read_declaration,
    parse_declaration, select_declaration, effective_selection, admit_binding)
from api_agent.telemetry.genai_metrics import MetricConfigurationError
from api_agent.telemetry.privacy import EmissionBoundary

ROOT = Path(__file__).resolve().parents[2] / 'src/api_agent/telemetry/trace_adapters'


def config(mode='OTEL', *, explicit=(), **overrides):
    values = {'TRACE_PROVIDER': str(ROOT/'provider.py')+':create_tracing_provider',
              'TRACE_MODE': mode, 'TRACE_EXPORT_ENDPOINT': 'http://localhost:4318/v1/traces',
              'TRACE_VIEWER_URL': '', 'JAEGER_VIEWER_PORT': 16686, **overrides}
    fields = {key: ResolvedValue(value, 'cli' if key in explicit else 'default', key in explicit)
              for key, value in values.items()}
    return ConfigResult(values, fields, (), (), ())


def test_profiles_keep_original_immutable_source_and_select_disabled_without_capture(monkeypatch):
    declaration = read_declaration(ROOT)
    assert select_declaration(declaration, {'TRACE_MODE': ''}) is None
    with pytest.raises(TracingProviderError,match='TRACE_MODE.*unavailable'):
        select_declaration(declaration, {})
    otel = select_declaration(declaration, {'TRACE_MODE': 'OTEL'})
    langfuse = select_declaration(declaration, {'TRACE_MODE': 'LangFuse'})
    assert otel.source is declaration.source is langfuse.source
    assert otel.profile == 'OTEL' and otel.signals == {'traces', 'metrics'}
    assert langfuse.profile == 'LangFuse' and langfuse.signals == {'traces'}
    monkeypatch.setattr('api_agent.tooling.admission.capture_tool_factory', Mock(side_effect=AssertionError('allocation')))
    assert admit_binding(config(''), owner_root=ROOT, boundary=EmissionBoundary()) is None
    with pytest.raises(TracingProviderError, match='Unsupported'):
        select_declaration(declaration, {'TRACE_MODE': 'unknown'})


@pytest.mark.parametrize('body', [
    'version=2\nid="sample"\n',
    'version=2\nid="sample"\nselector="MODE"\ndisabled_value=""\nprofiles=[]',
    'version=2\nid="sample"\nselector="bad"\ndisabled_value=""\nprofiles=[{value="on",signals=["traces"]}]',
    'version=2\nid="sample"\nselector="MODE"\ndisabled_value=""\nprofiles=[{value="on",signals=["traces"]},{value="on",signals=["traces"]}]',
    'version=2\nid="sample"\nselector="MODE"\ndisabled_value="off"\nprofiles=[{value="off",signals=["traces"]}]',
    'version=2\nid="sample"\nselector="MODE"\ndisabled_value=""\nprofiles=[{value="on",id="other",signals=["traces"]}]',
    'version=2\nid="sample"\nselector="MODE"\ndisabled_value=""\nprofiles=[{value="on",signals=["traces"],managed_compose_profile="invalid profile"}]',
    'version=2\nid="sample"\nselector="MODE"\ndisabled_value=""\nprofiles=[{value="on",signals=["traces"],managed_compose_profile="local"}]',
])
def test_malformed_profile_tables_reject_before_factory(tmp_path, body):
    (tmp_path/'tracing-provider.toml').write_text(body)
    with pytest.raises(TracingProviderError):
        read_declaration(tmp_path)


@pytest.mark.parametrize('trace,viewer,explicit,managed,dashboard', [
    ('http://localhost:4318/v1/traces', '', (), 'otel', 'http://localhost:16686'),
    ('http://127.0.0.1:4318/v1/traces', '', (), 'otel', 'http://localhost:16686'),
    ('https://collector.example.test/v1/traces', '', (), None, None),
    ('https://collector.example.test/v1/traces', 'https://viewer.example.test', ('TRACE_VIEWER_URL',), None, 'https://viewer.example.test'),
    ('http://localhost:4318/v1/traces', '', ('TRACE_VIEWER_URL',), None, None),
    ('http://localhost:4318/v1/traces', 'https://viewer.example.test', ('TRACE_VIEWER_URL',), None, 'https://viewer.example.test'),
])
def test_effective_destinations_control_both_dashboard_and_local_ownership(trace, viewer, explicit, managed, dashboard):
    selected = effective_selection(config(explicit=explicit, TRACE_EXPORT_ENDPOINT=trace, TRACE_VIEWER_URL=viewer), environ={})
    assert selected.values['TRACE_EXPORT_ENDPOINT'] == trace
    assert selected.declaration.managed_compose_profile == managed
    assert (selected.values['TRACE_VIEWER_URL'] if selected.declaration.dashboard else None) == dashboard


def test_configured_dashboard_port_changes_both_viewer_and_ownership():
    selected = effective_selection(config(JAEGER_VIEWER_PORT=17686), environ={})
    assert selected.values['TRACE_VIEWER_URL'] == 'http://localhost:17686'
    assert selected.declaration.managed_compose_profile == 'otel'
    with pytest.raises(TracingProviderError, match='port'):
        effective_selection(config(JAEGER_VIEWER_PORT=True), environ={})


@pytest.mark.parametrize('explicit,expected', [((), 'https://environment.example.test/traces'), (('TRACE_EXPORT_ENDPOINT',), 'http://localhost:4318/v1/traces')])
def test_explicit_configuration_precedes_signal_environment_then_defaults(explicit, expected):
    env = {'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': 'https://environment.example.test/traces', 'UNRELATED_SECRET': 'never capture'}
    selected = effective_selection(config(explicit=explicit), environ=env)
    env['OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'] = 'https://changed.example.test/traces'
    assert selected.values['TRACE_EXPORT_ENDPOINT'] == expected
    assert selected.environment['OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'] == 'https://environment.example.test/traces'
    assert 'UNRELATED_SECRET' not in selected.environment
    with pytest.raises(TypeError):
        selected.values['TRACE_EXPORT_ENDPOINT'] = 'changed'


def test_explicit_empty_trace_endpoint_never_falls_back_to_environment_or_local():
    with pytest.raises(MetricConfigurationError, match='nonempty explicit endpoint'):
        effective_selection(config(explicit=('TRACE_EXPORT_ENDPOINT',), TRACE_EXPORT_ENDPOINT=''),
            environ={'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': 'https://environment.example.test/traces'})


def test_bundled_metric_opt_in_requires_separate_signal_destination():
    values = config(TRACE_METRICS=True)
    with pytest.raises(MetricConfigurationError, match='explicit supported'):
        effective_selection(values, environ={'OTEL_EXPORTER_OTLP_ENDPOINT': 'https://generic.example.test'})
    selected = effective_selection(values, environ={'OTEL_EXPORTER_OTLP_METRICS_ENDPOINT': 'https://metrics.example.test/v1/metrics'})
    assert selected.values['TRACE_METRICS_ENDPOINT'] == 'https://metrics.example.test/v1/metrics'
    with pytest.raises(TracingProviderError, match='does not support metrics'):
        effective_selection(config('LangFuse', TRACE_METRICS=True), environ={})


def test_captured_profiles_restore_same_identity_after_live_descriptor_replacement(tmp_path):
    original = read_declaration(ROOT)
    selected = effective_selection(config(), environ={}, declaration=original)
    restored = effective_selection(config(), environ={}, declaration=parse_declaration(original.source))
    remote = effective_selection(config(TRACE_EXPORT_ENDPOINT='https://remote.example.test/v1/traces'), environ={}, declaration=original)
    assert restored.identity == selected.identity
    assert remote.identity != selected.identity
    assert restored.declaration.source.sha256 == original.source.sha256


def test_custom_static_dashboard_selection_does_not_require_unused_export_configuration(tmp_path):
    (tmp_path/'tracing-provider.toml').write_text('version=1\nid="sample"\nsignals=["traces"]\n[dashboard]\nurl_field="DASHBOARD"\n')
    (tmp_path/'provider.py').write_text('raise AssertionError("must stay lazy")')
    values = {'TRACE_PROVIDER': str(tmp_path/'provider.py')+':create_tracing_provider', 'DASHBOARD': 'https://dashboard.example.test'}
    selected = effective_selection(values, environ={}, require_export=False)
    assert selected.declaration.signals == {'traces'}
    assert selected.declaration.dashboard.url_field == 'DASHBOARD'
    with pytest.raises(MetricConfigurationError):
        effective_selection(values, environ={})


@pytest.mark.parametrize('path', [1, 'relative', '//remote.test', '/ready?token=x', '/a/../b', '/ready\n', '/ready\\x'])
def test_dashboard_readiness_paths_cannot_change_origin_or_carry_queries(tmp_path, path):
    import json
    text = 'version=1\nid="sample"\nsignals=[]\n[dashboard]\nurl_field="DASHBOARD"\nreadiness_path=' + json.dumps(path)
    (tmp_path/'tracing-provider.toml').write_text(text)
    with pytest.raises(TracingProviderError):
        read_declaration(tmp_path)


@pytest.mark.parametrize('managed', [
    '1', 'true', '{trace_endpoint="http://local/traces"}',
    '{trace_endpoint="http://local/traces",dashboard_url="http://local",extra="x"}',
    '{trace_endpoint="http://local/traces",dashboard_url=1}',
    '{trace_endpoint="file:///tmp/traces",dashboard_url="http://local"}',
    '{trace_endpoint="http://local/traces",dashboard_url="http://local",trace_endpoint_aliases="x"}',
    '{trace_endpoint="http://local/traces",dashboard_url="http://local",trace_endpoint_aliases=["http://a","http://a"]}',
    '{trace_endpoint="http://local/traces",dashboard_url="http://local",dashboard_port_field="bad field"}',
])
def test_invalid_managed_destination_contracts_reject_before_any_planning(tmp_path, managed):
    (tmp_path/'tracing-provider.toml').write_text('version=2\nid="sample"\nselector="MODE"\ndisabled_value=""\n[[profiles]]\nvalue="on"\nsignals=["traces"]\nmanaged_endpoints='+managed+'\n[profiles.dashboard]\nurl_field="DASHBOARD"\n')
    with pytest.raises(TracingProviderError):
        read_declaration(tmp_path)


def test_managed_destinations_require_both_dashboard_and_local_authority(tmp_path):
    body = 'version=2\nid="sample"\nselector="MODE"\ndisabled_value=""\n[[profiles]]\nvalue="on"\nsignals=["traces"]\nmanaged_endpoints={trace_endpoint="http://local/traces",dashboard_url="http://local"}\n'
    (tmp_path/'tracing-provider.toml').write_text(body)
    with pytest.raises(TracingProviderError, match='require infrastructure and dashboard'):
        read_declaration(tmp_path)


def test_captured_selection_does_not_read_live_provider_and_accepts_relative_owner_path(tmp_path):
    declaration = read_declaration(ROOT)
    relative = config(TRACE_PROVIDER='removed/provider.py:create_tracing_provider')
    selected = effective_selection(relative, owner_root=tmp_path, declaration=declaration, environ={})
    assert selected.path == tmp_path/'removed/provider.py'
    with pytest.raises(TracingProviderError, match='captured tracing provider location'):
        effective_selection(config(TRACE_PROVIDER='invalid'), declaration=declaration, environ={})


@pytest.mark.parametrize('body', [b'not = [valid', b'\xff'])
def test_malformed_captured_descriptor_bytes_fail_closed(body):
    source = read_declaration(ROOT).source
    with pytest.raises(TracingProviderError, match='cannot be admitted'):
        parse_declaration(replace(source, body=body))


def test_direct_binding_enforces_signal_capabilities_even_without_selection():
    from api_agent.telemetry.tracing_providers import TraceProviderBinding
    descriptor = select_declaration(read_declaration(ROOT), {'TRACE_MODE': 'LangFuse'})
    with pytest.raises(TracingProviderError, match='does not support metrics'):
        TraceProviderBinding(descriptor, SimpleNamespace(identity='synthetic'), {'TRACE_METRICS':True}, EmissionBoundary(), ROOT)


def test_bundled_exporter_uses_complete_url_and_owns_both_signals(monkeypatch):
    from api_agent.telemetry.trace_adapters.provider import create_tracing_provider
    from api_agent.telemetry.genai_metrics import export_plan
    from test_genai_metrics import declaration
    trace, tracer, meter_provider, meter = object(), object(), object(), object()
    calls, owned = [], []
    monkeypatch.setattr('api_agent.telemetry.tracing._sdk_factory', lambda endpoint, headers, resource, *, own, environ=None: (calls.append((endpoint,headers,resource)) or trace, tracer))
    monkeypatch.setattr('api_agent.telemetry.genai_metrics.sdk_meter', lambda plan, *, own: (meter_provider,meter))
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'https://traces.example.test/full/path','TRACE_METRICS':True,'TRACE_METRICS_ENDPOINT':'https://metrics.example.test/full/path'},declaration(),{},environ={})
    adapter = create_tracing_provider({'TRACE_MODE':'OTEL'}, {'emissions':EmissionBoundary(),'own_exporter':owned.append})
    handles = adapter.create_exporter(plan)
    assert handles.provider is trace and handles.tracer is tracer
    assert handles.meter_provider is meter_provider and handles.meter is meter
    assert calls == [('https://traces.example.test/full/path',{}, {})]
    assert owned == [meter_provider]


def test_bundled_langfuse_auth_is_owned_and_factory_injection_keeps_full_url():
    from api_agent.telemetry.trace_adapters.provider import create_tracing_provider
    from api_agent.telemetry.genai_metrics import export_plan
    from test_genai_metrics import declaration
    boundary = EmissionBoundary()
    factory = Mock(return_value=(object(),object()))
    services = {'emissions':boundary,'own_exporter':Mock(),'sdk_factory':factory}
    plan = export_plan({'TRACE_EXPORT_ENDPOINT':'https://trace.example.test/api/public/otel/v1/traces'},declaration(),{},environ={})
    with pytest.raises(ValueError, match='API keys'):
        create_tracing_provider({'TRACE_MODE':'LangFuse'},services).create_exporter(plan)
    adapter = create_tracing_provider({'TRACE_MODE':'LangFuse','LANGFUSE_PUBLIC_KEY':'synthetic-public','LANGFUSE_SECRET_KEY':'synthetic-secret'},services)
    adapter.create_exporter(plan)
    endpoint, headers, resource = factory.call_args.args
    assert endpoint == plan.trace_endpoint
    assert headers['Authorization'].startswith('Basic ')
    assert boundary.content_export({'value':headers['Authorization']})['value'] == '[REDACTED]'


def dashboard_page():
    node = SimpleNamespace(fill=AsyncMock(),click=AsyncMock(),is_visible=AsyncMock(return_value=True))
    project = SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock()))
    page = SimpleNamespace(url='https://dashboard.example.test',goto=AsyncMock(),wait_for_url=AsyncMock(),
        get_by_label=Mock(return_value=node),get_by_role=Mock(side_effect=lambda role, **kw:project if role=='link' else node))
    return page,node


@pytest.mark.asyncio
@pytest.mark.parametrize('continue_visible', [False, True])
async def test_bundled_dashboard_login_uses_human_credentials_and_verifies_session(continue_visible):
    from api_agent.telemetry.trace_adapters.provider import create_tracing_provider
    adapter = create_tracing_provider({}, {'emissions':EmissionBoundary(),'own_exporter':Mock()})
    page,node = dashboard_page()
    node.is_visible.return_value = continue_visible
    await adapter.prepare_dashboard(page,url=page.url,username='viewer@example.test',password='synthetic-password')
    page.goto.assert_awaited_once_with('https://dashboard.example.test/auth/sign-in',wait_until='domcontentloaded')
    assert node.fill.await_args_list[0].args == ('viewer@example.test',)
    assert node.fill.await_args_list[1].args == ('synthetic-password',)
    predicate = page.wait_for_url.await_args.args[0]
    assert predicate('https://dashboard.example.test/project')
    assert not predicate('https://other.example.test/project')
    assert not predicate('https://dashboard.example.test/auth/sign-in')
    response = SimpleNamespace(status=200,json=AsyncMock(return_value={'user':{'email':'viewer@example.test'}}))
    page.request = SimpleNamespace(get=AsyncMock(return_value=response))
    assert await adapter.dashboard_identity(page,url=page.url) == 'viewer@example.test'
    response.status=403
    assert await adapter.dashboard_identity(page,url=page.url) is None
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('after_continue',[False,True])
async def test_bundled_dashboard_checks_origin_before_submitting_each_credential(after_continue):
    from api_agent.telemetry.trace_adapters.provider import create_tracing_provider
    from api_agent.runtime.local_tracing import DashboardError
    adapter=create_tracing_provider({}, {'emissions':EmissionBoundary(),'own_exporter':Mock()})
    page,node=dashboard_page()
    async def leave_origin(*args,**kwargs):
        page.url='https://other.example.test'
    if after_continue:
        node.click.side_effect=leave_origin
    else:
        page.goto.side_effect=leave_origin
    with pytest.raises(DashboardError,match='admitted origin'):
        await adapter.prepare_dashboard(page,url='https://dashboard.example.test',username='viewer@example.test',password='synthetic-password')
    assert node.fill.await_count == (1 if after_continue else 0)


def test_identity_uses_effective_plan_and_preserves_original_explicit_sources():
    endpoint='https://collector.example.test/v1/traces'
    by_environment=effective_selection(config(),environ={'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT':endpoint})
    by_config=effective_selection(config(TRACE_EXPORT_ENDPOINT=endpoint,explicit=('TRACE_EXPORT_ENDPOINT',)),environ={})
    assert by_environment.identity == by_config.identity
    assert 'TRACE_EXPORT_ENDPOINT' not in by_environment.explicit_fields
    assert 'TRACE_EXPORT_ENDPOINT' in by_config.explicit_fields
    overridden=effective_selection(config(TRACE_EXPORT_ENDPOINT=endpoint,explicit=('TRACE_EXPORT_ENDPOINT',)),environ={},explicit_fields=())
    assert overridden.explicit_fields == frozenset()


def test_live_sdk_disable_policy_fails_closed_without_allocating_captured_exporters(monkeypatch):
    from api_agent.telemetry.tracing import _sdk_factory,TraceError
    from api_agent.telemetry.genai_metrics import sdk_meter
    session=Mock()
    monkeypatch.setattr('requests.Session',session)
    monkeypatch.setenv('OTEL_SDK_DISABLED','true')
    with pytest.raises(TraceError,match='Ambient SDK disable policy'):
        _sdk_factory('https://collector.example.test/v1/traces',{}, {},environ={})
    with pytest.raises(MetricConfigurationError,match='Ambient SDK disable policy'):
        sdk_meter(SimpleNamespace())
    session.assert_not_called()


@pytest.mark.parametrize('signal',['traces','metrics'])
def test_owned_http_transport_never_reads_ambient_destination_or_header_fallback(monkeypatch,signal):
    from api_agent.telemetry.tracing import _sdk_factory
    from api_agent.telemetry.genai_metrics import sdk_meter,export_plan
    from test_genai_metrics import declaration
    response=SimpleNamespace(status_code=200,close=Mock())
    session=SimpleNamespace(post=Mock(return_value=response),close=Mock(),trust_env=True)
    monkeypatch.setattr('requests.Session',lambda:session)
    endpoint='https://admitted.example.test/custom/'+signal
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT','https://ambient.example.test')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_HEADERS','X-Ambient=must-never-export')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_'+signal.upper()+'_HEADERS','X-Signal=must-never-export')
    if signal=='traces':
        owner,tracer=_sdk_factory(endpoint,{}, {},environ={})
        tracer.start_span('synthetic').end()
    else:
        plan=export_plan({'TRACE_EXPORT_ENDPOINT':'https://admitted.example.test/traces','TRACE_METRICS':True,'TRACE_METRICS_ENDPOINT':endpoint},declaration(),{},environ={})
        owner,meter=sdk_meter(plan)
        meter.create_counter('synthetic.counter').add(1)
    assert owner.force_flush()
    assert session.post.call_args.args == (endpoint,)
    assert session.post.call_args.kwargs['headers'] == {'Content-Type':'application/x-protobuf'}
    assert session.trust_env is False
    owner.shutdown()


@pytest.mark.asyncio
async def test_binding_cleanup_survives_worker_scheduler_failure():
    import asyncio
    selected=admit_binding(config(),owner_root=ROOT,boundary=EmissionBoundary(),environ={})
    async def scheduler_failure():
        raise RuntimeError('synthetic scheduler unavailable')
    selected._creation_task=asyncio.create_task(scheduler_failure())
    await selected.aclose()
    assert selected._closed
    assert selected._signals.disposal_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_kind',['exception','cancellation'])
@pytest.mark.parametrize('retry_shutdown',[False,True])
async def test_partial_startup_shares_one_acknowledged_signal_disposal(failure_kind,retry_shutdown):
    import asyncio
    from api_agent.telemetry.tracing import TraceRuntime,TraceError,TraceDeliveryError
    selected=admit_binding(config(),owner_root=ROOT,boundary=EmissionBoundary(),environ={})
    events=[]
    class PartialOwner:
        released=False
        attempts=0
        def force_flush(self,timeout_millis=5000):
            events.append('flush')
            return True
        def shutdown(self):
            self.attempts+=1
            events.append('shutdown')
            if retry_shutdown and self.attempts==1:
                raise RuntimeError('synthetic disposal pending')
            if self.released:
                raise AssertionError('acknowledged owner released twice')
            self.released=True
    partial=PartialOwner()
    error=asyncio.CancelledError() if failure_kind=='cancellation' else RuntimeError('synthetic startup failure')
    error.cleanup_owner=partial
    def create(plan):
        selected.signal_owner.add(partial)
        raise error
    selected.owner=SimpleNamespace(implementation=SimpleNamespace(create_exporter=create),aclose=AsyncMock())
    selected.load_adapter=AsyncMock(return_value=selected.owner.implementation)
    runtime=TraceRuntime(config(),binding=selected)
    expected=asyncio.CancelledError if failure_kind=='cancellation' else TraceError
    with pytest.raises(expected) as caught:
        await runtime.astart()
    if failure_kind=='cancellation':
        assert caught.value is error
    assert runtime._provider is selected.signal_owner
    if retry_shutdown:
        with pytest.raises(TraceError,match='shutdown failed'):
            await runtime.aclose()
        assert not selected.signal_owner.disposal_confirmed
        selected.owner.aclose.assert_not_awaited()
    with pytest.raises(TraceDeliveryError):
        await runtime.aclose()
    assert partial.released
    assert events == ['flush','shutdown']+(['shutdown'] if retry_shutdown else [])
    assert selected.signal_owner.disposal_confirmed and selected._closed
    selected.owner.aclose.assert_awaited_once()
    with pytest.raises(TraceDeliveryError):
        await runtime.aclose()
    assert partial.attempts == (2 if retry_shutdown else 1)


@pytest.fixture
def profiled_configuration(tmp_path):
    from api_agent.config import schema_from_dict
    from api_agent.providers.loading import bootstrap_schema
    from api_agent.config.resolve import resolve_config
    provider=tmp_path/'profiled'
    provider.mkdir()
    (provider/'cfg').mkdir()
    (provider/'provider.py').write_text('raise AssertionError("profile discovery must not import a factory")\n')
    (provider/'tracing-provider.toml').write_text('''version=2
id="profiled-example"
selector="EXPORT_MODE"
disabled_value=""
[[profiles]]
value="enabled"
signals=["traces"]
''')
    location=str(provider/'provider.py')+':create_tracing_provider'
    base=schema_from_dict({'version':1,'fields':{'TRACE_PROVIDER':{'type':'str','default':location}}})
    def resolve(contribution,environment):
        (provider/'cfg/config.toml').write_text('version=1\n'+contribution)
        schema=bootstrap_schema(base,(),environment,{},owner_root=provider)
        result=resolve_config(schema,environ=environment,dotenv={},interactive=True)
        assert not result.errors
        return result
    return provider,resolve


@pytest.mark.parametrize('contribution',[
    '',
    '[fields.ENABLE_EXPORT]\ntype="bool"\ndefault=false\n[fields.EXPORT_MODE]\ntype="str"\ndefault=""\napplies_when={ENABLE_EXPORT=true}\n',
],ids=['undeclared-selector','inactive-selector'])
def test_resolved_profile_requires_available_typed_selector_before_admission(profiled_configuration,contribution,monkeypatch):
    provider,resolve=profiled_configuration
    resolved=resolve(contribution,{'EXPORT_MODE':'enabled'})
    capture=Mock(side_effect=AssertionError('unavailable selector must not capture a factory'))
    monkeypatch.setattr('api_agent.tooling.admission.capture_tool_factory',capture)
    assert 'EXPORT_MODE' not in resolved.values

    with pytest.raises(TracingProviderError,match='EXPORT_MODE.*unavailable.*active configuration field'):
        admit_binding(resolved,owner_root=provider,boundary=EmissionBoundary(),environ={})

    capture.assert_not_called()


def test_resolved_declared_blank_profile_selector_remains_disabled_before_admission(profiled_configuration,monkeypatch):
    provider,resolve=profiled_configuration
    resolved=resolve('[fields.EXPORT_MODE]\ntype="str"\ndefault=""\n',{})
    capture=Mock(side_effect=AssertionError('disabled selector must not capture a factory'))
    monkeypatch.setattr('api_agent.tooling.admission.capture_tool_factory',capture)
    assert resolved.values['EXPORT_MODE']==''

    assert admit_binding(resolved,owner_root=provider,boundary=EmissionBoundary(),environ={}) is None

    capture.assert_not_called()


@pytest.fixture
def dashboard_provider(tmp_path):
    root=tmp_path/'dashboard'
    root.mkdir()
    (root/'provider.py').write_text('raise AssertionError("viewer must not import an unused factory")\ndef create_tracing_provider(config,services):\n    raise AssertionError("viewer must not call an unused factory")\n')
    def declare(version,*,signals=('traces','metrics'),managed=''):
        capabilities='signals='+repr(list(signals)).replace("'",'"')+'\n'+managed+'\n'
        if version==1:
            body='version=1\nid="dashboard-example"\n'+capabilities+'[dashboard]\nurl_field="DASHBOARD_URL"\n'
        else:
            body='version=2\nid="dashboard-example"\nselector="MODE"\ndisabled_value=""\n[[profiles]]\nvalue="enabled"\n'+capabilities+'[profiles.dashboard]\nurl_field="DASHBOARD_URL"\n'
        (root/'tracing-provider.toml').write_text(body)
        return {'TRACE_PROVIDER':str(root/'provider.py')+':create_tracing_provider','MODE':'enabled',
                'DASHBOARD_URL':'https://viewer.example.test',
                'TRACE_EXPORT_ENDPOINT':'https://collector.example.test/v1/traces'}
    return root,declare


@pytest.mark.asyncio
@pytest.mark.parametrize('version',[1,2])
@pytest.mark.parametrize('policy',[
    'missing-endpoint','empty-endpoint','invalid-endpoint','sdk-disabled','unsupported-metrics',
    'missing-metrics-endpoint','grpc','invalid-headers','invalid-resource','invalid-temporality',
])
async def test_dashboard_selection_ignores_unused_export_policy_but_export_admission_remains_strict(dashboard_provider,monkeypatch,version,policy):
    from api_agent.runtime.local_tracing import LocalTracing
    from api_agent.telemetry.tracing_selection import OTEL_KEYS
    root,declare=dashboard_provider
    values=declare(version,signals=('traces',) if policy=='unsupported-metrics' else ('traces','metrics'))
    for name in OTEL_KEYS:
        monkeypatch.delenv(name,raising=False)
    changes={
        'missing-endpoint':{}, 'empty-endpoint':{'TRACE_EXPORT_ENDPOINT':''},
        'invalid-endpoint':{'TRACE_EXPORT_ENDPOINT':'not-a-url'}, 'sdk-disabled':{},
        'unsupported-metrics':{'TRACE_METRICS':True}, 'missing-metrics-endpoint':{'TRACE_METRICS':True},
        'grpc':{'TRACE_EXPORT_PROTOCOL':'grpc'}, 'invalid-headers':{'TRACE_EXPORT_HEADERS':'invalid'},
        'invalid-resource':{'TRACE_RESOURCE_ATTRIBUTES':{'unsupported':'value'}},
        'invalid-temporality':{'TRACE_METRICS':True,'TRACE_METRICS_ENDPOINT':'https://metrics.example.test/v1/metrics',
                               'TRACE_METRICS_TEMPORALITY':'invalid'},
    }
    baseline=effective_selection(values,owner_root=root,environ={},require_export=False)
    values.update(changes[policy])
    if policy=='missing-endpoint':
        values.pop('TRACE_EXPORT_ENDPOINT')
    if policy=='sdk-disabled':
        monkeypatch.setenv('OTEL_SDK_DISABLED','true')
    selected=effective_selection(values,owner_root=root,require_export=False)
    assert selected.identity==baseline.identity
    probe=Mock(return_value=True)
    viewer=LocalTracing(values,emissions=EmissionBoundary(),probe=probe,owner_root=root)

    await viewer.start()

    assert viewer.state=='ready' and viewer.viewer_url=='https://viewer.example.test'
    assert viewer._binding.values.get('TRACE_METRICS')==values.get('TRACE_METRICS')
    assert viewer._binding.declaration.signals==selected.declaration.signals
    assert viewer._binding.pipeline_identity is None
    with pytest.raises(TracingProviderError,match='Dashboard-only'):
        await viewer._binding.open({})
    with pytest.raises((MetricConfigurationError,TracingProviderError)):
        admit_binding(values,owner_root=root,boundary=EmissionBoundary())
    await viewer.aclose()
    probe.assert_called_once_with('https://viewer.example.test',None)


@pytest.mark.asyncio
async def test_v2_viewer_resolves_without_exporter_endpoint_through_ordinary_bootstrap(dashboard_provider):
    from api_agent.config import load_shared_schema
    from api_agent.runtime.trace_viewer import resolve_viewer_config
    from api_agent.runtime.local_tracing import LocalTracing
    root,declare=dashboard_provider
    values=declare(2)
    (root/'cfg').mkdir()
    (root/'cfg/config.toml').write_text('version=1\n[fields.MODE]\ntype="str"\ndefault="enabled"\n[fields.DASHBOARD_URL]\ntype="str"\ndefault="https://viewer.example.test"\n')
    config=resolve_viewer_config(load_shared_schema(ROOT.parents[3]),argv=(),
        environ={'TRACE_PROVIDER':values['TRACE_PROVIDER']},dotenv={},anchors={'owner_root':root})
    viewer=LocalTracing(config,emissions=EmissionBoundary(),probe=Mock(return_value=True),owner_root=root)
    await viewer.start()
    assert viewer.state=='ready'
    with pytest.raises(MetricConfigurationError,match='explicit supported'):
        admit_binding(config,owner_root=root,boundary=EmissionBoundary(),environ={})
    await viewer.aclose()


@pytest.mark.asyncio
async def test_ipv6_managed_dashboard_port_remains_admissible_to_viewer(dashboard_provider):
    from api_agent.runtime.local_tracing import LocalTracing
    root,declare=dashboard_provider
    managed='infrastructure=["dashboard-example/viewer"]\nmanaged_endpoints={trace_endpoint="https://collector.example.test/v1/traces",dashboard_url="http://[::1]:16686",dashboard_port_field="VIEWER_PORT"}\n'
    values=declare(2,managed=managed)
    values.pop('DASHBOARD_URL')
    values['VIEWER_PORT']=17686
    selected=effective_selection(values,owner_root=root,environ={},require_export=False)
    assert selected.values['DASHBOARD_URL']=='http://[::1]:17686'
    assert selected.declaration.infrastructure==('dashboard-example/viewer',)
    probe=Mock(return_value=True)
    viewer=LocalTracing(values,emissions=EmissionBoundary(),probe=probe,owner_root=root)
    await viewer.start()
    assert viewer.state=='ready' and viewer.viewer_url=='http://[::1]:17686'
    probe.assert_called_once_with('http://[::1]:17686',None)
    await viewer.aclose()


@pytest.mark.asyncio
async def test_authenticated_viewer_adapter_needs_no_exporter_methods_or_unused_export_credentials(dashboard_provider):
    from api_agent.runtime.local_tracing import LocalTracing
    root,declare=dashboard_provider
    values=declare(2,signals=('traces',))
    descriptor=root/'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('[profiles.dashboard]', 'secret_fields=["EXPORT_TOKEN"]\n[profiles.dashboard]')+
        'username_field="DASHBOARD_USER"\npassword_field="DASHBOARD_PASSWORD"\nlogin="provider"\n')
    (root/'provider.py').write_text('''class Adapter:
    version=1
    async def prepare_dashboard(self,page,*,url,username,password): pass
    async def dashboard_identity(self,page,*,url): return None
    async def aclose(self): pass
def create_tracing_provider(config,services): return Adapter()
''')
    values.update(TRACE_EXPORT_ENDPOINT='',TRACE_METRICS=True,EXPORT_TOKEN=123,
                  DASHBOARD_USER='viewer@example.test',DASHBOARD_PASSWORD='synthetic-login')
    viewer=LocalTracing(values,emissions=EmissionBoundary(),probe=Mock(return_value=True),owner_root=root)

    await viewer.start()

    assert viewer.state=='ready'
    assert not hasattr(viewer._binding.owner.implementation,'create_exporter')
    assert viewer._binding.values['EXPORT_TOKEN']==123
    assert viewer._binding.values['TRACE_METRICS'] is True
    assert viewer._binding.signals=={'traces'}
    await viewer.aclose()


def test_viewer_local_classification_uses_only_valid_effective_endpoint():
    local=effective_selection(config(TRACE_EXPORT_PROTOCOL='grpc'),environ={},require_export=False)
    assert local.declaration.managed_compose_profile=='otel'
    assert local.values['TRACE_EXPORT_PROTOCOL']=='grpc'
    invalid=effective_selection(config(explicit=('TRACE_EXPORT_ENDPOINT','TRACE_VIEWER_URL'),
        TRACE_EXPORT_ENDPOINT='not-a-url',TRACE_VIEWER_URL='http://localhost:16686',JAEGER_VIEWER_PORT=None),environ={},require_export=False)
    assert invalid.declaration.managed_compose_profile is None
    assert invalid.declaration.dashboard is not None
    assert invalid.values['TRACE_EXPORT_ENDPOINT']=='not-a-url'


@pytest.mark.asyncio
@pytest.mark.parametrize('dashboard',['https://viewer.example.test','http://localhost:16686'])
@pytest.mark.parametrize('port',[None,True,0,65536,'invalid'])
async def test_explicit_dashboard_ignores_unused_invalid_managed_port_without_granting_local_ownership(dashboard,port):
    from api_agent.runtime.local_tracing import LocalTracing
    values=config(explicit=('TRACE_VIEWER_URL',),TRACE_VIEWER_URL=dashboard,JAEGER_VIEWER_PORT=port)
    selected=effective_selection(values,environ={},require_export=False)
    probe=Mock(return_value=True)
    viewer=LocalTracing(values,emissions=EmissionBoundary(),probe=probe,owner_root=ROOT)

    await viewer.start()

    assert viewer.state=='ready' and viewer.viewer_url==dashboard
    assert selected.values['TRACE_VIEWER_URL']==dashboard
    assert selected.values['JAEGER_VIEWER_PORT']==port
    assert type(selected.values['JAEGER_VIEWER_PORT']) is type(port)
    assert selected.declaration.managed_compose_profile is None
    assert selected.declaration.infrastructure==()
    assert viewer._binding.declaration.managed_compose_profile is None
    probe.assert_called_once_with(dashboard,None)
    with pytest.raises(TracingProviderError,match='valid configured port'):
        admit_binding(values,owner_root=ROOT,boundary=EmissionBoundary(),environ={})
    await viewer.aclose()


@pytest.mark.parametrize('explicit,dashboard',[((),''),((),'https://viewer.example.test'),(('TRACE_VIEWER_URL',),'')])
def test_invalid_local_port_requires_explicit_nonempty_dashboard_for_view_only(explicit,dashboard):
    values=config(explicit=explicit,TRACE_VIEWER_URL=dashboard,JAEGER_VIEWER_PORT=None)

    with pytest.raises(TracingProviderError,match='valid configured port'):
        effective_selection(values,environ={},require_export=False)


@pytest.mark.asyncio
async def test_invalid_unused_local_port_does_not_bypass_final_dashboard_url_admission():
    from api_agent.runtime.local_tracing import LocalTracing,DashboardError
    values=config(explicit=('TRACE_VIEWER_URL',),TRACE_VIEWER_URL='https://user:password@viewer.example.test',JAEGER_VIEWER_PORT=None)
    probe=Mock()
    viewer=LocalTracing(values,emissions=EmissionBoundary(),probe=probe,owner_root=ROOT)

    with pytest.raises(DashboardError,match='same-origin HTTP'):
        await viewer.start()

    probe.assert_not_called()
    assert viewer._binding.declaration.managed_compose_profile is None
    await viewer.aclose()
