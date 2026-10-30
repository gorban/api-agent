"""Deterministic authentication-only factory checks without live transports."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from api_agent.llm_providers.gpt import factory as module
from api_agent.llm_providers.gpt.connections import ConnectionStore
from api_agent.llm_providers.gpt.contracts import Connection
from api_agent.providers.contracts import ContractError, InteractionRequiredError, SetupPrerequisiteError
from api_agent.providers.control import CleanupPendingError


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def store(monkeypatch):
    class Store:
        entries = ()
        loaded = []
        def __init__(self, path):
            self.path = path
        def list(self):
            return self.entries
        def load(self, profile):
            self.loaded.append(profile)
        def profile_id(self, connection):
            return connection if isinstance(connection, str) else connection.profile
    monkeypatch.setattr(module, "ConnectionStore", Store)
    return Store


def setup(provider, interactive=False):
    return provider.asetup(interactive=interactive, user_index=None, credentials=None)


def test_factory_no_io(monkeypatch):
    monkeypatch.setattr(module, "ConnectionStore", lambda *_: pytest.fail("storage at creation"))
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda: pytest.fail("transport at creation"))
    provider = module.factory({}, object())
    assert provider.version == 1
    assert provider.capabilities.operations == ("describe", "complete")
    assert provider.capabilities.credential_roles == ()
    assert provider.state_dir == module.Path.home() / ".config/api-agent/gpt"
    assert module.factory({"GPT_AUTH_MODE": "api_key", "GPT_STATE_DIR": "/tmp/test"}, None).api_mode == "completions"


@pytest.mark.parametrize("config,message", [({"GPT_AUTH_MODE": "api_key"}, "Select an LLM User"), ({"GPT_API_MODE": "completions"}, "requires responses")])
def test_route_rejected_before_io(config, message, monkeypatch):
    monkeypatch.setattr(module, "ConnectionStore", lambda *_: pytest.fail("storage"))
    with pytest.raises(SetupPrerequisiteError, match=message):
        run(setup(module.factory(config, None)))


@pytest.mark.parametrize('name', ['GPT_AUTH_MODE', 'GPT_API_MODE'])
@pytest.mark.parametrize('value', [None, False, 42, [], 'malformed'])
def test_malformed_modes_are_errors_not_missing_prerequisites(name, value, monkeypatch):
    monkeypatch.setattr(module, 'ConnectionStore', lambda *_: pytest.fail('unexpected storage'))
    with pytest.raises(ContractError, match=name) as error:
        run(setup(module.factory({name: value}, None)))
    assert not isinstance(error.value, SetupPrerequisiteError)


def test_uncached_and_ambiguous(store, monkeypatch):
    monkeypatch.setattr(module, "connect_account", AsyncMock(side_effect=AssertionError("browser")))
    provider = module.factory({}, None)
    with pytest.raises(InteractionRequiredError, match="attended sign-in.*start attended"):
        run(setup(provider))
    store.entries = ("one", "two")
    with pytest.raises(InteractionRequiredError, match="attended sign-in"):
        run(setup(provider))


@pytest.mark.parametrize("config,entries,interactive,expected", [({}, ("single",), False, "single"), ({}, ("one", "two"), True, "new"), ({}, (), True, "new")])
def test_select_and_close(config, entries, interactive, expected, store, monkeypatch):
    store.entries = entries
    connect = AsyncMock(return_value="new")
    monkeypatch.setattr(module, "connect_account", connect)
    async def scenario():
        provider = module.factory(config, None)
        session = await setup(provider, interactive)
        assert session.profile_id == expected
        assert session.client is None
        await provider.aclose()
        await provider.aclose()
        assert session._closed
        with pytest.raises(ContractError, match="closed"):
            await setup(provider)
    run(scenario())
    assert connect.await_count == int(len(entries) != 1)


def test_cached_setup_never_allocates_a_browser(store, monkeypatch):
    store.entries = ('single',)
    monkeypatch.setattr(module, 'subscription_browser', lambda: pytest.fail('browser allocated for cached setup'))
    async def scenario():
        provider = module.factory({}, None)
        session = await setup(provider)
        assert session.profile_id == 'single' and not provider.browsers
        await provider.aclose()
    run(scenario())


@pytest.mark.parametrize('config,expected', [({}, 'api-agent'), ({'GPT_APP_NAME': 'My Agent'}, 'My Agent')])
def test_application_name_reaches_attended_connection(config, expected, store, monkeypatch):
    connect = AsyncMock(return_value='new')
    monkeypatch.setattr(module, 'connect_account', connect)
    async def scenario():
        provider = module.factory(config, None)
        await setup(provider, interactive=True)
        assert connect.await_args.kwargs['app_name'] == expected
        await provider.aclose()
    run(scenario())


@pytest.mark.parametrize('mode', ['run-local','integration','e2e'])
@pytest.mark.parametrize('run_ui', [False, True])
def test_failed_attended_setup_keeps_browser_until_cleanup_confirmed(store, monkeypatch, run_ui, mode):
    browser = SimpleNamespace(aclose=AsyncMock())
    browser.aclose.side_effect = [CleanupPendingError((browser,), browser.aclose), None]
    options = []
    def create(**kwargs):
        options.append(kwargs)
        return browser
    monkeypatch.setattr(module, 'subscription_browser', create)
    connect = AsyncMock(side_effect=RuntimeError('consent failed'))
    monkeypatch.setattr(module, 'connect_account', connect)
    async def scenario():
        provider = module.factory({'TUI_MODE':mode,'RUN_UI': run_ui}, None)
        with pytest.raises(RuntimeError, match='consent failed'):
            await setup(provider, interactive=True)
        assert len(options) == 1
        assert options[0].get('headless', False) is False
        assert provider.browsers == [browser] and not provider.sessions
        assert connect.await_args.kwargs['browser_owner'] is browser
        with pytest.raises(CleanupPendingError):
            await provider.aclose()
        assert provider.browsers == [browser]
        await provider.aclose()
        assert not provider.browsers
        browser.aclose.assert_awaited()
        assert browser.aclose.await_count == 2
    run(scenario())


@pytest.mark.parametrize("username,entries,expected", [
    (" Person@example.test ", ("person@example.test", "other@example.test"), "0"),
    ("missing@example.test", ("other@example.test",), "new"),
    ("person@example.test", ("person@example.test", "person@example.test"), None),
    ("", (), "invalid"), (None, (), "invalid"),
])
def test_optional_username_selects_only_unique_matching_cache(username, entries, expected, store, monkeypatch):
    store.entries = tuple(SimpleNamespace(email=email, profile=str(index))
                          for index, email in enumerate(entries))
    reader = AsyncMock(return_value=username)
    connect = AsyncMock(return_value='new')
    monkeypatch.setattr(module, "connect_account", connect)
    async def scenario():
        provider = module.factory({}, None)
        try:
            if expected == "invalid":
                with pytest.raises(ContractError, match="nonempty username"):
                    await provider.asetup(interactive=False, user_index=1, credentials=reader)
            elif expected is None:
                with pytest.raises(InteractionRequiredError, match="no unique cached"):
                    await provider.asetup(interactive=False, user_index=1, credentials=reader)
            else:
                auth = await provider.asetup(interactive=False, user_index=1, credentials=reader)
                assert auth.profile_id == expected
        finally:
            await provider.aclose()
    run(scenario())
    reader.assert_awaited_once_with(1, "username")
    assert connect.await_count == int(expected == 'new')
    if expected == 'new':
        assert connect.await_args.kwargs['login_username'] == username.strip()


@pytest.mark.parametrize('mode', [None,'run-local','integration','e2e'])
@pytest.mark.parametrize('run_ui', [False, True])
@pytest.mark.parametrize('debug', [False, True])
def test_uncached_selected_username_drives_browser_visibility(store, monkeypatch, run_ui, debug, mode):
    inspection = object() if debug else None
    browser = SimpleNamespace(aclose=AsyncMock())
    options = []
    def create(**kwargs):
        options.append(kwargs)
        return browser
    monkeypatch.setattr(module, 'subscription_browser', create)
    connect = AsyncMock(return_value='new')
    monkeypatch.setattr(module, 'connect_account', connect)
    async def scenario():
        provider = module.factory({'TUI_MODE':mode,'RUN_UI': run_ui}, {'browser_inspection': inspection})
        reader = AsyncMock(return_value=' person@example.test ')
        session = await provider.asetup(interactive=False, user_index=0, credentials=reader)
        assert session.profile_id == 'new'
        reader.assert_awaited_once_with(0, 'username')
        assert connect.await_args.kwargs['login_username'] == 'person@example.test'
        assert options == [({'inspection': inspection} if debug else {}) | {'headless': not (mode == 'run-local' and run_ui or debug)}]
        assert connect.await_args.kwargs['allow_interaction'] is (mode == 'run-local' and run_ui or debug)
        credential_reader = connect.await_args.kwargs['login_credentials']
        assert await credential_reader('password') == ' person@example.test '
        assert await credential_reader('totp') == ' person@example.test '
        assert [call.args for call in reader.await_args_list] == [(0, 'username'), (0, 'password'), (0, 'totp')]
        await provider.aclose()
        browser.aclose.assert_awaited_once()
    run(scenario())


@pytest.mark.parametrize('interactive,no_std_in,debug', [
    (True, True, False), (False, False, False),
    (False, True, True), (False, True, False),
    (False, None, False),
])
def test_username_cli_failure_never_falls_back_to_another_identity(store, monkeypatch, capsys, interactive, no_std_in, debug):
    store.entries = (SimpleNamespace(email='unbound@example.test', profile='unbound'),)
    browser = SimpleNamespace(aclose=AsyncMock())
    created = []
    def create(**kwargs):
        created.append(kwargs)
        return browser
    monkeypatch.setattr(module, 'subscription_browser', create)
    connect = AsyncMock(return_value='verified-attended')
    monkeypatch.setattr(module, 'connect_account', connect)
    inspection = object() if debug else None
    async def scenario():
        config = {} if no_std_in is None else {'NO_STD_IN': no_std_in}
        provider = module.factory(config, {'browser_inspection': inspection})
        failure = InteractionRequiredError('Password-manager username retrieval is unavailable.')
        reader = AsyncMock(side_effect=failure)
        try:
            with pytest.raises(InteractionRequiredError) as error:
                await provider.asetup(interactive=interactive, user_index=0, credentials=reader)
            assert error.value is failure
            assert not created and not provider.sessions and not provider.browsers
            connect.assert_not_awaited()
            reader.assert_awaited_once_with(0, 'username')
        finally:
            await provider.aclose()
    run(scenario())
    assert not capsys.readouterr().out


def test_selected_unattended_user_requires_a_reader(store, monkeypatch):
    browser = AsyncMock()
    monkeypatch.setattr(module, 'subscription_browser', browser)
    async def scenario():
        provider = module.factory({}, None)
        with pytest.raises(ContractError, match='requires a credential reader'):
            await provider.asetup(interactive=True, user_index=0, credentials=None)
        await provider.aclose()
    run(scenario())
    browser.assert_not_called()


@pytest.mark.parametrize('code,expected', [
    ('browser_closed', 'browser closed'), ('browser_unavailable', 'could not open'),
    ('callback_timeout', 'timed out'), ('username_submission_failed', 'submit the configured username'),
    ('login_step_failed', 'current login page action'),
    ('login_interaction_required', 'needs browser interaction'),
    ('account_mismatch', 'different account'), ('private upstream text', 'retry authentication'),
])
def test_browser_auth_failure_crosses_provider_boundary_safely(store, monkeypatch, code, expected):
    from api_agent.llm_providers.gpt.contracts import AuthError
    browser = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(module, 'subscription_browser', lambda: browser)
    monkeypatch.setattr(module, 'connect_account', AsyncMock(side_effect=AuthError(code)))
    async def scenario():
        provider = module.factory({}, None)
        try:
            with pytest.raises(InteractionRequiredError, match=expected) as error:
                await setup(provider, interactive=True)
            assert 'private upstream text' not in str(error.value)
            assert not provider.sessions
        finally:
            await provider.aclose()
        browser.aclose.assert_awaited_once()
    run(scenario())


def test_refresh_transport_lifetime(store, monkeypatch):
    client = type("Client", (), {"aclose": AsyncMock()})()
    client_factory = lambda: client
    monkeypatch.setattr(module.httpx, "AsyncClient", client_factory)
    refresh = AsyncMock(return_value="updated")
    monkeypatch.setattr(module, "refresh_connection", refresh)
    async def scenario():
        session = module.OwnedSubscriptionSession(store("path"), "one")
        assert await session._refresh("connection", 1) == "updated"
        assert await session._refresh("connection", 2) == "updated"
        await session.aclose()
        await session.aclose()
        assert session.client is None
    run(scenario())
    client.aclose.assert_awaited_once()


def test_cancelled_dispose_retains_transport(store, monkeypatch):
    client = type("Client", (), {"aclose": AsyncMock()})()
    async def cancelled(self):
        raise asyncio.CancelledError
    monkeypatch.setattr(module.SubscriptionSession, "_dispose", cancelled)
    async def scenario():
        session = module.OwnedSubscriptionSession(store("path"), "one")
        session.client = client
        with pytest.raises(asyncio.CancelledError):
            await session.aclose()
        assert session.client is client
    run(scenario())
    client.aclose.assert_not_awaited()


def test_cancelled_cleanup_waits_for_refresh_before_transport_close(tmp_path, monkeypatch):
    store = ConnectionStore(tmp_path / "state")
    record = Connection("subject", "client", store.host_id(), "person@example.test",
                        "access", "refresh", "identity", ("scope",), 0)
    profile = store.save(record)
    client = type("Client", (), {"aclose": AsyncMock()})()
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda: client)

    async def scenario():
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def refresh(connection, *, client, now, pending):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            client.aclose.assert_not_awaited()
            return replace(connection, expires_at=200)

        monkeypatch.setattr(module, "refresh_connection", refresh)
        provider = module.factory({"GPT_STATE_DIR": store.root}, None)
        session = await setup(provider)
        session.clock = lambda: 100
        reader = asyncio.create_task(session())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            closer = asyncio.create_task(session.aclose())
            await asyncio.wait_for(cancelled.wait(), timeout=5)
            closer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closer
            assert not session._task.done()
            assert session.client is client
            client.aclose.assert_not_awaited()
        finally:
            release.set()
            try:
                done, _ = await asyncio.wait({reader}, timeout=5)
                if not done:
                    reader.cancel()
                    pytest.fail("Refresh reader did not stop after release")
                await reader
            finally:
                cleanup = asyncio.create_task(provider.aclose())
                done, _ = await asyncio.wait({cleanup}, timeout=5)
                if not done:
                    cleanup.cancel()
                    pytest.fail("Provider cleanup did not finish")
                await cleanup
        assert session._task.done()
        assert session.client is None
        client.aclose.assert_awaited_once()
        await asyncio.wait_for(provider.aclose(), timeout=5)

    run(scenario())


def test_factory_contributes_common_browser_inspector(store, monkeypatch):
    inspection = object()
    browser = SimpleNamespace(aclose=AsyncMock())
    create = lambda **kwargs: browser if kwargs == {'inspection': inspection} else pytest.fail('wrong inspector')
    monkeypatch.setattr(module, 'subscription_browser', create)
    monkeypatch.setattr(module, 'connect_account', AsyncMock(return_value='new'))
    async def scenario():
        provider = module.factory({}, {'browser_inspection': inspection})
        await setup(provider, interactive=True)
        assert provider.inspection is inspection
        await provider.aclose()
        browser.aclose.assert_awaited_once()
    run(scenario())


@pytest.mark.parametrize('mode', ['integration','e2e'])
@pytest.mark.parametrize('run_ui', [False,True])
@pytest.mark.parametrize('retry_failure', [False, True])
def test_headless_blocker_hands_off_once_to_headed_with_same_expected_account(store, monkeypatch, capsys, retry_failure, mode, run_ui):
    from api_agent.llm_providers.gpt.contracts import AuthError
    created = []
    def create(**kwargs):
        browser = SimpleNamespace(aclose=AsyncMock())
        created.append((kwargs, browser))
        return browser
    monkeypatch.setattr(module, 'subscription_browser', create)
    connect = AsyncMock(side_effect=[AuthError('login_interaction_required'),
                        AuthError('account_mismatch') if retry_failure else 'new'])
    monkeypatch.setattr(module, 'connect_account', connect)
    async def scenario():
        provider = module.factory({'TUI_MODE':mode,'NO_STD_IN': False, 'RUN_UI': run_ui}, None)
        reader = AsyncMock(return_value='person@example.test')
        try:
            if retry_failure:
                with pytest.raises(InteractionRequiredError, match='different account'):
                    await provider.asetup(interactive=False, user_index=0, credentials=reader)
                assert not provider.sessions
            else:
                session = await provider.asetup(interactive=False, user_index=0, credentials=reader)
                assert session.profile_id == 'new'
            assert [options for options, _ in created] == [{'headless': True}, {}]
            assert connect.await_count == 2
            first, retry = connect.await_args_list
            assert first.kwargs['allow_interaction'] is False
            assert retry.kwargs['allow_interaction'] is True
            assert first.kwargs['login_username'] == retry.kwargs['login_username'] == 'person@example.test'
            assert first.kwargs['login_credentials'] is retry.kwargs['login_credentials']
            reader.assert_awaited_once_with(0, 'username')
        finally:
            await provider.aclose()
        for _, browser in created:
            browser.aclose.assert_awaited_once()
    run(scenario())
    assert capsys.readouterr().out.count('Opening visible Chromium') == 1


def test_headless_blocker_opens_visible_retry_with_no_standard_input(store, monkeypatch, capsys):
    from api_agent.llm_providers.gpt.contracts import AuthError
    browser = SimpleNamespace(aclose=AsyncMock())
    # Explicit browser intervention remains available without terminal input.
    from unittest.mock import Mock
    create = Mock(return_value=browser)
    monkeypatch.setattr(module, 'subscription_browser', create)
    connect = AsyncMock(side_effect=AuthError('login_interaction_required'))
    monkeypatch.setattr(module, 'connect_account', connect)
    async def scenario():
        provider = module.factory({'NO_STD_IN': True, 'RUN_UI': False}, None)
        try:
            with pytest.raises(InteractionRequiredError, match='needs browser interaction'):
                await provider.asetup(interactive=False, user_index=0,
                                      credentials=AsyncMock(return_value='person@example.test'))
            assert create.call_count == 2
            assert create.call_args_list[0].kwargs == {"headless": True}
            assert create.call_args_list[1].kwargs == {}
            assert connect.await_count == 2
            assert not provider.sessions
        finally:
            await provider.aclose()
    run(scenario())
    assert capsys.readouterr().out.count("Opening visible Chromium") == 1


@pytest.mark.parametrize('failure', ['pending', 'cancel', 'account_mismatch', 'visible_retry', 'blocked_headless'])
def test_session_setup_retains_pending_login_in_provider_until_disposal_succeeds(store, monkeypatch, failure):
    from test_gpt_login import Page, workflow
    from api_agent.llm_providers.gpt.contracts import AuthError
    from api_agent.providers.sessions import LLMSessionOwner
    async def scenario():
        login = workflow(Page())
        document = SimpleNamespace(dispose=AsyncMock(side_effect=RuntimeError('private release diagnostic')))
        login.documents.append(document)
        login.last_document = document
        pending = CleanupPendingError((login,), login.aclose)
        error = pending
        if failure in {'cancel', 'account_mismatch', 'blocked_headless'}:
            error = asyncio.CancelledError() if failure == 'cancel' else AuthError(
                'login_interaction_required' if failure == 'blocked_headless' else 'account_mismatch')
            error.cleanup_error = pending
            error.retained_owners = (login,)
        effects = [AuthError('login_interaction_required'), error] if failure == 'visible_retry' else error
        connect = AsyncMock(side_effect=effects)
        monkeypatch.setattr(module, 'connect_account', connect)
        browsers = []
        def browser(**options):
            owned = SimpleNamespace(aclose=AsyncMock())
            browsers.append(owned)
            return owned
        monkeypatch.setattr(module, 'subscription_browser', browser)
        provider = module.factory({'NO_STD_IN': True, 'RUN_UI': False}, None)
        session = LLMSessionOwner(provider, user_index=0, interactive=False,
                                  credentials=AsyncMock(return_value='person@example.test'))
        operation = AsyncMock()
        expected = asyncio.CancelledError if failure == 'cancel' else InteractionRequiredError if failure in {'account_mismatch', 'blocked_headless'} else CleanupPendingError
        with pytest.raises(expected) as caught:
            await session.call(operation)
        operation.assert_not_awaited()
        assert provider.pending_logins == [login]
        if failure in {'account_mismatch', 'blocked_headless'}:
            assert caught.value.cleanup_error is pending
            assert caught.value.retained_owners == (login,)
        if failure == 'blocked_headless':
            assert len(browsers) == 1
            connect.assert_awaited_once()
        await session.aclose()
        assert provider.pending_logins == [login]
        with pytest.raises(CleanupPendingError):
            await provider.aclose()
        assert provider.pending_logins == [login]
        for owned in browsers:
            owned.aclose.assert_not_awaited()
        document.dispose.side_effect = None
        await provider.aclose()
        assert not provider.pending_logins
        assert not login.documents
        await provider.aclose()
        for owned in browsers:
            owned.aclose.assert_awaited_once()
    run(scenario())


def test_provider_deduplicates_repeated_receipts_for_same_login_owner(store, monkeypatch):
    async def scenario():
        owner = SimpleNamespace(aclose=AsyncMock())
        pending = CleanupPendingError((owner,), owner.aclose)
        monkeypatch.setattr(module, 'connect_account', AsyncMock(side_effect=pending))
        provider = module.factory({}, None)
        for _ in range(2):
            with pytest.raises(CleanupPendingError):
                await provider._connect_account(object())
        assert provider.pending_logins == [owner]
        await provider.aclose()
        owner.aclose.assert_awaited_once()
        assert provider.pending_logins == []
    run(scenario())


def test_login_error_mapping_keeps_cleanup_cancellation_without_inventing_retained_owners():
    from api_agent.llm_providers.gpt.contracts import AuthError
    primary = AuthError('account_mismatch')
    primary.cleanup_error = asyncio.CancelledError()
    converted = module.GPTProvider._login_error(primary)
    assert isinstance(converted, InteractionRequiredError)
    assert converted.cleanup_error is primary.cleanup_error
    assert not hasattr(converted, 'retained_owners')


def test_provider_disposal_cancellation_preserves_retry_for_blocked_login_task(store, monkeypatch):
    from test_gpt_login import Page, workflow
    from api_agent.llm_providers.gpt import login as login_module
    report = login_module.report_timeout
    monkeypatch.setattr(login_module, 'report_timeout', lambda name, timeout: report(name, .05))
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def dispose():
            entered.set()
            await release.wait()
        login = workflow(Page())
        document = SimpleNamespace(dispose=AsyncMock(side_effect=dispose))
        login.documents.append(document)
        provider = module.factory({}, None)
        provider.pending_logins.append(login)
        task = asyncio.create_task(provider.aclose())
        await entered.wait()
        original = login.closing
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 5)
        assert caught.value.retained_owners == (provider,)
        assert caught.value.cleanup_error.retry == provider.aclose
        assert provider.pending_logins == [login]
        assert login.closing is original
        assert not original.done()
        document.dispose.assert_awaited_once()
        release.set()
        await caught.value.cleanup_error.retry()
        assert provider.pending_logins == []
        assert login.documents == []
        assert original.done()
        document.dispose.assert_awaited_once()
    run(scenario())
