"""Pytest fixture handoff retains full owners through failure and inspection."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.testing import provider_setup as setup


@pytest.fixture
def fixture_owner(monkeypatch):
    from api_agent.testing import ui_runtime
    session=SimpleNamespace(browser=SimpleNamespace(is_connected=lambda:True),context=SimpleNamespace(pages=[object()]),
        inspection_endpoint=None)
    inspection=SimpleNamespace(sessions=[session],wait_closed=AsyncMock(),aclose=AsyncMock())
    owner=SimpleNamespace(start=AsyncMock(return_value='prepared'),capture=AsyncMock(),
        aclose=AsyncMock(),browser_owner=SimpleNamespace(aclose=AsyncMock()),inspection=inspection)
    factory=Mock(return_value=owner)
    monkeypatch.setattr(ui_runtime,'UIIntegrationOwner',factory)
    monkeypatch.setattr(setup,'_read_snapshot',lambda:{'config':{},'owner_root':'/synthetic'})
    monkeypatch.setattr(setup,'_worker_manager_registry',lambda data:'registry')
    retained=[]
    monkeypatch.setattr(setup,'_inspection_providers',retained)
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('capture_error',[None,RuntimeError('capture failure'),asyncio.CancelledError()])
def test_finalization_retains_original_loop_even_when_capture_fails(fixture_owner,tmp_path,monkeypatch,capture_error):
    r=fixture_owner
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(tmp_path/'events'))
    loop=asyncio.new_event_loop()
    r.owner.capture.side_effect=capture_error
    request=SimpleNamespace(node=SimpleNamespace(_api_agent_ui_outcome='success'))
    fixture=setup.ui_provider_session.__wrapped__(request,loop)
    assert next(fixture)=='prepared'
    if capture_error is not None and isinstance(capture_error,asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):fixture.close()
    else:fixture.close()
    r.owner.capture.assert_awaited_once_with('success')
    assert len(r.retained)==1 and not loop.is_closed()
    r.owner.aclose.assert_not_awaited()
    command=tmp_path/'inspection-command.json'
    command.write_text('{"version":1,"command":"end_inspection","attempt":1}')
    setup.serve_inspections(lambda **row:None)
    assert loop.is_closed()
    r.owner.browser_owner.aclose.assert_awaited_once()
    r.owner.aclose.assert_awaited_once()


def test_setup_failure_capture_precedes_owned_disposal(fixture_owner,monkeypatch):
    r=fixture_owner
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS',raising=False)
    r.owner.inspection=None
    r.owner.start.side_effect=RuntimeError('setup failed')
    order=[]
    async def capture(phase):order.append(('capture',phase))
    async def close():order.append(('close',None))
    r.owner.capture.side_effect=capture
    r.owner.aclose.side_effect=close
    loop=asyncio.new_event_loop()
    fixture=setup.ui_provider_session.__wrapped__(SimpleNamespace(node=SimpleNamespace()),loop)
    with pytest.raises(RuntimeError,match='setup failed'):next(fixture)
    assert order==[('capture','failure'),('close',None)]
    assert loop.is_closed()


@pytest.mark.parametrize('retained',[False,True])
def test_loop_fixture_only_closes_unretained_loops(monkeypatch,retained):
    owners=[]
    monkeypatch.setattr(setup,'_inspection_providers',owners)
    fixture=setup.ui_provider_loop.__wrapped__()
    loop=next(fixture)
    if retained:owners.append(SimpleNamespace(loop=loop))
    fixture.close()
    assert loop.is_closed() is not retained
    if retained:loop.close()


def test_loop_fixture_accepts_prior_owner_disposal(monkeypatch):
    monkeypatch.setattr(setup, '_inspection_providers', [])
    fixture = setup.ui_provider_loop.__wrapped__()
    loop = next(fixture)
    loop.close()
    fixture.close()
    assert loop.is_closed()


@pytest.mark.parametrize('passed,expected', [(True, 'success'), (False, 'failure')])
def test_terminal_call_report_sets_capture_phase(passed, expected):
    node = SimpleNamespace()
    report = SimpleNamespace(when='call', passed=passed)
    hook = setup.pytest_runtest_makereport(node, SimpleNamespace(excinfo=None))
    next(hook)
    with pytest.raises(StopIteration) as finished:
        hook.send(report)
    assert finished.value.value is report
    assert node._api_agent_ui_outcome == expected


@pytest.mark.parametrize('phase', ['setup', 'teardown'])
def test_non_call_reports_preserve_test_capture_phase(phase):
    node = SimpleNamespace(_api_agent_ui_outcome='failure')
    report = SimpleNamespace(when=phase, passed=True)
    hook = setup.pytest_runtest_makereport(node, SimpleNamespace(excinfo=None))
    next(hook)
    with pytest.raises(StopIteration) as finished:
        hook.send(report)
    assert finished.value.value is report
    assert node._api_agent_ui_outcome == 'failure'
