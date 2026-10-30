"""Bounded account-specific model discovery without inference or retries."""
import json
import re

import httpx

from api_agent.providers.contracts import ProviderAccessError
from api_agent.providers.llm import AvailableSelection
from api_agent.providers.progress import component_timeout

_OFFICIAL_BASE = 'https://api.openai.com/v1'
_LIMIT = 1048576
_TIMEOUT = 15
_BEARER = re.compile(r'bearer\s+[^\s,;]+', flags=re.IGNORECASE)


def _safe_text(value, token, *, limit=512):
    if type(value) is not str:
        return None
    # Mask before normalization consumes control whitespace separating a Bearer.
    value = _BEARER.sub('Bearer [redacted]', value)
    value = ''.join(c for c in value if c.isprintable())
    value = _BEARER.sub('Bearer [redacted]', value)
    value = re.sub(r'https?://[^\s]+', '[endpoint]', value, flags=re.IGNORECASE)
    value = value.replace(token, '[redacted]')
    if token in value:
        return None
    return value[:limit] or None


class ModelDiscoveryError(ProviderAccessError):
    """Only bounded, redacted upstream classification crosses the boundary."""


def _authentication_type(challenge):
    """Extract only a bounded Bearer error identifier, never a raw challenge."""
    if type(challenge) is not str or len(challenge) > 4096:
        return None
    scheme, separator, parameters = challenge.partition(' ')
    if not separator or scheme.lower() != 'bearer':
        return None
    errors = re.findall(r'(?:^|,)\s*error\s*=\s*"([A-Za-z0-9_.-]{1,100})"\s*(?=,|$)',
                        parameters, flags=re.IGNORECASE)
    return errors[0] if len(errors) == 1 else None


def _failure(status, data, request_id, token, authentication_type=None):
    row = data.get('error') if type(data) is dict else None
    row = row if type(row) is dict else {}
    kind = {401: 'authentication', 403: 'authorization', 429: 'rate_limited'}.get(status, 'invalid_request' if 400 <= status < 500 else 'unavailable')
    message = _safe_text(row.get('message'), token) or 'GPT model discovery failed.'
    code = row.get('code')
    if type(code) is int and -2147483648 <= code <= 2147483647:
        code = str(code)
    return ModelDiscoveryError(kind, message, status=status,
        code=_safe_text(code, token, limit=100),
        type=_safe_text(row.get('type'), token, limit=100) or _safe_text(authentication_type, token, limit=100),
        request_id=_safe_text(request_id, token, limit=100))


def _catalog(data, subscription, token, *, base_url=None):
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
        if not (identifier.startswith('gpt-') or identifier.startswith('openai/gpt-')):
            continue
        name = row.get('display_name' if subscription else 'name', '')
        version = row.get('version')
        top = row.get('top_provider')
        top = top if type(top) is dict else {}
        maximum = row.get('max_output_tokens', top.get('max_completion_tokens'))
        context = row.get('context_length')
        if (type(name) is not str or len(name) > 200 or (name and not name.isprintable())
                or (version is not None and (type(version) is not str or len(version) > 100 or (version and not version.isprintable())))
                or any(token in text for text in (name, version or '') if text)
                or any(value is not None and (type(value) is not int or not 0 < value <= 2147483647)
                       for value in (maximum, context))):
            raise ModelDiscoveryError('unknown', 'GPT model discovery returned invalid metadata.')
        family, inferred_version = _selection_identity(identifier)
        from .reasoning import reasoning_efforts
        efforts = reasoning_efforts(identifier, base_url=base_url)
        # This exact route/model is qualified separately; catalog parameters alone
        # do not establish equivalent support across all models and routes.
        supported = row.get('supported_parameters')
        limit_supported = (not subscription and base_url == 'https://openrouter.ai/api/v1'
                           and identifier == 'openai/gpt-6-luna'
                           and type(supported) is list and 'max_tokens' in supported)
        features = (('reasoning_effort',) if efforts else ()) + (('max_output_tokens',) if limit_supported else ())
        selections.append(AvailableSelection(identifier, description, display_name=name,
                          version=version if version is not None else inferred_version,
                          max_output_tokens=maximum, context_length=context, family=family,
                          features=features, reasoning_efforts=efforts))
    if len(selections) > 256:
        raise ModelDiscoveryError('unknown', 'GPT model discovery returned too many selections.')
    return tuple(selections)


def _selection_identity(identifier):
    """Normalize structured public GPT identifiers; unknown names stay unknown."""
    match = re.fullmatch(r'(?:openai/)?gpt-(\d+(?:\.\d+)*)(-[a-z][a-z0-9-]*?)?(?:-(\d{4}-\d{2}-\d{2}))?', identifier)
    if match is None:
        return None, None
    version, variant, date = match.groups()
    if date:
        version += '+' + date.replace('-', '.')
    return 'gpt' + (variant or ''), version


def _endpoint(base_url, access_token, subscription):
    from .endpoint import validate_base_url
    if (type(subscription) is not bool or type(access_token) is not str
            or not access_token or not access_token.isascii() or len(access_token) > 16384
            or any(c.isspace() or not c.isprintable() for c in access_token)):
        raise ModelDiscoveryError('invalid_request', 'GPT model discovery requires valid authentication.')
    base_url = validate_base_url(base_url)
    if subscription and base_url != _OFFICIAL_BASE:
        raise ModelDiscoveryError('invalid_request', 'GPT subscription model discovery requires the official endpoint.')
    return base_url


async def discover_models(*, client, base_url, access_token, subscription):
    """One authorized catalog GET; the caller owns the injected client."""
    base_url = _endpoint(base_url, access_token, subscription)
    failure = None
    try:
        async with component_timeout('GPT model discovery', _TIMEOUT) as budget:
            status, data, request_id, authentication_type = await _get(client, base_url + '/models', access_token, budget)
            if status != 200:
                raise _failure(status, data, request_id, access_token, authentication_type)
            return _catalog(data, subscription, access_token, base_url=base_url)
    except (TimeoutError, httpx.TimeoutException):
        failure = ModelDiscoveryError('deadline', 'GPT model discovery timed out.')
    except httpx.RequestError:
        failure = ModelDiscoveryError('unavailable', 'GPT model discovery transport is unavailable.')
    raise failure


async def _get(client, url, token, budget):
    headers = {'Accept-Encoding': 'identity'}
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    async with client.stream('GET', url, headers=headers, follow_redirects=False,
                             timeout=budget.remaining) as response:
        # Reject compression before HTTPX's decoder can allocate expanded chunks.
        if response.headers.get('content-encoding', '').strip().lower() not in ('', 'identity'):
            raise ModelDiscoveryError('unknown', 'GPT model discovery returned unsupported content encoding.')
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _LIMIT:
                raise ModelDiscoveryError('unknown', 'GPT model discovery response exceeded its limit.')
            body.extend(chunk)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            data = None
        return (response.status_code, data, response.headers.get('x-request-id'),
                _authentication_type(response.headers.get('www-authenticate')))


async def discover_selections(*, client, base_url, access_token, subscription, authority=None):
    """Verify remote access before returning a public or authenticated catalog."""
    base_url = _endpoint(base_url, access_token, subscription)
    failure = None
    async def checked_get(url, token, budget):
        result = await _get(client, url, token, budget)
        from .inference import _guard_content
        try:
            await _guard_content(list(result), access_token, authority)
        except ProviderAccessError:
            status = result[0]
            if type(status) is int and 400 <= status <= 599:
                kind = {401: 'authentication', 403: 'authorization', 429: 'rate_limited'}.get(
                    status, 'invalid_request' if status < 500 else 'unavailable')
                raise ModelDiscoveryError(kind, 'GPT model discovery response contains protected authentication content.', status=status) from None
            raise ModelDiscoveryError('unknown', 'GPT model discovery returned invalid metadata or protected authentication content.') from None
        return result
    async def handoff(catalog):
        from api_agent.providers.catalog import catalog_from_wire, catalog_to_wire
        from api_agent.providers.credential_guard import async_guard_json, CredentialProtectionError
        rows = catalog_to_wire(catalog)
        try:
            if authority is None:
                return catalog_from_wire(await async_guard_json(rows, {access_token}))
            generation, snapshot = authority.snapshot_content()
            owned = await async_guard_json(rows, {*snapshot._secrets, access_token})
            result = catalog_from_wire(owned)
            published = []
            if not authority.commit_if_current(generation, lambda: published.append(result)):
                raise CredentialProtectionError('Credential registrations changed during handoff.')
            return published.pop()
        except CredentialProtectionError:
            raise ModelDiscoveryError('unknown', 'GPT model discovery returned invalid metadata or protected authentication content.') from None
    try:
        async with component_timeout('GPT authenticated discovery', _TIMEOUT) as budget:
            status, data, request_id, authentication_type = await checked_get(base_url + '/models', None, budget)
            if status == 200:
                if subscription:
                    raise ModelDiscoveryError('authentication', 'GPT subscription requires an authenticated account catalog; the endpoint returned a public catalog.')
                catalog = _catalog(data, False, access_token, base_url=base_url)
                status, data, request_id, authentication_type = await checked_get(base_url + '/key', access_token, budget)
                if status != 200:
                    raise _failure(status, data, request_id, access_token, authentication_type)
                if (type(data) is not dict or type(data.get('data')) is not dict
                        or type(data['data'].get('label')) is not str or not data['data']['label']
                        or len(data['data']['label']) > 512 or not data['data']['label'].isprintable()):
                    raise ModelDiscoveryError('authentication', 'GPT endpoint did not verify the selected API key.')
                return await handoff(catalog)
            if status != 401:
                raise _failure(status, data, request_id, access_token, authentication_type)
            status, data, request_id, authentication_type = await checked_get(base_url + '/models', access_token, budget)
            if status != 200:
                raise _failure(status, data, request_id, access_token, authentication_type)
            return await handoff(_catalog(data, subscription, access_token, base_url=base_url))
    except (TimeoutError, httpx.TimeoutException):
        failure = ModelDiscoveryError('deadline', 'GPT model discovery timed out.')
    except httpx.RequestError:
        failure = ModelDiscoveryError('unavailable', 'GPT model discovery transport is unavailable.')
    raise failure
