"""Bounded, owned SDK credential readiness probe used only during acquisition."""
import os
import time

from api_agent.providers.contracts import ContractError
from .acquisition import SessionMaterial, identity_binding
from .login_transport import LoginTransport


class AmbientSession:
    def __init__(self, *, environment=None):
        self._environment = dict(environment or {})
        self.transport = LoginTransport()
        self._identity = None
        self._binding = None

    @property
    def verified_identity(self):
        return self._identity

    async def acquire(self, request, *, account=None, principal=None):
        binding = (request.profile, request.region, request.user_index)
        if self._binding is not None and binding != self._binding:
            raise ContractError('Bedrock SDK session binding changed')
        self._binding = binding
        if self._identity is not None:
            account, principal = self._identity
        environment = dict(os.environ)
        environment.update(self._environment)
        result = await self.transport.acquire(dict(method='ambient', login=False,
            profile=request.profile, region=request.region, account=account, principal=principal), environment)
        if type(result) is not dict:
            raise ContractError('Invalid Bedrock readiness result')
        if 'unavailable' in result:
            route = result.get('sso')
            if (set(result) not in ({'unavailable', 'sso'}, {'unavailable', 'sso', 'identity'}) or result['unavailable'] is not True
                    or type(route) is not dict
                    or not set(route) <= {'sso_start_url', 'sso_region', 'sso_account_id', 'sso_role_name'}
                    or any(value is not None and type(value) is not str for value in route.values())):
                raise ContractError('Invalid Bedrock readiness result')
            if 'identity' in result:
                self._identity = identity_binding(result['identity'])
            return None, route
        if (set(result) != {'AccessKeyId', 'SecretAccessKey', 'SessionToken', 'expires_at', 'region', 'identity'}
                or type(result['region']) is not str):
            raise ContractError('Invalid Bedrock readiness result')
        binding = identity_binding(result['identity'])
        token = result.get('SessionToken')
        expiry = result.get('expires_at')
        # Unknown-expiry session tokens get a periodic readiness re-probe, not a
        # fabricated credential expiration. Static keys have neither deadline.
        material = SessionMaterial(kind='frozen', access_key=result.get('AccessKeyId'),
            secret_key=result.get('SecretAccessKey'), session_token=token,
            expires_at=expiry, resolved_region=result.get('region'), refresh_after=time.time() + 60 if token is not None and expiry is None else None)
        material.worker_config(request, time.time())
        self._identity = binding
        return material, {}

    async def aclose(self):
        await self.transport.aclose()
