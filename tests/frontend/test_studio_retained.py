"""Retained interaction uses real HTTP and a distinct normal provider owner."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from api_agent.providers.contracts import ProviderCapabilities, SessionClosedError, ProviderAccessError
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, Usage
from api_agent.testing.studio_retained import RetainedStudio, retained_provider_factory


@pytest.fixture
def connection():
    path = Path(__file__).parents[1] / 'integration' / 'test_studio_request.py'
    spec = importlib.util.spec_from_file_location('retained_connection', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Automated:
    def __init__(self):
        self.calls = 0
    def describe(self):
        return (AvailableSelection('sample'),)
    def complete(self, request):
        self.calls += 1
        return LLMResult(Message('assistant', (TextContent('Ok'),)))


@pytest.fixture
def manual(tmp_path, monkeypatch):
    from api_agent.testing import provider_setup
    seen = []
    class Adapter:
        capabilities = ProviderCapabilities(kind='llm', features=('reasoning_effort',))
        async def asetup(self, **kwargs):
            seen.append(('setup', kwargs, asyncio.get_running_loop()))
            class Auth:
                async def __call__(self): return object()
                async def aclose(self): seen.append(('auth_close',))
            return Auth()
        async def adescribe(self, *, auth):
            return (AvailableSelection('sample'), AvailableSelection('other',
                features=('reasoning_effort',), reasoning_efforts=('low', 'high')))
        async def acomplete(self, request, *, auth):
            seen.append(('complete', request))
            return LLMResult(Message('assistant', (TextContent('manual'),)), Usage(2, 3),
                actual_model='other-v2', service_tier='default', serving_revision='v2')
        async def aclose(self): seen.append(('provider_close',))
    implementation = Adapter()
    loaded = SimpleNamespace(implementation=implementation, aclose=implementation.aclose)
    monkeypatch.setattr(provider_setup, 'aload_provider', AsyncMock(return_value=loaded))
    data = {'config': {'LLM_PROVIDER': 'synthetic', 'LLM_ALLOWED_MODELS': 'sample|other',
            'LLM_DEFAULT_MODEL': 'sample', 'UNATTENDED_USER_INDEX': 3}, 'owner_root': tmp_path,
            'unattended_index_source': 'configuration'}
    return retained_provider_factory(data, object()), seen, data


async def chat(client, endpoint, headers, model='sample', effort=None):
    payload = {'model': model, 'session_id': str(uuid4()),
        'messages': [{'role': 'user', 'content': 'synthetic'}]}
    if effort is not None:
        payload['reasoning_effort'] = effort
    return await client.post(endpoint + '/app/agent/chat', headers=headers, json=payload)


def test_http_retirement_catalog_and_accounting_are_separate(connection, manual, monkeypatch, tmp_path):
    factory, seen, data = manual
    events = tmp_path / 'events.jsonl'
    events.write_text('frozen suite evidence\n')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(events))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'original::test (call)')
    async def run():
        ready = False
        original = Automated()
        retained = RetainedStudio(original, factory, lambda: ready)
        headers = {'Origin': 'http://localhost:9443', 'X-API-Agent-Session': 'x' * 32}
        async with connection.agent_endpoint(retained, 'sample', headers['Origin'], 'x' * 32,
                policy_config=data['config']) as endpoint:
            async with httpx.AsyncClient(trust_env=False) as client:
                assert (await chat(client, endpoint, headers)).status_code == 200
                assert original.calls == 1 and retained.phase == 'waiting'
                pending = await chat(client, endpoint, headers)
                assert pending.status_code == 503
                assert pending.json() == {'code': 'unavailable', 'message':
                    'This session is unavailable. Wait for suite completion, or start a new session.'}
                assert (await client.get(endpoint + '/app/agent/models', headers=headers)).status_code == 503
                assert not seen and retained.manual is None
                ready = True
                catalog = await client.get(endpoint + '/app/agent/models', headers=headers)
                assert catalog.status_code == 200
                assert 'other' in catalog.text
                assert [row[0] for row in seen] == ['setup']
                setup = seen[0][1]
                assert setup == {'interactive': False, 'user_index': 3, 'credentials': None}
                assert retained.manual.loop is asyncio.get_running_loop()
                response = await chat(client, endpoint, headers, 'other', 'high')
                assert response.status_code == 200, response.text
                assert seen[-1][1].selected_llm == 'other'
                assert seen[-1][1].payload.generation.reasoning_effort == 'high'
                report = retained.usage.report()
                assert report['prompt_count'] == 1
                assert report['usage_rows'][0]['total_tokens'] == 5
                assert len(retained.usage.inference_calls) == 1
                assert original.calls == 1
                assert events.read_text() == 'frozen suite evidence\n'
        assert retained.phase == 'closed'
        assert [row[0] for row in seen][-2:] == ['auth_close', 'provider_close']
        with pytest.raises(SessionClosedError):
            await retained.describe()
    asyncio.run(run())


def test_listener_survives_its_completed_startup_scope(connection):
    from api_agent.providers.progress import component_timeout
    async def run():
        context = connection.agent_endpoint(Automated(), 'sample', 'http://localhost:9443', 'x' * 32)
        async with component_timeout('Synthetic startup', .15):
            endpoint = await context.__aenter__()
        try:
            await asyncio.sleep(.2)
            async with httpx.AsyncClient(trust_env=False) as client:
                headers = {'Origin': 'http://localhost:9443', 'X-API-Agent-Session': 'x' * 32}
                response = await chat(client, endpoint, headers)
                assert response.status_code == 200, response.text
        finally:
            await context.__aexit__(None, None, None)
    asyncio.run(run())


def test_original_browser_accepts_manual_turn_after_test_handle_retirement(connection, manual, tmp_path):
    from contextlib import asynccontextmanager
    from api_agent.testing.ui_runtime import UIIntegrationOwner
    from api_agent.ui import provider as included
    from playwright.async_api import expect
    factory, _, data = manual
    async def run():
        ready = False
        original = Automated()
        retained = RetainedStudio(original, factory, lambda: ready)
        @asynccontextmanager
        async def backend(values):
            async with connection.agent_endpoint(retained, 'sample', values['UI_LISTENER_URL'], 'x' * 32,
                    policy_config=data['config']) as endpoint:
                yield {'agent_url': endpoint, 'access_token': 'x' * 32}
        owner = UIIntegrationOwner({'config': {'UI_LISTENER_URL': 'http://127.0.0.1:9443',
            'UI_PROVIDER': str(Path(included.__file__)) + ':create_provider'},
            'owner_root': str(tmp_path)}, None, backend=backend)
        try:
            prepared = await owner.start()
            page = prepared.session.page
            assert await connection.browser_prompt(prepared) == 'Ok'
            # Retire the actual test handle. A later call must use another owner.
            def retired(*args): raise AssertionError('retired test handle used')
            original.complete = original.describe = retired
            await page.locator('#prompt').fill('synthetic pending request')
            await page.locator('#send').click()
            await expect(page.locator('#status')).to_contain_text('Wait for suite completion')
            assert retained.manual is None
            ready = True
            await page.locator('#prompt').fill('synthetic manual request')
            await page.locator('#send').click()
            await expect(page.locator('.assistant .message-body').last).to_have_text('manual')
            assert prepared.session.page is page and not page.is_closed()
            assert retained.usage.report()['prompt_count'] == 1
        finally:
            await owner.aclose()
        assert page.is_closed()
        assert retained.manual.closed
    asyncio.run(run())


def test_review_endpoint_receipt_finishes_entire_retained_owner(connection, manual):
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.sessions import InspectionOwner
    factory, seen, _ = manual
    async def run():
        retained = RetainedStudio(Automated(), factory, lambda: True)
        retained.retire_test()
        context = connection.agent_endpoint(retained, 'sample', 'http://localhost:9443', 'x' * 32)
        await context.__aenter__()
        await retained.describe()
        provider = retained.manual
        provider.inspection = InspectionOwner()
        inspected = AsyncMock()
        provider.inspection.retain([(SimpleNamespace(aclose=inspected), 'aclose')])
        original_close = provider.owner._auth.aclose
        attempts = 0
        async def auth_close():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError('synthetic authentication cleanup failure')
            await original_close()
        provider.owner._auth.aclose = auth_close
        try:
            with pytest.raises(CleanupPendingError) as pending:
                await context.__aexit__(None, None, None)
            await pending.value.retry()
            assert attempts == 2
            assert provider.closed, 'retry discarded remaining RetainedStudio cleanup'
            assert seen[-1][0] == 'provider_close'
            inspected.assert_awaited_once()
        finally:
            await retained.aclose()
    asyncio.run(run())



def test_review_pending_http_cleanup_preserves_manual_dependencies(connection, manual, monkeypatch):
    from api_agent.providers.control import CleanupPendingError
    factory, seen, _ = manual
    saved = []
    from api_agent.testing import studio_http
    original = studio_http.StudioBackend
    class Backend(original):
        async def aclose(self):
            if len(saved) < 2:
                saved.append(self)
                raise CleanupPendingError((self,), self.aclose)
            await super().aclose()
    monkeypatch.setattr(studio_http, 'StudioBackend', Backend)
    async def run():
        retained = RetainedStudio(Automated(), factory, lambda: True)
        retained.retire_test()
        context = connection.agent_endpoint(retained, 'sample', 'http://localhost:9443', 'x' * 32)
        await context.__aenter__()
        await retained.describe()
        try:
            with pytest.raises(CleanupPendingError) as pending:
                await context.__aexit__(None, None, None)
            assert not retained.manual.closed, 'manual dependencies closed before HTTP cleanup acknowledged'
            await pending.value.retry()
            assert saved[0].closed and retained.manual.closed
        finally:
            if saved:
                await saved[0].aclose()
            await retained.aclose()
    asyncio.run(run())


def test_review_endpoint_interruption_exports_complete_cleanup_receipt(connection, manual):
    from api_agent.providers.control import CleanupPendingError
    factory, seen, _ = manual
    async def run():
        retained = RetainedStudio(Automated(), factory, lambda: True)
        retained.retire_test()
        context = connection.agent_endpoint(retained, 'sample', 'http://localhost:9443', 'x' * 32)
        await context.__aenter__()
        await retained.describe()
        provider = retained.manual
        close = provider._close
        attempts = 0
        async def interrupted_close():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise asyncio.CancelledError()
            await close()
        provider._close = interrupted_close
        try:
            with pytest.raises(asyncio.CancelledError) as stopped:
                await context.__aexit__(None, None, None)
            receipt = getattr(stopped.value, 'cleanup_pending', None)
            assert isinstance(receipt, CleanupPendingError), 'endpoint lost interrupted cleanup receipt'
            assert not provider.closed
            await receipt.retry()
            assert provider.closed and seen[-1][0] == 'provider_close'
        finally:
            await retained.aclose()
    asyncio.run(run())


def test_review_endpoint_stops_two_requests_before_manual_dependencies(connection, manual):
    factory, seen, _ = manual
    async def run():
        retained = RetainedStudio(Automated(), factory, lambda: True)
        retained.retire_test()
        context = connection.agent_endpoint(retained, 'sample', 'http://localhost:9443', 'x' * 32)
        endpoint = await context.__aenter__()
        await retained.describe()
        entered, stopped = set(), set()
        both = asyncio.Event()
        async def wait(request, *, auth):
            entered.add(request.request_id)
            if len(entered) == 2:
                both.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(request.request_id)
        retained.manual.provider.implementation.acomplete = wait
        original_close = retained.manual.provider.aclose
        async def checked_close():
            assert len(stopped) == 2, 'manual provider disposed while HTTP requests remained active'
            await original_close()
        retained.manual.provider.aclose = checked_close
        headers = {'Origin': 'http://localhost:9443', 'X-API-Agent-Session': 'x' * 32}
        tasks = []
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            try:
                tasks = [asyncio.create_task(chat(client, endpoint, headers)) for _ in range(2)]
                await asyncio.wait_for(both.wait(), 2)
                await asyncio.wait_for(context.__aexit__(None, None, None), 6)
                await asyncio.gather(*tasks, return_exceptions=True)
                assert stopped == entered
                assert retained.manual.closed
                assert retained.usage.report()['prompt_count'] == 2
                assert all(row['finished_at'] is not None for row in retained.usage.inference_calls.values())
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await retained.aclose()
    asyncio.run(run())
