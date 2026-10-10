"""Direct worker inspection protocol proofs, without subprocess coverage gaps."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.auth.managers import bundled_registry
from api_agent.testing import provider_setup as p


@pytest.fixture
def registry(monkeypatch):
    providers = []
    monkeypatch.setattr(p, '_inspection_providers', providers)
    yield providers
    for owner in providers:
        if not owner.loop.is_closed():
            owner.loop.close()


@pytest.mark.parametrize('error', [None, RuntimeError('ordinary failure')])
def test_noncredential_reports_are_not_reformatted(error):
    report = SimpleNamespace(longrepr=object())
    original = report.longrepr
    hook = p.pytest_runtest_makereport(None,
        SimpleNamespace(excinfo=None if error is None else SimpleNamespace(value=error)))
    next(hook)
    with pytest.raises(StopIteration) as finished:
        hook.send(report)
    assert finished.value.value is report and report.longrepr is original


def retained(tmp_path, registry, *, windows=True, endpoint=None, cleanup=None):
    worker = p.IntegrationLLM({}, tmp_path)
    worker.inspection = SimpleNamespace(sessions=[SimpleNamespace(
        inspection_endpoint=endpoint,
        browser=SimpleNamespace(is_connected=lambda: windows),
        context=SimpleNamespace(pages=[object()] if windows else []))],
        aclose=cleanup or AsyncMock())
    registry.append(worker)
    return worker


def test_no_inspections_requires_no_command_context(monkeypatch, registry):
    monkeypatch.delenv('API_AGENT_INTEGRATION_EVENTS', raising=False)
    send = []
    p.serve_inspections(lambda **row: send.append(row))
    assert send == [] and registry == []


@pytest.mark.parametrize('debug', [False, True])
def test_finish_run_without_retained_windows_closes_loop(tmp_path, registry, debug):
    worker = p.IntegrationLLM({'DEBUG': debug}, tmp_path)
    worker.finish_run()
    assert worker.closed and worker.loop.is_closed() and registry == []
    if debug:
        assert worker.inspection._closed


def test_fixture_event_transport_uses_run_finalization(tmp_path, monkeypatch, registry):
    monkeypatch.setattr(p, '_read_snapshot', lambda: {'owner_root': str(tmp_path), 'config': {},
        'password_manager_definitions': p._manager_definitions(bundled_registry())})
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    fixture = p.llm_provider.__wrapped__()
    worker = next(fixture)
    assert not worker.closed and worker.provider is None
    with pytest.raises(StopIteration):
        next(fixture)
    assert worker.closed and worker.loop.is_closed() and registry == []


@pytest.mark.parametrize('invalid', [None, '{', [], {}, {'version': 2, 'command': 'end_inspection', 'attempt': 1},
    {'version': True, 'command': 'end_inspection', 'attempt': 1},
    {'version': 1.0, 'command': 'end_inspection', 'attempt': 1},
    {'version': 1, 'command': 'other', 'attempt': 1},
    {'version': 1, 'command': 'end_inspection', 'attempt': True},
    {'version': 1, 'command': 'end_inspection', 'attempt': 0}])
def test_malformed_command_keeps_browser_owned_until_valid_end(tmp_path, monkeypatch, registry, invalid):
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    worker = retained(tmp_path, registry, endpoint='http://127.0.0.1:3210')
    command = tmp_path / 'inspection-command.json'
    if invalid is not None:
        command.write_text(invalid if invalid == '{' else json.dumps(invalid))
    waits = []
    original_sleep = asyncio.sleep
    async def tick(delay):
        waits.append(delay)
        worker.inspection.aclose.assert_not_awaited()
        assert not worker.closed and not worker.loop.is_closed()
        command.write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 1}))
        await original_sleep(0)
    monkeypatch.setattr(p.asyncio, 'sleep', tick)
    events = []
    try:
        p.serve_inspections(lambda **row: events.append(row))
    finally:
        if not worker.loop.is_closed():
            worker.loop.close()
    assert waits == [.03]
    assert events == [{'kind': 'inspection_open', 'endpoint': 'http://127.0.0.1:3210'},
                      {'kind': 'inspection_closed', 'attempt': 1}]
    worker.inspection.aclose.assert_awaited_once()
    assert worker.closed and worker.loop.is_closed() and registry == []


def test_natural_close_failed_disposal_waits_for_explicit_fresh_retry(tmp_path, monkeypatch, registry):
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    cleanup = AsyncMock(side_effect=[RuntimeError('disposal not confirmed'), None])
    worker = retained(tmp_path, registry, windows=False, cleanup=cleanup)
    command = tmp_path / 'inspection-command.json'
    waits = []
    original_sleep = asyncio.sleep
    async def tick(delay):
        waits.append(delay)
        assert cleanup.await_count == 1
        assert not worker.closed and not worker.loop.is_closed()
        command.write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 2}))
        await original_sleep(0)
    monkeypatch.setattr(p.asyncio, 'sleep', tick)
    events = []
    try:
        p.serve_inspections(lambda **row: events.append(row))
    finally:
        if not worker.loop.is_closed():
            worker.loop.close()
    assert waits == [.03] and cleanup.await_count == 2
    assert events == [{'kind': 'inspection_open', 'endpoint': ''},
                      {'kind': 'inspection_cleanup_pending', 'attempt': None},
                      {'kind': 'inspection_closed', 'attempt': 2}]
    assert worker.closed and worker.loop.is_closed()


def test_duplicate_command_cannot_retry_failed_disposal(tmp_path, monkeypatch, registry):
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(tmp_path / 'events'))
    cleanup = AsyncMock(side_effect=[RuntimeError('pending'), None])
    worker = retained(tmp_path, registry, cleanup=cleanup)
    command = tmp_path / 'inspection-command.json'
    command.write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 4}))
    original_sleep = asyncio.sleep
    async def tick(delay):
        assert cleanup.await_count == 1 and not worker.closed
        command.write_text(json.dumps({'version': 1, 'command': 'end_inspection', 'attempt': 5}))
        await original_sleep(0)
    monkeypatch.setattr(p.asyncio, 'sleep', tick)
    events = []
    try:
        p.serve_inspections(lambda **row: events.append(row))
    finally:
        if not worker.loop.is_closed():
            worker.loop.close()
    assert cleanup.await_count == 2
    assert events[-2:] == [{'kind': 'inspection_cleanup_pending', 'attempt': 4},
                          {'kind': 'inspection_closed', 'attempt': 5}]


@pytest.mark.parametrize('changed', ['provider', 'role', 'account', 'descriptor', 'manager'])
def test_selection_change_during_loading_rejects_original_provider(tmp_path, monkeypatch, changed):
    from copy import deepcopy
    from api_agent.providers.contracts import ContractError
    config = {'LLM_PROVIDER': 'original', 'LLM_CREDENTIAL_ROLE': 'LLM User',
              'UNATTENDED_USER_INDEX': 0, 'UNATTENDED_AUTH': {
                  'password_manager': {'manager': '1password', 'cli_path': 'op'},
                  'roles': {'LLM User': [{'vault': 'fixture', 'user': 'original'}]}}}
    original = deepcopy(config)
    worker = p.IntegrationLLM(config, tmp_path)
    adapter = SimpleNamespace(asetup=AsyncMock())
    loaded = SimpleNamespace(implementation=adapter, aclose=AsyncMock())
    entered, resume = asyncio.Event(), asyncio.Event()
    supplied = []
    async def load(location, values, services, **kwargs):
        assert location == 'original'
        supplied.append(values)
        entered.set()
        await resume.wait()
        assert values == original
        return loaded
    monkeypatch.setattr(p, 'aload_provider', load)
    async def scenario():
        setup = asyncio.create_task(worker._ensure())
        await entered.wait()
        if changed == 'provider':
            worker.config['LLM_PROVIDER'] = 'replacement'
        elif changed == 'role':
            worker.config['LLM_CREDENTIAL_ROLE'] = 'Other User'
        elif changed == 'account':
            worker.config['UNATTENDED_USER_INDEX'] = 1
        elif changed == 'descriptor':
            config['UNATTENDED_AUTH']['roles']['LLM User'][0]['user'] = 'replacement'
        else:
            config['UNATTENDED_AUTH']['password_manager']['cli_path'] = 'replacement-cli'
        resume.set()
        with pytest.raises(ContractError, match='selection changed during setup'):
            await setup
        loaded.aclose.assert_awaited_once()
        adapter.asetup.assert_not_awaited()
        assert worker.provider is None and worker._shared_setup is None
        assert not worker._retained_owners
        assert supplied == [original]
    try:
        worker.loop.run_until_complete(scenario())
    finally:
        worker.close()


@pytest.mark.parametrize('cancelled', [False, True])
def test_changed_selection_retains_failed_original_provider_cleanup(tmp_path, monkeypatch, cancelled):
    from api_agent.providers.contracts import ContractError
    worker = p.IntegrationLLM({'LLM_PROVIDER': 'original'}, tmp_path)
    failure = asyncio.CancelledError() if cancelled else RuntimeError('disposal pending')
    loaded = SimpleNamespace(implementation=SimpleNamespace(asetup=AsyncMock()),
                             aclose=AsyncMock(side_effect=[failure, None]))
    async def load(*args, **kwargs):
        worker.config['LLM_PROVIDER'] = 'replacement'
        return loaded
    monkeypatch.setattr(p, 'aload_provider', load)
    try:
        with pytest.raises(ContractError, match='selection changed during setup') as caught:
            worker.ensure_ready()
        assert caught.value.retained_owners == (loaded,)
        assert worker._retained_owners == [loaded]
        assert not worker.closed and not worker.loop.is_closed()
        assert worker.provider is None and worker._shared_setup is None
        loaded.implementation.asetup.assert_not_awaited()
        worker.close()
        assert loaded.aclose.await_count == 2
        assert not worker._retained_owners and worker.closed and worker.loop.is_closed()
    finally:
        worker.close()
