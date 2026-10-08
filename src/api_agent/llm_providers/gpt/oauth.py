"""Public-client authorization with pinned issuer and signed identity checks."""
from collections.abc import Mapping
from dataclasses import dataclass, field
import base64
import hashlib
import json
import math
import secrets
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
import jwt

from .contracts import AuthError, Connection

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
METADATA = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
INITIAL_CLIENT = "dynamic_agent_client"
LIMIT = 131072


@dataclass(frozen=True)
class AuthorizationAttempt:
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    verifier: str = field(repr=False)
    redirect_uri: str
    host_id: str
    client_id: str
    authorization_url: str = field(repr=False)
    registration: Connection | None = field(default=None, repr=False)


def begin_authorization(host_id: str, redirect_uri: str,
                        registration: Connection | None = None,
                        app_name: str = "api-agent") -> AuthorizationAttempt:
    try:
        if not host_id.startswith("urn:uuid:"):
            raise ValueError
        UUID(host_id[9:])
        uri = urlsplit(redirect_uri)
        if (uri.scheme, uri.hostname, uri.path) != ("http", "127.0.0.1", "/auth/callback"):
            raise ValueError
        if uri.username or uri.password or uri.query or uri.fragment or not uri.port:
            raise ValueError
        if registration is not None and (registration.host_id != host_id or registration.client_id == INITIAL_CLIENT):
            raise ValueError
        if registration is None and (not isinstance(app_name, str) or not app_name.strip()):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        pass
    else:
        return _begin(host_id, redirect_uri, registration, app_name)
    raise AuthError("invalid_attempt")


def _begin(host_id, redirect_uri, registration, app_name):
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    client_id = registration.client_id if registration else INITIAL_CLIENT
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    query = dict(client_id=client_id, ext_agent_host_id=host_id, response_type="code",
                 redirect_uri=redirect_uri, scope=SCOPES, resource=RESOURCE, state=state,
                 nonce=nonce, code_challenge_method="S256", code_challenge=challenge)
    if registration:
        query.update(id_token_hint=registration.id_token, login_hint=registration.email)
    else:
        query["agent_name_hint"] = app_name
    return AuthorizationAttempt(state, nonce, verifier, redirect_uri, host_id, client_id,
                                AUTHORIZE + "?" + urlencode(query), registration)


async def _request(client, method, url, **kwargs):
    try:
        async with client.stream(method, url, timeout=15, follow_redirects=False, **kwargs) as response:
            if response.status_code != 200:
                raise ValueError
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > LIMIT:
                    raise ValueError
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    raise AuthError("authorization_failed")


def _text(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise AuthError("invalid_credentials")
    return value


def _number(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError
    except (ValueError, OverflowError):
        pass
    else:
        return value
    raise AuthError("invalid_credentials")


def _jwks_uri(metadata):
    value = _text(metadata, "jwks_uri")
    try:
        uri = urlsplit(value)
        if uri.scheme == "https" and uri.netloc == "auth.openai.com" and not uri.fragment and not uri.query:
            return uri.geturl()
    except ValueError:
        pass
    raise AuthError("invalid_issuer")


async def _identity(token, client_id, nonce, client, now):
    metadata = await _request(client, "GET", METADATA)
    if (metadata.get("issuer"), metadata.get("authorization_endpoint"), metadata.get("token_endpoint")) != (ISSUER, AUTHORIZE, TOKEN):
        raise AuthError("invalid_issuer")
    keys = await _request(client, "GET", _jwks_uri(metadata))
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise ValueError
        kid = _text(header, "kid")
        matching = [key for key in keys["keys"] if key.get("kid") == kid]
        if len(matching) != 1:
            raise ValueError
        selected = matching[0]
        if (selected.get("kty") != "RSA" or selected.get("alg", "RS256") != "RS256"
                or selected.get("use", "sig") != "sig"
                or selected.get("key_ops", ["verify"]) != ["verify"]):
            raise ValueError
        key = jwt.PyJWK.from_dict(selected, algorithm="RS256").key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
                            options={"require": ["exp", "sub", "aud", "iss"],
                                     "verify_exp": False, "verify_iat": False, "verify_nbf": False})
        if ("azp" in claims and claims["azp"] != client_id) or (isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id):
            raise ValueError
        if _number(claims["exp"]) <= now:
            raise ValueError
        if "nbf" in claims and _number(claims["nbf"]) > now:
            raise ValueError
        if nonce is not None and claims.get("nonce") != nonce:
            raise ValueError
        _text(claims, "sub")
        return claims
    except (jwt.PyJWTError, ValueError, KeyError, TypeError, AttributeError):
        pass
    raise AuthError("invalid_identity")


async def _connection(data, client_id, host_id, subject, nonce, client, now, previous=None):
    scopes = tuple(_text(data, "scope").split())
    if not {"resource.invoke", "chatgpt.tokens.use.direct", "offline_access"}.issubset(scopes):
        raise AuthError("plan_permission_required")
    if _text(data, "token_type").lower() != "bearer":
        raise AuthError("invalid_credentials")
    expiry = _number(data.get("expires_in"))
    earliest = _number(data.get("earliest_refresh_at", 0))
    expires_at = _number(now + expiry)
    if expiry <= 0 or earliest < 0:
        raise AuthError("invalid_credentials")
    if previous is not None and "id_token" not in data:
        token = previous.id_token
        claims = dict(sub=previous.subject, email=previous.email)
    else:
        token = _text(data, "id_token")
        claims = await _identity(token, client_id, nonce, client, now)
    if subject is not None and claims["sub"] != subject:
        raise AuthError("identity_mismatch")
    return Connection(claims["sub"], client_id, host_id, _text(claims, "email"),
                      _text(data, "access_token"), _text(data, "refresh_token"), token,
                      scopes, expires_at, earliest)


async def finish_authorization(attempt: AuthorizationAttempt, parameters: Mapping[str, str],
                               *, client: httpx.AsyncClient, now: float) -> Connection:
    now = _number(now)
    if parameters.get("state") != attempt.state:
        raise AuthError("invalid_state")
    if "error" in parameters:
        raise AuthError("authorization_denied")
    client_id = parameters.get("client_id", attempt.client_id)
    if not isinstance(client_id, str) or not client_id or client_id == INITIAL_CLIENT:
        raise AuthError("incomplete_registration")
    if attempt.registration is not None and client_id != attempt.client_id:
        raise AuthError("client_mismatch")
    code = _text(parameters, "code")
    data = await _request(client, "POST", TOKEN, data=dict(grant_type="authorization_code",
                          client_id=client_id, code=code, code_verifier=attempt.verifier,
                          redirect_uri=attempt.redirect_uri, resource=RESOURCE))
    subject = attempt.registration.subject if attempt.registration else None
    return await _connection(data, client_id, attempt.host_id, subject, attempt.nonce, client, now)


async def refresh_connection(connection: Connection, *, client: httpx.AsyncClient,
                             now: float) -> Connection:
    now = _number(now)
    if now < connection.earliest_refresh_at:
        raise AuthError("refresh_not_ready")
    data = await _request(client, "POST", TOKEN, data=dict(grant_type="refresh_token",
                          client_id=connection.client_id, refresh_token=connection.refresh_token,
                          resource=RESOURCE))
    return await _connection(data, connection.client_id, connection.host_id,
                             connection.subject, None, client, now, connection)
