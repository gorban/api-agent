"""Lazy personal subscription authentication through the provider boundary."""
from pathlib import Path

import httpx

from api_agent.providers.contracts import ContractError, InteractionRequiredError, ProviderCapabilities, SetupPrerequisiteError
from .connect import _dispose, connect_account
from .connections import ConnectionStore, SubscriptionSession
from .contracts import AuthError
from .browser import subscription_browser
from .oauth import refresh_connection
from .api_key import APIKeySession
from .endpoint import validate_base_url


class OwnedSubscriptionSession(SubscriptionSession):
    """Own refresh transport on the same loop as the subscription session."""

    def __init__(self, store, profile):
        self.client = None
        super().__init__(store, profile, self._refresh)

    async def _refresh(self, connection, now):
        if self.client is None:
            self.client = httpx.AsyncClient()
        return await refresh_connection(connection, client=self.client, now=now)

    async def _dispose(self):
        await super()._dispose()
        if self.client is not None:
            await _dispose(self.client.aclose())
            self.client = None


class GPTProvider:
    version = 1
    capabilities = ProviderCapabilities(kind="llm", operations=("describe", "complete"))

    def __init__(self, config, *, inspection=None):
        self.inspection = inspection
        from api_agent.telemetry.accounting import compatibility
        from api_agent.providers.rates import cached_rate_card
        self.cost_compatibility = compatibility(config)
        self.rate_card = cached_rate_card(config=config)
        self.auth_mode = config.get("GPT_AUTH_MODE", "subscription")
        self.capabilities = ProviderCapabilities(kind="llm", operations=("describe", "complete"),
                                                 features=("cache_policy",) if self.auth_mode == "subscription" else ("max_output_tokens", "cache_policy"))
        self.cache_policy = config.get('LLM_CACHE_POLICY')
        self.api_mode = config.get("GPT_API_MODE", "responses" if self.auth_mode == "subscription" else "completions")
        self.app_name = config.get("GPT_APP_NAME", "api-agent")
        self.base_url = config.get("GPT_BASE_URL", "https://api.openai.com/v1")
        self.run_ui = config.get("RUN_UI", False)
        self.allow_browser_interaction = config.get("NO_STD_IN") is False or inspection is not None
        self.state_dir = Path(config.get("GPT_STATE_DIR") or Path.home() / ".config/api-agent/gpt")
        self.sessions = []
        self.payment_usd = config.get('LLM_PAY_PERIOD_LIMIT', 100.0)
        self.quota_period = config.get('LLM_QUOTA_PERIOD', 'week')
        self.pay_period = config.get('LLM_PAY_PERIOD', 'month')
        self.browsers = []
        self.closed = False

    @staticmethod
    def _login_error(error):
        message = {
            'browser_closed': 'GPT login browser closed before authentication completed.',
            'browser_unavailable': 'GPT login browser could not open or navigate.',
            'callback_timeout': 'GPT sign-in timed out before authentication completed.',
            'username_submission_failed': 'GPT could not submit the configured username on the login page.',
            'login_step_failed': 'GPT could not complete the current login page action; retry or complete login in the visible browser.',
            'login_interaction_required': 'GPT login needs browser interaction; start attended or show the browser with RUN_UI=True or DEBUG.',
            'account_mismatch': 'GPT sign-in returned a different account from the selected login.',
        }.get(str(error), 'GPT sign-in failed; retry authentication through launch.py.')
        return InteractionRequiredError(message)

    async def asetup(self, *, interactive, user_index, credentials):
        if self.closed:
            raise ContractError("GPT provider is closed.")
        if not isinstance(self.auth_mode, str) or self.auth_mode not in {"subscription", "api_key"}:
            raise ContractError("GPT_AUTH_MODE must be subscription or api_key.")
        if not isinstance(self.api_mode, str) or self.api_mode not in {"responses", "completions"}:
            raise ContractError("GPT_API_MODE must be responses or completions.")
        if self.auth_mode == "api_key":
            self.base_url = validate_base_url(self.base_url)
            if user_index is not None and (type(user_index) is not int or user_index < 0):
                raise ContractError("The selected LLM User index must be a nonnegative integer.")
            if user_index is None or credentials is None:
                raise SetupPrerequisiteError("Select an LLM User with an API key stored in its password credential and configure the shared password-manager credential reader.")
            key = await credentials(user_index, "password")
            if self.closed:
                raise ContractError("GPT provider is closed.")
            session = APIKeySession(key)
            self.sessions.append(session)
            return session
        if self.api_mode != "responses":
            raise SetupPrerequisiteError("GPT subscription authentication requires responses mode; set GPT_API_MODE=responses.")
        store = ConnectionStore(self.state_dir)
        connections = store.list()
        username = None
        if user_index is not None:
            if credentials is None:
                raise ContractError("The selected unattended user requires a credential reader.")
            username = await credentials(user_index, "username")
            if not isinstance(username, str) or not username.strip():
                raise ContractError("The selected login must supply a nonempty username.")
            connections = tuple(connection for connection in connections
                                if connection.email.strip().casefold() == username.strip().casefold())
            username = username.strip()
            if len(connections) > 1:
                raise InteractionRequiredError("The selected login has no unique cached OAuth connection; use attended sign-in first.")
        if len(connections) == 1:
            profile = store.profile_id(connections[0])
        elif interactive or username is not None:
            # Let the verified OAuth result identify the account instead of a manual ID.
            browser_options = {}
            if self.inspection is not None:
                browser_options['inspection'] = self.inspection
            if not interactive:
                browser_options['headless'] = not (self.run_ui or self.inspection is not None)
            browser = subscription_browser(**browser_options)
            self.browsers.append(browser)
            async def login_credentials(kind):
                return await credentials(user_index, kind)
            login_options = ({'login_username': username, 'login_credentials': login_credentials,
                              'allow_interaction': not browser_options.get('headless', False)}
                             if username is not None else {})
            try:
                profile = await connect_account(store, browser_owner=browser, app_name=self.app_name, **login_options)
            except AuthError as error:
                if (str(error) == 'login_interaction_required' and browser_options.get('headless')
                        and (interactive or self.allow_browser_interaction)):
                    print('Headless GPT login needs browser interaction. Opening visible Chromium; retry or complete sign-in there.', flush=True)
                    visible_options = {'inspection': self.inspection} if self.inspection is not None else {}
                    visible = subscription_browser(**visible_options)
                    self.browsers.append(visible)
                    try:
                        profile = await connect_account(store, browser_owner=visible, app_name=self.app_name,
                                                        login_username=username, login_credentials=login_credentials,
                                                        allow_interaction=True)
                    except AuthError as retry_error:
                        raise self._login_error(retry_error) from None
                else:
                    raise self._login_error(error) from None
        else:
            raise InteractionRequiredError("GPT requires attended sign-in; start attended integration through launch.py to connect or select your account.")
        session = OwnedSubscriptionSession(store, profile)
        self.sessions.append(session)
        return session

    async def adescribe(self, *, auth=None):
        if self.closed:
            raise ContractError("GPT provider is closed.")
        from .discovery import discover_selections
        base = "https://api.openai.com/v1" if self.auth_mode == "subscription" else self.base_url
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            return await discover_selections(client=client, base_url=base, access_token=auth,
                                              subscription=self.auth_mode == "subscription")

    def validate_request(self, request):
        """Validate selected cache support before retrieving authentication."""
        from api_agent.providers.cache_policy import configured_request
        from .inference import validate_text_request
        request = configured_request(request, self.cache_policy)
        base = "https://api.openai.com/v1" if self.auth_mode == "subscription" else self.base_url
        validate_text_request(base_url=base, access_token='preflight-validation',
                              subscription=self.auth_mode == 'subscription', api_mode=self.api_mode, request=request)

    async def acomplete(self, request, *, auth=None):
        if self.closed:
            raise ContractError("GPT provider is closed.")
        from .inference import complete_text, validate_text_request
        from api_agent.providers.cache_policy import configured_request
        request = configured_request(request, self.cache_policy)
        if not isinstance(self.auth_mode, str) or self.auth_mode not in {'subscription', 'api_key'}:
            raise ContractError('GPT_AUTH_MODE must be subscription or api_key.')
        base = "https://api.openai.com/v1" if self.auth_mode == "subscription" else self.base_url
        validate_text_request(base_url=base, access_token=auth, subscription=self.auth_mode == 'subscription',
                              api_mode=self.api_mode, request=request)
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            return await complete_text(client=client, base_url=base, access_token=auth,
                                       subscription=self.auth_mode == "subscription", api_mode=self.api_mode,
                                       request=request)

    async def asnapshot(self, *, auth=None):
        if self.closed:
            raise ContractError('GPT provider is closed.')
        if self.auth_mode != 'subscription':
            return None
        from api_agent.telemetry.usage import QuotaReadError
        from .quota_client import read_quota
        connections = ConnectionStore(self.state_dir).list()
        matches = [connection for connection in connections if connection.access_token == auth]
        if len(matches) != 1:
            return QuotaReadError('identity')
        return await read_quota(matches[0], period=self.quota_period)

    def quote_cost(self, request, result):
        from .pricing import quote
        from api_agent.providers.cache_policy import configured_request
        from api_agent.providers.costs import period_seconds
        request = configured_request(request, self.cache_policy)
        return quote(request, result, direct=self.auth_mode == 'subscription' or self.base_url == 'https://api.openai.com/v1',
                     subscription=self.auth_mode == 'subscription', rate_card=self.rate_card, compatibility=self.cost_compatibility, payment_usd=self.payment_usd, quota_seconds=period_seconds(self.quota_period), pay_period=self.pay_period)

    async def aclose(self):
        self.closed = True
        for session in self.sessions:
            await _dispose(session.aclose())
        self.sessions.clear()
        for browser in self.browsers:
            await _dispose(browser.aclose())
        self.browsers.clear()


def factory(config, services):
    """Create without reading saved state or starting transport/browser work."""
    from collections.abc import Mapping
    return GPTProvider(config, inspection=services.get('browser_inspection') if isinstance(services, Mapping) else None)
