"""Optional selected-account snapshots are bounded, private and reset-aware."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest
from api_agent.llm_providers.gpt.quota import read_snapshot
from api_agent.telemetry.usage import SuiteUsage, QuotaSnapshot, QuotaReadError
from api_agent.testing.quota_events import capture, publish, validate_snapshot

NOW=datetime.now(timezone.utc)
RESET=int((NOW+timedelta(days=3)).timestamp())
TOKEN='header.'+base64.urlsafe_b64encode(json.dumps({'sub':'selected-user'}).encode()).decode().rstrip('=')+'.signature'
WINDOW={'used_percent':25,'reset_at':RESET,'limit_window_seconds':604800}


@pytest.mark.parametrize('data,status,expected', [
    ({'rate_limit':{'primary_window':WINDOW}},200,25),
    ({'rate_limit':{'secondary_window':WINDOW}},200,25),
    ({'rate_limit':{'primary_window':dict(WINDOW,limit_window_seconds=18000)}},200,'window'),
    ({'rate_limit':{'secondary_window':dict(WINDOW,used_percent=101)}},200,'response'),
    ({'rate_limit':{'secondary_window':dict(WINDOW,reset_at='invalid')}},200,'response'),
    ({'rate_limit':{'secondary_window':dict(WINDOW,reset_at=10**100)}},200,'request'),
    ({},200,'response'),([] ,200,'response'),({'rate_limit':[]},200,'response'),({},401,'http')])
def test_weekly_window_only_uses_selected_token(data,status,expected):
    requests=[]
    def respond(request):
        requests.append(request)
        return httpx.Response(status,json=data)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=await read_snapshot(client,TOKEN)
        assert requests[0].url=='https://chatgpt.com/backend-api/wham/usage'
        assert requests[0].headers['Authorization']=='Bearer '+TOKEN
        if type(expected) is str:
            assert isinstance(result, QuotaReadError)
            assert result.code == expected
            if status != 200:
                assert result.status == status
        else:
            assert result.pct_used==expected and result.reset_at.timestamp()==RESET
            assert result.window_id.startswith('window:604800:') and 'selected-user' not in result.window_id
    asyncio.run(run())


@pytest.mark.parametrize('token', [None,'not-jwt','h.'+base64.urlsafe_b64encode(b'{"sub":null}').decode()+'.s'])
def test_unreadable_identity_skips_transport(token):
    class Never:
        def stream(self,*a,**kw):
            raise AssertionError('Must not contact an account without its identity')
    assert asyncio.run(read_snapshot(Never(),token)) == QuotaReadError('identity')


def test_snapshot_transport_failure_and_cancellation():
    async def fail(request):
        raise httpx.ConnectError('sensitive diagnostic')
    async def cancelled(request):
        raise asyncio.CancelledError
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            result = await read_snapshot(client,TOKEN)
            assert result == QuotaReadError('request')
            assert 'sensitive' not in result.message
        async with httpx.AsyncClient(transport=httpx.MockTransport(cancelled)) as client:
            with pytest.raises(asyncio.CancelledError):
                await read_snapshot(client,TOKEN)
    asyncio.run(run())


def test_worker_events_choose_outermost_suite_boundaries(tmp_path,monkeypatch):
    path=tmp_path/'events';monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.delenv('PYTEST_CURRENT_TEST',raising=False)
    publish('before',QuotaSnapshot(1,'weekly:opaque',NOW+timedelta(days=3),NOW))
    assert not path.exists()
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test.py::test_one (call)')
    publish('before',QuotaSnapshot(1,'weekly:opaque',NOW+timedelta(days=3),NOW))
    publish('after',QuotaSnapshot(2,'weekly:opaque',NOW+timedelta(days=3),NOW+timedelta(seconds=1)))
    tracker=SuiteUsage('run')
    for line in path.read_text().splitlines():
        event=json.loads(line)
        assert event['node']=='test.py::test_one'
        capture(SimpleNamespace(text=json.dumps(event['data'])),tracker.observe_snapshot)
    tracker.observe_snapshot('before',QuotaSnapshot(9,'weekly:opaque',NOW+timedelta(days=3),NOW+timedelta(seconds=3)))
    tracker.observe_snapshot('after',QuotaSnapshot(3,'weekly:opaque',NOW+timedelta(days=3),NOW+timedelta(seconds=2)))
    tracker.observe_snapshot('after',QuotaSnapshot(1,'weekly:opaque',NOW+timedelta(days=3),NOW))
    assert tracker.report()['pct_point_delta']==2
    with pytest.raises(ValueError):tracker.observe_snapshot('other',tracker.before)
    with pytest.raises(ValueError):tracker.observe_snapshot('before',None)


@pytest.mark.parametrize('data', [[],{}, {'phase':'other','snapshot':{}}, {'phase':'before','snapshot':[]},
    {'phase':'before','snapshot':{}}])
def test_malformed_events_fail_closed(data):
    with pytest.raises(ValueError):
        capture(SimpleNamespace(text=json.dumps(data)),lambda *a:None)


@pytest.mark.parametrize('snapshot', [None,QuotaSnapshot(101,'w',NOW,NOW),QuotaSnapshot(1,None,NOW,NOW),
    QuotaSnapshot(1,'w'*101,NOW,NOW),QuotaSnapshot(1,'\n',NOW,NOW),QuotaSnapshot(1,'w',None,NOW)])
def test_invalid_snapshot(snapshot):
    with pytest.raises(ValueError):validate_snapshot(snapshot)


@pytest.mark.parametrize('code,status', [('unknown',None), ([],None), ('http',None),
    ('http',True), ('http',99), ('http',600), ('request',401)])
def test_quota_error_rejects_uncontrolled_diagnostics(code,status):
    with pytest.raises(ValueError):
        QuotaReadError(code,status)


@pytest.mark.parametrize('code', ['identity','response','window','request'])
def test_quota_error_message_and_wire_round_trip(tmp_path,monkeypatch,code):
    path=tmp_path/'events'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(path))
    monkeypatch.setenv('PYTEST_CURRENT_TEST','test.py::test (call)')
    error=QuotaReadError(code)
    assert error.message and 'secret' not in error.message
    assert validate_snapshot(error) is error
    publish('before',error)
    event=json.loads(path.read_text())
    usage=SuiteUsage('run')
    capture(SimpleNamespace(text=json.dumps(event['data'])),usage.observe_snapshot)
    assert usage.report()['quota_errors'] == {'before':{'code':code,'status':None}}


@pytest.mark.parametrize('error', [[], {}, {'code':'http','status':401,'message':'secret'},
    {'code':'secret','status':None}, {'code':'http','status':True}])
def test_bad_quota_error_events_cannot_publish_messages(error):
    with pytest.raises(ValueError):
        capture(SimpleNamespace(text=json.dumps({'phase':'before','error':error})),lambda *a:None)
