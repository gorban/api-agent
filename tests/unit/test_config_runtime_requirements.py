"""Runtime setup requirements use declared settings and normal configuration review."""
import pytest

from api_agent.config.declarations import schema_from_dict
from api_agent.config.resolve import resolve_config


@pytest.mark.parametrize('value', [None, '', ' \t '])
def test_required_string_preserves_explicit_empty_as_provided(value):
    schema = schema_from_dict({'version': 1, 'fields': {
        'REQUIRED': {'type': 'str', 'required': True},
        'OPTIONAL': {'type': 'str', 'default': ''}}})
    result = resolve_config(schema, environ={} if value is None else {'REQUIRED': value}, interactive=True)
    assert result.missing == (('REQUIRED',) if value is None else ())
    assert result.values['OPTIONAL'] == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('blank', [None, '', '   '])
async def test_required_false_acknowledgment_rows_and_help_use_real_review(tmp_path, blank):
    from textual.widgets import DataTable, Static
    from test_tui import fixture, setting_row
    fields = {'IDENTITY': {'type': 'str', 'required': True,
                          'acknowledge_default': False, 'help': 'Copy the non-secret identifier from the account menu.'}}
    app = fixture(tmp_path, fields=fields)
    if blank is not None:
        app.session.environ['IDENTITY'] = blank
    assert app.session.require_fields(('IDENTITY',), snapshot=app.session.result().values)

    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        assert app.screen.kind == 'defaults'
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=setting_row(table, 'IDENTITY'))
        assert str(table.get_row_at(table.cursor_row)[1]) == 'Required'
        app.refresh_configuration_rows(app.screen)
        assert str(table.get_row_at(table.cursor_row)[1]) == 'Required'
        await pilot.press('?')
        await pilot.pause()
        assert 'Copy the non-secret identifier' in str(app.screen.query_one('#setting-help-text', Static).render())
        await pilot.press('escape')


@pytest.mark.parametrize('default', ['', '   ', 'configured'])
def test_required_string_defaults_preserve_declared_empty_values(default):
    schema = schema_from_dict({'version': 1, 'fields': {
        'VALUE': {'type': 'str', 'required': True, 'default': default}}})
    result = resolve_config(schema, environ={}, interactive=True)
    assert result.missing == ()



@pytest.fixture
def runtime_session(tmp_path):
    from types import SimpleNamespace
    from api_agent.config.session import ConfigSession
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path,
        pack_id='fixture', dotenv_path=tmp_path / '.env', anchors={}, external_pack=False)
    schema = schema_from_dict({'version': 1, 'fields': {
        'PROVIDER': {'type': 'str', 'default': 'selected'},
        'PROFILE': {'type': 'str', 'default': 'default'},
        'MODE': {'type': 'str', 'default': 'direct'},
        'SELECTOR': {'type': 'str', 'default': ''},
        'ROUTE': {'type': 'str', 'default': '', 'acknowledge_default': False},
        'REGION': {'type': 'str', 'acknowledge_default': False},
        'INDEX': {'type': 'int', 'allow_empty': True, 'default': ''},
        'COUNT': {'type': 'int', 'default': 0},
        'SWITCH': {'type': 'bool', 'default': False}}})
    return ConfigSession(context, schema, environ={})


@pytest.mark.parametrize('fields', [None, 'ROUTE', (), ['ROUTE'] * 33,
    ['ROUTE', 'ROUTE'], [4], ['bad'], ['BAD-NAME'], ['A' * 129]])
def test_structured_requirement_rejects_unbounded_or_invalid_fields(fields):
    from api_agent.providers.contracts import ConfigurationRequiredError, ContractError
    with pytest.raises(ContractError, match='Invalid configuration requirement fields'):
        ConfigurationRequiredError(fields)


def test_structured_requirement_contains_only_validated_names():
    from api_agent.providers.contracts import ConfigurationRequiredError, InteractionRequiredError
    error = ConfigurationRequiredError(['ROUTE', 'REGION'])
    assert isinstance(error, InteractionRequiredError)
    assert error.fields == ('ROUTE', 'REGION')
    assert str(error) == 'Additional configuration is required. Open Review configuration.'


def test_runtime_facts_remain_until_each_missing_field_is_filled(runtime_session):
    session = runtime_session
    snapshot = session.result().values
    assert session.require_fields(('ROUTE', 'REGION'), snapshot=snapshot)
    assert session.result().missing == ('ROUTE', 'REGION')
    assert session.runtime_required_fields == ('ROUTE', 'REGION')
    assert session.result(_review=True).missing == ('ROUTE', 'REGION')
    assert session.result(_cleanup=True).missing == ('ROUTE', 'REGION')
    assert session.result(operation_intents=('ui_session',)).missing == ('ROUTE', 'REGION')
    session.set_value('ROUTE', '   ')
    assert session.result().missing == ('ROUTE', 'REGION')
    session.set_value('ROUTE', 'synthetic')
    assert session.result().missing == ('REGION',)
    session.set_value('REGION', 'synthetic-region', persist=True)
    assert session.result().missing == ()
    assert 'ROUTE' not in (session.dotenv_path.read_text())
    assert '_runtime' not in session.dotenv_path.read_text()


@pytest.mark.parametrize('value', [None, ''])
def test_runtime_optional_integer_blank_is_missing(runtime_session, value):
    session = runtime_session
    if value is None:
        session.environ.pop('INDEX', None)
    assert session.require_fields(('INDEX',), snapshot=session.result().values)
    assert session.result().missing == ('INDEX',)
    session.set_value('INDEX', '0')
    assert session.result().missing == ()


def test_zero_and_false_satisfy_runtime_requirement(runtime_session):
    session = runtime_session
    assert session.require_fields(('COUNT', 'SWITCH'), snapshot=session.result().values)
    assert session.result().missing == ()


@pytest.mark.parametrize('field, changed, restored', [('PROVIDER', 'other', 'selected'),
    ('PROFILE', 'other', 'default'), ('MODE', 'sso', 'direct'),
    ('SELECTOR', 'other.py:factory', ''), ('INDEX', '0', '')])
def test_configuration_change_discards_facts_and_rejects_stale_result(runtime_session, field, changed, restored):
    session = runtime_session
    old = dict(session.result().values)
    assert session.require_fields(('ROUTE',), snapshot=old)
    session.set_value(field, changed)
    assert session.result(_review=True).missing == ()
    assert session.runtime_required_fields == ()
    assert not session.require_fields(('ROUTE',), snapshot=old)
    session.set_value(field, restored)
    assert session.result().missing == ()


def test_unknown_declaration_requirement_is_rejected(runtime_session):
    from api_agent.config.resolve import ConfigError
    with pytest.raises(ConfigError, match='undeclared field'):
        runtime_session.require_fields(('UNKNOWN',), snapshot=runtime_session.result().values)


def test_schema_removal_discards_runtime_facts(runtime_session):
    from api_agent.config.declarations import ConfigSchema
    session = runtime_session
    assert session.require_fields(('ROUTE',), snapshot=session.result().values)
    session.schema = ConfigSchema({name: field for name, field in session.schema.fields.items() if name != 'ROUTE'})
    assert session.result().missing == ()


def test_runtime_facts_are_not_reloaded_from_saved_configuration(runtime_session):
    from api_agent.config.session import ConfigSession
    session = runtime_session
    assert session.require_fields(('ROUTE',), snapshot=session.result().values)
    fresh = ConfigSession(session.context, session.schema, environ={})
    assert fresh.result().missing == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_site', ['loader', 'authentication'])
async def test_local_llm_publishes_structured_authentication_requirement(tmp_path, failure_site):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.providers.contracts import ConfigurationRequiredError, ProviderCapabilities
    from api_agent.runtime.local_llm import LocalLLM
    error = ConfigurationRequiredError(('ROUTE',))
    provider = SimpleNamespace(capabilities=ProviderCapabilities(kind='llm'),
        asetup=AsyncMock(side_effect=error), adescribe=AsyncMock(),
        credential_protection_version=1, bind_credential_authority=lambda authority: None)
    loaded = SimpleNamespace(implementation=provider, astart=AsyncMock(), aclose=AsyncMock())
    loader = AsyncMock(side_effect=error) if failure_site == 'loader' else AsyncMock(return_value=loaded)
    events = []
    owner = LocalLLM({'LLM_PROVIDER': 'synthetic'}, tmp_path, loader=loader, on_status=events.append)
    with pytest.raises(ConfigurationRequiredError) as raised:
        await owner.ensure_ready()
    assert raised.value is error
    assert [dict(row) for row in events if row['event'] == 'configuration-required'] == [
        {'event': 'configuration-required', 'fields': ('ROUTE',)}]
    await owner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('observer', [None, 'broken'])
async def test_requirement_observer_cannot_replace_provider_error(tmp_path, observer):
    from unittest.mock import AsyncMock
    from api_agent.providers.contracts import ConfigurationRequiredError
    from api_agent.runtime.local_llm import LocalLLM
    error = ConfigurationRequiredError(('ROUTE',))
    def broken(row):
        raise RuntimeError('observer failure')
    owner = LocalLLM({'LLM_PROVIDER': 'synthetic'}, tmp_path, loader=AsyncMock(side_effect=error),
                     on_status=broken if observer else None)
    with pytest.raises(ConfigurationRequiredError) as raised:
        await owner.ensure_ready()
    assert raised.value is error
    await owner.aclose()


def test_local_workflow_projection_preserves_only_declared_requirement_names():
    from api_agent.runtime.local_run import LocalRunOwner
    from api_agent.runtime.local_contracts import LocalEvent
    owner = object.__new__(LocalRunOwner)
    events = []
    owner._notify = events.append
    owner._workflow_event({'event': 'configuration-required', 'fields': ('ROUTE',), 'ignored': 'synthetic'})
    assert events == [LocalEvent('configuration-required', required_fields=('ROUTE',))]


@pytest.mark.asyncio
async def test_local_requirement_event_review_cleanup_edit_and_retry(tmp_path, monkeypatch):
    import asyncio
    from textual.widgets import DataTable, Input, Static
    from unittest.mock import AsyncMock
    from api_agent.providers.contracts import ConfigurationRequiredError
    from api_agent.runtime.local_contracts import LocalSnapshot
    from api_agent.runtime.local_llm import LocalLLM
    from api_agent.runtime.local_run import LocalRunOwner
    from api_agent.tui.local_run import LocalRunScreen
    from test_tui import fixture, setting_row
    owners = []
    class Owner:
        def __init__(self, *args, **kwargs):
            self.stopped = asyncio.Event()
            self.cleanup_calls = 0
            self.pending_cleanup = None
            self._diagnostic_projection = None
            self._notify = kwargs['on_event']
            self.llm = LocalLLM({'LLM_PROVIDER': 'synthetic'}, tmp_path,
                loader=AsyncMock(side_effect=ConfigurationRequiredError(('ROUTE',))),
                on_status=self.workflow_event)
            owners.append(self)
        def workflow_event(self, row):
            LocalRunOwner._workflow_event(self, row)
        async def start(self):
            await self.llm.ensure_ready()
            return LocalSnapshot('running')
        async def wait(self):
            await self.stopped.wait()
            return 0
        def request_stop(self):
            self.stopped.set()
        async def finish_cleanup(self):
            self.cleanup_calls += 1
            self.stopped.set()
            await self.llm.aclose()
        async def aclose(self):
            await self.finish_cleanup()
        def diagnostic(self, error):
            return str(error)
    app = fixture(tmp_path, fields={
        'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False},
        'ROUTE': {'type': 'str', 'default': '', 'acknowledge_default': False,
                  'help': 'Copy the non-secret route from your account administrator.'}})
    retried = []
    async def retry(**kwargs):
        retried.append(dict(app.session.result().values))
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        screen = LocalRunScreen(app.context, app.session.result, schema=app.schema, owner_factory=Owner)
        app.local_run_screen = screen
        await app.push_screen(screen)
        await pilot.pause()
        monkeypatch.setattr(app, 'enter_mode', retry)
        assert app.session.result().missing == ('ROUTE',)
        assert screen.check_action('review_configuration', ())
        assert owners[0].cleanup_calls == 0
        await pilot.press('r')
        await pilot.pause()
        assert owners[0].cleanup_calls == 1
        assert app.local_run_screen is None
        assert app.screen.kind == 'defaults'
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=setting_row(table, 'ROUTE'))
        assert str(table.get_row_at(table.cursor_row)[1]) == 'Required'
        await pilot.press('?')
        await pilot.pause()
        assert 'account administrator' in str(app.screen.query_one('#setting-help-text', Static).render())
        await pilot.click('#setting-help-close')
        await pilot.press('e')
        await pilot.pause()
        app.screen.query_one(Input).value = 'synthetic-route'
        await pilot.press('enter')
        await pilot.pause()
        assert app.session.result().missing == ()
        assert 'synthetic-route' in (tmp_path / '.env').read_text()
        assert retried == []
        await pilot.press('c')
        await pilot.pause()
        assert len(retried) == 1 and retried[0]['ROUTE'] == 'synthetic-route'
        assert len(owners) == 1


@pytest.mark.asyncio
async def test_runtime_review_does_not_leave_without_owned_cleanup(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.tui.local_run import LocalRunScreen
    screen = LocalRunScreen(None, lambda: SimpleNamespace(values={'RUN_LOG_SOURCE': 'agent'}))
    cleanup = AsyncMock(return_value=False)
    monkeypatch.setattr(screen, 'close_owned', cleanup)
    assert not screen.check_action('review_configuration', ())
    await screen.action_review_configuration()
    cleanup.assert_not_called()
    screen.configuration_required = True
    assert screen.check_action('review_configuration', ())
    await screen.action_review_configuration()
    cleanup.assert_awaited_once()


def test_explicit_runtime_reset_preserves_static_requirements_and_settings(runtime_session):
    from dataclasses import replace
    from api_agent.config.declarations import ConfigSchema
    session = runtime_session
    session.schema = ConfigSchema({**session.schema.fields, 'STATIC': replace(
        session.schema.fields['REGION'], name='STATIC', required=True)})
    before = dict(session.result().values)
    assert session.require_fields(('ROUTE',), snapshot=before)
    token = session.runtime_requirement_token
    assert session.clear_runtime_requirements(expected_token=token)
    assert session.result().missing == ('STATIC',)
    assert session.runtime_required_fields == ()
    assert session.runtime_requirement_token is None
    assert session.result().values == before
    assert not session.dotenv_path.exists()


@pytest.fixture
def recheck_case(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from api_agent.providers.contracts import ConfigurationRequiredError
    from api_agent.runtime.local_contracts import LocalSnapshot
    from api_agent.runtime.local_run import LocalRunOwner
    import api_agent.tui.local_run as local_run
    from test_tui import fixture
    state = SimpleNamespace(ready=False, attempts=[], cleanup_failure=False)
    class Owner:
        pending_cleanup = None
        _diagnostic_projection = None
        def __init__(self, *args, on_event, **kwargs):
            self._notify = on_event
            self.stop = asyncio.Event()
            self.cleanup_calls = 0
            state.attempts.append(self)
        async def start(self):
            if not state.ready:
                error = ConfigurationRequiredError(('ROUTE',))
                LocalRunOwner._workflow_event(self, {'event': 'configuration-required', 'fields': error.fields})
            return LocalSnapshot('ready')
        async def wait(self):
            await self.stop.wait()
            return 0
        def request_stop(self):
            self.stop.set()
        async def finish_cleanup(self):
            self.cleanup_calls += 1
            if state.cleanup_failure:
                raise RuntimeError('synthetic cleanup failure')
            self.stop.set()
        aclose = finish_cleanup
        def diagnostic(self, error):
            return 'Local cleanup remains pending.'
    original = local_run.LocalRunScreen
    class OfflineScreen(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, owner_factory=Owner, **kwargs)
    monkeypatch.setattr(local_run, 'LocalRunScreen', OfflineScreen)
    app = fixture(tmp_path, 'run-local', fields={
        'RUN_LOG_SOURCE': {'type': 'str', 'default': 'agent', 'acknowledge_default': False},
        'PROFILE': {'type': 'str', 'default': 'synthetic-profile', 'acknowledge_default': False},
        'ROUTE': {'type': 'str', 'default': '', 'acknowledge_default': False}})
    return app, state, OfflineScreen


@pytest.mark.asyncio
@pytest.mark.parametrize('externally_ready', [False, True])
async def test_explicit_recheck_starts_one_fresh_owner_with_same_config(recheck_case, externally_ready):
    from api_agent.runtime.local_contracts import LocalEvent
    app, state, screen_type = recheck_case
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        old = app.local_run_screen
        assert isinstance(old, screen_type)
        before = dict(app.session.result().values)
        assert app.session.result().missing == ('ROUTE',)
        await pilot.press('r')
        await pilot.pause()
        assert app.screen.kind == 'defaults'
        assert state.attempts[0].cleanup_calls == 1
        assert app.screen.check_action('recheck_setup', ())
        assert 'rechecks setup' in app.configuration_review_text()
        await pilot.press('c')
        await pilot.pause()
        assert len(state.attempts) == 1 and app.session.result().missing == ('ROUTE',)
        # c may display the ordinary missing-setting blocker; reopen review.
        app.review_destination = 'run-local'
        app.show_configuration()
        await pilot.pause()
        state.ready = externally_ready
        await pilot.press('r')
        await pilot.pause()
        assert len(state.attempts) == 2 and isinstance(app.screen, screen_type)
        assert app.session.result().values == before
        assert app.session.result().missing == (() if externally_ready else ('ROUTE',))
        old.receive(LocalEvent('configuration-required', required_fields=('ROUTE',)))
        assert app.session.result().missing == (() if externally_ready else ('ROUTE',))
        assert not app.session.dotenv_path.exists()
        assert await app.local_run_screen.close_owned()


def test_runtime_reset_rejects_stale_tokens_and_changed_scope(runtime_session):
    session = runtime_session
    assert not session.clear_runtime_requirements(expected_token=None)
    assert not session.clear_runtime_requirements(expected_token=object())
    assert session.require_fields(('ROUTE',), snapshot=session.result().values)
    old = session.runtime_requirement_token
    assert session.require_fields(('REGION',), snapshot=session.result().values)
    current = session.runtime_requirement_token
    assert current is not old
    assert not session.clear_runtime_requirements(expected_token=old)
    assert session.runtime_requirement_token is current
    session.set_value('PROFILE', 'changed')
    assert not session.clear_runtime_requirements(expected_token=current)
    assert session.runtime_requirement_token is None


@pytest.mark.asyncio
async def test_recheck_preserves_visible_static_blockers_as_required_rows(recheck_case):
    from api_agent.config.declarations import ConfigSchema, FieldDeclaration
    from textual.widgets import DataTable
    from test_tui import setting_row
    app, state, _ = recheck_case
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        await pilot.press('r')
        await pilot.pause()
        app.schema = ConfigSchema({**app.schema.fields,
            'STATIC': FieldDeclaration('STATIC', 'str', required=True, acknowledge_default=True)})
        state.ready = True
        await pilot.press('r')
        await pilot.pause()
        assert len(state.attempts) == 1
        assert app.session.runtime_required_fields == ()
        assert app.session.result().missing == ('STATIC',)
        assert app.screen.kind == 'defaults'
        table = app.screen.query_one(DataTable)
        assert str(table.get_row_at(setting_row(table, 'STATIC'))[1]) == 'Required'
        assert not app.screen.check_action('recheck_setup', ())


@pytest.mark.asyncio
async def test_failed_local_cleanup_keeps_owner_and_facts(recheck_case):
    app, state, screen_type = recheck_case
    async with app.run_test() as pilot:
        await pilot.pause()
        old = app.local_run_screen
        token = app.session.runtime_requirement_token
        state.cleanup_failure = True
        await pilot.press('r')
        await pilot.pause()
        assert isinstance(app.screen, screen_type) and app.local_run_screen is old
        assert old.pending_cleanup is not None
        await app.recheck_setup()
        assert len(state.attempts) == 1
        assert app.session.runtime_requirement_token is token
        assert app.session.result().missing == ('ROUTE',)
        state.cleanup_failure = False
        assert await old.close_owned()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['failure', 'scope', 'replacement', 'screen'])
async def test_recheck_revalidates_after_remaining_owner_cleanup(recheck_case, change):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from textual.widgets import Static
    app, state, _ = recheck_case
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        await pilot.press('r')
        await pilot.pause()
        old_token = app.session.runtime_requirement_token
        replacement = []
        async def cleanup():
            if change == 'failure':
                return False
            app.local_run_screen = None
            if change == 'scope':
                app.session.set_value('PROFILE', 'new-profile')
            elif change == 'replacement':
                app.session.require_fields(('ROUTE',), snapshot=app.session.result().values)
                replacement.append(app.session.runtime_requirement_token)
            else:
                app.show_menu()
            return True
        pending = SimpleNamespace(close_owned=AsyncMock(side_effect=cleanup))
        app.local_run_screen = pending
        try:
            await pilot.press('r')
            await pilot.pause()
            pending.close_owned.assert_awaited_once()
            assert len(state.attempts) == 1
            if change == 'failure':
                assert app.local_run_screen is pending
                assert app.session.runtime_requirement_token is old_token
                assert 'cleanup remains pending' in str(app.screen.query_one('#error', Static).render())
            elif change == 'scope':
                assert app.session.runtime_requirement_token is None
                assert app.session.result().values['PROFILE'] == 'new-profile'
            elif change == 'replacement':
                assert app.session.runtime_requirement_token is replacement[0]
                assert app.session.result().missing == ('ROUTE',)
                assert 'requirements changed' in str(app.screen.query_one('#error', Static).render())
            else:
                assert app.screen.kind == 'menu'
                assert app.session.runtime_requirement_token is old_token
        finally:
            if app.local_run_screen is pending:
                app.local_run_screen = None


@pytest.mark.asyncio
async def test_recheck_is_contextual_and_closing_or_unowned_events_are_ignored(recheck_case):
    from api_agent.runtime.local_contracts import LocalEvent
    app, state, _ = recheck_case
    state.ready = True
    async with app.run_test(size=(110, 16)) as pilot:
        await pilot.pause()
        current = app.local_run_screen
        assert not app.can_recheck_setup()
        current.closing = True
        current.receive(LocalEvent('configuration-required', required_fields=('ROUTE',)))
        assert app.session.runtime_requirement_token is None
        current.closing = False
        app.local_run_screen = None
        current.receive(LocalEvent('configuration-required', required_fields=('ROUTE',)))
        assert app.session.runtime_requirement_token is None
        app.local_run_screen = current
        assert await current.close_owned()
        app.show_menu()
        await pilot.pause()
        assert not app.screen.check_action('recheck_setup', ())
        await app.recheck_setup()
        app.session.require_fields(('ROUTE',), snapshot=app.session.result().values)
        app.mode = 'integration'
        app.review_destination = 'integration'
        app.show_configuration()
        await pilot.pause()
        assert not app.screen.check_action('recheck_setup', ())
        await app.recheck_setup()
        assert app.session.result().missing == ('ROUTE',)
        app.mode = 'run-local'
        assert app.can_recheck_setup()
        assert 'r rechecks setup' in app.configuration_review_text()


@pytest.mark.asyncio
async def test_menu_run_local_reopens_required_review_and_explicitly_rechecks(recheck_case):
    from textual.widgets import DataTable
    from test_tui import setting_row
    app, state, screen_type = recheck_case
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        before = dict(app.session.result().values)
        await app.local_run_screen.action_stop()
        await pilot.pause()
        app.screen.action_close_view()
        await pilot.pause()
        assert app.screen.kind == 'menu' and app.local_run_screen is None
        state.ready = True
        await pilot.click('#mode-run-local')
        await pilot.pause()
        assert app.screen.kind == 'defaults' and len(state.attempts) == 1
        assert app.screen.check_action('recheck_setup', ())
        table = app.screen.query_one(DataTable)
        assert str(table.get_row_at(setting_row(table, 'ROUTE'))[1]) == 'Required'
        await pilot.press('r')
        await pilot.pause()
        assert len(state.attempts) == 2 and isinstance(app.screen, screen_type)
        assert app.session.result().missing == ()
        assert app.session.result().values == before
        assert await app.local_run_screen.close_owned()


@pytest.mark.asyncio
async def test_runtime_auth_requirement_keeps_dedicated_auth_wizard(tmp_path):
    from test_tui import fixture
    app = fixture(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.mode = 'run-local'
        app.session.set_value('TUI_MODE', 'run-local')
        app.session.environ.pop('UNATTENDED_AUTH')
        assert app.session.require_fields(('UNATTENDED_AUTH',), snapshot=app.session.result().values)
        await app.configure_operation()
        await pilot.pause()
        assert app.screen.kind == 'error'
        assert app.screen.heading == 'Unattended authentication required'
        assert app.session.result().missing == ('UNATTENDED_AUTH',)
