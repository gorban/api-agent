"""Bounded account-specific model discovery without inference or retries."""
import json
import re

import httpx

from api_agent.providers.control import OperationalError
from api_agent.providers.llm import AvailableSelection
from api_agent.providers.progress import component_timeout

_OFFICIAL_BASE = 'https://api.openai.com/v1'
_LIMIT = 1048576
_TIMEOUT = 15


def _safe_text(value, token, *, limit=512):
    if type(value) is not str:
        return None
    value = value.replace(token, '[redacted]')
    value = re.sub(r'(?i)bearer\s+[^\s,;]+', 'Bearer [redacted]', value)
    value = re.sub(r'https?://[^\s]+', '[endpoint]', value, flags=re.IGNORECASE)
    value = ''.join(c for c in value if c.isprintable())
    return value[:limit] or None


class ModelDiscoveryError(OperationalError):
    """Only bounded, redacted upstream classification crosses the boundary."""
    def __init__(self, kind, message, *, status=None, code=None, type=None, request_id=None):
        super().__init__(kind)
        self.status, self.code, self.type, self.request_id = status, code, type, request_id
        self.message = message
        self.args = (message,)


def _failure(status, data, request_id, token):
    row = data.get('error') if type(data) is dict else None
    row = row if type(row) is dict else {}
    kind = {401: 'authentication', 403: 'authorization', 429: 'rate_limited'}.get(status, 'invalid_request' if 400 <= status < 500 else 'unavailable')
    message = _safe_text(row.get('message'), token) or 'GPT model discovery failed.'
    return ModelDiscoveryError(kind, message, status=status,
        code=_safe_text(row.get('code'), token, limit=100),
        type=_safe_text(row.get('type'), token, limit=100),
        request_id=_safe_text(request_id, token, limit=100))


def _catalog(data, subscription, token):
    if type(data) is not dict or type(data.get('models' if subscription else 'data')) is not list:
        raise ModelDiscoveryError('unknown', 'GPT model discovery returned an invalid catalog.')
    selections, seen = [], set()
    for row in data['models' if subscription else 'data']:
        if type(row) is not dict:
            raise ModelDiscoveryError('unknown', 'GPT model discovery returned an invalid catalog.')
        if subscription and row.get('visibility') != 'list':
            continue
        identifier = row.get('slug' if subscription else 'id')
        description = row.get('display_name') if subscription else ''
        if (type(identifier) is not str or not identifier or len(identifier) > 200
                or not identifier.isprintable() or identifier.strip() != identifier
                or identifier in seen or token in identifier
                or type(description) is not str or (subscription and not description) or len(description) > 512
                or (description and (not description.isprintable() or token in description))):
            raise ModelDiscoveryError('unknown', 'GPT model discovery returned an invalid catalog.')
        seen.add(identifier)
        if not identifier.startswith('gpt-'):
            continue
        selections.append(AvailableSelection(identifier, description))
    return tuple(selections)


async def discover_models(*, client, base_url, access_token, subscription):
    """Use one scoped GET; the caller retains ownership of the injected client."""
    from .endpoint import validate_base_url
    if (type(subscription) is not bool or type(access_token) is not str
            or not access_token or not access_token.isascii() or len(access_token) > 16384
            or any(c.isspace() or not c.isprintable() for c in access_token)):
        raise ModelDiscoveryError('invalid_request', 'GPT model discovery requires valid authentication.')
    base_url = validate_base_url(base_url)
    if subscription and base_url != _OFFICIAL_BASE:
        raise ModelDiscoveryError('invalid_request', 'GPT subscription model discovery requires the official endpoint.')
    failure = None
    try:
        async with component_timeout('GPT model discovery', _TIMEOUT) as budget:
            async with client.stream('GET', base_url + '/models',
                    headers={'Authorization': 'Bearer ' + access_token},
                    follow_redirects=False, timeout=budget.remaining) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _LIMIT:
                        raise ModelDiscoveryError('unknown', 'GPT model discovery response exceeded its limit.')
                    body.extend(chunk)
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    data = None
                if response.status_code != 200:
                    raise _failure(response.status_code, data, response.headers.get('x-request-id'), access_token)
                return _catalog(data, subscription, access_token)
    except (TimeoutError, httpx.TimeoutException):
        failure = ModelDiscoveryError('deadline', 'GPT model discovery timed out.')
    except httpx.RequestError:
        failure = ModelDiscoveryError('unavailable', 'GPT model discovery transport is unavailable.')
    raise failure
