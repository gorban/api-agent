"""Local quota reads never borrow native credentials for inference."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import json
from types import SimpleNamespace

import pytest
from api_agent.llm_providers.gpt import quota_client as q
from api_agent.telemetry.usage import QuotaSnapshot, QuotaReadError

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
ACCOUNT = {'account': {'type': 'chatgpt', 'email': 'User@example.test', 'planType': 'pro'}}
WINDOW = {'usedPercent': 28, 'windowDurationMins': 10080, 'resetsAt': 1792295916}
DATA = {'rateLimitsByLimitId': {'codex': {'limitId': 'codex', 'primary': WINDOW, 'secondary': None}}}
CONNECTION = SimpleNamespace(email='user@example.test', subject='verified-subject', access_token='app-secret')

class Process:
    def __init__(self, frames=None):
        self.frames = frames or [dict(id=i, result=r) for i, r in enumerate(({}, ACCOUNT, DATA, ACCOUNT), 1)]
        self.stdout = self.stdin = self
        self.written = []
        self.returncode = None
        self.signals = []
        self.waits = 0
    def write(self, raw): self.written.append(json.loads(raw))
    async def drain(self): pass
    async def readline(self):
        frame = self.frames.pop(0)
        if isinstance(frame, bytes): return frame
        return json.dumps(frame).encode() + b'\n'
    def terminate(self): self.signals.append('terminate'); self.returncode = -15
    def kill(self): self.signals.append('kill'); self.returncode = -9
    async def wait(self): self.waits += 1; return self.returncode

@pytest.fixture
def client(monkeypatch):
    p = Process()
    calls = []
    async def spawn(*args, **kwargs): calls.append((args, kwargs)); return p
    monkeypatch.setattr(q.shutil, 'which', lambda _: '/tools/codex')
    monkeypatch.setattr(q.asyncio, 'create_subprocess_exec', spawn)
    return p, calls

def test_read_matching_native_account_preserves_integer_precision_and_route(client, monkeypatch):
    p, calls = client
    for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'ACCESS_TOKEN'): monkeypatch.setenv(key, 'external-secret')
    result = asyncio.run(q.read_quota(CONNECTION, period='week'))
    assert isinstance(result, QuotaSnapshot) and result.pct_used == 28 and result.pct_step == 1
    assert result.reset_at == datetime.fromtimestamp(WINDOW['resetsAt'], timezone.utc)
    assert result.window_id.startswith('codex:') and 'verified-subject' not in result.window_id
    assert len(result.window_id) < 100
    assert [f['method'] for f in p.written] == ['initialize', 'initialized', 'account/read', 'account/rateLimits/read', 'account/read']
    assert 'app-secret' not in json.dumps(p.written) and 'user@example.test' not in json.dumps(p.written)
    assert p.signals == ['terminate'] and p.waits == 1
    args, options = calls[0]
    assert args == ('/tools/codex', 'app-server', '--listen', 'stdio://')
    assert all(key not in options['env'] for key in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'ACCESS_TOKEN'))
    assert options['stderr'] == asyncio.subprocess.DEVNULL and options['limit'] == 65536

@pytest.mark.parametrize('email, executable', [('', '/codex'), ('x@test', None)])
def test_missing_source_never_starts_process(client, monkeypatch, email, executable):
    monkeypatch.setattr(q.shutil, 'which', lambda _: executable)
    assert asyncio.run(q.read_quota(SimpleNamespace(email=email), period='week')) == QuotaReadError('source')
    assert not client[1]

@pytest.mark.parametrize('account', [None, {}, {'type': 'apiKey', 'email': CONNECTION.email},
                                    {'type': 'chatgpt', 'email': 123}, {'type': 'chatgpt', 'email': 'another@test'}])
def test_wrong_account_does_not_read_quota(client, account):
    p, _ = client; p.frames[1]['result'] = {'account': account}
    assert asyncio.run(q.read_quota(CONNECTION, period='week')) == QuotaReadError('source')
    assert all(f['method'] != 'account/rateLimits/read' for f in p.written)
    assert p.signals == ['terminate']

@pytest.mark.parametrize('changed', [dict(account={'type':'chatgpt','email':'other@test'}), dict(ACCOUNT, requiresOpenaiAuth=True)])
def test_changed_account_is_not_attributed(client, changed):
    client[0].frames[3]['result'] = changed
    assert asyncio.run(q.read_quota(CONNECTION, period='week')) == QuotaReadError('source')

@pytest.mark.parametrize('bad', [b'', b'X'*65537, b'not-json', [], {'id':1,'error':{'message':'private'}},
                               {'id':1,'result':[]}, {'id':1,'result':{},'error':{}}])
def test_malformed_rpc_does_not_bubble_private_output(client, bad):
    client[0].frames[0] = bad
    assert asyncio.run(q.read_quota(CONNECTION, period='week')) == QuotaReadError('request')
    assert client[0].signals == ['terminate']

def test_notifications_and_frame_bound(client):
    p, _ = client
    p.frames.insert(0, {'method':'account/updated'})
    assert isinstance(asyncio.run(q.read_quota(CONNECTION, period='week')), QuotaSnapshot)
    p = Process([{'method':'notice'}]*16)
    with pytest.raises(ValueError, match='Too many'): asyncio.run(q._rpc(p, 1, 'read'))

def test_spawn_failure_is_safe(client, monkeypatch):
    async def spawn(*args, **kwargs): raise OSError('private path')
    monkeypatch.setattr(q.asyncio, 'create_subprocess_exec', spawn)
    assert asyncio.run(q.read_quota(CONNECTION, period='week')) == QuotaReadError('request')

@pytest.mark.parametrize('data, expected', [({},'window'), ({'rateLimits':{}},'window'),
    ({'rateLimits':{'limitId':'other'}},'window'),
    ({'rateLimits':{'limitId':'codex','primary':WINDOW,'secondary':WINDOW}},'window'),
    ({'rateLimits':{'limitId':'codex','primary':dict(WINDOW, windowDurationMins=10)}},'window')])
def test_snapshot_selects_one_correct_window(data, expected):
    assert q._snapshot(data, 'subject', 'week', NOW) == QuotaReadError(expected)

@pytest.mark.parametrize('key,value', [('usedPercent',True), ('usedPercent',28.0),('usedPercent',-1),('usedPercent',101),
    ('resetsAt',True), ('resetsAt',0), ('resetsAt',253402300800)])
def test_invalid_snapshot_measurements(key, value):
    data={'rateLimits':{'limitId':'codex','primary':dict(WINDOW, **{key:value})}}
    assert q._snapshot(data,'subject','week',NOW)==QuotaReadError('response')

def test_zero_quota_and_daily_secondary_are_real_measurements():
    data={'rateLimits':{'limitId':'codex','primary':WINDOW,'secondary':dict(WINDOW, usedPercent=0,windowDurationMins=1440)}}
    result=q._snapshot(data,'subject','day',NOW)
    assert isinstance(result,QuotaSnapshot) and result.pct_used==0 and ':1440:' in result.window_id
    assert result.window_id != q._snapshot(data,'other','day',NOW).window_id


@pytest.mark.parametrize('year,month,days', [(2026,10,31),(2026,4,30),(2026,2,28),(2028,2,29)])
def test_monthly_window_does_not_reuse_weekly_quota(year, month, days):
    now = datetime(year, month, 1, tzinfo=timezone.utc)
    assert q._snapshot(DATA, 'subject', 'month', now) == QuotaReadError('window')
    monthly = dict(WINDOW, windowDurationMins=days*1440, resetsAt=int((now+timedelta(days=days)).timestamp()))
    data = {'rateLimits':{'limitId':'codex','primary':monthly}}
    snapshot = q._snapshot(data, 'subject', 'month', now)
    assert isinstance(snapshot, QuotaSnapshot) and snapshot.pct_used == 28
    assert f':{days*1440}:' in snapshot.window_id
    assert q._snapshot(data, 'subject', 'week', now) == QuotaReadError('window')

@pytest.mark.parametrize('signal', ['terminate','kill'])
def test_disappearing_process_and_timeout_cleanup(client, signal):
    p,_=client
    if signal=='kill':
        def terminating():p.signals.append('terminate')
        p.terminate=terminating
    async def wait():
        p.waits+=1
        if p.waits==1 and signal=='kill': raise TimeoutError
        return 0
    p.wait=wait
    def gone():
        p.signals.append(signal)
        raise ProcessLookupError
    setattr(p, signal, gone)
    async def run():
        task=asyncio.create_task(asyncio.sleep(0,result=p))
        await q._close(task)
    asyncio.run(run())
    assert signal in p.signals

def test_already_exited_process_is_not_signalled(client):
    p,_=client;p.returncode=0
    async def run(): await q._close(asyncio.create_task(asyncio.sleep(0,result=p)))
    asyncio.run(run());assert not p.signals and p.waits==1

def test_timeout_reaped_process_is_not_killed(client):
    p,_=client
    async def wait():
        p.waits+=1
        if p.waits==1: raise TimeoutError
        return 0
    p.wait=wait
    async def run(): await q._close(asyncio.create_task(asyncio.sleep(0,result=p)))
    asyncio.run(run());assert p.signals==['terminate'] and p.waits==2

def test_hung_process_is_killed_and_reaped(client):
    p,_=client
    def terminating():p.signals.append('terminate')
    p.terminate=terminating
    async def wait():
        p.waits+=1
        if p.waits==1:raise TimeoutError
        return p.returncode
    p.wait=wait
    async def run():await q._close(asyncio.create_task(asyncio.sleep(0,result=p)))
    asyncio.run(run());assert p.signals==['terminate','kill'] and p.waits==2

def test_cancellation_during_spawn_still_disposes_acquisition(client, monkeypatch):
    p,_=client
    async def run():
        started=asyncio.Event(); release=asyncio.Event()
        async def spawn(*args,**kwargs):
            started.set();await release.wait();return p
        monkeypatch.setattr(q.asyncio,'create_subprocess_exec',spawn)
        task=asyncio.create_task(q.read_quota(CONNECTION,period='week'))
        await started.wait();task.cancel();await asyncio.sleep(0);task.cancel();release.set()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run());assert p.signals==['terminate'] and p.waits==1

def test_rpc_deadline_has_fresh_synchronous_cleanup(client, monkeypatch):
    original=q.component_timeout
    @asynccontextmanager
    async def short(name, timeout):
        assert name=='GPT subscription quota' and timeout==20
        async with original(name, .01) as budget:yield budget
    monkeypatch.setattr(q,'component_timeout',short)
    p,_=client
    async def blocked():await asyncio.Event().wait()
    p.readline=blocked
    assert asyncio.run(q.read_quota(CONNECTION,period='week'))==QuotaReadError('request')
    assert p.signals==['terminate'] and p.waits==1
