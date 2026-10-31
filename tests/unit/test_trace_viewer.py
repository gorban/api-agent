"""Standalone trace viewer uses only managed synthetic owners."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.config import load_shared_schema
from api_agent.config.resolve import ConfigError
from api_agent.runtime import trace_viewer
from api_agent.providers.control import CleanupPendingError

ROOT = Path(__file__).resolve().parents[2]


def resolve(*, argv=(), environ=None, dotenv=None):
    return trace_viewer.resolve_viewer_config(load_shared_schema(ROOT), argv=argv,
        environ=environ or {}, dotenv=dotenv or {}, anchors={'owner_root': ROOT})


@pytest.mark.parametrize('mode,target', [('', None), ('OTEL', 'http://example.invalid:4318/v1/traces'),
                                         ('LangFuse', 'http://localhost:3001')])
def test_disabled_and_custom_collectors_are_rejected(mode, target):
    environ = {'TRACE_MODE': mode}
    if target is not None:
        environ['TRACE_TARGET'] = target
    with pytest.raises(ConfigError, match='managed local'):
        resolve(environ=environ)


def test_trace_config_uses_cli_environment_and_dotenv_precedence():
    values = resolve(argv=['--trace-mode=OTEL', '--jaeger-viewer-port=16687'],
                     environ={'TRACE_MODE': 'LangFuse', 'UNRELATED_REQUIRED': ''},
                     dotenv={'TRACE_MODE': 'OTEL', 'JAEGER_VIEWER_PORT': '16686'})
    assert values['TRACE_MODE'] == 'OTEL'
    assert values['TRACE_TARGET'] == 'http://localhost:4318/v1/traces'
    assert values['JAEGER_VIEWER_PORT'] == 16687


def test_langfuse_admin_values_use_managed_defaults():
    values = resolve(environ={'TRACE_MODE': 'LangFuse'})
    assert values['LANGFUSE_ADMIN_EMAIL'] == 'admin@local.dev'
    assert values['LANGFUSE_ADMIN_PASSWORD'] == 'password'
    assert values['LANGFUSE_PUBLIC_KEY'] == 'pk-lf-local-dev-0000000000'
    assert values['LANGFUSE_SECRET_KEY'] == 'sk-lf-local-dev-0000000000'


def test_langfuse_project_keys_honor_configuration():
    values = resolve(environ={'TRACE_MODE': 'LangFuse', 'LANGFUSE_PUBLIC_KEY': 'public-test',
                              'LANGFUSE_SECRET_KEY': 'secret-test'})
    assert values['LANGFUSE_PUBLIC_KEY'] == 'public-test'
    assert values['LANGFUSE_SECRET_KEY'] == 'secret-test'


@pytest.mark.parametrize('url', ['https://example.invalid/', 'http://localhost:1234/',
                                 'http://user:password@localhost:16686/', 'http://localhost:bad/'])
def test_remote_or_invalid_dashboard_url_is_rejected(url):
    with pytest.raises(ConfigError, match='managed local'):
        resolve(environ={'TRACE_MODE': 'OTEL', 'TRACE_VIEWER_URL': url})


@pytest.mark.parametrize('endpoint', ['http://127.0.0.1:1234', None])
@pytest.mark.parametrize('mode,credentials', [('OTEL', None),
    ('LangFuse', ('viewer@example.test', 'synthetic-dashboard-password'))])
def test_viewer_keeps_collector_until_interrupted_then_disposes_in_order(monkeypatch, capsys, endpoint, mode, credentials):
    events = []
    ready = None
    class Manager:
        timeout = cleanup_timeout = 5
        def acquire(self): events.append('acquire')
        def release(self): events.append('release')
    class Inspection:
        # A closed window must not end the command or release its collector.
        async def wait_closed(self): events.append('window-closed')
        async def aclose(self): events.append('inspection-close')
    class Dashboard:
        viewer_url = 'http://localhost:16686'
        def __init__(self, values, *, emissions): pass
        async def start(self): events.append('dashboard-start')
        async def login(self, page):
            events.append('login')
            ready.set()
        def interactive_credentials(self): return credentials
        async def aclose(self): events.append('dashboard-close')
    class Browser:
        attempts = 0
        async def start(self):
            events.append('browser-start')
            return SimpleNamespace(page=object(), inspection_endpoint=endpoint)
        async def aclose(self):
            self.attempts += 1
            events.append('browser-close')
            if self.attempts == 1:
                raise CleanupPendingError((self,), self.aclose)
    monkeypatch.setattr(trace_viewer, 'local_trace_prerequisites', lambda *a, **kw: Manager())
    monkeypatch.setattr(trace_viewer, 'InspectionOwner', Inspection)
    monkeypatch.setattr(trace_viewer, 'LocalTracing', Dashboard)
    monkeypatch.setattr(trace_viewer, 'managed_browser', lambda **kw: Browser())
    async def scenario():
        nonlocal ready
        ready = asyncio.Event()
        task = asyncio.create_task(trace_viewer.view_trace(object(), {'TRACE_MODE': mode,
            'LANGFUSE_PUBLIC_KEY': 'synthetic-exporter-public',
            'LANGFUSE_SECRET_KEY': 'synthetic-exporter-secret'}))
        await asyncio.wait_for(ready.wait(), 3)
        assert not task.done()
        assert events == ['acquire', 'dashboard-start', 'browser-start', 'login']
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert events == ['acquire', 'dashboard-start', 'browser-start', 'login',
                      'browser-close', 'browser-close', 'inspection-close', 'dashboard-close', 'release']
    output = capsys.readouterr().out
    assert output.count('cleanup remains pending') == 1
    assert ('Trace browser CDP:' in output) == (endpoint is not None)
    assert 'Trace viewer: http://localhost:16686' in output
    assert 'Press Ctrl+C to stop' in output
    assert 'synthetic-exporter-public' not in output
    assert 'synthetic-exporter-secret' not in output
    if credentials is None:
        assert 'Username:' not in output and 'Password:' not in output
    else:
        assert f'\n  Username: {credentials[0]}\n  Password: {credentials[1]}\n' in output


def test_cancelled_acquisition_drains_before_release(monkeypatch):
    from threading import Event

    started, finish = Event(), Event()
    events = []
    class Manager:
        timeout = cleanup_timeout = 5
        def acquire(self):
            events.append('acquire')
            started.set()
            assert finish.wait(5)
            events.append('acquire-finished')
        def release(self): events.append('release')
    class Inspection:
        async def aclose(self): events.append('inspection-close')
    class Dashboard:
        def __init__(self, values, *, emissions): pass
        async def aclose(self): events.append('dashboard-close')
    monkeypatch.setattr(trace_viewer, 'local_trace_prerequisites', lambda *a, **kw: Manager())
    monkeypatch.setattr(trace_viewer, 'InspectionOwner', Inspection)
    monkeypatch.setattr(trace_viewer, 'LocalTracing', Dashboard)
    async def scenario():
        task = asyncio.create_task(trace_viewer.view_trace(object(), {'TRACE_MODE': 'OTEL'}))
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert 'release' not in events
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert events == ['acquire', 'acquire-finished', 'inspection-close', 'dashboard-close', 'release']


@pytest.mark.parametrize("handoff_status", [None, 73])
def test_bootstrap_selects_viewer_before_provider_and_workflow_discovery(monkeypatch, handoff_status):
    from api_agent.runtime import bootstrap
    import api_agent.config
    import api_agent.runtime.dependencies
    import api_agent.runtime.hook_assurance
    import api_agent.providers.loading

    observed = []
    monkeypatch.setattr(api_agent.config, 'read_dotenv', lambda path: {})
    monkeypatch.setattr(api_agent.runtime.hook_assurance, 'assure_hook', lambda root: None)
    monkeypatch.setattr(api_agent.providers.loading, 'bootstrap_schema', lambda *a, **k: pytest.fail('provider discovery'))
    monkeypatch.setattr(bootstrap, '_prepare_workflow_discovery', lambda *a, **k: pytest.fail('workflow discovery'))
    monkeypatch.setattr(api_agent.runtime.dependencies, 'ensure_environment', lambda *a, **k: handoff_status)
    monkeypatch.setattr(trace_viewer, 'run_trace_viewer', lambda context, values: observed.append(values) or 0)
    assert bootstrap.main(['--trace-viewer', '--trace-mode=OTEL'], base_root=ROOT, _worktrees_verified=True) == (0 if handoff_status is None else handoff_status)
    if handoff_status is None:
        assert observed[0]['TRACE_MODE'] == 'OTEL'
    else:
        assert observed == []


def test_trace_help_is_lazy(monkeypatch, capsys):
    from api_agent.runtime import bootstrap
    import api_agent.runtime.dependencies
    import api_agent.runtime.hook_assurance
    monkeypatch.setattr(api_agent.runtime.hook_assurance, 'assure_hook', lambda root: None)
    monkeypatch.setattr(api_agent.runtime.dependencies, 'ensure_environment', lambda *a, **k: pytest.fail('dependency preparation'))
    assert bootstrap.main(['--trace-viewer', '--help'], base_root=ROOT, _worktrees_verified=True) == 0
    assert '--trace-viewer' in capsys.readouterr().out


def test_invalid_collector_and_positional_inputs_fail():
    with pytest.raises(ConfigError, match='Invalid trace destination'):
        resolve(environ={'TRACE_MODE': 'OTEL', 'TRACE_TARGET': 'http://localhost:bad/v1/traces'})
    with pytest.raises(ConfigError, match='positional'):
        resolve(argv=['unexpected'], environ={'TRACE_MODE': 'OTEL'})


def test_valid_local_jaeger_viewer_url_is_accepted():
    values = resolve(environ={'TRACE_MODE': 'OTEL', 'TRACE_VIEWER_URL': 'http://127.0.0.1:16686/'})
    assert values['TRACE_VIEWER_URL'] == 'http://127.0.0.1:16686/'


def test_run_viewer_reports_fixed_failure_without_exception_text(monkeypatch, capsys):
    async def fail(*args):
        raise RuntimeError('synthetic-secret')
    monkeypatch.setattr(trace_viewer, 'view_trace', fail)
    assert trace_viewer.run_trace_viewer(object(), {}) == 2
    assert 'synthetic-secret' not in capsys.readouterr().out


def test_run_viewer_success_and_interrupt(monkeypatch):
    async def done(*args): pass
    monkeypatch.setattr(trace_viewer, 'view_trace', done)
    assert trace_viewer.run_trace_viewer(object(), {}) == 0
    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt
    monkeypatch.setattr(trace_viewer.asyncio, 'run', interrupted)
    assert trace_viewer.run_trace_viewer(object(), {}) == 130


def test_unavailable_dashboard_releases_acquired_collector(monkeypatch):
    events = []
    class Manager:
        timeout = cleanup_timeout = 5
        def acquire(self): events.append('acquire')
        def release(self): events.append('release')
    class Inspection:
        async def aclose(self): events.append('inspection-close')
    class Dashboard:
        viewer_url = None
        def __init__(self, values, *, emissions): pass
        async def start(self): events.append('dashboard-start')
        async def aclose(self): events.append('dashboard-close')
    monkeypatch.setattr(trace_viewer, 'local_trace_prerequisites', lambda *a, **kw: Manager())
    monkeypatch.setattr(trace_viewer, 'InspectionOwner', Inspection)
    monkeypatch.setattr(trace_viewer, 'LocalTracing', Dashboard)
    monkeypatch.setattr(trace_viewer, 'managed_browser', lambda **kw: pytest.fail('browser must not start'))
    with pytest.raises(trace_viewer.DashboardError):
        asyncio.run(trace_viewer.view_trace(object(), {'TRACE_MODE': 'OTEL'}))
    assert events == ['acquire', 'dashboard-start', 'inspection-close', 'dashboard-close', 'release']


def test_terminated_inspection_settles_before_collector_release(monkeypatch):
    events = []
    class Inspection:
        async def aclose(self):
            events.append('inspection-close')
            raise RuntimeError('pending')
        async def settle_terminated_inspection(self):
            events.append('settle')
            return True
    inspection = Inspection()
    asyncio.run(trace_viewer._dispose(inspection, label='inspection', inspection=inspection))
    assert events == ['inspection-close', 'settle']


def test_slow_collector_release_is_not_duplicated(monkeypatch, capsys):
    from threading import Event
    started, unblock = Event(), Event()
    class Manager:
        cleanup_timeout = .01
        calls = 0
        def release(self):
            self.calls += 1
            started.set()
            assert unblock.wait(3)
    manager = Manager()
    async def scenario():
        task = asyncio.create_task(trace_viewer._dispose(manager, label='collector'))
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(.05)
        assert manager.calls == 1 and not task.done()
        unblock.set()
        await task
    asyncio.run(scenario())
    assert manager.calls == 1
    assert capsys.readouterr().out.count('cleanup remains pending') == 1


def test_missing_local_collector_is_rejected_before_opening_browser(monkeypatch):
    monkeypatch.setattr(trace_viewer, 'local_trace_prerequisites', lambda *a, **kw: None)
    with pytest.raises(ConfigError, match='managed local'):
        asyncio.run(trace_viewer.view_trace(object(), {}))


def test_local_dashboard_requires_effective_admin_credentials():
    from dataclasses import replace
    from api_agent.config.declarations import ConfigSchema
    schema = load_shared_schema(ROOT)
    fields = dict(schema.fields)
    fields['LANGFUSE_ADMIN_PASSWORD'] = replace(fields['LANGFUSE_ADMIN_PASSWORD'], conditional_defaults=())
    with pytest.raises(ConfigError, match='dashboard credentials'):
        trace_viewer.resolve_viewer_config(ConfigSchema(fields), argv=(),
            environ={'TRACE_MODE': 'LangFuse'}, dotenv={}, anchors={'owner_root': ROOT})


def test_subset_schema_without_optional_ca_is_supported():
    from api_agent.config.declarations import ConfigSchema
    schema = load_shared_schema(ROOT)
    fields = {name: field for name, field in schema.fields.items() if name != 'CLIENT_CA_FILE'}
    values = trace_viewer.resolve_viewer_config(ConfigSchema(fields), argv=(),
        environ={'TRACE_MODE': 'OTEL'}, dotenv={}, anchors={'owner_root': ROOT})
    assert values['TRACE_MODE'] == 'OTEL'


@pytest.mark.parametrize('settle_result', [False, RuntimeError('unavailable')])
def test_pending_inspection_retries_after_unconfirmed_settlement(settle_result, capsys):
    class Inspection:
        attempts = 0
        async def aclose(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError('pending')
        async def settle_terminated_inspection(self):
            if isinstance(settle_result, Exception):
                raise settle_result
            return settle_result
    inspection = Inspection()
    asyncio.run(trace_viewer._dispose(inspection, label='inspection', inspection=inspection))
    assert inspection.attempts == 2
    assert capsys.readouterr().out.count('cleanup remains pending') == 1


def test_repeated_cleanup_failures_only_report_once(capsys):
    class Owner:
        attempts = 0
        async def aclose(self):
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError('pending')
    owner = Owner()
    asyncio.run(trace_viewer._dispose(owner, label='dashboard'))
    assert owner.attempts == 3
    assert capsys.readouterr().out.count('cleanup remains pending') == 1


@pytest.mark.parametrize('argv', [('--trace-viewer', '--trace-viewer'), ('--trace-viewer=yes',),
                                  ('--trace-viewer', '--view-flow')])
def test_trace_viewer_rejects_duplicate_value_and_conflicting_mode(argv, capsys):
    from api_agent.runtime import bootstrap
    assert bootstrap.main(argv, base_root=ROOT, _worktrees_verified=True) == 2
    assert 'trace-viewer' in capsys.readouterr().err


@pytest.mark.parametrize('phase', ['browser-start', 'dashboard-login'])
def test_cancel_during_browser_or_login_disposes_before_collector(monkeypatch, phase):
    events = []
    class Manager:
        timeout = cleanup_timeout = 5
        def acquire(self): events.append('acquire')
        def release(self): events.append('release')
    class Inspection:
        async def aclose(self): events.append('inspection-close')
    class Dashboard:
        viewer_url = 'http://localhost:16686'
        def __init__(self, values, *, emissions): pass
        async def start(self): events.append('dashboard-start')
        async def login(self, page):
            events.append('login')
            if phase == 'dashboard-login':
                entered.set()
                await asyncio.Event().wait()
        async def aclose(self): events.append('dashboard-close')
    class Browser:
        async def start(self):
            events.append('browser-start')
            if phase == 'browser-start':
                entered.set()
                await asyncio.Event().wait()
            return SimpleNamespace(page=object(), inspection_endpoint=None)
        async def aclose(self): events.append('browser-close')
    monkeypatch.setattr(trace_viewer, 'local_trace_prerequisites', lambda *a, **kw: Manager())
    monkeypatch.setattr(trace_viewer, 'InspectionOwner', Inspection)
    monkeypatch.setattr(trace_viewer, 'LocalTracing', Dashboard)
    monkeypatch.setattr(trace_viewer, 'managed_browser', lambda **kw: Browser())
    async def scenario():
        nonlocal entered
        entered = asyncio.Event()
        task = asyncio.create_task(trace_viewer.view_trace(object(), {'TRACE_MODE': 'OTEL'}))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    entered = None
    asyncio.run(scenario())
    assert events[-4:] == ['browser-close', 'inspection-close', 'dashboard-close', 'release']


def test_trace_viewer_flag_after_separator_remains_ordinary_selector(monkeypatch):
    from api_agent.runtime import bootstrap
    import api_agent.config
    import api_agent.runtime.hook_assurance
    import api_agent.providers.loading

    seen = []
    monkeypatch.setattr(api_agent.config, 'read_dotenv', lambda path: {})
    monkeypatch.setattr(api_agent.runtime.hook_assurance, 'assure_hook', lambda root: None)
    monkeypatch.setattr(api_agent.providers.loading, 'bootstrap_schema', lambda schema, *a, **k: schema)
    monkeypatch.setattr(bootstrap, '_prepare_workflow_discovery', lambda context, argv, **kw: seen.append(argv) or 73)
    monkeypatch.setattr(trace_viewer, 'run_trace_viewer', lambda *a: pytest.fail('viewer selected'))
    assert bootstrap.main(['--tui-mode=unit', '--', '--trace-viewer'], base_root=ROOT, _worktrees_verified=True) == 73
    assert seen == [['--tui-mode=unit', '--', '--trace-viewer']]


@pytest.mark.parametrize('option,value', [('--trace-target', 'http://local\nhost:4318/v1/traces'),
                                         ('--trace-viewer-url', 'http://local\nhost:16686/'),
                                         ('--trace-target', ' http://localhost:4318/v1/traces'),
                                         ('--trace-viewer-url', 'http://localhost:16686/\t')])
def test_malformed_raw_urls_are_rejected_before_collector_acquisition(monkeypatch, capsys, option, value):
    from api_agent.runtime import bootstrap
    import api_agent.config
    import api_agent.runtime.dependencies
    import api_agent.runtime.hook_assurance

    acquisitions = []
    monkeypatch.setattr(api_agent.config, 'read_dotenv', lambda path: {})
    monkeypatch.setattr(api_agent.runtime.hook_assurance, 'assure_hook', lambda root: None)
    monkeypatch.setattr(api_agent.runtime.dependencies, 'ensure_environment', lambda *a, **k: pytest.fail('dependency preparation'))
    monkeypatch.setattr(trace_viewer, 'local_trace_prerequisites', lambda *a, **k: acquisitions.append(True))
    assert bootstrap.main(['--trace-viewer', '--trace-mode=OTEL', f'{option}={value}'],
                          base_root=ROOT, _worktrees_verified=True) == 2
    assert acquisitions == []
    assert value not in capsys.readouterr().err
