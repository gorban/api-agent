"""Best-effort weekly quota from the selected subscription's own token.

This optional endpoint follows the official Codex backend client. Rejection or
schema changes mean unavailable accounting, never another account's auth cache.
"""
from datetime import datetime, timezone
import hashlib
import json
import base64

from api_agent.providers.progress import component_timeout
from api_agent.telemetry.usage import QuotaSnapshot, QuotaReadError, _valid_pct
from .discovery import _get


async def read_snapshot(client, token, *, quota_seconds=604800):
    try:
        # Decode only an accounting identity, not an authentication decision.
        claim=json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
        subject=claim.get('sub')
        if type(subject) is not str or not subject:
            return QuotaReadError('identity')
    except Exception:
        return QuotaReadError('identity')
    try:
        async with component_timeout('GPT weekly quota', 2) as budget:
            status, data, _, _ = await _get(client, 'https://chatgpt.com/backend-api/wham/usage', token, budget)
        if status != 200:
            return QuotaReadError('http', status)
        if type(data) is not dict:
            return QuotaReadError('response')
        limits=data.get('rate_limit')
        if type(limits) is not dict:
            return QuotaReadError('response')
        for key in ('primary_window','secondary_window'):
            row=limits.get(key)
            if type(row) is not dict or row.get('limit_window_seconds') != quota_seconds:
                continue
            pct, reset=row.get('used_percent'),row.get('reset_at')
            if not _valid_pct(pct) or type(reset) is not int:
                return QuotaReadError('response')
            now=datetime.now(timezone.utc)
            reset_at=datetime.fromtimestamp(reset,timezone.utc)
            owner=hashlib.sha256(subject.encode()).hexdigest()
            return QuotaSnapshot(pct, f'window:{quota_seconds}:'+owner[:32],reset_at,now)
    except Exception:
        # Never surface token/response bodies from an optional accounting read.
        return QuotaReadError('request')
    return QuotaReadError('window')
