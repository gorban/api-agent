"""Lazy personal subscription authentication through the provider boundary."""
from pathlib import Path

import httpx

from api_agent.providers.contracts import ContractError, InteractionRequiredError, ProviderCapabilities, SetupPrerequisiteError
from .connect import _dispose, connect_account
from .connections import ConnectionStore, SubscriptionSession
from .contracts import AuthError
from .browser import subscription_browser
from .oauth import refresh_connection


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
    capabilities = ProviderCapabilities(kind="llm", operations=())

    def __init__(self, config, *, inspection=None):
        self.inspection = inspection
        self.auth_mode = config.get("GPT_AUTH_MODE", "subscription")
        self.api_mode = config.get("GPT_API_MODE", "responses" if self.auth_mode == "subscription" else "completions")
        self.app_name = config.get("GPT_APP_NAME", "api-agent")
        self.run_ui = config.get("RUN_UI", False)
        self.attended_fallback = config.get("NO_STD_IN") is False or inspection is not None
        self.state_dir = Path(config.get("GPT_STATE_DIR") or Path.home() / ".config/api-agent/gpt")
        self.sessions = []
        self.browsers = []
        self.closed = False

    @staticmethod
    def _login_error(error):
        message = {
            'browser_closed': 'GPT login browser closed before authentication completed.',
            'browser_unavailable': 'GPT login browser could not open or navigate.',
            'callback_timeout': 'GPT sign-in timed out before authentication completed.',
            'username_submission_failed': 'GPT could not submit the configured username on the login page.',
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
        if self.auth_mode != "subscription":
            raise SetupPrerequisiteError("GPT API-key authentication is pending implementation; select subscription authentication.")
        if self.api_mode != "responses":
            raise SetupPrerequisiteError("GPT subscription authentication requires responses mode; set GPT_API_MODE=responses.")
        store = ConnectionStore(self.state_dir)
        connections = store.list()
        username = None
        fallback = False
        if user_index is not None and credentials is not None:
            try:
                username = await credentials(user_index, "username")
            except InteractionRequiredError:
                if not (interactive or self.attended_fallback):
                    raise InteractionRequiredError("Password-manager username retrieval needs attention. Sign in to the configured CLI or start attended login; noninteractive execution cannot fall back.") from None
                print("Password-manager username retrieval is unavailable. Sign in to the configured CLI and check the login entry; continuing with attended browser login.", flush=True)
                fallback = True
                connections = ()
        if user_index is not None and credentials is not None and not fallback:
            if not isinstance(username, str) or not username.strip():
                raise ContractError("The selected login must supply a nonempty username.")
            connections = tuple(connection for connection in connections
                                if connection.email.strip().casefold() == username.strip().casefold())
            username = username.strip()
            if len(connections) > 1:
                raise InteractionRequiredError("The selected login has no unique cached OAuth connection; use attended sign-in first.")
        if len(connections) == 1:
            profile = store.profile_id(connections[0])
        elif interactive or username is not None or fallback:
            # Let the verified OAuth result identify the account instead of a manual ID.
            browser_options = {}
            if self.inspection is not None:
                browser_options['inspection'] = self.inspection
            if not interactive and not fallback:
                browser_options['headless'] = not (self.run_ui or self.inspection is not None)
            browser = subscription_browser(**browser_options)
            self.browsers.append(browser)
            login_options = ({'login_username': username,
                              'allow_interaction': not browser_options.get('headless', False)}
                             if username is not None else {})
            try:
                profile = await connect_account(store, browser_owner=browser, app_name=self.app_name, **login_options)
            except AuthError as error:
                if (str(error) == 'login_interaction_required' and browser_options.get('headless')
                        and (interactive or self.attended_fallback)):
                    print('Headless GPT login needs browser interaction. Opening visible Chromium; retry or complete sign-in there.', flush=True)
                    visible_options = {'inspection': self.inspection} if self.inspection is not None else {}
                    visible = subscription_browser(**visible_options)
                    self.browsers.append(visible)
                    try:
                        profile = await connect_account(store, browser_owner=visible, app_name=self.app_name,
                                                        login_username=username, allow_interaction=True)
                    except AuthError as retry_error:
                        raise self._login_error(retry_error) from None
                else:
                    raise self._login_error(error) from None
        else:
            raise InteractionRequiredError("GPT requires attended sign-in; start attended integration through launch.py to connect or select your account.")
        session = OwnedSubscriptionSession(store, profile)
        self.sessions.append(session)
        return session

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
