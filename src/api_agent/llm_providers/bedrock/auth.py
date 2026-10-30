"""Opaque credentials and lazily selected provider-local acquisition owners."""
import asyncio

from api_agent.providers.contracts import ContractError, InteractionRequiredError, SessionClosedError, UnattendedCredentialError
from api_agent.providers.progress import component_timeout, independent_timeout_scope, report_timeout
from api_agent.providers.control import finish_cleanup, CleanupPendingError


class SelectedCredentialReader:
    """Revocable ownership of selected-account reads passed to an adapter."""
    def __init__(self, credentials, user_index):
        self._credentials = credentials
        self._index = user_index
        self._closed = False
        self._reads = set()
        self._closing = None

    def __repr__(self):
        return '<SelectedCredentialReader>'

    def revoke(self):
        self._closed = True

    def _live(self):
        if self._closed:
            raise SessionClosedError('Bedrock credential reader is closed')

    async def _read(self, index, kind):
        async with report_timeout('Bedrock selected credential helper', 60):
            try:
                value = await self._credentials(index, kind)
            except UnattendedCredentialError:
                raise
            except Exception:
                raise InteractionRequiredError('Unable to read selected Bedrock credentials.') from None
            self._live()
            return value

    async def __call__(self, index, kind):
        self._live()
        if (type(index) is not int or type(self._index) is not int or index < 0
                or index != self._index or type(kind) is not str
                or kind not in {'username', 'password', 'totp'}):
            raise InteractionRequiredError('Bedrock credentials require the selected account.')
        with independent_timeout_scope():
            task = asyncio.create_task(self._read(index, kind))
        self._reads.add(task)
        try:
            async with component_timeout('Bedrock selected credential read', 60):
                return await asyncio.shield(task)
        finally:
            async def settle():
                if not task.done() and not task.cancelling():
                    task.cancel()
                await self._drain((task,))
            with independent_timeout_scope():
                await finish_cleanup(settle())

    async def _drain(self, tasks):
        async with report_timeout('Bedrock selected credential cleanup', 60) as budget:
            if not tasks:
                return
            done, pending = await asyncio.wait(tasks, timeout=budget.remaining)
            await asyncio.gather(*done, return_exceptions=True)
            self._reads.difference_update(done)
            if pending:
                raise CleanupPendingError((self,), self.aclose)

    async def _dispose(self):
        tasks = tuple(self._reads)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await self._drain(tasks)
        self._credentials = None

    async def aclose(self):
        self.revoke()
        if self._closing is None or self._closing.done():
            with independent_timeout_scope():
                self._closing = asyncio.create_task(self._dispose())
        async def drain():
            await asyncio.shield(self._closing)
        await finish_cleanup(drain())


class BedrockAuthSession:
    def __init__(self, values):
        self._values = values

    def __repr__(self):
        return '<BedrockAuthSession>'

    async def __call__(self):
        self.worker_config()
        return self

    def worker_config(self):
        if self._values is None:
            raise SessionClosedError('Bedrock session is closed')
        return dict(self._values)

    async def aclose(self):
        self._values = None


def validate_config(config):
    """Validate mode choices before credential acquisition or adapter loading."""
    mode = config.get('BEDROCK_AUTH_MODE', 'session')
    api_mode = config.get('BEDROCK_API_MODE', 'responses')
    if type(mode) is not str or mode not in {'session', 'access_key', 'api_key'}:
        raise ContractError('Unsupported Bedrock authentication mode')
    if type(api_mode) is not str or api_mode not in {'completions', 'responses'}:
        raise ContractError('Unsupported Bedrock API mode')
    return api_mode


async def create_session(config, *, interactive, user_index, credentials, services=None):
    async with component_timeout('Bedrock credential acquisition', 60):
        validate_config(config)
        mode = config.get('BEDROCK_AUTH_MODE', 'session')
        values = {'mode': mode, 'profile': config.get('BEDROCK_PROFILE') or None,
                  'region': config.get('BEDROCK_REGION') or None}
        if mode == 'session':
            if user_index is not None and (type(user_index) is not int or user_index < 0):
                raise InteractionRequiredError('Bedrock credentials require the selected account.')
            from api_agent.llm_providers.bedrock.acquisition import SessionRequest
            from api_agent.llm_providers.bedrock.session_providers import load_acquirer
            from api_agent.llm_providers.bedrock.sessions import AcquiredSession
            reader = SelectedCredentialReader(credentials, user_index) if credentials is not None else None
            services = services or {}
            interaction = services.get('interaction', services.get('bedrock_interaction'))
            allow_interaction = bool(interactive) and (interaction is not None or not config.get('NO_STD_IN', False))
            request = SessionRequest(profile=values['profile'], region=values['region'],
                user_index=user_index, interactive=allow_interaction,
                credentials=reader,
                interaction=interaction if allow_interaction else None)
            session = None
            try:
                acquirer = await load_acquirer(config.get('BEDROCK_SESSION_PROVIDER', ''), config, services)
                session = AcquiredSession(acquirer, request, credential_owner=reader)
                await session()
            except BaseException as original_error:
                try:
                    if session is not None:
                        await finish_cleanup(session.aclose())
                    elif reader is not None:
                        await finish_cleanup(reader.aclose())
                except BaseException as error:
                    retained = []
                    for owner in (*getattr(original_error, 'retained_owners', ()),
                                  *getattr(error, 'retained_owners', ()),
                                  session if session is not None else reader):
                        if not any(owner is existing for existing in retained):
                            retained.append(owner)
                    safe = asyncio.CancelledError() if isinstance(error, asyncio.CancelledError) else ContractError('Bedrock session cleanup failed')
                    safe.retained_owners = tuple(retained)
                    raise safe from None
                raise
            return session
        if user_index is not None and (type(user_index) is not int or user_index < 0):
            raise InteractionRequiredError('Bedrock credentials require the selected account.')
        attended = None
        if user_index is None:
            from api_agent.llm_providers.bedrock.key_interaction import collect_keys
            services = services or {}
            owner = services.get('interaction', services.get('bedrock_interaction'))
            if not interactive or config.get('NO_STD_IN', False) or owner is None:
                raise InteractionRequiredError('Bedrock credentials require private interaction.')
            attended = await collect_keys(mode, owner)
        if mode == 'api_key':
            if attended is None and credentials is None:
                raise InteractionRequiredError('Bedrock api_key requires a selected credential account.')
            try:
                secret = attended['api_key'] if attended is not None else await credentials(user_index, 'password')
            except UnattendedCredentialError:
                raise
            except Exception:
                raise InteractionRequiredError('Unable to read selected Bedrock credentials.') from None
            if not isinstance(secret, str) or not secret.strip():
                raise ContractError('Bedrock api_key requires a password field.')
            values.update(api_key=secret, profile=None)
        else:  # access_key, validated before acquisition
            if attended is None and credentials is None:
                raise InteractionRequiredError('Bedrock access_key requires a selected credential account.')
            try:
                if attended is not None:
                    key, secret = attended['access_key'], attended['secret_key']
                else:
                    key = await credentials(user_index, 'username')
                    secret = await credentials(user_index, 'password')
            except UnattendedCredentialError:
                raise
            except Exception:
                raise InteractionRequiredError('Unable to read selected Bedrock credentials.') from None
            if not isinstance(key, str) or not isinstance(secret, str) or not key.strip() or not secret.strip():
                raise ContractError('Bedrock access_key requires username and password fields.')
            if key.startswith('ASIA'):
                raise ContractError('Temporary access keys require a session token; use session mode.')
            values.update(access_key=key, secret_key=secret)
        return BedrockAuthSession(values)
