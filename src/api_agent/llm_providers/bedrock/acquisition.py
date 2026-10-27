"""Opaque, provider-local session acquisition contracts."""
import math
import re
from dataclasses import dataclass
from typing import Protocol

from api_agent.providers.contracts import ContractError


def identity_binding(identity):
    """Validate the worker's canonical STS account/ARN before pinning it."""
    if type(identity) is not dict or set(identity) != {'Account', 'Arn'}:
        raise ContractError('Invalid Bedrock verified identity')
    account, arn = identity['Account'], identity['Arn']
    if type(account) is not str or not re.fullmatch(r'\d{12}', account) or type(arn) is not str:
        raise ContractError('Invalid Bedrock verified identity')
    match = re.fullmatch(r'arn:aws(?:-us-gov|-cn)?:(iam|sts)::(\d{12}):([^\s]+)', arn)
    if match is None or match[2] != account:
        raise ContractError('Invalid Bedrock verified identity')
    assumed_role = match[1] == 'sts' and match[3].startswith('assumed-role/')
    return account, arn.rpartition('/')[0] if assumed_role else arn


@dataclass(frozen=True, repr=False)
class SessionRequest:
    profile: str | None
    region: str | None
    user_index: int | None
    interactive: bool
    credentials: object = None
    interaction: object = None

    def __repr__(self):
        return '<SessionRequest>'


@dataclass(frozen=True, repr=False)
class SessionMaterial:
    kind: str
    user_index: int | None = None
    access_key: str | None = None
    secret_key: str | None = None
    session_token: str | None = None
    expires_at: float | None = None
    refresh_after: float | None = None
    resolved_region: str | None = None

    def __repr__(self):
        return '<SessionMaterial>'

    def worker_config(self, request, now):
        if request.user_index is not None and (type(request.user_index) is not int or request.user_index < 0):
            raise ContractError('Bedrock session account binding mismatch')
        if type(self.kind) is not str:
            raise ContractError('Invalid Bedrock session material kind')
        if self.resolved_region is not None:
            if (self.kind != 'frozen' or type(self.resolved_region) is not str
                    or not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]+', self.resolved_region)
                    or request.region is not None and request.region != self.resolved_region):
                raise ContractError('Invalid Bedrock resolved region binding')
        values = {'profile': request.profile, 'region': self.resolved_region or request.region}
        if self.kind == 'profile':
            if any(value is not None for value in (self.user_index, self.access_key, self.secret_key, self.session_token, self.expires_at, self.refresh_after, request.user_index)):
                raise ContractError('Invalid Bedrock profile session material')
            return dict(values, mode='session')
        if self.kind == 'frozen':
            if self.user_index != request.user_index or request.user_index is not None:
                raise ContractError('Bedrock session account binding mismatch')
            if not all(type(value) is str and value.strip() for value in (self.access_key, self.secret_key)):
                raise ContractError('Incomplete Bedrock frozen credentials')
            if self.session_token is not None and (type(self.session_token) is not str or not self.session_token.strip()):
                raise ContractError('Invalid Bedrock frozen session token')
            for deadline in (self.expires_at, self.refresh_after):
                if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= now):
                    raise ContractError('Expired or invalid Bedrock frozen credentials')
            if self.session_token is not None and self.expires_at is None and self.refresh_after is None:
                raise ContractError('Bedrock frozen session requires a refresh policy')
            return dict(values, mode='access_key' if self.session_token is None else 'temporary',
                        access_key=self.access_key, secret_key=self.secret_key, session_token=self.session_token)
        if self.kind != 'temporary':
            raise ContractError('Invalid Bedrock session material kind')
        if self.refresh_after is not None:
            raise ContractError('Invalid Bedrock temporary refresh policy')
        if ((self.user_index is not None and (type(self.user_index) is not int or self.user_index < 0)) or
                self.user_index != request.user_index):
            raise ContractError('Bedrock session account binding mismatch')
        if not all(type(value) is str and value.strip() for value in (self.access_key, self.secret_key, self.session_token)):
            raise ContractError('Incomplete Bedrock temporary session material')
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at) or self.expires_at <= now:
            raise ContractError('Expired or invalid Bedrock temporary session material')
        return dict(values, mode='temporary', access_key=self.access_key,
                    secret_key=self.secret_key, session_token=self.session_token)


class SessionAcquirer(Protocol):
    version: int = 1

    async def acquire(self, request: SessionRequest, *, previous: SessionMaterial | None = None) -> SessionMaterial: ...

    async def aclose(self) -> None: ...
