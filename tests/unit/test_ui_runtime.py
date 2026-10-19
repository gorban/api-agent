"""UI-only integration services retain original browser and helper lifetimes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from api_agent.testing import ui_runtime
from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.control import CleanupPendingError


@pytest.fixture
def operation(tmp_path, monkeypatch):
    browser = SimpleNamespace(session=None, aclose=AsyncMock())
    browser_factory = Mock(return_value=browser)
    monkeypatch.setattr(ui_runtime,'managed_browser',browser_factory)
    processes = Mock(return_value=object())
    monkeypatch.setattr(ui_runtime,'StudioProcesses',processes)
    monkeypatch.setattr(ui_runtime,'verified_client_context',Mock(return_value='verified-tls'))
    prepared = SimpleNamespace(start=AsyncMock(), aclose=AsyncMock(), provider=None)
    prepared_factory = Mock(return_value=prepared)
    monkeypatch.setattr(ui_runtime,'PreparedUISession',prepared_factory)
    def create(**config):
        return ui_runtime.UIIntegrationOwner({'config':{'UI_LISTENER_URL':'http://127.0.0.1:9443', **config},
            'owner_root':str(tmp_path)}, None)
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('origin', ['http://127.0.0.1:9443','http://localhost:9443','http://[::1]:9443'])
def test_selected_ui_uses_own_ephemeral_listener_and_injected_services(operation, origin):
    r=operation
    owner=r.create(UI_LISTENER_URL=origin, UI_PROVIDER='selected-ui')
    async def exercise():
        assert await owner.start() is r.prepared
        values, services = r.prepared_factory.call_args.args
        from urllib.parse import urlsplit
        supplied, original = urlsplit(values['UI_LISTENER_URL']), urlsplit(origin)
        assert (supplied.scheme,supplied.hostname) == (original.scheme,original.hostname)
        assert supplied.port != original.port and supplied.port > 0
        assert 'llm' not in services
        assert services['runtime_values'] is values
        assert services['tls_material'] is None
        assert services['ssl_context'] == 'verified-tls'
        assert r.prepared_factory.call_args.kwargs['browser_owner'] is r.browser
        await owner.aclose()
        r.prepared.aclose.assert_awaited_once()
        r.browser.aclose.assert_not_awaited()
    asyncio.run(exercise())


def test_https_tls_task_remains_owned_after_cancel(operation, monkeypatch):
    from threading import Event
    entered, release = Event(), Event()
    material=SimpleNamespace(ca_file='ca')
    def tls(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(material=material)
    monkeypatch.setattr(ui_runtime,'prepare_local_https',tls)
    owner=operation.create(UI_LISTENER_URL='https://127.0.0.1:9443')
    async def exercise():
        task=asyncio.create_task(owner.start())
        while not entered.is_set(): await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert owner._tls_task is not None and not owner._tls_task.done()
        release.set()
        await owner.aclose()
        assert owner._tls_task.done()
        operation.browser.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_https_material_used_before_provider_launch(operation,monkeypatch):
    material=SimpleNamespace(ca_file='ca')
    monkeypatch.setattr(ui_runtime,'prepare_local_https',Mock(return_value=SimpleNamespace(material=material)))
    owner=operation.create(UI_LISTENER_URL='https://127.0.0.1:9443',NO_STD_IN=True)
    async def exercise():
        await owner.start()
        assert operation.prepared_factory.call_args.args[1]['tls_material'] is material
        ui_runtime.verified_client_context.assert_called_once_with('ca')
        assert ui_runtime.prepare_local_https.call_args.kwargs['interactive'] is False
        await owner.aclose()
    asyncio.run(exercise())


def test_content_off_never_inspects_browser(operation):
    owner=operation.create(LOG_CONTENT=False)
    class Forbidden:
        @property
        def session(self): raise AssertionError('content off accessed browser')
    owner.browser_owner=Forbidden()
    asyncio.run(owner.capture('failure'))


@pytest.mark.parametrize('capability,session', [(False,False),(False,True),(True,True)])
def test_content_capture_uses_only_advertised_policy(operation,monkeypatch,capability,session):
    from api_agent.testing import browser_evidence, browser_attachments
    capture=AsyncMock()
    monkeypatch.setattr(browser_evidence,'capture_ui_evidence',capture)
    publish=Mock()
    monkeypatch.setattr(browser_attachments,'publish_attachment',publish)
    owner=operation.create(LOG_CONTENT=True)
    context=object()
    if session: operation.browser.session=SimpleNamespace(context=context)
    provider=SimpleNamespace(implementation=SimpleNamespace(capabilities=ProviderCapabilities('ui',('capture_evidence',) if capability else ())),
        require=Mock(return_value=SimpleNamespace(acapture_evidence=AsyncMock(return_value='policy-result'))))
    owner.prepared=SimpleNamespace(provider=provider)
    async def exercise():
        await owner.capture('failure')
        args,kw=capture.call_args
        assert args[0] == ((context,) if session else ())
        assert args[2] is publish and kw['phase']=='failure'
        if capability:
            assert await kw['capture_policy']('page','boundary') == 'policy-result'
            provider.require.assert_called_once_with('ui','capture_evidence')
        else: assert kw['capture_policy'] is None
    asyncio.run(exercise())


def test_browser_inspection_endpoint_printing(capsys):
    ui_runtime.UIIntegrationOwner._show_browser(SimpleNamespace(inspection_endpoint=None))
    assert capsys.readouterr().out == ''
    ui_runtime.UIIntegrationOwner._show_browser(SimpleNamespace(inspection_endpoint='http://127.0.0.1:1234'))
    assert 'http://127.0.0.1:1234' in capsys.readouterr().out


def test_role_binding_delegation_preserves_registry_and_index_source(operation,monkeypatch):
    binder=Mock(return_value='scoped')
    monkeypatch.setattr(ui_runtime,'bind_role_credentials',binder)
    owner=operation.create()
    owner.index_source='dotenv'
    assert owner._credentials_for(('UI User',),2)=='scoped'
    assert binder.call_args.args == (owner.config,('UI User',),2)
    assert binder.call_args.kwargs['registry'] is owner.registry
    assert binder.call_args.kwargs['index_source']=='dotenv'


def test_cleanup_settles_helpers_and_interaction(operation):
    owner=operation.create()
    owner.interaction=SimpleNamespace(cancel=Mock(),close=Mock())
    async def exercise():
        done=asyncio.create_task(asyncio.sleep(0))
        cancelled=asyncio.create_task(asyncio.sleep(0))
        cancelled.cancel()
        owner.pending_tasks.update((done,cancelled))
        await owner.aclose()
        assert done.done() and cancelled.cancelled()
        owner.interaction.cancel.assert_called_once()
        owner.interaction.close.assert_called_once()
    asyncio.run(exercise())


def test_pending_helper_cleanup_retains_retry_duty(operation,monkeypatch):
    owner=operation.create()
    original=ui_runtime.report_timeout
    monkeypatch.setattr(ui_runtime,'report_timeout',lambda name,timeout: original(name,.01))
    async def exercise():
        release=asyncio.Event()
        task=asyncio.create_task(release.wait())
        owner.pending_tasks.add(task)
        with pytest.raises(CleanupPendingError) as caught:
            await owner.aclose()
        assert not task.done()
        release.set()
        await caught.value.retry()
        assert task.done()
    asyncio.run(exercise())


def test_start_is_one_shot_and_closed_owner_cannot_relaunch(operation):
    from api_agent.providers.contracts import SessionClosedError
    owner=operation.create()
    async def exercise():
        await owner.start()
        with pytest.raises(SessionClosedError): await owner.start()
        assert operation.prepared_factory.call_count==1
        await owner.aclose()
        other=operation.create()
        await other.aclose()
        with pytest.raises(SessionClosedError): await other.start()
    asyncio.run(exercise())


def test_close_during_tls_preparation_prevents_late_provider_launch(operation,monkeypatch):
    from threading import Event
    entered, release = Event(), Event()
    def tls(*args,**kwargs):
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(material=SimpleNamespace(ca_file='ca'))
    monkeypatch.setattr(ui_runtime,'prepare_local_https',tls)
    owner=operation.create(UI_LISTENER_URL='https://127.0.0.1:9443')
    async def exercise():
        task=asyncio.create_task(owner.start())
        while not entered.is_set(): await asyncio.sleep(.001)
        closing=asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        release.set()
        await closing
        with pytest.raises(asyncio.CancelledError): await task
        operation.prepared_factory.assert_not_called()
    asyncio.run(exercise())


def test_closed_state_rechecked_after_external_tls_work(operation,monkeypatch):
    from api_agent.providers.contracts import SessionClosedError
    owner=operation.create(UI_LISTENER_URL='https://127.0.0.1:9443')
    def tls(*args,**kwargs):
        owner.closed=True
        return SimpleNamespace(material=SimpleNamespace(ca_file='ca'))
    monkeypatch.setattr(ui_runtime,'prepare_local_https',tls)
    async def exercise():
        with pytest.raises(SessionClosedError): await owner.start()
        operation.prepared_factory.assert_not_called()
        await owner.aclose()
    asyncio.run(exercise())


def test_repeated_close_preserves_startup_drain_and_cannot_handoff_closed_owner(operation,monkeypatch):
    from api_agent.providers.contracts import SessionClosedError
    owner=operation.create()
    original=ui_runtime.report_timeout
    monkeypatch.setattr(ui_runtime,'report_timeout',lambda name,timeout:original(name,.01))
    async def exercise():
        entered,release=asyncio.Event(),asyncio.Event()
        cancellations=[]
        async def prepare():
            entered.set()
            try: await release.wait()
            except asyncio.CancelledError:
                cancellations.append(1)
                await release.wait()
        operation.prepared.start.side_effect=prepare
        starter=asyncio.create_task(owner.start())
        await entered.wait()
        with pytest.raises(CleanupPendingError):await owner.aclose()
        with pytest.raises(CleanupPendingError):await owner.aclose()
        assert cancellations==[1] and not starter.done()
        operation.prepared.aclose.assert_not_awaited()
        release.set()
        with pytest.raises(SessionClosedError):await starter
        await owner.aclose()
        operation.prepared.aclose.assert_awaited_once()
    asyncio.run(exercise())


@pytest.mark.parametrize('config,headless', [({},True),({'RUN_UI':True},True),({'DEBUG':True},False)])
def test_browser_visibility_uses_shared_owner(operation,config,headless):
    owner=operation.create(**config)
    assert operation.browser_factory.call_args.kwargs == {'inspection':owner.inspection,'headless':headless}


@pytest.mark.parametrize('value',[False,None,1,'false','true'])
def test_capture_does_not_read_state_without_boolean_content_consent(operation,value):
    owner=operation.create(LOG_CONTENT=value)
    class Forbidden:
        @property
        def session(self): raise AssertionError('capture accessed withheld browser')
    owner.browser_owner=Forbidden()
    asyncio.run(owner.capture('failure'))


def test_backend_binding_uses_effective_origin_and_lives_until_ui_disposal(operation):
    from contextlib import asynccontextmanager
    order=[]
    @asynccontextmanager
    async def backend(values):
        order.append(('backend',values['UI_LISTENER_URL']))
        yield {'agent_url':'http://127.0.0.1:8123','access_token':'x'*32}
        order.append(('backend-close',None))
    owner=ui_runtime.UIIntegrationOwner({'config':{'UI_LISTENER_URL':'http://localhost:9443'},
        'owner_root':str(operation.tmp_path)},None,backend=backend)
    async def close(): order.append(('ui-close',None))
    operation.prepared.aclose.side_effect=close
    async def exercise():
        await owner.start()
        values,services=operation.prepared_factory.call_args.args
        assert order==[('backend',values['UI_LISTENER_URL'])]
        assert values['UI_LISTENER_URL']!='http://localhost:9443'
        assert values['AGENT_DJANGO_URL']=='http://127.0.0.1:8123'
        assert services['runtime_values'] is values
        assert services['ui_access_token']=='x'*32
        assert owner.emissions.content_export({'text': 'x'*32}) != {'text': 'x'*32}
        await owner.aclose()
        assert order[-2:]==[('ui-close',None),('backend-close',None)]
        await owner.aclose()
        assert order.count(('backend-close',None))==1
    asyncio.run(exercise())


@pytest.mark.parametrize('binding', [None,{}, {'agent_url':'http://127.0.0.1:8123','access_token':'x'*32,'override':True},
    {'agent_url':'https://remote.example','access_token':'x'*32},
    *[{'agent_url':'http://127.0.0.1:8123','access_token':token} for token in [None,31*'x',257*'x',32*'é',32*'\n']]])
def test_backend_binding_rejects_extra_services_remote_urls_and_invalid_nonce(operation,binding):
    from contextlib import asynccontextmanager
    closed=[]
    @asynccontextmanager
    async def backend(values):
        try: yield binding
        finally: closed.append(True)
    owner=ui_runtime.UIIntegrationOwner({'config':{'UI_LISTENER_URL':'http://localhost:9443'},
        'owner_root':str(operation.tmp_path)},None,backend=backend)
    async def exercise():
        with pytest.raises(ValueError): await owner.start()
        operation.prepared_factory.assert_not_called()
        await owner.aclose()
        assert closed==[True]
    asyncio.run(exercise())


def test_backend_pending_cleanup_is_retried_after_context_exit(operation):
    retry=AsyncMock()
    pending=CleanupPendingError(('backend',),retry)
    backend=SimpleNamespace(__aenter__=AsyncMock(return_value={'agent_url':'http://127.0.0.1:8123','access_token':'x'*32}),
        __aexit__=AsyncMock(side_effect=pending))
    owner=ui_runtime.UIIntegrationOwner({'config':{'UI_LISTENER_URL':'http://localhost:9443'},
        'owner_root':str(operation.tmp_path)},None,backend=lambda values:backend)
    async def exercise():
        await owner.start()
        with pytest.raises(CleanupPendingError): await owner.aclose()
        await owner.aclose()
        backend.__aexit__.assert_awaited_once()
        retry.assert_awaited_once()
        assert owner.backend is None
    asyncio.run(exercise())


def test_backend_closing_during_enter_cannot_launch_ui(operation):
    from contextlib import asynccontextmanager
    from api_agent.providers.contracts import SessionClosedError
    @asynccontextmanager
    async def backend(values):
        owner.closed=True
        yield {'agent_url':'http://127.0.0.1:8123','access_token':'x'*32}
    owner=ui_runtime.UIIntegrationOwner({'config':{'UI_LISTENER_URL':'http://localhost:9443'},
        'owner_root':str(operation.tmp_path)},None,backend=backend)
    async def exercise():
        with pytest.raises(SessionClosedError): await owner.start()
        operation.prepared_factory.assert_not_called()
        await owner.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('mode', ['integration','e2e'])
@pytest.mark.parametrize('run_ui', [False,True])
@pytest.mark.parametrize('debug', [False,True])
@pytest.mark.parametrize('legacy', [False,True])
def test_ui_test_visibility_and_retention_matrix(operation,mode,run_ui,debug,legacy):
    owner = operation.create(TUI_MODE=mode,RUN_UI=run_ui,DEBUG=debug,E2E_DEBUG=legacy)
    headed = debug or mode == 'e2e' and legacy
    assert operation.browser_factory.call_args.kwargs['headless'] is not headed
    assert (owner.inspection is not None) is headed
    assert operation.browser_factory.call_args.kwargs['inspection'] is owner.inspection
