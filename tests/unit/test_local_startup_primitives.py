"""Local read-only status and ordered original-browser startup contracts."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from api_agent.runtime.local_status import LocalStatusApplication, project_status, safe_destination
from api_agent.runtime.local_browser import BorrowedBrowserPage, LocalBrowserOwner


@pytest.mark.parametrize('url', [None, '', 'https://u:p@example.org/', 'https://example.org/?secret=x',
    'https://example.org/#secret', 'file:///tmp/x', 'https://example.org:0', 'https://example.org:bad', 'https://example.org/\n'])
def test_rejects_credential_bearing_or_invalid_destination(url):
    assert safe_destination(url) is None


def test_projection_drops_unknown_data():
    assert project_status({'application':'secret', 'components':{'agent':'ready','ui':'secret'},
        'destinations':{'flow':'http://127.0.0.1:123/r/flow.html'}, 'token':'secret'}) == {
        'application':'Agent', 'components':{'agent':'ready','ui':'unavailable','tracing':'unavailable','flow':'unavailable'},
        'destinations':{'flow':'http://127.0.0.1:123/r/flow.html'}}


@pytest.mark.parametrize('path,method', [('/', 'GET'),('/local-status.json','GET'),('/', 'POST')])
def test_status_bypasses_actual_application(path, method):
    actual = AsyncMock(side_effect=AssertionError('authentication must remain lazy'))
    app = LocalStatusApplication(actual, lambda: {'components':{'agent':'ready'}})
    sent = []
    async def exercise():
        await app({'type':'http','path':path,'method':method}, AsyncMock(), AsyncMock(side_effect=sent.append))
    asyncio.run(exercise())
    actual.assert_not_awaited()
    assert sent[0]['status'] == (200 if method == 'GET' else 405)


def test_status_failure_does_not_expose_diagnostics():
    app = LocalStatusApplication(AsyncMock(), Mock(side_effect=RuntimeError('private secret')))
    sent = []
    asyncio.run(app({'type':'http','path':'/local-status.json','method':'GET'}, AsyncMock(), AsyncMock(side_effect=sent.append)))
    assert json.loads(sent[-1]['body'])['components']['agent'] == 'unavailable'
    assert b'private' not in sent[-1]['body']


def test_other_routes_delegate():
    actual = AsyncMock()
    scope = {'type':'lifespan'}
    receive, send = AsyncMock(), AsyncMock()
    asyncio.run(LocalStatusApplication(actual, lambda: {}) (scope, receive, send))
    actual.assert_awaited_once_with(scope, receive, send)


@pytest.mark.parametrize('run_ui,debug', [(False,False),(False,True),(True,False),(True,True)])
def test_ordered_tabs_use_one_original_browser(run_ui, debug):
    events = []
    def page(name):
        async def goto(url, **kwargs):
            events.append((name,url))
            return SimpleNamespace(status=200)
        return SimpleNamespace(goto=goto)
    home, ui_page, flow = page('home'), page('ui'), page('flow')
    ui_page.bring_to_front = AsyncMock(side_effect=lambda: events.append(('front','ui')))
    context = SimpleNamespace(new_page=AsyncMock(side_effect=[flow,ui_page] if run_ui else [flow]))
    ui_page.context, ui_page.is_closed, ui_page.url = context, lambda: False, 'http://localhost:9443/'
    context.pages = [home, flow, ui_page] if run_ui else [home, flow]
    for retained in (home, flow):
        retained.context, retained.is_closed = context, lambda: False
    session = SimpleNamespace(page=home, context=context, browser=SimpleNamespace(is_connected=lambda: True), inspection_endpoint='http://127.0.0.1:123')
    browser = SimpleNamespace(start=AsyncMock(return_value=session), aclose=AsyncMock())
    factory = Mock(return_value=browser)
    host = SimpleNamespace(entry_url='http://127.0.0.1:124/r/flow.html', aclose=AsyncMock())
    ui = SimpleNamespace(aclose=AsyncMock())
    async def prepare_ui(*, browser_owner):
        borrowed = await browser_owner.start()
        assert borrowed.browser is session.browser and borrowed.context is context
        assert borrowed.page is ui_page
        await borrowed.page.goto('http://localhost:9443')
        ui.session = borrowed
        await browser_owner.aclose()
        browser.aclose.assert_not_awaited()
        return ui
    owner = LocalBrowserOwner(object(), {'RUN_UI':run_ui,'DEBUG':debug,'UI_LISTENER_URL':'http://localhost:9443'}, 'http://localhost:8443',
        prepare_ui=prepare_ui, browser_factory=factory, prepare_view=lambda _: (None,'projection',None,'bundle','sources'),
        host_factory=AsyncMock(return_value=host))
    async def exercise():
        await owner.start()
        assert owner.flow_url == host.entry_url
        if run_ui or debug:
            assert events == [('home','http://localhost:8443'), ('flow',host.entry_url)] + ([('ui','http://localhost:9443'), ('front','ui')] if run_ui else [])
            if run_ui:
                ui_page.bring_to_front.assert_awaited_once()
            factory.assert_called_once_with(inspection=owner.inspection, headless=False)
        else:
            factory.assert_not_called()
        await owner.aclose()
        await owner.aclose()
        with pytest.raises(RuntimeError):
            await owner.start()
    asyncio.run(exercise())
    host.aclose.assert_awaited_once()


def test_default_view_helpers_use_existing_renderer(monkeypatch):
    from api_agent.runtime.local_browser import _prepare_view, _start_viewer_host
    import sys
    prepare, host = Mock(return_value='prepared'), AsyncMock(return_value='host')
    monkeypatch.setitem(sys.modules, 'api_agent.runtime.view_flow', SimpleNamespace(_prepare_view=prepare))
    monkeypatch.setitem(sys.modules, 'api_agent.runtime.viewer_host', SimpleNamespace(start_viewer_host=host))
    assert _prepare_view('selection') == 'prepared'
    assert asyncio.run(_start_viewer_host('bundle', projection='proof')) == 'host'
    prepare.assert_called_once_with('selection')
    host.assert_awaited_once_with('bundle', projection='proof')


@pytest.mark.parametrize('failure', ['missing-ui', 'flow-http', 'flow-no-response', 'ui-retained', 'cleanup', 'ui-replaced'])
def test_startup_failure_releases_owned_resources_and_preserves_retry(failure):
    home = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)))
    flow = SimpleNamespace(goto=AsyncMock(return_value=None if failure == 'flow-no-response' else
        SimpleNamespace(status=503 if failure == 'flow-http' else 200)))
    context = SimpleNamespace(new_page=AsyncMock(return_value=flow))
    browser = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=home, context=context,
        browser=object(), inspection_endpoint=None)), aclose=AsyncMock())
    host = SimpleNamespace(entry_url='http://127.0.0.1:124/r/flow.html', aclose=AsyncMock())
    retained = SimpleNamespace(aclose=AsyncMock())
    failure_error = RuntimeError('Preparation failed')
    failure_error.retained_owners = (retained,)
    prepare = AsyncMock(side_effect=failure_error) if failure in {'ui-retained','cleanup'} else None
    if failure == 'ui-replaced':
        prepare = AsyncMock(return_value=SimpleNamespace(session=object(), aclose=AsyncMock()))
    if failure == 'cleanup':
        retained.aclose.side_effect = [RuntimeError('retry disposal'), None]
    prepared = (None,object(),None,object(),object())
    view = Mock(return_value=prepared)
    host_factory = AsyncMock(return_value=host)
    owner = LocalBrowserOwner(object(), {'RUN_UI':failure in {'missing-ui','ui-retained','cleanup','ui-replaced'},'DEBUG':True},
        'http://localhost:8443', prepare_ui=prepare, browser_factory=Mock(return_value=browser),
        prepare_view=view, host_factory=host_factory)
    async def exercise():
        with pytest.raises(RuntimeError) as caught:
            await owner.start()
        if failure == 'ui-replaced':
            assert str(caught.value) == 'UI preparation replaced the original session'
        if failure == 'cleanup':
            assert caught.value is failure_error
            assert caught.value.retained_owners == (owner,)
            assert isinstance(caught.value.cleanup_error, RuntimeError)
            browser.aclose.assert_not_awaited()
        await owner.aclose()
    asyncio.run(exercise())
    view.assert_called_once_with(owner.context)
    args, kwargs = host_factory.call_args
    assert args[0] is prepared[3] and kwargs['projection'] is prepared[1]
    assert kwargs['repository_sources'] is prepared[4]
    browser.aclose.assert_awaited_once()
    host.aclose.assert_awaited_once()
    assert not owner._failed_owners


@pytest.mark.parametrize('response', [None, SimpleNamespace(status=503)])
def test_home_failure_prevents_later_tabs(response):
    home = SimpleNamespace(goto=AsyncMock(return_value=response))
    context = SimpleNamespace(new_page=AsyncMock())
    browser = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=home, context=context)), aclose=AsyncMock())
    host = SimpleNamespace(entry_url='http://localhost/flow', aclose=AsyncMock())
    owner = LocalBrowserOwner(None, {'DEBUG':True}, 'http://localhost/', browser_factory=Mock(return_value=browser),
        prepare_view=lambda _: (None,None,None,None,None),host_factory=AsyncMock(return_value=host))
    async def exercise():
        with pytest.raises(RuntimeError, match='Agent home did not load'):
            await owner.start()
    asyncio.run(exercise())
    context.new_page.assert_not_awaited()
    host.aclose.assert_awaited_once()


@pytest.mark.parametrize('path', ['/', '/local-status.json'])
def test_actual_application_host_admission_is_preserved(path):
    import httpx
    from api_agent.api.application import Runtime, create_application
    async def exercise():
        app = LocalStatusApplication(create_application(Runtime('synthetic')), lambda: {'components':{'agent':'ready'}})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://untrusted.invalid') as client:
            assert (await client.get(path)).status_code == 400
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
            response = await client.get(path)
            assert response.status_code == 200
            if path == '/':
                assert 'http-equiv="refresh" content="2"' in response.text
    asyncio.run(exercise())


@pytest.mark.parametrize('replacement', ['valid', 'foreign', 'closed', 'origin', 'extra', 'browser', 'context', 'already-closed'])
def test_ui_active_page_handoff(replacement):
    browser = SimpleNamespace(is_connected=lambda: replacement != 'closed')
    context = SimpleNamespace()
    home = SimpleNamespace(context=context,is_closed=lambda:False)
    original = SimpleNamespace(is_closed=lambda: replacement == 'already-closed', close=AsyncMock())
    active = SimpleNamespace(context=object() if replacement == 'foreign' else context,
        is_closed=lambda: False, url='https://localhost:9443/' if replacement != 'origin' else 'https://elsewhere.invalid/')
    flow = SimpleNamespace(context=context,is_closed=lambda:False)
    context.pages = [home,flow,active] + ([object()] if replacement == 'extra' else [])
    session = SimpleNamespace(browser=browser,context=context,page=active)
    owner = LocalBrowserOwner(None, {'UI_LISTENER_URL':'https://localhost:9443'}, 'http://localhost/')
    owner.session = SimpleNamespace(browser=browser,context=context,page=home)
    owner.ui = SimpleNamespace(session=session)
    owner._tab_prefix = [home,flow]
    if replacement == 'browser':
        session.browser = object()
    if replacement == 'context':
        session.context = object()
    async def exercise():
        if replacement in {'valid','already-closed'}:
            await owner._verify_ui(BorrowedBrowserPage(session), original, flow)
            owner._verify_browser()
        else:
            with pytest.raises(RuntimeError):
                await owner._verify_ui(BorrowedBrowserPage(session), original, flow)
                owner._verify_browser()
    asyncio.run(exercise())
    if replacement == 'valid':
        original.close.assert_awaited_once()
    if replacement == 'already-closed':
        original.close.assert_not_awaited()


def test_close_acknowledges_late_host_acquisition():
    async def exercise():
        entered, cancelled, proceed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        host = SimpleNamespace(entry_url='http://localhost/flow', aclose=AsyncMock())
        async def acquire(*args, **kwargs):
            entered.set()
            try:
                await proceed.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await proceed.wait()
            return host
        owner = LocalBrowserOwner(None, {}, 'http://localhost/', prepare_view=lambda _: (None,None,None,None,None),host_factory=acquire)
        starting = asyncio.create_task(owner.start())
        await entered.wait()
        closing = asyncio.create_task(owner.aclose())
        await cancelled.wait()
        assert not closing.done()
        proceed.set()
        await closing
        with pytest.raises(RuntimeError, match='closed during preparation'):
            await starting
        host.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_close_reports_pending_until_startup_acknowledges(monkeypatch):
    from api_agent.providers.control import CleanupPendingError
    async def exercise():
        entered, proceed = asyncio.Event(), asyncio.Event()
        host = SimpleNamespace(entry_url='http://localhost/flow', aclose=AsyncMock())
        async def acquire(*args, **kwargs):
            entered.set()
            try:
                await proceed.wait()
            except asyncio.CancelledError:
                await proceed.wait()
            return host
        owner = LocalBrowserOwner(None, {}, 'http://localhost/', prepare_view=lambda _: (None,None,None,None,None),host_factory=acquire)
        starting = asyncio.create_task(owner.start())
        await entered.wait()
        original_wait = asyncio.wait
        monkeypatch.setattr(asyncio, 'wait', AsyncMock(return_value=(set(),{starting})))
        with pytest.raises(CleanupPendingError):
            await owner.aclose()
        monkeypatch.setattr(asyncio, 'wait', original_wait)
        assert not starting.done()
        proceed.set()
        with pytest.raises(RuntimeError, match='closed during preparation'):
            await starting
        await owner.aclose()
        host.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_closed_during_final_navigation_never_reports_ready():
    async def exercise():
        entered, proceed = asyncio.Event(), asyncio.Event()
        async def goto(*args, **kwargs):
            entered.set()
            try:
                await proceed.wait()
            except asyncio.CancelledError:
                await proceed.wait()
            return SimpleNamespace(status=200)
        flow = SimpleNamespace(goto=goto)
        home = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)))
        browser = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=home,context=SimpleNamespace(new_page=AsyncMock(return_value=flow)))),aclose=AsyncMock())
        host = SimpleNamespace(entry_url='http://localhost/flow',aclose=AsyncMock())
        owner = LocalBrowserOwner(None, {'DEBUG':True}, 'http://localhost/',browser_factory=Mock(return_value=browser),
            prepare_view=lambda _: (None,None,None,None,None),host_factory=AsyncMock(return_value=host))
        starting = asyncio.create_task(owner.start())
        await entered.wait()
        owner.closed = True
        proceed.set()
        with pytest.raises(RuntimeError, match='closed during preparation'):
            await starting
        host.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_closed_during_flow_navigation_does_not_allocate_ui_tab():
    async def exercise():
        entered, proceed = asyncio.Event(), asyncio.Event()
        async def goto_flow(*args, **kwargs):
            entered.set()
            try:
                await proceed.wait()
            except asyncio.CancelledError:
                await proceed.wait()
            return SimpleNamespace(status=200)
        home = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)))
        flow = SimpleNamespace(goto=goto_flow)
        context = SimpleNamespace(new_page=AsyncMock(return_value=flow))
        browser = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(page=home,context=context)),aclose=AsyncMock())
        host = SimpleNamespace(entry_url='http://localhost/flow',aclose=AsyncMock())
        prepare_ui = AsyncMock()
        owner = LocalBrowserOwner(None,{'RUN_UI':True},'http://localhost/',prepare_ui=prepare_ui,
            browser_factory=Mock(return_value=browser),prepare_view=lambda _:(None,None,None,None,None),
            host_factory=AsyncMock(return_value=host))
        starting = asyncio.create_task(owner.start())
        await entered.wait()
        closing = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        proceed.set()
        await closing
        with pytest.raises(RuntimeError, match='closed during preparation'):
            await starting
        context.new_page.assert_awaited_once()
        prepare_ui.assert_not_awaited()
        browser.aclose.assert_awaited_once()
        host.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_closed_during_ui_focus_never_reports_ready():
    async def exercise():
        home = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)))
        flow = SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200)))
        context = SimpleNamespace()
        ui_page = SimpleNamespace(context=context,is_closed=lambda:False,url='http://localhost:9443/')
        context.pages = [home,flow,ui_page]
        for retained in (home,flow):
            retained.context, retained.is_closed = context, lambda: False
        context.new_page = AsyncMock(side_effect=[flow,ui_page])
        browser_handle = SimpleNamespace(is_connected=lambda:True)
        session = SimpleNamespace(page=home,context=context,browser=browser_handle,inspection_endpoint=None)
        browser = SimpleNamespace(start=AsyncMock(return_value=session),aclose=AsyncMock())
        host = SimpleNamespace(entry_url='http://localhost/flow',aclose=AsyncMock())
        ui = SimpleNamespace(aclose=AsyncMock())
        async def prepare_ui(*,browser_owner):
            ui.session = await browser_owner.start()
            return ui
        owner = LocalBrowserOwner(None,{'RUN_UI':True,'UI_LISTENER_URL':'http://localhost:9443'},
            'http://localhost/',prepare_ui=prepare_ui,browser_factory=Mock(return_value=browser),
            prepare_view=lambda _:(None,None,None,None,None),host_factory=AsyncMock(return_value=host))
        async def focus():
            owner.closed = True
        ui_page.bring_to_front = focus
        with pytest.raises(RuntimeError, match='closed during preparation'):
            await owner.start()
        ui.aclose.assert_awaited_once()
        browser.aclose.assert_awaited_once()
        host.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_original_ui_active_page_must_match_configured_origin():
    browser = SimpleNamespace(is_connected=lambda:True)
    context = SimpleNamespace()
    home = SimpleNamespace(context=context,is_closed=lambda:False)
    page = SimpleNamespace(context=context,is_closed=lambda:False,url='https://foreign.invalid/')
    flow = SimpleNamespace(context=context,is_closed=lambda:False)
    context.pages = [home,flow,page]
    session = SimpleNamespace(browser=browser,context=context,page=page)
    owner = LocalBrowserOwner(None,{'UI_LISTENER_URL':'https://localhost:9443'},'https://localhost/')
    owner.session = SimpleNamespace(browser=browser,context=context,page=home)
    owner.ui = SimpleNamespace(session=session)
    owner._tab_prefix = [home,flow]
    with pytest.raises(RuntimeError, match='unexpected origin'):
        asyncio.run(owner._verify_ui(BorrowedBrowserPage(session),page,flow))


def test_concurrent_closes_cancel_startup_once_and_acknowledge_disposal():
    async def exercise():
        entered, disposal_entered, allow_disposal = asyncio.Event(), asyncio.Event(), asyncio.Event()
        disposed = False
        async def dispose():
            nonlocal disposed
            disposal_entered.set()
            await allow_disposal.wait()
            disposed = True
        host = SimpleNamespace(entry_url='http://localhost/flow',aclose=dispose)
        async def acquire(*args,**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await host.aclose()
                raise
        owner = LocalBrowserOwner(None,{},'https://localhost/',prepare_view=lambda _:(None,None,None,None,None),host_factory=acquire)
        starting = asyncio.create_task(owner.start())
        await entered.wait()
        first = asyncio.create_task(owner.aclose())
        await disposal_entered.wait()
        second = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert starting.cancelling() == 1
        assert not first.done() and not second.done() and not disposed
        allow_disposal.set()
        await asyncio.gather(first,second)
        assert disposed and starting.cancelled()
        assert starting.cancelling() == 1
    asyncio.run(exercise())


@pytest.mark.parametrize('configured,actual', [
    ('https://localhost:443','https://localhost/'),
    ('https://localhost','https://localhost:443/'),
    ('http://localhost:80','http://localhost/'),
    ('http://localhost','http://localhost:80/'),
])
def test_ui_origin_normalizes_explicit_default_port(configured, actual):
    browser = SimpleNamespace(is_connected=lambda:True)
    context = SimpleNamespace()
    home = SimpleNamespace(context=context,is_closed=lambda:False)
    page = SimpleNamespace(context=context,is_closed=lambda:False,url=actual)
    flow = SimpleNamespace(context=context,is_closed=lambda:False)
    context.pages = [home,flow,page]
    session = SimpleNamespace(browser=browser,context=context,page=page)
    owner = LocalBrowserOwner(None,{'UI_LISTENER_URL':configured},'http://localhost/')
    owner.session = SimpleNamespace(browser=browser,context=context,page=home)
    owner.ui = SimpleNamespace(session=session)
    owner._tab_prefix = [home,flow]
    asyncio.run(owner._verify_ui(BorrowedBrowserPage(session),page,flow))


def test_status_projects_bounded_qualified_services_and_verified_endpoints():
    snapshot = {'components':{'agent':'ready','service:sample/database':'ready','SECRET':'ready',
        'service:sample/other':'raw error', 'service:Bad/id':'ready', 4:'ready'},
        'destinations':{'service:sample/database:console':'https://localhost:9443/',
        'service:sample/database:secret':'https://localhost/?token=secret',
        'service:sample/missing:console':'https://localhost/', 'service:bad/<script>:url':'https://localhost/',4:'https://localhost/'}}
    projected = project_status(snapshot)
    assert projected['components'] == {'agent':'ready','ui':'unavailable','tracing':'unavailable','flow':'unavailable',
        'service:sample/database':'ready'}
    assert projected['destinations'] == {'service:sample/database:console':'https://localhost:9443/'}


def test_status_caps_service_and_endpoint_declaration_totals():
    snapshot = {'components':{f'service:owner/s{i}':'ready' for i in range(40)},
        'destinations':{f'service:owner/s{i}:e{j}':'http://localhost/' for i in range(32) for j in range(20)}}
    projected = project_status(snapshot)
    assert len(projected['components']) == 36
    assert all(sum(name.startswith(f'service:owner/s{i}:') for name in projected['destinations']) <= 16 for i in range(32))
    assert len(projected['destinations']) <= 512
    assert 'service:owner/s32' not in projected['components']


def test_service_links_are_visible_in_friendly_home():
    sent = []
    app = LocalStatusApplication(AsyncMock(), lambda: {'components':{'service:sample/database':'ready'},
        'destinations':{'service:sample/database:console':'https://localhost:9443/'}})
    asyncio.run(app({'type':'http','path':'/','method':'GET'},AsyncMock(),AsyncMock(side_effect=sent.append)))
    assert b'service:sample/database: ready' in sent[-1]['body']
    assert b'>service:sample/database:console</a>' in sent[-1]['body']


@pytest.mark.parametrize('run_ui,debug',[(False,False),(False,True),(True,False)])
def test_tracing_tab_between_home_and_actual_flow(run_ui,debug):
    home,tracing_page,flow,ui_page=(SimpleNamespace(goto=AsyncMock(return_value=SimpleNamespace(status=200))) for _ in range(4))
    context=SimpleNamespace(new_page=AsyncMock(side_effect=[tracing_page,flow,ui_page]),pages=[home,tracing_page,flow,ui_page] if run_ui else [home,tracing_page,flow])
    for retained in (home,tracing_page,flow):
        retained.context, retained.is_closed = context, lambda: False
    browser=SimpleNamespace(is_connected=lambda:True)
    session=SimpleNamespace(page=home,context=context,browser=browser,inspection_endpoint=None)
    ui_page.context=context;ui_page.is_closed=lambda:False;ui_page.url='http://localhost:9443';ui_page.bring_to_front=AsyncMock()
    owner_browser=SimpleNamespace(start=AsyncMock(return_value=session),aclose=AsyncMock())
    from api_agent.runtime.local_tracing import LocalTracing
    tracing = LocalTracing({'TRACE_MODE': 'OTEL', 'TRACE_TARGET': 'http://127.0.0.1:4318/v1/traces',
                            'TRACE_VIEWER_URL': '', 'JAEGER_VIEWER_PORT': 17686},
                           emissions=Mock(), probe=Mock(return_value=True))
    tracing_page.url = 'http://localhost:17686'
    tracing_page.route = AsyncMock()
    async def prepare_ui(*,browser_owner):
        return SimpleNamespace(session=await browser_owner.start(),aclose=AsyncMock())
    owner=LocalBrowserOwner(object(),{'RUN_UI':run_ui,'DEBUG':debug,'UI_LISTENER_URL':'http://localhost:9443'},'http://localhost:8443',
        tracing=tracing,prepare_ui=prepare_ui,browser_factory=Mock(return_value=owner_browser),
        prepare_view=lambda context:(None,None,None,None,None),
        host_factory=AsyncMock(return_value=SimpleNamespace(entry_url='http://localhost:123/flow',aclose=AsyncMock())))
    async def exercise():
        await tracing.start()
        await owner.start()
        if run_ui or debug:
            tracing_page.goto.assert_awaited_once_with('http://localhost:17686', wait_until='domcontentloaded')
            assert owner._tab_prefix==[home,tracing_page,flow]
        else:
            tracing_page.goto.assert_not_awaited()
        await owner.aclose()
        await tracing.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize('failure',['missing-home','popup','order','disconnected','closed-home','closed-dashboard','closed-flow','foreign-page'])
def test_debug_only_validates_original_pages_before_handoff(failure):
    context=SimpleNamespace()
    def page():
        return SimpleNamespace(context=context,is_closed=lambda:False,
            goto=AsyncMock(return_value=SimpleNamespace(status=200)))
    home,dashboard,flow=page(),page(),page()
    context.pages=[home,dashboard,flow]
    context.new_page=AsyncMock(side_effect=[dashboard,flow])
    browser=SimpleNamespace(is_connected=lambda:failure!='disconnected')
    session=SimpleNamespace(page=home,context=context,browser=browser,inspection_endpoint=None)
    browser_owner=SimpleNamespace(start=AsyncMock(return_value=session),aclose=AsyncMock())
    async def login(page):
        if failure=='missing-home': context.pages=[dashboard,flow]
        if failure=='popup': context.pages.append(object())
        if failure=='order': context.pages=[dashboard,home,flow]
        if failure.startswith('closed-'):
            {'closed-home':home,'closed-dashboard':dashboard,'closed-flow':flow}[failure].is_closed=lambda:True
        if failure=='foreign-page': dashboard.context=object()
    host=SimpleNamespace(entry_url='http://localhost:12345/flow',aclose=AsyncMock())
    owner=LocalBrowserOwner(None,{'DEBUG':True,'RUN_UI':False},'http://localhost:8443',
        tracing=SimpleNamespace(viewer_url='http://localhost:3000',login=login),
        browser_factory=Mock(return_value=browser_owner),prepare_view=lambda _:(None,None,None,None,None),
        host_factory=AsyncMock(return_value=host))
    async def exercise():
        with pytest.raises(RuntimeError,match='Local startup'):
            await owner.start()
        browser_owner.aclose.assert_awaited_once()
        host.aclose.assert_awaited_once()
    asyncio.run(exercise())


def test_local_browser_acknowledges_only_proven_terminated_inspection():
    from api_agent.providers.control import CleanupPendingError
    owner = LocalBrowserOwner(None, {}, 'http://localhost/')
    pending = CleanupPendingError((owner.inspection,), owner.inspection.aclose)
    inspection = SimpleNamespace(aclose=AsyncMock(side_effect=pending),
        settle_terminated_inspection=AsyncMock(return_value=True))
    host = SimpleNamespace(aclose=AsyncMock())
    owner.inspection, owner.host = inspection, host
    asyncio.run(owner.aclose())
    inspection.aclose.assert_awaited_once()
    inspection.settle_terminated_inspection.assert_awaited_once()
    host.aclose.assert_awaited_once()
    assert owner.inspection is owner.host is None


def test_local_browser_keeps_unconfirmed_inspection_and_host_owned():
    from api_agent.providers.control import CleanupPendingError
    owner = LocalBrowserOwner(None, {}, 'http://localhost/')
    pending = CleanupPendingError((owner.inspection,), owner.inspection.aclose)
    inspection = SimpleNamespace(aclose=AsyncMock(side_effect=pending),
        settle_terminated_inspection=AsyncMock(return_value=False))
    host = SimpleNamespace(aclose=AsyncMock())
    owner.inspection, owner.host = inspection, host
    with pytest.raises(CleanupPendingError) as caught:
        asyncio.run(owner.aclose())
    assert caught.value is pending
    inspection.settle_terminated_inspection.assert_awaited_once()
    host.aclose.assert_not_awaited()
    assert owner.inspection is inspection and owner.host is host


@pytest.mark.parametrize('failed', ['ui', 'browser', 'host'])
def test_other_local_browser_cleanup_failure_is_not_terminal_browser_proof(failed):
    from api_agent.providers.control import CleanupPendingError
    owner = LocalBrowserOwner(None, {}, 'http://localhost/')
    inspection = SimpleNamespace(aclose=AsyncMock(), settle_terminated_inspection=AsyncMock(return_value=True))
    owned = SimpleNamespace(aclose=AsyncMock())
    pending = CleanupPendingError((owned,), owned.aclose)
    owned.aclose.side_effect = pending
    owner.inspection = inspection
    setattr(owner, failed, owned)
    with pytest.raises(CleanupPendingError) as caught:
        asyncio.run(owner.aclose())
    assert caught.value is pending
    inspection.settle_terminated_inspection.assert_not_awaited()
    assert getattr(owner, failed) is owned
