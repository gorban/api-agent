"""Content-free optional quota observations, shared across worker modes."""
from datetime import datetime
import json
import os

from api_agent.telemetry.usage import QuotaSnapshot, QuotaReadError, _snapshot_json, _valid_pct


def validate_snapshot(snapshot):
    if isinstance(snapshot, QuotaReadError):
        return snapshot
    if (not isinstance(snapshot, QuotaSnapshot) or not _valid_pct(snapshot.pct_used)
            or snapshot.window_id is None or len(snapshot.window_id) > 100
            or not snapshot.window_id.isprintable() or snapshot.reset_at is None):
        raise ValueError('Invalid quota snapshot')
    return snapshot


def publish(phase, snapshot):
    path, node = os.environ.get('API_AGENT_INTEGRATION_EVENTS'), os.environ.get('PYTEST_CURRENT_TEST')
    if not path or not node:
        return
    data = {'phase': phase, 'error': snapshot.to_json()} if isinstance(snapshot, QuotaReadError) else {'phase': phase, 'snapshot': _snapshot_json(snapshot)}
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind':'quota_snapshot', 'node':node.rsplit(' ',1)[0],
                                'data':data}, allow_nan=False)+'\n')


def capture(event, observe):
    data=json.loads(event.text)
    if type(data) is not dict or set(data) not in ({'phase','snapshot'}, {'phase','error'}) or data['phase'] not in ('before','after'):
        raise ValueError('Invalid quota event')
    if 'error' in data:
        error = data['error']
        if type(error) is not dict or set(error) != {'code', 'status'}:
            raise ValueError('Invalid quota event')
        observe(data['phase'], QuotaReadError(error['code'], error['status']))
        return
    row=data['snapshot']
    required = {'pct_used','window_id','reset_at','captured_at'}
    allowed = required | {'pct_step','allocation_id','observation_id'}
    if type(row) is not dict or not required <= set(row) or set(row) - allowed:
        raise ValueError('Invalid quota event')
    snapshot=QuotaSnapshot(row['pct_used'],row['window_id'],datetime.fromisoformat(row['reset_at']),datetime.fromisoformat(row['captured_at']),row.get('pct_step'),row.get('allocation_id'),row.get('observation_id'))
    observe(data['phase'], validate_snapshot(snapshot))
