"""Optional selected-account snapshots are bounded, private and reset-aware."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from api_agent.telemetry.usage import SuiteUsage, QuotaSnapshot, QuotaReadError
from api_agent.testing.quota_events import capture, publish, validate_snapshot

NOW=datetime.now(timezone.utc)


@pytest.mark.parametrize('mode', ['subscription', 'api_key'])
@pytest.mark.parametrize('period', ['day', 'week', 'month'])
def test_gpt_snapshot_does_not_send_app_credentials_to_unsupported_routes(monkeypatch,mode,period):
    from api_agent.llm_providers.gpt import factory
    from api_agent.providers.contracts import ContractError
    from api_agent.telemetry.usage_output import final_usage_text
    def forbidden(*args,**kwargs):
        pytest.fail('Optional quota accounting must not create a transport or reuse another login')
    monkeypatch.setattr(factory.httpx, 'AsyncClient', forbidden)
    from api_agent.llm_providers.gpt import quota_client
    connection=SimpleNamespace(access_token='selected-app-token')
    monkeypatch.setattr(factory, 'ConnectionStore', lambda _:SimpleNamespace(list=lambda:[connection]))
    async def snapshot(selected, *, period):
        assert selected is connection
        return None
    monkeypatch.setattr(quota_client,'read_quota',snapshot)
    subject=factory.GPTProvider({'GPT_AUTH_MODE':mode,'LLM_QUOTA_PERIOD':period})
    async def run():
        assert await subject.asnapshot(auth='selected-app-token') is None
        assert await subject.asnapshot(auth=None) == (QuotaReadError('identity') if mode=='subscription' else None)
        await subject.aclose()
        with pytest.raises(ContractError,match='closed'):
            await subject.asnapshot(auth='selected-app-token')
    asyncio.run(run())
    text=final_usage_text(SuiteUsage('run').report())
    assert 'quota' not in text.lower() and 'Cost accounting:' not in text
    assert 'Tokens this run:' in text and 'This suite cost:' in text


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


@pytest.mark.parametrize('code', ['identity','response','window','request','source'])
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
