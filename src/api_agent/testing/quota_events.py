"""Content-free optional quota observations, shared across worker modes."""
from datetime import datetime
import json
import os

from api_agent.telemetry.usage import QuotaSnapshot, _snapshot_json, _valid_pct


def validate_snapshot(snapshot):
    if (not isinstance(snapshot, QuotaSnapshot) or not _valid_pct(snapshot.pct_used)
            or snapshot.window_id is None or len(snapshot.window_id) > 100
            or not snapshot.window_id.isprintable() or snapshot.reset_at is None):
        raise ValueError('Invalid quota snapshot')
    return snapshot


def publish(phase, snapshot):
    path, node = os.environ.get('API_AGENT_INTEGRATION_EVENTS'), os.environ.get('PYTEST_CURRENT_TEST')
    if not path or not node:
        return
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind':'quota_snapshot', 'node':node.rsplit(' ',1)[0],
                                'data':{'phase':phase,'snapshot':_snapshot_json(snapshot)}}, allow_nan=False)+'\n')


def capture(event, observe):
    data=json.loads(event.text)
    if type(data) is not dict or set(data) != {'phase','snapshot'} or data['phase'] not in ('before','after'):
        raise ValueError('Invalid quota event')
    row=data['snapshot']
    if type(row) is not dict or set(row) != {'pct_used','window_id','reset_at','captured_at'}:
        raise ValueError('Invalid quota event')
    snapshot=QuotaSnapshot(row['pct_used'],row['window_id'],datetime.fromisoformat(row['reset_at']),datetime.fromisoformat(row['captured_at']))
    observe(data['phase'], validate_snapshot(snapshot))
