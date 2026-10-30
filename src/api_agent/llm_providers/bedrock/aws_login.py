"""Selected SDK readiness, then attended AWS recovery in an owned profile."""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from urllib.parse import urlsplit

from api_agent.providers.contracts import ConfigurationRequiredError, ContractError, InteractionRequiredError, SessionClosedError
from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import component_timeout
from api_agent.llm_providers.bedrock.acquisition import SessionMaterial, identity_binding
from api_agent.llm_providers.bedrock.ambient import AmbientSession
from api_agent.llm_providers.bedrock.login_transport import LoginTransport
from api_agent.llm_providers.bedrock.persistence import capture_paths
from api_agent.llm_providers.bedrock.login_gate import profile_login_gate


class AwsLoginAcquirer:
    version = 1

    def __init__(self, config, *, method="direct"):
        self._method = method
        self._account = config.get('BEDROCK_LOGIN_EXPECTED_ACCOUNT_ID', '')
        self._principal = config.get('BEDROCK_LOGIN_EXPECTED_PRINCIPAL', '')
        if (self._method not in {'direct', 'sso'} or type(self._account) is not str
                or type(self._principal) is not str
                or self._account and not re.fullmatch(r'\d{12}', self._account)
                or self._principal and not re.fullmatch(r'arn:aws(?:-us-gov|-cn)?:(?:iam::\d{12}:user/[^\s]+|sts::\d{12}:assumed-role/[^/\s]+)', self._principal)
                or self._account and self._principal and self._principal.split(':')[4] != self._account):
            raise ContractError('AWS sign-in identity assertion is invalid')
        self._config = dict(config)
        self._sso = None
        self._persistence = capture_paths()
        self._ambient = AmbientSession(environment=dict(HOME=self._persistence['home'],
            AWS_CONFIG_FILE=self._persistence['config'],
            AWS_SHARED_CREDENTIALS_FILE=self._persistence['credentials'],
            AWS_LOGIN_CACHE_DIRECTORY=self._persistence['login']))
        self._transport = LoginTransport()
        self._root = None
        self._closed = False
        self._logged_in = False
        self._ambient_selected = False
        self._verified_identity = None
        self._request_binding = None
        self._interaction = None
        self._lock = asyncio.Lock()

    def __repr__(self):
        return '<AwsLoginAcquirer>'

    def _sso_route(self, route):
        names = ('SSO_START_URL', 'SSO_REGION', 'SSO_ACCOUNT_ID', 'SSO_ROLE_NAME')
        values = tuple(self._config.get('BEDROCK_LOGIN_' + name) or route.get(name.lower()) for name in names)
        missing = tuple('BEDROCK_LOGIN_' + name for name, value in zip(names, values) if not value)
        if missing:
            raise ConfigurationRequiredError(missing)
        start, region, account, role = values
        if any(type(value) is not str or any(ord(char) < 32 for char in value) for value in values):
            raise ContractError('AWS sign-in SSO configuration is invalid')
        parsed = urlsplit(start)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ContractError('AWS sign-in SSO URL is invalid')
        if not re.fullmatch(r'[a-z0-9-]+', region) or not re.fullmatch(r'\d{12}', account) or not re.fullmatch(r'[\w+=,.@-]+', role):
            raise ContractError('AWS sign-in SSO configuration is invalid')
        self._sso = values

    def _prepare(self, request):
        profile, region = request.profile, request.region
        if (type(profile) is not str or type(region) is not str
                or not re.fullmatch(r'[\w-]+', profile) or not re.fullmatch(r'[a-z0-9-]+', region)):
            raise ContractError('AWS sign-in profile or region is invalid')
        binding = (profile, region, request.user_index)
        if self._request_binding is not None and binding != self._request_binding:
            raise ContractError('AWS sign-in identity binding changed')
        self._request_binding = binding
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix='api-agent-login-'))
            aws = self._root / '.aws'
            aws.mkdir(mode=0o700)
            login = aws / 'login' / 'cache'
            login.parent.mkdir(mode=0o700)
            login.mkdir(mode=0o700)
            (aws / 'sso').mkdir(mode=0o700)
            (aws / 'sso' / 'cache').mkdir(mode=0o700)
            section = 'default' if profile == 'default' else 'profile ' + profile
            text = f'[{section}]\nregion = {region}\n'
            if self._sso is not None:
                start, sso_region, sso_account, role = self._sso
                session = 'api-agent-' + hashlib.sha256(profile.encode()).hexdigest()[:16]
                text += f'sso_session = {session}\nsso_account_id = {sso_account}\nsso_role_name = {role}\n\n[sso-session {session}]\nsso_start_url = {start}\nsso_region = {sso_region}\nsso_registration_scopes = sso:account:access\n'
            for name, content in (('config', text), ('credentials', '')):
                target = aws / name
                target.touch(mode=0o600)
                target.write_text(content)
        environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT') if key in os.environ}
        environment.update(HOME=str(self._root), AWS_CONFIG_FILE=str(self._root / '.aws' / 'config'),
                           AWS_SHARED_CREDENTIALS_FILE=str(self._root / '.aws' / 'credentials'),
                           AWS_LOGIN_CACHE_DIRECTORY=str(self._root / '.aws' / 'login' / 'cache'),
                           AWS_EC2_METADATA_DISABLED='true', AWS_CLI_AUTO_PROMPT='off', AWS_PAGER='')
        account, principal = self._verified_identity or (self._account, self._principal)
        return dict(method=self._method, account=account, principal=principal,
                    profile=profile, region=region, login=not self._logged_in, persistence=self._persistence), environment

    async def acquire(self, request, *, previous=None):
        async with component_timeout('AWS sign-in acquisition', 60):
            async with self._lock:
                if self._closed:
                    raise SessionClosedError('AWS sign-in acquirer is closed')
                if request.user_index is not None:
                    raise InteractionRequiredError('Indexed AWS sign-in requires separate identity qualification.')
                async with profile_login_gate(self._persistence, request.profile):
                    return await self._acquire_admitted(request)

    async def _acquire_admitted(self, request):
        if self._closed:
            raise SessionClosedError('AWS sign-in acquirer is closed')
        if not self._logged_in:
            material, route = await self._ambient.acquire(request, account=self._account, principal=self._principal)
            if self._closed:
                raise SessionClosedError('AWS sign-in acquirer is closed')
            if material is not None:
                self._ambient_selected = True
                return material
            self._verified_identity = self._ambient.verified_identity
            if self._ambient_selected:
                raise InteractionRequiredError('Selected SDK credentials need reauthentication; restart the session after sign-in.')
            if self._method == 'sso':
                self._sso_route(route)
        if not self._logged_in and (not request.interactive or request.interaction is None):
            raise InteractionRequiredError('AWS sign-in requires a visible interaction owner.')
        config, environment = self._prepare(request)
        if config['login']:
            from api_agent.llm_providers.bedrock.login_interaction import LoginInteraction
            if self._interaction is None:
                self._interaction = LoginInteraction(request)
            await self._interaction.open()
        try:
            result = await self._transport.acquire(config, environment, self._interaction)
            if type(result) is not dict or set(result) != {'AccessKeyId', 'SecretAccessKey', 'SessionToken', 'expires_at', 'identity'}:
                raise ContractError('Invalid AWS sign-in result')
            binding = identity_binding(result['identity'])
            if self._verified_identity is not None and binding != self._verified_identity:
                raise ContractError('AWS sign-in verified identity changed')
            material = SessionMaterial(kind='temporary', access_key=result.get('AccessKeyId'),
                secret_key=result.get('SecretAccessKey'), session_token=result.get('SessionToken'),
                expires_at=result.get('expires_at'))
            material.worker_config(request, time.time())
            if self._closed:
                raise SessionClosedError('AWS sign-in acquirer is closed')
            self._verified_identity = binding
            self._logged_in = True
            return material
        finally:
            if self._interaction is not None:
                await finish_cleanup(self._interaction.aclose())
                self._interaction = None

    async def aclose(self):
        self._closed = True
        async with component_timeout('AWS sign-in cleanup', 60):
            try:
                await finish_cleanup(self._transport.aclose())
            finally:
                await finish_cleanup(self._ambient.aclose())
            async with self._lock:
                if self._interaction is not None:
                    await finish_cleanup(self._interaction.aclose())
                    self._interaction = None
                if self._root is not None:
                    shutil.rmtree(self._root)
                    self._root = None


def factory(config, services):
    return AwsLoginAcquirer(config)
