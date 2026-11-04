"""Generic dashboard capabilities reuse the existing viewer and service leases."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.runtime.local_tracing import LocalTracing, DashboardError
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.telemetry.tracing_providers import TracingProviderError
from test_tracing_provider_extension import synthetic_provider, values, BASE


def configure_dashboard(root, *, login=False, infrastructure=''):
    (root / 'tracing-provider.toml').write_text('version=1\nid="synthetic"\nsignals=["traces"]\nsecret_fields=["EXPORT_TOKEN"]\n'+infrastructure+'''[dashboard]
url_field="DASHBOARD_URL"
readiness_field="DASHBOARD_READY_URL"
'''+('login="provider"\nusername_field="DASHBOARD_USERNAME"\npassword_field="DASHBOARD_PASSWORD"\n' if login else ''))
    cfg = root / 'cfg'
    cfg.mkdir(exist_ok=True)
    (cfg / 'config.toml').write_text('''version=1
[fields.DASHBOARD_URL]
type="str"
default="https://dashboard.example.test"
[fields.DASHBOARD_READY_URL]
type="str"
default="https://dashboard.example.test/ready"
[fields.DASHBOARD_USERNAME]
type="str"
default="viewer@example.test"
[fields.DASHBOARD_PASSWORD]
type="str"
sensitive=true
default="synthetic-human-password"
[fields.EXPORT_TOKEN]
type="str"
sensitive=true
default="synthetic-export-token"
''')
    return values(root,DASHBOARD_URL='https://dashboard.example.test',DASHBOARD_READY_URL='https://dashboard.example.test/ready',
                  DASHBOARD_USERNAME='viewer@example.test',DASHBOARD_PASSWORD='synthetic-human-password',EXPORT_TOKEN='synthetic-export-token')


class Page:
    def __init__(self): self.url='about:blank'; self.guard=None
    async def route(self, pattern, callback): self.guard=callback
    async def goto(self, url, **kwargs): self.url=url; return SimpleNamespace(status=200)


@pytest.mark.asyncio
async def test_export_only_provider_never_allocates_a_dashboard(synthetic_provider):
    probe=Mock()
    dashboard=LocalTracing(values(synthetic_provider),emissions=EmissionBoundary(),probe=probe,owner_root=synthetic_provider)
    await dashboard.start()
    assert dashboard.state=='unavailable' and dashboard.viewer_url is None
    assert dashboard.interactive_credentials() is None
    probe.assert_not_called()
    await dashboard.aclose()


@pytest.mark.asyncio
async def test_no_login_dashboard_does_not_import_factory_and_guards_origin(synthetic_provider):
    config=configure_dashboard(synthetic_provider)
    (synthetic_provider/'provider.py').write_text('def create_tracing_provider(config,services):\n    raise RuntimeError("must remain lazy")\n')
    dashboard=LocalTracing(config,emissions=EmissionBoundary(),probe=Mock(return_value=True),owner_root=synthetic_provider)
    await dashboard.start()
    page=Page()
    await dashboard.login(page)
    assert dashboard.state=='ready' and dashboard.authentication=='not_attempted'
    assert dashboard.interactive_credentials() is None
    route=SimpleNamespace(request=SimpleNamespace(url='https://unselected.example.test'),abort=AsyncMock(),continue_=AsyncMock())
    await page.guard(route)
    route.abort.assert_awaited_once()
    await dashboard.aclose()


@pytest.mark.asyncio
async def test_authenticated_dashboard_prepares_login_and_verifies_selected_identity(synthetic_provider):
    config=configure_dashboard(synthetic_provider,login=True)
    source=(synthetic_provider/'provider.py').read_text().replace('    async def aclose(self): pass','''    async def prepare_dashboard(self,page,*,url,username,password):
        page.identity=username
        page.login_password=password
    async def dashboard_identity(self,page,*,url):
        return page.identity
    async def aclose(self): pass''')
    (synthetic_provider/'provider.py').write_text(source)
    dashboard=LocalTracing(config,emissions=EmissionBoundary(),probe=Mock(return_value=True),owner_root=synthetic_provider)
    await dashboard.start()
    page=Page()
    await dashboard.login(page)
    assert dashboard.authentication=='authenticated'
    assert dashboard.interactive_credentials()==('viewer@example.test','synthetic-human-password')
    assert 'synthetic-export-token' not in str(dashboard.interactive_credentials())
    assert page.login_password=='synthetic-human-password'
    await dashboard.aclose()


@pytest.mark.asyncio
async def test_missing_declared_login_capability_rejects_before_probe(synthetic_provider):
    config=configure_dashboard(synthetic_provider,login=True)
    probe=Mock()
    dashboard=LocalTracing(config,emissions=EmissionBoundary(),probe=probe,owner_root=synthetic_provider)
    with pytest.raises(TracingProviderError,match='login capability'):
        await dashboard.start()
    probe.assert_not_called()
    await dashboard.aclose()


@pytest.mark.parametrize('config_change', [
    {'DASHBOARD_URL':'https://user:secret@dashboard.example.test'},
    {'DASHBOARD_READY_URL':'https://other.example.test/ready'},
    {'DASHBOARD_USERNAME':''}, {'DASHBOARD_PASSWORD':None},
])
@pytest.mark.asyncio
async def test_invalid_dashboard_binding_rejects_before_probe(synthetic_provider,config_change):
    config={**configure_dashboard(synthetic_provider,login=True),**config_change}
    probe=Mock()
    dashboard=LocalTracing(config,emissions=EmissionBoundary(),probe=probe,owner_root=synthetic_provider)
    with pytest.raises(DashboardError): await dashboard.start()
    probe.assert_not_called()
    await dashboard.aclose()


def test_custom_viewer_resolves_provider_fields_and_cli_precedence_without_import(synthetic_provider):
    from api_agent.config import load_shared_schema
    from api_agent.runtime.trace_viewer import resolve_viewer_config
    configure_dashboard(synthetic_provider)
    (synthetic_provider/'provider.py').write_text('def create_tracing_provider(config,services):\n    raise RuntimeError("must remain lazy")\n')
    config=resolve_viewer_config(load_shared_schema(BASE),argv=['--dashboard-url=https://selected.example.test', '--dashboard-ready-url=https://selected.example.test/ready'],
        environ={'TRACE_PROVIDER':values(synthetic_provider)['TRACE_PROVIDER']},dotenv={},anchors={'base_root':BASE,'pack_root':synthetic_provider,'owner_root':synthetic_provider})
    assert config['DASHBOARD_URL']=='https://selected.example.test'
    assert config['TRACE_PROVIDER']==values(synthetic_provider)['TRACE_PROVIDER']


def test_remote_dashboard_has_no_managed_infrastructure(synthetic_provider,tmp_path):
    from api_agent.runtime.tracing_services import custom_trace_prerequisites
    context=SimpleNamespace(owner_root=synthetic_provider)
    config=configure_dashboard(synthetic_provider)
    assert custom_trace_prerequisites(context,config) is None


def test_managed_trace_provider_plans_only_its_recursive_closure_with_shared_lease(synthetic_provider,tmp_path,monkeypatch):
    import json
    from threading import Event
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.runtime.tracing_services import custom_trace_prerequisites
    from test_pack_composition import pack
    config=configure_dashboard(synthetic_provider,infrastructure='infrastructure=["synthetic/collector"]\n')
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
readiness={kind="health"}
[[services]]
id="unselected"
compose="unselected.json"
readiness={kind="health"}
''')
    for name in ('collector','unselected'):
        (synthetic_provider/f'{name}.json').write_text('{"image":"example/service@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
    (synthetic_provider/'database.json').write_text('{"image":"example/database@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","volumes":[{"volume":"data","target":"/var/lib/data"}]}')
    root=pack(tmp_path,'application')
    context=load_launch_context(BASE,root)
    made=Mock()
    monkeypatch.setenv('DOCKER_HOST','unix:///tmp/synthetic-engine.sock')
    monkeypatch.delenv('CONTAINER_HOST',raising=False)
    monkeypatch.setattr('api_agent.runtime.prerequisites.PrerequisiteManager',made)
    cancelled=Event()
    manager=custom_trace_prerequisites(context,config,cancel_event=cancelled)
    assert manager is made.return_value
    plan,scope,directory=made.call_args.args
    assert [service.id for service in plan.conditional]==['synthetic/database','synthetic/collector']
    document=json.loads(plan.assets['compose.json'])
    assert document['volumes']
    assert 'langfuse' not in plan.profiles and 'otel' not in plan.profiles
    made.return_value.acquire.assert_not_called()
    assert directory==root/'artifacts/prerequisites'
    assert not made.call_args.kwargs['cancelled']()
    cancelled.set()
    assert made.call_args.kwargs['cancelled']()
    # The ordinary application planner must use the same declared closure.
    from api_agent.runtime.local_run import _plan
    from api_agent.config import load_shared_schema
    from api_agent.config.resolve import ConfigResult
    from api_agent.providers.loading import bootstrap_schema
    from api_agent.runtime.ownership import StackScope
    schema = bootstrap_schema(load_shared_schema(BASE), (), {}, {}, config, owner_root=context.owner_root)
    planned = _plan(ConfigResult(config, {}, (), (), ()), schema, context, scope,
                    engine_environment={'DOCKER_HOST': 'unix:///tmp/synthetic-engine.sock'})
    assert [item.id for item in planned.conditional] == ['synthetic/database', 'synthetic/collector']
    descriptor = synthetic_provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text().replace('infrastructure=["synthetic/collector"]', 'infrastructure=[]'))
    remote = _plan(ConfigResult(config, {}, (), (), ()), schema, context, scope,
                   engine_environment={'DOCKER_HOST': 'unix:///tmp/synthetic-engine.sock'})
    assert not remote.conditional


@pytest.mark.parametrize('dependency', ['synthetic/missing','synthetic/collector'])
def test_managed_tracing_missing_or_cyclic_dependency_rejects_before_lease(synthetic_provider,tmp_path,dependency):
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.runtime.tracing_services import selected_tracing_infrastructure
    from test_pack_composition import pack
    config=configure_dashboard(synthetic_provider,infrastructure='infrastructure=["synthetic/collector"]\n')
    (synthetic_provider/'infrastructure.toml').write_text(f'version=1\nowner="synthetic"\n[[services]]\nid="collector"\ncompose="collector.json"\ndependencies=["{dependency}"]\nreadiness={{kind="health"}}\n')
    (synthetic_provider/'collector.json').write_text('{"image":"example/service@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
    context=load_launch_context(BASE,pack(tmp_path,'application'))
    with pytest.raises(TracingProviderError,match='missing or cyclic'):
        selected_tracing_infrastructure(context,config)


def test_viewer_projection_follows_conditional_controllers(synthetic_provider):
    from dataclasses import replace
    from api_agent.config import load_shared_schema
    from api_agent.runtime.tracing_viewer_config import custom_viewer_config, _controllers
    assert _controllers({'any':[{'FIRST':True}, {'any':[{'SECOND':'selected'}]}]}) == {'FIRST','SECOND'}
    assert custom_viewer_config(SimpleNamespace(fields={}), argv=(), environ={}, dotenv={}, anchors={}) is None
    configure_dashboard(synthetic_provider)
    descriptor = synthetic_provider / 'tracing-provider.toml'
    descriptor.write_text(descriptor.read_text() + 'config_fields=["DASHBOARD_REQUIRED"]\n')
    cfg = synthetic_provider/'cfg/config.toml'
    cfg.write_text(cfg.read_text() + '\n[fields.DASHBOARD_REQUIRED]\ntype="str"\nrequired=true\n')
    kwargs = dict(argv=(), environ={'TRACE_PROVIDER':values(synthetic_provider)['TRACE_PROVIDER']}, dotenv={},
                  anchors={'base_root':BASE,'pack_root':synthetic_provider,'owner_root':synthetic_provider})
    with pytest.raises(ValueError, match='valid selected dashboard'):
        custom_viewer_config(load_shared_schema(BASE), **kwargs)
    cfg.write_text(cfg.read_text().replace('required=true', 'default="selected"'))
    kwargs['argv'] = ('unexpected-positional',)
    with pytest.raises(ValueError, match='positional'):
        custom_viewer_config(load_shared_schema(BASE), **kwargs)


def test_viewer_rejects_export_only_or_undeclared_dashboard_fields(synthetic_provider):
    from api_agent.config import load_shared_schema
    from api_agent.runtime.tracing_viewer_config import custom_viewer_config
    kwargs = dict(argv=(), environ={'TRACE_PROVIDER':values(synthetic_provider)['TRACE_PROVIDER']}, dotenv={},
                  anchors={'base_root':BASE,'pack_root':synthetic_provider,'owner_root':synthetic_provider})
    with pytest.raises(ValueError, match='no dashboard'):
        custom_viewer_config(load_shared_schema(BASE), **kwargs)
    configure_dashboard(synthetic_provider)
    path=synthetic_provider/'tracing-provider.toml'
    path.write_text(path.read_text().replace('DASHBOARD_URL','UNDECLARED_URL'))
    with pytest.raises(ValueError, match='undeclared'):
        custom_viewer_config(load_shared_schema(BASE), **kwargs)


def test_selected_infrastructure_deduplicates_shared_dependencies_and_checks_owner(synthetic_provider, tmp_path, monkeypatch):
    from dataclasses import dataclass
    from api_agent.runtime.tracing_services import selected_tracing_infrastructure
    @dataclass(frozen=True)
    class Context:
        owner_root: Path
        inventory_sources: tuple = ()
    context = Context(synthetic_provider)
    assert selected_tracing_infrastructure(context, {}) == ((), context)
    configure_dashboard(synthetic_provider, infrastructure='infrastructure=["synthetic/first","synthetic/second"]\n')
    shared = SimpleNamespace(id='synthetic/shared', dependencies=(), owner='synthetic', source=synthetic_provider/'infrastructure.toml')
    first = SimpleNamespace(id='synthetic/first', dependencies=('synthetic/shared',), owner='synthetic', source=shared.source)
    second = SimpleNamespace(id='synthetic/second', dependencies=('synthetic/shared',), owner='synthetic', source=shared.source)
    monkeypatch.setattr('api_agent.runtime.infrastructure_contract.load_infrastructure_contributions', lambda *a, **kw: (shared, first, second))
    result, admitted = selected_tracing_infrastructure(context, values(synthetic_provider))
    assert result == (shared, first, second) and len(admitted.inventory_sources) == 1
    first.owner = 'unselected'
    with pytest.raises(TracingProviderError, match='owner must match'):
        selected_tracing_infrastructure(context, values(synthetic_provider))
    for item in (first, second, shared): item.source = tmp_path/'pack-owned'/'infrastructure.toml'
    result, admitted = selected_tracing_infrastructure(context, values(synthetic_provider))
    assert result == (shared, first, second) and admitted is context


def test_viewer_rejects_unavailable_conditional_controller(synthetic_provider, monkeypatch):
    from dataclasses import replace
    from api_agent.config import load_shared_schema
    from api_agent.runtime.tracing_viewer_config import custom_viewer_config
    from api_agent.providers.loading import bootstrap_schema
    configure_dashboard(synthetic_provider)
    base = load_shared_schema(BASE)
    config = values(synthetic_provider)
    schema = bootstrap_schema(base, (), {}, {}, config, owner_root=synthetic_provider)
    fields = dict(schema.fields)
    fields['DASHBOARD_URL'] = replace(fields['DASHBOARD_URL'], applies_when={'UNDECLARED_CONTROLLER':True})
    monkeypatch.setattr('api_agent.providers.loading.bootstrap_schema', lambda *a, **kw: replace(schema, fields=fields))
    with pytest.raises(ValueError, match='unavailable configuration controller'):
        custom_viewer_config(base, argv=(), environ={'TRACE_PROVIDER':config['TRACE_PROVIDER']},dotenv={},
            anchors={'base_root':BASE,'pack_root':synthetic_provider,'owner_root':synthetic_provider})


def test_inactive_selected_service_rejects_before_lease(synthetic_provider, tmp_path, monkeypatch):
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.runtime.tracing_services import custom_trace_prerequisites
    from test_pack_composition import pack
    context = load_launch_context(BASE, pack(tmp_path, 'application'))
    selected = SimpleNamespace(id='synthetic/collector')
    monkeypatch.setattr('api_agent.runtime.tracing_services.selected_tracing_infrastructure', lambda *a, **kw: ((selected,),context))
    monkeypatch.setattr('api_agent.runtime.infrastructure_plan.plan_infrastructure', lambda *a, **kw: SimpleNamespace(conditional=()))
    with pytest.raises(TracingProviderError, match='activation configuration'):
        custom_trace_prerequisites(context, values(synthetic_provider))

@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['unready', 'failed', 'cancelled', 'closed'])
async def test_custom_dashboard_readiness_failure_and_shutdown_races(synthetic_provider, result):
    import asyncio
    config = configure_dashboard(synthetic_provider)
    dashboard = LocalTracing(config, emissions=EmissionBoundary(), owner_root=synthetic_provider)
    pending = asyncio.get_running_loop().create_future()
    dashboard._probe_task = pending
    start = asyncio.create_task(dashboard.start())
    await asyncio.sleep(0)
    if result == 'failed':
        pending.set_exception(RuntimeError('private server response'))
        with pytest.raises(DashboardError, match='readiness failed'): await start
    elif result == 'cancelled':
        start.cancel()
        with pytest.raises(asyncio.CancelledError): await start
        assert not pending.cancelled()
        pending.set_result(False)
    elif result == 'closed':
        close = asyncio.create_task(dashboard.aclose())
        await asyncio.sleep(0)
        pending.set_result(False)
        await start
        await close
        assert dashboard.viewer_url is None
    else:
        pending.set_result(False)
        await start
        assert dashboard.diagnostic == 'The selected tracing dashboard is not ready.'
    await dashboard.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['load', 'redirect', 'identity'])
async def test_custom_login_rejects_untrusted_navigation_or_identity(synthetic_provider, failure):
    config = configure_dashboard(synthetic_provider, login=True)
    path = synthetic_provider/'provider.py'
    hook = "page.url='https://unselected.example.test'" if failure == 'redirect' else 'pass'
    path.write_text(path.read_text().replace('    async def aclose(self): pass',
        f'    async def prepare_dashboard(self,page,*,url,username,password): {hook}\n'
        '    async def dashboard_identity(self,page,*,url): return "unselected-user"\n'
        '    async def aclose(self): pass'))
    dashboard=LocalTracing(config,emissions=EmissionBoundary(),probe=lambda *a: True,owner_root=synthetic_provider)
    await dashboard.start()
    page = Page()
    if failure == 'load': page.goto = AsyncMock(return_value=None)
    with pytest.raises(DashboardError): await dashboard.login(page)
    assert dashboard.authentication != 'authenticated'
    await dashboard.aclose()


@pytest.mark.parametrize('manifests', [(), ('captured-tracing-manifest',)])
def test_trace_viewer_bootstrap_prepares_only_selected_provider_dependencies(monkeypatch, manifests):
    from api_agent.runtime import bootstrap, trace_viewer
    selected = {'TRACE_PROVIDER':'synthetic.py:create_tracing_provider'}
    ensure = Mock(return_value=None)
    monkeypatch.setattr('api_agent.runtime.hook_assurance.assure_hook', lambda root: None)
    monkeypatch.setattr('api_agent.config.read_dotenv', lambda path: {})
    monkeypatch.setattr(trace_viewer,'resolve_viewer_config', lambda *a, **kw: selected)
    monkeypatch.setattr('api_agent.providers.distribution.selected_manifests', lambda *a, **kw: manifests)
    monkeypatch.setattr('api_agent.runtime.dependencies.ensure_environment', ensure)
    monkeypatch.setattr(trace_viewer,'run_trace_viewer',lambda *a: 0)
    assert bootstrap.main(['--trace-viewer'],base_root=BASE,_worktrees_verified=True) == 0
    assert ensure.call_args.kwargs.get('selected_provider_manifests', ()) == manifests


@pytest.mark.asyncio
async def test_remote_viewer_owns_browser_without_allocating_managed_services(monkeypatch, capsys):
    import asyncio
    from api_agent.runtime import trace_viewer
    ready = asyncio.Event()
    dashboard = SimpleNamespace(viewer_url='https://dashboard.example.test', start=AsyncMock(),
        aclose=AsyncMock(), interactive_credentials=lambda: None)
    async def login(page): ready.set()
    dashboard.login = login
    browser = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=object(),inspection_endpoint=None)),aclose=AsyncMock())
    inspection = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(trace_viewer,'local_trace_prerequisites', lambda *a, **kw: None)
    monkeypatch.setattr(trace_viewer,'LocalTracing',lambda *a, **kw: dashboard)
    monkeypatch.setattr(trace_viewer,'InspectionOwner',lambda:inspection)
    monkeypatch.setattr(trace_viewer,'managed_browser',lambda **kw:browser)
    task = asyncio.create_task(trace_viewer.view_trace(SimpleNamespace(owner_root=BASE), {'TRACE_PROVIDER':'selected'}))
    await asyncio.wait_for(ready.wait(), 2)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    browser.aclose.assert_awaited_once()
    dashboard.aclose.assert_awaited_once()
    assert 'Username:' not in capsys.readouterr().out


def test_prerequisite_entrypoint_routes_selected_provider_to_generic_manager(monkeypatch):
    from api_agent.testing.prerequisites import local_trace_prerequisites
    expected = object()
    selected = Mock(return_value=expected)
    monkeypatch.setattr('api_agent.runtime.tracing_services.custom_trace_prerequisites', selected)
    config={'TRACE_PROVIDER':'selected'}
    assert local_trace_prerequisites('context',config,cancel_event='cancel',tool_plan='tools') is expected
    selected.assert_called_once_with('context',config,cancel_event='cancel',tool_plan='tools',require_export=True)


def test_local_planning_rejects_conflicting_selected_infrastructure_identity(monkeypatch):
    from api_agent.runtime.local_run import _plan
    base=SimpleNamespace(id='synthetic/collector', value='first')
    changed=SimpleNamespace(id='synthetic/collector', value='changed')
    context=SimpleNamespace(infrastructure_paths=('pack.toml',),owner_root=BASE)
    monkeypatch.setattr('api_agent.runtime.infrastructure_contract.load_infrastructure_contributions',lambda *a, **kw:(base,))
    monkeypatch.setattr('api_agent.runtime.tracing_services.selected_tracing_infrastructure',lambda *a, **kw:((changed,),context))
    monkeypatch.setattr('api_agent.telemetry.tracing_providers.effective_selection',
        lambda *a, **kw:SimpleNamespace(values={'TRACE_PROVIDER':'selected'},declaration=SimpleNamespace(managed_compose_profile=None,dashboard=None,endpoint_field='TRACE_EXPORT_ENDPOINT')))
    with pytest.raises(ValueError,match='Conflicting selected'):
        _plan(SimpleNamespace(values={'TRACE_PROVIDER':'selected'}),object(),context,'scope')


def test_local_planner_retries_exact_frozen_services_with_verified_engine(monkeypatch):
    from api_agent.runtime.local_run import _plan
    from api_agent.runtime.infrastructure_plan import MissingEngineTransport
    context=SimpleNamespace(infrastructure_paths=('pack.toml',))
    config=SimpleNamespace(values={})
    contributions=(object(),)
    planner=Mock(side_effect=[MissingEngineTransport('missing'), 'owned-plan'])
    monkeypatch.setattr('api_agent.runtime.infrastructure_contract.load_infrastructure_contributions',lambda *a, **kw:contributions)
    monkeypatch.setattr('api_agent.runtime.infrastructure_plan.plan_infrastructure',planner)
    monkeypatch.setattr('api_agent.runtime.inventory.capture_inventory',lambda *a:'inventory')
    monkeypatch.setattr('api_agent.runtime.reporting_engine.verified_engine_binding',lambda *a, **kw:(None,'unix:///tmp/verified.sock'))
    assert _plan(config, 'schema', context, 'scope') == 'owned-plan'
    assert planner.call_args.args == (config,'schema',contributions,'scope')
    assert planner.call_args.kwargs['engine_environment'] == {'DOCKER_HOST':'unix:///tmp/verified.sock'}


@pytest.mark.parametrize('authenticated', [False, True])
@pytest.mark.asyncio
async def test_static_trace_capable_viewer_needs_no_unused_export_endpoint(synthetic_provider, authenticated):
    from api_agent.config import load_shared_schema
    from api_agent.config.resolve import ConfigResult
    from api_agent.runtime.trace_viewer import resolve_viewer_config
    from api_agent.telemetry.tracing import TraceRuntime, TraceError
    configure_dashboard(synthetic_provider, login=authenticated)
    marker = synthetic_provider / 'factory-imported'
    (synthetic_provider/'provider.py').write_text('''from pathlib import Path
Path(MARKER_PATH).write_text('imported')
class Adapter:
    version=1
    def create_exporter(self,plan):raise AssertionError('viewer must not export')
    async def prepare_dashboard(self,page,*,url,username,password):pass
    async def dashboard_identity(self,page,*,url):return 'viewer@example.test'
    async def aclose(self):pass
def create_tracing_provider(config,services):return Adapter()
'''.replace('MARKER_PATH',repr(str(marker))))
    config = resolve_viewer_config(load_shared_schema(BASE), argv=(),
        environ={'TRACE_PROVIDER': values(synthetic_provider)['TRACE_PROVIDER']}, dotenv={},
        anchors={'base_root':BASE,'pack_root':synthetic_provider,'owner_root':synthetic_provider})
    assert 'TRACE_EXPORT_ENDPOINT' not in config
    assert not marker.exists()
    probe = Mock(return_value=True)
    dashboard = LocalTracing(config, emissions=EmissionBoundary(), probe=probe, owner_root=synthetic_provider)
    assert not marker.exists()
    await dashboard.start()
    assert dashboard.state == 'ready'
    assert marker.exists() is authenticated
    probe.assert_called_once_with('https://dashboard.example.test/ready',None)
    # The same trace-capable declaration still requires an endpoint for actual export.
    with pytest.raises(TraceError, match='explicit supported OTLP signal endpoint'):
        TraceRuntime(ConfigResult(config,{},(),(),()), boundary=EmissionBoundary())
    await dashboard.aclose()


def test_viewer_selects_dashboard_profile_among_export_only_profiles(synthetic_provider):
    from api_agent.config import load_shared_schema
    from api_agent.runtime.trace_viewer import resolve_viewer_config
    configure_dashboard(synthetic_provider)
    (synthetic_provider/'tracing-provider.toml').write_text('''version=2
id="synthetic"
selector="PROFILE_CHOICE"
disabled_value=""
[[profiles]]
value="export"
signals=["traces"]
[[profiles]]
value="viewer"
signals=[]
[profiles.dashboard]
url_field="DASHBOARD_URL"
''')
    with (synthetic_provider/'cfg/config.toml').open('a') as stream:
        stream.write('\n[fields.PROFILE_CHOICE]\ntype="str"\ndefault="viewer"\n')
    config = resolve_viewer_config(load_shared_schema(BASE),argv=(),
        environ={'TRACE_PROVIDER':values(synthetic_provider)['TRACE_PROVIDER']},dotenv={},
        anchors={'base_root':BASE,'pack_root':synthetic_provider,'owner_root':synthetic_provider})
    assert config['PROFILE_CHOICE'] == 'viewer'
    assert config['DASHBOARD_URL'] == 'https://dashboard.example.test'


@pytest.mark.asyncio
async def test_standalone_viewer_prerequisites_do_not_require_unused_export_settings(synthetic_provider,monkeypatch):
    import asyncio
    from api_agent.config import load_shared_schema
    from api_agent.runtime import trace_viewer
    configure_dashboard(synthetic_provider)
    (synthetic_provider/'provider.py').write_text('raise AssertionError("no-login viewer must remain lazy")\ndef create_tracing_provider(config,services):pass\n')
    config=trace_viewer.resolve_viewer_config(load_shared_schema(BASE),argv=(),
        environ={'TRACE_PROVIDER':values(synthetic_provider)['TRACE_PROVIDER']},dotenv={},
        anchors={'base_root':BASE,'pack_root':synthetic_provider,'owner_root':synthetic_provider})
    assert 'TRACE_EXPORT_ENDPOINT' not in config
    started=asyncio.Event()
    navigated=asyncio.Event()
    class ObservedPage(Page):
        async def goto(self,url,**kwargs):
            response=await super().goto(url,**kwargs)
            navigated.set()
            return response
    page=ObservedPage()
    class Browser:
        async def start(self):
            started.set()
            return SimpleNamespace(page=page,inspection_endpoint=None)
        async def aclose(self):pass
    monkeypatch.setattr(trace_viewer,'managed_browser',lambda **kw:Browser())
    monkeypatch.setattr(trace_viewer,'InspectionOwner',lambda:SimpleNamespace(aclose=AsyncMock()))
    probe=Mock(return_value=True)
    monkeypatch.setattr('api_agent.runtime.local_tracing._probe',probe)
    task=asyncio.create_task(trace_viewer.view_trace(SimpleNamespace(owner_root=synthetic_provider),config))
    try:
        await asyncio.wait_for(started.wait(),2)
        await asyncio.wait_for(navigated.wait(),2)
        assert not task.done()
        assert page.url=='https://dashboard.example.test'
        probe.assert_called_once_with('https://dashboard.example.test/ready',None)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
