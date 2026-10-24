"""Controlled operation lifecycle; no actual listener, browser or infrastructure."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.providers.control import CleanupPendingError
from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
from api_agent.runtime.local_run import LocalRunOwner, LocalCleanup


@pytest.fixture
def local(tmp_path):
    events, calls = [], []
    config = SimpleNamespace(errors=(), missing=(), values={'RUN_UI': False, 'TRACE_MODE': '', 'UI_LISTENER_URL': 'https://localhost:9443'})
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path)
    definition = SimpleNamespace(definition_id='selected-definition')
    prepared = (definition, object(), False, object(), {})
    application_factory = Mock()
    runtime = SimpleNamespace(emissions=EmissionBoundary(ContentPolicy()), services={}, tracing=None)
    session = SimpleNamespace(callback=SimpleNamespace(origin='https://localhost:8443'), runtime=runtime,
                              infrastructure=None, listener=None)
    class Operation:
        pending_cleanup = None
        @asynccontextmanager
        async def open(self, **options):
            calls.append('operation-start')
            try: yield session
            finally: calls.append('operation-stop')
    operation = Operation()
    factory = Mock(return_value=operation)
    class Browser:
        session = None
        flow_url = 'http://127.0.0.1:1234/flow'
        async def start(self): calls.append('browser-start')
        async def aclose(self): calls.append('browser-stop')
    browser = Browser()
    browser_factory = Mock(return_value=browser)
    owner = LocalRunOwner(context, lambda: config, operation_factory=factory, browser_factory=browser_factory,
        prepare_view=Mock(return_value=prepared), application_factory=application_factory, on_event=events.append,
        plan_factory=Mock(return_value=SimpleNamespace(conditional=(), services=())))
    return SimpleNamespace(owner=owner, config=config, calls=calls, events=events, prepared=prepared,
                           operation=operation, operation_factory=factory, browser=browser,
                           browser_factory=browser_factory, application_factory=application_factory, session=session)


def test_start_freezes_selected_definition_and_stop_releases_reverse(local):
    async def run():
        snapshot = await local.owner.start()
        assert snapshot.phase == 'ready' and snapshot.definition_id == 'selected-definition'
        assert [item.name for item in snapshot.destinations] == ['agent', 'flow']
        local.application_factory.assert_called_once_with(local.prepared[0], config=local.config.values)
        assert local.browser_factory.call_args.kwargs['prepare_view'](object()) is local.prepared
        assert local.calls == ['operation-start', 'browser-start']
        assert local.operation_factory.call_args.kwargs['groups'] == ('browser',)
        with pytest.raises(RuntimeError): await local.owner.start()
        local.owner.request_stop()
        assert await local.owner.wait() == 0
        await local.owner.aclose()
        assert local.calls == ['operation-start', 'browser-start', 'browser-stop', 'operation-stop']
        assert local.owner.snapshot.phase == 'stopped'
        assert any(event.kind == 'log' and 'ready' in event.text for event in local.events)
    asyncio.run(run())


@pytest.mark.parametrize('invalid', ['errors', 'missing', 'graph'])
def test_preflight_rejects_without_runtime_side_effects(local, invalid):
    if invalid == 'errors': local.config.errors = ('bad',)
    if invalid == 'missing': local.config.missing = ('required',)
    if invalid == 'graph': local.application_factory.side_effect = ValueError('Unsupported selected graph')
    async def run():
        with pytest.raises(ValueError): await local.owner.start()
        local.operation_factory.assert_not_called()
        local.browser_factory.assert_not_called()
        assert local.calls == []
    asyncio.run(run())


def test_failed_browser_cleanup_retains_operation(local):
    local.browser.aclose = AsyncMock(side_effect=[RuntimeError('pending'), None])
    async def run():
        await local.owner.start()
        with pytest.raises(CleanupPendingError) as pending: await local.owner.aclose()
        assert local.calls == ['operation-start', 'browser-start']
        assert local.owner.pending_cleanup is pending.value
        await pending.value.retry()
        assert local.calls[-1] == 'operation-stop'
        assert local.owner.pending_cleanup is None
    asyncio.run(run())


def test_failed_start_closes_partial_browser_before_operation(local):
    local.browser.start = AsyncMock(side_effect=ValueError('failed readiness'))
    async def run():
        with pytest.raises(ValueError): await local.owner.start()
        assert local.calls == ['operation-start', 'browser-stop', 'operation-stop']
    asyncio.run(run())


def test_wait_without_start_rejected(local):
    async def run():
        with pytest.raises(RuntimeError): await local.owner.wait()
        await local.owner.aclose()
        with pytest.raises(RuntimeError): await local.owner.start()
    asyncio.run(run())


def test_runtime_defers_provider_activation_and_wraps_real_graph_callbacks(local, monkeypatch):
    from api_agent.runtime import local_run
    from api_agent.runtime import client_tls
    from api_agent.ui import process
    config, origin, material = local.config, 'https://localhost:8443', SimpleNamespace(ca_file=Path('/synthetic-ca'))
    config.values['LLM_REASONING_EFFORT'] = ''
    runtime = SimpleNamespace(services={}, emissions=EmissionBoundary(ContentPolicy()), tracing=None)
    compose = Mock(return_value=runtime)
    monkeypatch.setattr(local_run, 'compose_runtime', compose)
    monkeypatch.setattr(client_tls, 'verified_client_context', lambda value: 'verified')
    monkeypatch.setattr(process, 'StudioProcesses', Mock(return_value='process-owner'))
    llm = SimpleNamespace(describe=AsyncMock(return_value=()), complete=AsyncMock(),
                          ensure_ready=AsyncMock(), aclose=AsyncMock())
    local.owner.llm_factory = Mock(return_value=llm)
    local.owner._graph = SimpleNamespace(complete=AsyncMock(return_value='graph-result'))
    async def run():
        result = local.owner._runtime(config, origin, material)
        compose.assert_called_once_with(local.owner.context, config, callback_origin=origin,
                                         tls_material=material, required_kinds=())
        assert local.owner.llm_factory.call_args.kwargs['trace'] is runtime.tracing
        assert result.services['ui_processes'] == 'process-owner'
        assert await result.services['studio_backend'].describe(auth=None) == ()
        context = object()
        assert await result.services['studio_backend'].complete('request', auth=None, context=context) == 'graph-result'
        local.owner._graph.complete.assert_awaited_once_with('request', llm=llm, context=context)
        llm.ensure_ready.assert_not_called()
        await result.services['studio_backend'].aclose()
    asyncio.run(run())


def test_optional_ui_uses_one_prepared_owner_and_then_llm(local, monkeypatch):
    from api_agent.runtime import local_run
    local.config.values['RUN_UI'] = True
    prepared = SimpleNamespace(start=AsyncMock(), aclose=AsyncMock())
    factory = Mock(return_value=prepared)
    monkeypatch.setattr(local_run, 'PreparedUISession', factory)
    llm = SimpleNamespace(ensure_ready=AsyncMock(), _helpers=set())
    local.owner._llm = llm
    async def run():
        await local.owner.start()
        borrowed = object()
        assert await local.owner._ui(browser_owner=borrowed) is prepared
        prepared.start.assert_awaited_once()
        llm.ensure_ready.assert_awaited_once()
        assert factory.call_args.kwargs['browser_owner'] is borrowed
        assert local.owner._status()['components']['ui'] == 'ready'
        assert [row.name for row in local.owner.snapshot.destinations] == ['agent', 'flow', 'ui']
        await local.owner.aclose()
        prepared.aclose.assert_awaited_once()
    asyncio.run(run())


def test_ui_credential_binder_keeps_selected_account(local, monkeypatch):
    from api_agent.runtime import local_run
    from api_agent.auth import bindings, managers
    local.owner._config = local.config
    local.owner._session = local.session
    local.config.values['UNATTENDED_USER_INDEX'] = 3
    local.owner._llm = SimpleNamespace(ensure_ready=AsyncMock(), _helpers=set())
    bind = Mock(return_value='scoped')
    monkeypatch.setattr(bindings, 'bind_role_credentials', bind)
    monkeypatch.setattr(managers, 'registry_for_owner', lambda root: 'registry')
    def factory(config, services, **kwargs):
        assert kwargs['user_index'] == 3
        assert kwargs['credentials_for'](('ui',), 3) == 'scoped'
        return SimpleNamespace(start=AsyncMock(), aclose=AsyncMock())
    monkeypatch.setattr(local_run, 'PreparedUISession', factory)
    async def run():
        await local.owner._ui(browser_owner=object())
        assert bind.call_args.args == (local.config.values, ('ui',), 3)
        await local.owner.aclose()
    asyncio.run(run())


def test_status_without_event_consumer_and_sanitized_logs(local):
    local.owner.on_event = None
    async def run():
        await local.owner.start()
        local.owner._session.runtime.emissions.register_secret('credential-value')
        local.owner.on_event = local.events.append
        local.owner._log('agent', 'value credential-value\n')
        assert 'credential-value' not in local.events[-1].text
        assert local.owner._logs['agent'] == local.events[-1].text
        await local.owner.aclose()
    asyncio.run(run())


def test_continuous_owned_infrastructure_logs_and_endpoints(local):
    logs = Mock(return_value='owned output\n')
    infrastructure = SimpleNamespace(status=Mock(return_value=(SimpleNamespace(service='database', id='owned-id', running=True, health=None),)),
        logs=logs, endpoints={'db': (SimpleNamespace(name='web', host='http://localhost:2345'),
                                   SimpleNamespace(name='bad', host='http://user:password@localhost:2345'))})
    local.session.infrastructure = infrastructure
    local.owner.plan_factory.return_value = SimpleNamespace(
        conditional=(SimpleNamespace(compose_name='database', id='synthetic/database'),), services=('database',))
    async def run():
        await local.owner.start()
        assert local.owner.snapshot.log_sources == ('agent', 'synthetic/database')
        assert local.owner.snapshot.destinations[-1].url == 'http://localhost:2345'
        for _ in range(50):
            if any(event.source == 'synthetic/database' for event in local.events): break
            await asyncio.sleep(.01)
        assert any(event.text == 'owned output\n' for event in local.events)
        logs.assert_called_once_with('owned-id', tail=100, output_limit=16384)
        await local.owner.aclose()
    asyncio.run(run())


def test_log_capture_degrades_and_deduplicates(local):
    async def run():
        await local.owner.start()
        local.owner._infra_sources = {'service:db': 'owned-id'}
        local.session.infrastructure = SimpleNamespace(logs=Mock(side_effect=RuntimeError('private detail')))
        task = asyncio.create_task(local.owner._capture_logs())
        for _ in range(50):
            if local.owner._logs.get('service:db'): break
            await asyncio.sleep(.01)
        await asyncio.sleep(1.02)
        local.owner.request_stop()
        await task
        assert [event.text for event in local.events if event.source == 'service:db'] == ['Service logs are unavailable.']
        await local.owner.aclose()
    asyncio.run(run())


def test_stop_during_start_cancels_owned_preparation(local):
    entered = None
    async def run():
        entered = asyncio.Event()
        async def start():
            entered.set()
            await asyncio.Event().wait()
        local.browser.start = start
        task = asyncio.create_task(local.owner.start())
        await entered.wait()
        local.owner.request_stop()
        with pytest.raises(asyncio.CancelledError): await task
        assert local.calls == ['operation-start', 'browser-stop', 'operation-stop']
    asyncio.run(run())


@pytest.mark.parametrize('trigger', ['browser', 'listener'])
def test_wait_observes_owned_browser_or_listener_termination(local, trigger):
    async def run():
        await local.owner.start()
        if trigger == 'browser':
            local.browser.session = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: False))
            expected = 0
        else:
            task = asyncio.create_task(asyncio.sleep(0))
            await task
            local.session.listener = SimpleNamespace(task=task)
            expected = 3
        assert await local.owner.wait() == expected
        assert local.owner.snapshot.phase == 'stopped'
    asyncio.run(run())


def test_wait_reaches_stop_and_preserves_original_browser_inspection(local):
    local.browser.session = SimpleNamespace(inspection_endpoint='http://127.0.0.1:1235',
        browser=SimpleNamespace(is_connected=lambda: True))
    async def run():
        await local.owner.start()
        assert local.owner.snapshot.inspection_endpoint == 'http://127.0.0.1:1235'
        waiter = asyncio.create_task(local.owner.wait())
        await asyncio.sleep(.11)
        local.owner.request_stop()
        assert await waiter == 0
    asyncio.run(run())


def test_cleanup_receipt_requires_parent_acknowledgement():
    async def run():
        duties = LocalCleanup()
        receipt = CleanupPendingError((), AsyncMock())
        callback = AsyncMock(side_effect=[receipt, None])
        duties.push(callback)
        with pytest.raises(CleanupPendingError): await duties.aclose()
        await duties.aclose()
        receipt.retry.assert_awaited_once()
        assert callback.await_count == 2 and not duties.callbacks
    asyncio.run(run())


def test_partial_operation_entry_and_cleanup_receipt_survive(local):
    receipt = CleanupPendingError((), AsyncMock())
    @asynccontextmanager
    async def opening(**options):
        local.operation.pending_cleanup = receipt
        raise ValueError('startup failure')
        yield
    local.operation.open = opening
    async def run():
        with pytest.raises(ValueError): await local.owner.start()
        assert receipt.retry.await_count >= 1
        assert local.owner.pending_cleanup is None
    asyncio.run(run())


def test_start_failure_retains_failed_cleanup_receipt(local):
    local.browser.start = AsyncMock(side_effect=ValueError('startup failed'))
    local.browser.aclose = AsyncMock(side_effect=[RuntimeError('cleanup failed'), None])
    async def run():
        with pytest.raises(ValueError) as primary: await local.owner.start()
        assert primary.value.cleanup_pending is local.owner.pending_cleanup
        await primary.value.cleanup_pending.retry()
        assert local.calls[-1] == 'operation-stop'
    asyncio.run(run())


def test_log_projection_rejects_non_text_and_never_reads_prestart_config(local):
    local.owner._log('agent', 'before startup')
    assert local.events[-1].text == 'before startup'
    local.owner._session = local.session
    local.owner._config = local.config
    local.config.values['API_KEY'] = 'configured-material'
    local.owner._log('agent', 'configured-material')
    assert 'configured-material' not in local.events[-1].text
    async def run(): await local.owner._close_logs()
    asyncio.run(run())


def test_operation_injection_keeps_shared_listener_and_dependency_ownership(local, monkeypatch):
    from api_agent.runtime.operation import local_operation
    from api_agent.api import application
    core_app, wrapped_app, runtime = object(), object(), object()
    monkeypatch.setattr(application, 'create_application', Mock(return_value=core_app))
    runtime_factory, wrapper = Mock(return_value=runtime), Mock(return_value=wrapped_app)
    runner = local_operation(local.owner.context, resolve=lambda: local.config, argv=(),
        scope='scope', ownership_directory=local.owner.context.owner_root,
        runtime_factory=runtime_factory, application_wrapper=wrapper, required_kinds={'llm'}, prepared_plan='frozen-plan')
    assert runner.plan(local.config) == 'frozen-plan'
    assert runner.compose_runtime(local.config, 'https://localhost:8443', 'material') is runtime
    runtime_factory.assert_called_once_with(local.config, 'https://localhost:8443', 'material')
    assert runner.create_application(runtime, 'localhost') is wrapped_app
    wrapper.assert_called_once_with(core_app)


def test_cancelled_child_cleanup_preserves_cancellation_and_receipt(local):
    local.browser.aclose = AsyncMock(side_effect=[asyncio.CancelledError(), None])
    async def run():
        await local.owner.start()
        with pytest.raises(asyncio.CancelledError): await local.owner.aclose()
        assert local.owner.pending_cleanup is not None
        await local.owner.pending_cleanup.retry()
        assert local.calls[-1] == 'operation-stop'
    asyncio.run(run())


def test_finished_log_collector_failure_does_not_block_owned_cleanup(local):
    async def run():
        await local.owner.start()
        async def failed(): raise RuntimeError('collector failed')
        local.owner._log_task = asyncio.create_task(failed())
        await asyncio.sleep(0)
        await local.owner._close_logs()
        await local.owner.aclose()
        assert local.calls[-1] == 'operation-stop'
    asyncio.run(run())


@pytest.mark.parametrize('source', ['1', 'missing-service', 'ui'])
def test_unknown_or_unavailable_sources_rejected_before_preparation(local, source):
    local.config.values['RUN_LOG_SOURCE'] = source
    async def run():
        with pytest.raises(ValueError, match='RUN_LOG_SOURCE'): await local.owner.start()
        local.operation_factory.assert_not_called()
        local.browser_factory.assert_not_called()
        assert local.calls == []
    asyncio.run(run())


def test_declared_source_admission_uses_frozen_plan(local):
    plan = SimpleNamespace(conditional=(SimpleNamespace(compose_name='hashed-engine-name', id='synthetic/database'),),
                           services=('hashed-engine-name',))
    local.owner.plan_factory.return_value = plan
    local.config.values['RUN_LOG_SOURCE'] = 'synthetic/database'
    async def run():
        await local.owner.start()
        assert local.owner.snapshot.log_sources == ('agent', 'synthetic/database')
        assert local.operation_factory.call_args.kwargs['prepared_plan'] is plan
        local.owner.plan_factory.assert_called_once()
        await local.owner.aclose()
    asyncio.run(run())


def test_pure_infrastructure_planning_requires_authoritative_schema(local, monkeypatch):
    from api_agent.runtime.local_run import _plan
    from api_agent.runtime import infrastructure_contract, infrastructure_plan, compose
    context = local.owner.context
    context.infrastructure_paths = ('selected.toml',)
    capture = Mock(return_value=('captured',))
    monkeypatch.setattr(infrastructure_contract, 'load_infrastructure_contributions', capture)
    planner = Mock(return_value='frozen-plan')
    monkeypatch.setattr(infrastructure_plan, 'plan_infrastructure', planner)
    with pytest.raises(ValueError, match='schema'): _plan(local.config, None, context, 'scope')
    schema = object()
    assert _plan(local.config, schema, context, 'scope') == 'frozen-plan'
    planner.assert_called_once_with(local.config, schema, ('captured',), 'scope', engine_environment=None)
    capture.return_value = ()
    monkeypatch.setattr(compose, 'plan_compose', Mock(return_value='base-plan'))
    assert _plan(local.config, None, context, 'scope') == 'base-plan'
    context.infrastructure_paths = ()
    assert _plan(local.config, None, context, 'scope') == 'base-plan'


def test_close_during_start_waits_for_late_allocations_before_acknowledgement(local):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def start():
            entered.set()
            try: await release.wait()
            except asyncio.CancelledError: await release.wait()
            local.calls.append('late-browser-allocation')
        local.browser.start = start
        startup = asyncio.create_task(local.owner.start())
        await entered.wait()
        closing = asyncio.create_task(local.owner.aclose())
        await asyncio.sleep(.02)
        assert not closing.done() and 'browser-stop' not in local.calls
        local.owner.request_stop()
        release.set()
        with pytest.raises(asyncio.CancelledError): await startup
        await closing
        assert not any(event.snapshot is not None and event.snapshot.phase == 'ready' for event in local.events)
        assert local.calls == ['operation-start', 'late-browser-allocation', 'browser-stop', 'operation-stop']
    asyncio.run(run())


@pytest.mark.parametrize('run_ui,expected', [(False, ('llm',)), (True, ('llm', 'ui'))])
def test_pure_plan_reads_only_consumed_provider_declarations(local, monkeypatch, run_ui, expected):
    from api_agent.runtime.local_run import _plan
    from api_agent.runtime import infrastructure_contract, compose
    context = local.owner.context
    context.infrastructure_paths = ()
    local.config.values.update(LLM_PROVIDER='llm', UI_PROVIDER='ui', API_PROVIDER='unused', RUN_UI=run_ui)
    capture = Mock(return_value=())
    monkeypatch.setattr(infrastructure_contract, 'load_infrastructure_contributions', capture)
    monkeypatch.setattr(compose, 'plan_compose', Mock(return_value='base'))
    assert _plan(local.config, None, context, 'scope') == 'base'
    capture.assert_called_once_with(context, selected_provider_locations=expected)


def test_log_events_escape_terminal_control_sequences_after_secret_scrubbing(local):
    local.owner._session = local.session
    local.owner._log('agent', 'safe\x1b[2J\r\u202econtent\n')
    text = local.events[-1].text
    assert '\x1b' not in text and '\r' not in text and '\u202e' not in text
    assert '\\u001b' in text and text.endswith('content\n')


@pytest.mark.parametrize('running,health,state', [(True, None, 'ready'), (True, 'unhealthy', 'degraded'), (False, 'unhealthy', 'unavailable')])
def test_verified_prerequisite_inventory_status_reaches_snapshot(local, running, health, state):
    local.owner.plan_factory.return_value = SimpleNamespace(
        conditional=(SimpleNamespace(compose_name='hashed-engine-name', id='synthetic/database'),),
        services=('hashed-engine-name',))
    local.session.infrastructure = SimpleNamespace(
        status=Mock(return_value=(SimpleNamespace(service='hashed-engine-name', id='owned-id', running=running, health=health),)),
        endpoints={}, logs=Mock(return_value=''))
    async def run():
        await local.owner.start()
        assert local.owner._status()['components']['service:synthetic/database'] == state
        assert 'service:hashed-engine-name' not in local.owner._status()['components']
        await local.owner.aclose()
    asyncio.run(run())


def test_diagnostic_retains_runtime_boundary_before_partial_composition_fails(local, monkeypatch):
    from api_agent.runtime import local_run
    boundary = EmissionBoundary(ContentPolicy())
    boundary.register_secret('partial-runtime-credential')
    monkeypatch.setattr(local_run, 'compose_runtime', Mock(return_value=SimpleNamespace(emissions=boundary, tracing=None)))
    local.owner.llm_factory = Mock(side_effect=RuntimeError('Connection setup failed: partial-runtime-credential\x1b[2J\r'))
    with pytest.raises(RuntimeError) as failure:
        local.owner._runtime(local.config, 'https://localhost:8443', SimpleNamespace(ca_file=Path('/synthetic-ca')))
    text = local.owner.diagnostic(failure.value)
    assert local.owner._session is None
    assert 'Connection setup failed' in text
    assert 'partial-runtime-credential' not in text
    assert '\x1b' not in text and '\r' not in text


def test_diagnostic_scrubs_config_before_runtime_and_registration_after_cleanup(local):
    local.config.values['API_KEY'] = 'configured-credential'
    local.owner._config = local.config
    text = local.owner.diagnostic(ValueError('Preflight failed: configured-credential\x1b[2J'))
    assert 'configured-credential' not in text and 'Preflight failed' in text and '\x1b' not in text
    async def run():
        await local.owner.start()
        local.session.runtime.emissions.register_secret('runtime-credential')
        await local.owner.aclose()
        text = local.owner.diagnostic(RuntimeError('Cleanup failed: runtime-credential configured-credential\r'))
        assert 'runtime-credential' not in text and 'configured-credential' not in text
        assert 'Cleanup failed' in text and '\r' not in text
    asyncio.run(run())


def test_repeated_diagnostics_preserve_latest_boundary_without_registration_growth(local):
    boundary = EmissionBoundary(ContentPolicy())
    secret = 'synthetic"quoted-material'
    boundary.register_secret(secret)
    local.owner._emissions = boundary
    original = frozenset(boundary._secrets)
    for _ in range(6):
        text = local.owner.diagnostic(RuntimeError('Connection failed: ' + secret))
        assert secret not in text and 'Connection failed' in text
        assert frozenset(boundary._secrets) == original
    boundary.register_secret('refreshed-runtime-material')
    text = local.owner.diagnostic(RuntimeError('Connection failed: refreshed-runtime-material'))
    assert 'refreshed-runtime-material' not in text and 'Connection failed' in text
    assert frozenset(boundary._secrets) == original | {'refreshed-runtime-material'}


def test_enabled_tracing_joins_verified_dashboard_before_owned_browser(local, monkeypatch):
    from api_agent.runtime import local_tracing
    local.config.values['TRACE_MODE'] = 'OTEL'
    local.config.values['TRACE_TARGET'] = 'https://collector.example/v1/traces'
    dashboard = SimpleNamespace(state='ready', viewer_url='https://viewer.example', diagnostic=None,
        start=AsyncMock(), aclose=AsyncMock(), interactive_credentials=Mock(return_value=None))
    factory = Mock(return_value=dashboard)
    monkeypatch.setattr(local_tracing, 'LocalTracing', factory)
    async def run():
        snapshot = await local.owner.start()
        factory.assert_called_once_with(local.config.values, emissions=local.session.runtime.emissions)
        dashboard.start.assert_awaited_once()
        assert local.browser_factory.call_args.kwargs['tracing'] is dashboard
        assert [item.name for item in snapshot.destinations] == ['agent', 'tracing', 'flow']
        assert next(item.state for item in snapshot.components if item.name == 'tracing') == 'ready'
        assert local.owner.interactive_dashboard_credentials() is None
        await local.owner.aclose()
        dashboard.aclose.assert_awaited_once()
    asyncio.run(run())


def test_dashboard_start_failure_keeps_cleanup_owned(local, monkeypatch):
    from api_agent.runtime import local_tracing
    dashboard = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError('unavailable')),
        aclose=AsyncMock())
    monkeypatch.setattr(local_tracing, 'LocalTracing', Mock(return_value=dashboard))
    async def run():
        with pytest.raises(RuntimeError, match='unavailable'):
            await local.owner.start()
        dashboard.aclose.assert_awaited_once()
        local.browser_factory.assert_not_called()
        assert local.calls == ['operation-start', 'operation-stop']
    asyncio.run(run())


def test_local_workflow_projection_does_not_apply_process_log_tail_limit(local):
    import json
    from types import MappingProxyType
    from api_agent.runtime.local_diagnostics import LocalDiagnosticProjection
    local.owner._workflow_event(MappingProxyType({'event': 'lifecycle'}))
    assert local.events == []
    local.owner._diagnostic_projection = LocalDiagnosticProjection(full_content=True)
    row = MappingProxyType({'event': 'capture', 'event_id': 'shared-event',
        'direction': 'output', 'values': MappingProxyType({'response_text': 'z' * 20000})})
    local.owner._workflow_event(row)
    emitted = local.events[-1]
    assert emitted.kind == 'diagnostic' and emitted.diagnostic is row
    assert json.loads(emitted.text)['values']['response_text'] == 'z' * 20000
    assert local.owner.interactive_dashboard_credentials() is None
    local.owner._tracing_dashboard = SimpleNamespace(interactive_credentials=lambda: ('admin@example.test', 'literal-password'))
    assert local.owner.interactive_dashboard_credentials() == ('admin@example.test', 'literal-password')
    local.owner.on_event = None
    local.owner._workflow_event(row)


@pytest.mark.parametrize('ready', [True, False])
def test_managed_jaeger_startup_destination_matches_verified_nondefault_viewer(local, monkeypatch, ready):
    from api_agent.runtime import local_tracing
    local.config.values.update(TRACE_MODE='OTEL', TRACE_TARGET='http://127.0.0.1:4318/v1/traces',
                               TRACE_VIEWER_URL='', JAEGER_VIEWER_PORT=17686, RUN_UI=True)
    monkeypatch.setattr(local_tracing, '_probe', Mock(return_value=ready))
    async def run():
        snapshot = await local.owner.start()
        names = [item.name for item in snapshot.destinations]
        assert names == (['agent', 'tracing', 'flow', 'ui'] if ready else ['agent', 'flow', 'ui'])
        assert next(item.state for item in snapshot.components if item.name == 'tracing') == ('ready' if ready else 'unavailable')
        diagnostic_events = [event.text for event in local.events if 'Tracing viewer readiness failed' in event.text]
        assert len(diagnostic_events) == (0 if ready else 1)
        if ready:
            assert snapshot.destinations[1].url == 'http://localhost:17686'
        await local.owner.aclose()
    asyncio.run(run())


def test_custom_otel_missing_viewer_logs_action_and_has_no_destination(local):
    local.config.values.update(TRACE_MODE='OTEL', TRACE_TARGET='http://localhost:3000', TRACE_VIEWER_URL='')
    async def run():
        snapshot = await local.owner.start()
        assert [item.name for item in snapshot.destinations] == ['agent', 'flow']
        assert any('set TRACE_VIEWER_URL' in event.text for event in local.events)
        assert next(item.state for item in snapshot.components if item.name == 'tracing') == 'unavailable'
        assert local.config.values['TRACE_TARGET'] == 'http://localhost:3000'
        await local.owner.aclose()
    asyncio.run(run())


def test_admitted_components_are_visible_before_start_and_during_stop(local):
    local.config.values['RUN_UI'] = True
    local.owner.plan_factory.return_value = SimpleNamespace(
        conditional=(SimpleNamespace(compose_name='extension-db', id='extension/database'),),
        services=('cache', 'extension-db'), profiles=('cache-profile', 'extension-profile'))
    def begin_operation(*args, **kwargs):
        assert local.owner.snapshot.phase == 'starting'
        assert 'profile:extension-profile' in tuple(item.name for item in local.owner.snapshot.components)
        return local.operation
    local.operation_factory.side_effect = begin_operation
    async def run():
        await local.owner.start()
        starting = next(event.snapshot for event in local.events
                        if event.snapshot is not None and event.snapshot.phase == 'starting')
        assert tuple(item.name for item in starting.components) == (
            'agent', 'api', 'ui', 'service:cache', 'service:extension/database',
            'profile:cache-profile', 'profile:extension-profile')
        assert all(item.state == 'starting' for item in starting.components)
        ready = next(event.text for event in local.events if event.kind == 'log' and 'ready' in event.text)
        assert ready.endswith('\n')
        await local.owner.aclose()
        stopping = next(event.snapshot for event in local.events
                        if event.snapshot is not None and event.snapshot.phase == 'stopping')
        assert tuple(item.name for item in stopping.components) == tuple(item.name for item in starting.components)
        assert all(item.state == 'cleanup scope' for item in stopping.components)
    asyncio.run(run())


def test_repeated_cancellation_drains_active_operation_exit_before_return(local):
    entered, released, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    @asynccontextmanager
    async def opening(**options):
        try:
            yield local.session
        finally:
            entered.set()
            await released.wait()
            finished.set()
    local.operation.open = opening
    async def run():
        await local.owner.start()
        closing = asyncio.create_task(local.owner.aclose())
        await entered.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        assert not finished.is_set()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert finished.is_set()
        assert local.owner._closing.done()
        assert local.owner.pending_cleanup is None
    asyncio.run(run())


def test_cancelled_cleanup_wait_preserves_real_cleanup_failure(local):
    entered, released = asyncio.Event(), asyncio.Event()
    async def failing_close():
        entered.set()
        await released.wait()
        raise RuntimeError('owned cleanup failed')
    local.browser.aclose = failing_close
    async def run():
        await local.owner.start()
        closing = asyncio.create_task(local.owner.aclose())
        await entered.wait()
        closing.cancel()
        await asyncio.sleep(0)
        released.set()
        with pytest.raises(CleanupPendingError) as caught:
            await closing
        assert caught.value is local.owner.pending_cleanup
        assert local.owner._closing.done()
        local.browser.aclose = AsyncMock()
        # The stack retained its original bound callback; replace the failed
        # callback explicitly to model its successful next disposal attempt.
        local.owner._stack.callbacks[0] = local.browser.aclose
        await caught.value.retry()
        assert local.calls[-1] == 'operation-stop'
    asyncio.run(run())


def test_cleanup_task_allocation_rejection_retains_owner_and_closes_coroutine(local):
    import inspect
    async def run():
        await local.owner.start()
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()
        rejected = []
        def reject_close(loop, coroutine, **kwargs):
            if coroutine.cr_code.co_qualname == 'LocalRunOwner._close':
                rejected.append(coroutine)
                raise RuntimeError('allocation rejected')
            return asyncio.Task(coroutine, loop=loop, **kwargs)
        loop.set_task_factory(reject_close)
        try:
            with pytest.raises(RuntimeError, match='allocation rejected') as caught:
                await local.owner.aclose()
            assert inspect.getcoroutinestate(rejected[0]) == inspect.CORO_CLOSED
            assert caught.value.cleanup_pending is local.owner.pending_cleanup
            assert local.owner._closing is None
            assert local.calls == ['operation-start', 'browser-start']
        finally:
            loop.set_task_factory(original_factory)
        await caught.value.cleanup_pending.retry()
        assert local.calls[-1] == 'operation-stop'
        assert local.owner.pending_cleanup is None
    asyncio.run(run())


def test_terminal_allocation_rejection_retries_retained_owner_until_acknowledged(local):
    from api_agent.runtime.local_terminal import run_local_terminal
    async def run():
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()
        start = local.owner.start
        rejected = []
        local.owner.CLEANUP_RETRY_SECONDS = .01
        def reject_close(loop, coroutine, **kwargs):
            if coroutine.cr_code.co_qualname == 'LocalRunOwner._close':
                rejected.append(coroutine)
                loop.call_soon(loop.set_task_factory, original_factory)
                raise RuntimeError('allocation rejected')
            return asyncio.Task(coroutine, loop=loop, **kwargs)
        async def start_then_reject():
            result = await start()
            loop.set_task_factory(reject_close)
            return result
        local.owner.start = start_then_reject
        local.owner.wait = AsyncMock(return_value=0)
        try:
            status = await run_local_terminal(None, lambda: SimpleNamespace(values={'RUN_LOG_SOURCE': 'agent'}),
                owner_factory=lambda *args, **kwargs: local.owner,
                output=lambda text: None, diagnostic=lambda text: None)
            assert status == 0
            assert len(rejected) == 1
            assert rejected[0].cr_frame is None
            assert local.owner.pending_cleanup is None
        finally:
            loop.set_task_factory(original_factory)
        assert local.calls[-1] == 'operation-stop'
    asyncio.run(run())


def test_failed_operation_entry_reports_selected_cleanup_scope_without_acquisition(local):
    local.owner.plan_factory.return_value = SimpleNamespace(
        conditional=(), services=('selected-database',), profiles=('selected-profile',))
    local.operation_factory.side_effect = RuntimeError('operation not acquired')
    async def run():
        with pytest.raises(RuntimeError, match='operation not acquired'):
            await local.owner.start()
        stopping = next(event.snapshot for event in local.events
                        if event.snapshot is not None and event.snapshot.phase == 'stopping')
        assert tuple((item.name, item.state) for item in stopping.components) == (
            ('agent', 'cleanup scope'), ('api', 'cleanup scope'),
            ('service:selected-database', 'cleanup scope'), ('profile:selected-profile', 'cleanup scope'))
        assert local.calls == []
        assert local.owner.snapshot.phase == 'stopped'
    asyncio.run(run())


@pytest.mark.parametrize('cancelled', [False, True])
def test_cleanup_attempt_retains_stalled_callback_and_retries_same_task(local, cancelled):
    local.owner.CLEANUP_ATTEMPT_SECONDS = .01
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def stalled():
        calls.append('close')
        entered.set()
        await release.wait()
    async def run():
        await local.owner.start()
        local.owner._stack.push(stalled)
        watchdog = asyncio.get_running_loop().call_later(.2, release.set)
        try:
            attempt = asyncio.create_task(local.owner.aclose())
            await entered.wait()
            if cancelled:
                attempt.cancel()
            with pytest.raises(CleanupPendingError) as caught:
                await attempt
            closing = local.owner._closing
            assert entered.is_set() and not closing.done()
            assert local.owner.snapshot.phase == 'cleanup-pending'
            with pytest.raises(CleanupPendingError):
                await caught.value.retry()
            assert local.owner._closing is closing and calls == ['close']
        finally:
            watchdog.cancel()
            release.set()
            await local.owner.aclose()
        assert local.owner.pending_cleanup is None
        assert local.owner.snapshot.phase == 'stopped'
        assert local.calls[-1] == 'operation-stop'
    asyncio.run(run())


def test_finish_cleanup_retries_visible_pending_until_callback_acknowledges(local):
    local.owner.CLEANUP_ATTEMPT_SECONDS = .01
    local.owner.CLEANUP_RETRY_SECONDS = .01
    entered, release = asyncio.Event(), asyncio.Event()
    async def stalled():
        entered.set()
        await release.wait()
    async def run():
        await local.owner.start()
        local.owner._stack.push(stalled)
        closing = asyncio.create_task(local.owner.finish_cleanup())
        try:
            await entered.wait()
            while local.owner.snapshot.phase != 'cleanup-pending':
                await asyncio.sleep(.001)
            closing.cancel()
            await asyncio.sleep(.02)
            assert not closing.done()
            assert local.owner.pending_cleanup is not None
        finally:
            release.set()
            await closing
        assert local.owner.snapshot.phase == 'stopped'
    asyncio.run(run())


@pytest.mark.parametrize('failure', [RuntimeError('failed disposal'), asyncio.CancelledError()])
def test_finish_cleanup_retries_acknowledged_callback_failure(local, failure):
    local.owner.CLEANUP_RETRY_SECONDS = .001
    callback = AsyncMock(side_effect=[failure, None])
    async def run():
        await local.owner.start()
        local.owner._stack.push(callback)
        await local.owner.finish_cleanup()
        assert callback.await_count == 2
        assert any(event.snapshot is not None and event.snapshot.phase == 'cleanup-pending'
                   for event in local.events)
        assert local.owner.pending_cleanup is None
        assert local.calls[-1] == 'operation-stop'
    asyncio.run(run())


@pytest.mark.parametrize('failure', [RuntimeError('unowned failure'), asyncio.CancelledError()])
def test_finish_cleanup_propagates_error_without_retained_ownership(local, failure):
    local.owner.aclose = AsyncMock(side_effect=failure)
    async def run():
        with pytest.raises(type(failure)) as caught:
            await local.owner.finish_cleanup()
        assert caught.value is failure
        local.owner.aclose.assert_awaited_once()
    asyncio.run(run())


def test_finish_cleanup_preserves_owner_when_cancelled_during_retry_delay(local, monkeypatch):
    local.owner.CLEANUP_ATTEMPT_SECONDS = .01
    local.owner.CLEANUP_RETRY_SECONDS = 10
    retry_entered, release = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep
    callbacks = []
    closing = None
    async def held_cleanup():
        callbacks.append('entered')
        await release.wait()
    async def observed_sleep(delay, result=None):
        if asyncio.current_task() is closing and delay == local.owner.CLEANUP_RETRY_SECONDS:
            retry_entered.set()
        return await real_sleep(delay, result=result)
    async def exercise():
        nonlocal closing
        await local.owner.start()
        local.owner._stack.push(held_cleanup)
        monkeypatch.setattr(asyncio, 'sleep', observed_sleep)
        closing = asyncio.create_task(local.owner.finish_cleanup())
        try:
            await asyncio.wait_for(retry_entered.wait(), timeout=1)
            retained = local.owner._closing
            assert local.owner.pending_cleanup is not None
            closing.cancel()
            await real_sleep(0)
            assert not closing.done()
            assert local.owner._closing is retained
            assert not retained.done()
            assert callbacks == ['entered']
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
            await local.owner.aclose()
        assert closing.result() is None
        assert local.owner.pending_cleanup is None
        assert local.owner.snapshot.phase == 'stopped'
        assert callbacks == ['entered']
        assert local.calls[-1] == 'operation-stop'
    asyncio.run(exercise())
