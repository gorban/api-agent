"""E2E admission reuses suite recording without weakening integration consent."""
from dataclasses import replace
from unittest.mock import Mock

import pytest

from api_agent.testing import launched, recorded
from test_recorded_integration import fixture, fake_runner, Storage
from test_launched_recording import owner


def selected(tmp_path):
    context, test, config = fixture(tmp_path)
    test = replace(test, tags=frozenset({'requires_ui_session'}))
    config.update(TUI_MODE='e2e', E2E_RERUNS=0)
    return context, test, config


@pytest.mark.parametrize('updates,options', [({}, {'accepted':False}),
    ({'COLLECT_ONLY':True}, {}), ({'NO_STD_IN':True, 'START':'True'}, {}),
    ({'START':'True'}, {'no_std_in':True})])
def test_e2e_consent_precedes_prerequisite_creation(tmp_path, monkeypatch, updates, options):
    context, test, config = selected(tmp_path)
    config.update(updates)
    create = Mock()
    monkeypatch.setattr(launched, 'local_recording_prerequisites', create)
    with pytest.raises(PermissionError):
        launched.run_launched_integration(context, [test], config,
            **{'accepted':True, **options})
    create.assert_not_called()
    assert not config['ALLURE_RESULTS_DIR'].exists()


@pytest.mark.parametrize('updates', [{}, {'NO_STD_IN':True, 'START':'E2E'}])
def test_e2e_uses_shared_recording_with_e2e_identity(tmp_path, updates):
    context, test, config = selected(tmp_path)
    config.update(updates)
    log = []
    result = launched.run_launched_integration(context, [test], config, accepted=True,
        prerequisites=owner(log), storage=Storage(), runner=fake_runner())
    assert result.exit_code == 0 and result.identity.mode == 'e2e'
    assert [name for name, _ in log] == ['prepare', 'finish']


@pytest.mark.parametrize('tests,updates,reason', [([], {}, 'UI-session'),
    (None, {}, 'UI-session'), ('ui', {'E2E_RERUNS':2}, 'retry')])
def test_unsupported_e2e_selection_fails_before_recording(tmp_path, tests, updates, reason):
    context, test, config = selected(tmp_path)
    tests = [replace(test, tags=frozenset())] if tests is None else [test] if tests == 'ui' else tests
    config.update(updates)
    with pytest.raises(ValueError, match=reason):
        recorded.run_recorded_integration(context, tests, config, accepted=True)
    assert not config['ALLURE_RESULTS_DIR'].exists()


def test_e2e_legacy_debug_uses_shared_headed_lifecycle(tmp_path, monkeypatch):
    context, test, config = selected(tmp_path)
    config.update(DEBUG=False, E2E_DEBUG=True)
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', lambda *args, **kwargs: [test, replace(test, id='second')])
    observed = []
    execute = fake_runner()
    def runner(*args, **hooks):
        observed.append(hooks['config']['DEBUG'])
        return execute(*args, **hooks)
    result = launched.run_launched_integration(context, [test], config, accepted=True,
        prerequisites=owner([]), storage=Storage(), runner=runner)
    assert result.exit_code == 0 and observed == [True]
    assert config['DEBUG'] is False

@pytest.mark.parametrize('allowance', [None, False, True, -1, 2, '0'])
def test_e2e_requires_effective_integer_zero(tmp_path, allowance):
    context, test, config = selected(tmp_path)
    config['E2E_RERUNS'] = allowance
    with pytest.raises(ValueError, match='retry'):
        recorded.validate_integration_start([test], config, accepted=True)


def test_e2e_all_test_debug_rejects_before_prerequisite_creation(tmp_path, monkeypatch):
    context, test, config = selected(tmp_path)
    config.update(E2E_DEBUG=True)
    monkeypatch.setattr('api_agent.testing.collection.collect_tests', lambda *args, **kwargs: [test])
    create = Mock()
    monkeypatch.setattr(launched, 'local_recording_prerequisites', create)
    with pytest.raises(ValueError, match='strict subset'):
        launched.run_launched_integration(context, [test], config, accepted=True)
    create.assert_not_called()
    assert not config['ALLURE_RESULTS_DIR'].exists()


@pytest.mark.parametrize('has_context,mode,prepares', [(False,'e2e',False),
    (True,'integration',False),(True,'e2e',True)])
def test_worker_prepares_ui_before_every_e2e_body(monkeypatch, has_context, mode, prepares):
    from types import SimpleNamespace
    from api_agent.testing import provider_setup
    if has_context:
        monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', 'synthetic-owned-context')
    else:
        monkeypatch.delenv('API_AGENT_INTEGRATION_CONTEXT', raising=False)
    monkeypatch.setattr(provider_setup, '_test_snapshot', lambda request: {'config':{'TUI_MODE':mode}})
    request = SimpleNamespace(getfixturevalue=Mock())
    provider_setup._e2e_prepared_ui.__wrapped__(request, None)
    assert request.getfixturevalue.call_count == int(prepares)
    if prepares:
        request.getfixturevalue.assert_called_once_with('ui_provider_session')


def test_ui_and_tools_dispose_before_shared_fixture_loop_closes():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.testing.provider_setup import finish_ui_browser_inspection
    loop = asyncio.new_event_loop()
    ui, tools = SimpleNamespace(aclose=AsyncMock()), SimpleNamespace(aclose=AsyncMock())
    try:
        finish_ui_browser_inspection(loop, None, owners=(ui,), close_loop=False)
        assert not loop.is_closed()
        finish_ui_browser_inspection(loop, None, owners=(tools,), close_loop=False)
        assert not loop.is_closed()
        ui.aclose.assert_awaited_once()
        tools.aclose.assert_awaited_once()
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


def test_retained_ui_and_tools_share_one_loop_disposal_owner(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.testing import provider_setup
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    monkeypatch.setattr(provider_setup, '_inspection_providers', [])
    loop = asyncio.new_event_loop()
    async def running():
        await asyncio.Event().wait()
    task = loop.create_task(running())
    async def stop():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    inspector = SimpleNamespace(sessions=[SimpleNamespace()], aclose=AsyncMock(side_effect=stop))
    tools = SimpleNamespace(aclose=AsyncMock())
    retained = provider_setup.finish_ui_browser_inspection(loop, inspector, close_loop=False)
    joined = provider_setup.finish_ui_browser_inspection(loop, None, owners=(tools,), close_loop=False)
    assert joined is retained and provider_setup._inspection_providers == [retained]
    assert not loop.is_closed() and not task.done()
    loop.run_until_complete(retained.dispose_inspection())
    assert task.cancelled()
    tools.aclose.assert_awaited_once()
    loop.close()


def test_joined_inspection_preserves_original_sessions_and_disposal():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.testing.provider_setup import _JoinedUIInspection
    first = SimpleNamespace(sessions=('first',), aclose=AsyncMock(), wait_closed=AsyncMock())
    second = SimpleNamespace(sessions=('second',), aclose=AsyncMock(), wait_closed=AsyncMock())
    subject = _JoinedUIInspection(first, second)
    assert subject.sessions == ('first', 'second')
    asyncio.run(subject.wait_closed())
    asyncio.run(subject.aclose())
    first.wait_closed.assert_awaited_once()
    second.wait_closed.assert_awaited_once()
    first.aclose.assert_awaited_once()
    second.aclose.assert_awaited_once()


def test_raw_e2e_runner_prepares_ui_even_when_body_does_not_request_it(tmp_path):
    from types import SimpleNamespace
    from api_agent.testing.collection import collect_tests
    from api_agent.testing.integration import run_integration_tests
    suite = tmp_path / 'tests/e2e'
    suite.mkdir(parents=True)
    (suite / 'test_body.py').write_text('''import pytest
from pathlib import Path
@pytest.fixture
def ui_provider_session():
    Path('ui-prepared').write_text('ready')
    yield object()
    Path('ui-disposed').write_text('closed')
@pytest.mark.requires_ui_session
def test_body():
    assert Path('ui-prepared').read_text() == 'ready'
''')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, suite_paths=lambda mode:[suite])
    config = {'TUI_MODE':'e2e', 'E2E_RERUNS':0}
    tests = collect_tests(context, 'e2e', config=config)
    result = run_integration_tests(context, tests, config=config, accepted=True)
    assert result.exit_code == 0 and [test.status for test in result.tests] == ['passed']
    assert (tmp_path / 'ui-disposed').read_text() == 'closed'


def test_raw_e2e_debug_all_test_selection_rejects_before_execution(tmp_path):
    from types import SimpleNamespace
    from api_agent.testing.collection import collect_tests, CollectionError
    from api_agent.testing.integration import run_integration_tests
    suite = tmp_path / 'tests/e2e'
    suite.mkdir(parents=True)
    (suite / 'test_body.py').write_text('import pytest\n@pytest.mark.requires_ui_session\ndef test_body():\n    raise AssertionError("body must not run")\n')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, suite_paths=lambda mode:[suite])
    config = {'TUI_MODE':'e2e', 'E2E_RERUNS':0, 'E2E_DEBUG':True}
    tests = collect_tests(context, 'e2e', config=config)
    with pytest.raises(CollectionError, match='strict subset'):
        run_integration_tests(context, tests, config=config, accepted=True)


@pytest.mark.parametrize('first_inspector', [False, True])
def test_later_inspection_joins_existing_pending_loop_owner(monkeypatch, first_inspector):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from api_agent.testing import provider_setup
    loop = asyncio.new_event_loop()
    first = SimpleNamespace(sessions=('first',), aclose=AsyncMock(), wait_closed=AsyncMock()) if first_inspector else None
    existing = provider_setup.UIBrowserInspectionRun(loop, first)
    monkeypatch.setattr(provider_setup, '_inspection_providers', [existing])
    second = SimpleNamespace(sessions=('second',), aclose=AsyncMock(), wait_closed=AsyncMock())
    assert provider_setup.finish_ui_browser_inspection(loop, second) is existing
    assert existing.inspection.sessions == (('first','second') if first_inspector else ('second',))
    existing.close()
    assert loop.is_closed()
    second.aclose.assert_awaited_once()
