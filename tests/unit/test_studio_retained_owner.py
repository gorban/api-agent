"""Default unit controls for retained ownership, accounting and disposal."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.providers.contracts import ProviderCapabilities, SessionClosedError, ProviderAccessError
from api_agent.providers.llm import AvailableSelection, LLMResult, Message, TextContent, Usage
from api_agent.testing.studio_retained import RetainedStudio, retained_provider_factory


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


def test_retirement_before_first_call_and_repeated_retirement(manual):
    factory, seen, _ = manual
    async def run():
        retained = RetainedStudio(Automated(), factory, lambda: False)
        retained.retire_test()
        retained.retire_test()
        with pytest.raises(SessionClosedError): await retained.complete(object())
        assert retained.automated.calls == 0 and not seen
        await retained.aclose()
        retained.retire_test()
        assert retained.phase == 'closed'
    asyncio.run(run())


def test_original_failure_retires_handle():
    class Failed(Automated):
        def complete(self, request): raise RuntimeError('controlled original failure')
    async def run():
        retained = RetainedStudio(Failed(), lambda _: None, lambda: False)
        with pytest.raises(RuntimeError): await retained.complete(object())
        with pytest.raises(SessionClosedError): await retained.describe()
        await retained.aclose()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['cancel', 'provider'])
def test_manual_failure_keeps_separate_usage_and_cleanup(manual, failure):
    factory, seen, _ = manual
    async def run():
        retained = RetainedStudio(Automated(), factory, lambda: True)
        retained.retire_test()
        await retained.describe()
        implementation = retained.manual.provider.implementation
        entered, release = asyncio.Event(), asyncio.Event()
        async def fail(request, *, auth):
            entered.set()
            if failure == 'provider':
                raise ProviderAccessError('unknown', 'Controlled failure.', usage=Usage(1, 1))
            await release.wait()
        implementation.acomplete = fail
        from api_agent.providers.llm import LLMRequest, CompletionRequest
        request = LLMRequest('sample', CompletionRequest((Message('user', (TextContent('synthetic'),)),)))
        task = asyncio.create_task(retained.complete(request))
        await entered.wait()
        if failure == 'cancel': task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else ProviderAccessError):
            await task
        facts = list(retained.usage.inference_calls.values())
        assert len(facts) == 1 and facts[0]['outcome'] == 'failed'
        assert facts[0]['finished_at'] is not None
        assert retained.usage.report()['prompt_count'] == 1
        await retained.aclose()
        assert seen[-1][0] == 'provider_close'
    asyncio.run(run())


def test_manual_missing_metadata_and_retryable_disposal(manual):
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.llm import LLMRequest, CompletionRequest
    from api_agent.providers.sessions import InspectionOwner
    factory, _, _ = manual
    async def run():
        retained = RetainedStudio(Automated(), factory, lambda: True)
        retained.retire_test()
        await retained.describe()
        provider = retained.manual
        provider.inspection = InspectionOwner()
        async def complete(request, *, auth):
            return LLMResult(Message('assistant', (TextContent('manual'),)))
        provider.provider.implementation.acomplete = complete
        request = LLMRequest('sample', CompletionRequest((Message('user', (TextContent('synthetic'),)),)))
        await retained.complete(request)
        assert retained.usage.report()['usage_rows'][0]['total_tokens'] is None
        close = provider._close
        async def fail_once():
            if not getattr(fail_once, 'attempted', False):
                fail_once.attempted = True
                raise CleanupPendingError((provider,), close)
            await close()
        provider._close = fail_once
        with pytest.raises(CleanupPendingError): await retained.aclose()
        assert retained.manual is provider
        assert not provider.closed and not provider.inspection._closed
        with pytest.raises(SessionClosedError): await retained.complete(request)
        await retained.aclose()
        assert provider.closed and provider.inspection._closed
    asyncio.run(run())




def test_direct_automation_and_manual_configuration_keep_distinct_accounting(manual, tmp_path, monkeypatch):
    from api_agent.providers.llm import LLMRequest, CompletionRequest, GenerationOptions
    factory, seen, _ = manual
    events = tmp_path / 'events'
    events.write_bytes(b'frozen accounting\n')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(events))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    async def run():
        ready = False
        original = Automated()
        owner = RetainedStudio(original, factory, lambda: ready)
        assert (await owner.describe())[0].selected_llm == 'sample'
        request = LLMRequest('other', CompletionRequest((Message('user', (TextContent('synthetic'),)),),
            generation=GenerationOptions(reasoning_effort='high')))
        assert (await owner.complete(request)).payload.content == (TextContent('Ok'),)
        assert owner.usage.report()['prompt_count'] == 0
        with pytest.raises(SessionClosedError): await owner.describe()
        ready = True
        choices = await owner.describe()
        assert [choice.selected_llm for choice in choices] == ['sample', 'other']
        assert seen[0][1] == {'interactive': False, 'user_index': 3, 'credentials': None}
        assert seen[0][2] is asyncio.get_running_loop()
        assert owner.manual.loop is asyncio.get_running_loop()
        await owner.complete(request)
        assert seen[-1][1].selected_llm == 'other'
        assert seen[-1][1].payload.generation.reasoning_effort == 'high'
        facts = list(owner.usage.inference_calls.values())
        assert len(facts) == 1
        assert facts[0]['actual_model'] == 'other-v2'
        assert facts[0]['service_tier'] == 'default' and facts[0]['serving_revision'] == 'v2'
        assert owner.usage.report()['prompt_count'] == 1
        assert owner.usage.report()['usage_rows'][0]['total_tokens'] == 5
        assert original.calls == 1 and events.read_bytes() == b'frozen accounting\n'
        await owner.aclose()
        await owner.aclose()
        assert owner.manual.closed
        assert [row[0] for row in seen].count('provider_close') == 1
    asyncio.run(run())


def test_ordered_cleanup_preserves_each_pending_stage_and_later_callbacks():
    from api_agent.providers.control import CleanupPendingError
    from api_agent.testing.studio_retained import StudioCleanup
    async def run():
        events = []
        owner = StudioCleanup()
        def stage(name):
            ready = False
            async def child():
                nonlocal ready
                events.append(name + '-child')
                ready = True
            async def close():
                events.append(name + '-parent')
                if not ready:
                    raise CleanupPendingError((name,), child)
                events.append(name + '-done')
            return close
        async def dependency(value): events.append(value)
        owner.push(dependency, 'dependency')
        owner.push(stage('backend'))
        owner.push(stage('server'))
        with pytest.raises(CleanupPendingError) as first: await owner.aclose()
        assert events == ['server-parent']
        with pytest.raises(CleanupPendingError) as second: await first.value.retry()
        assert events == ['server-parent', 'server-child', 'server-parent', 'server-done', 'backend-parent']
        await second.value.retry()
        assert events[-4:] == ['backend-child', 'backend-parent', 'backend-done', 'dependency']
        assert owner.callbacks == [] and owner.pending is None
        await owner.aclose()
        assert events.count('dependency') == 1
    asyncio.run(run())


def test_interrupted_retained_cleanup_receipt_finishes_remaining_duties(manual):
    from api_agent.providers.control import CleanupPendingError
    from api_agent.providers.sessions import InspectionOwner
    factory, seen, _ = manual
    async def run():
        owner = RetainedStudio(Automated(), factory, lambda: True)
        owner.retire_test()
        await owner.describe()
        provider = owner.manual
        provider.inspection = InspectionOwner()
        inspection_close = AsyncMock()
        provider.inspection.retain([(SimpleNamespace(aclose=inspection_close), 'aclose')])
        close = provider._close
        interrupted = False
        async def fail_once():
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise asyncio.CancelledError()
            await close()
        provider._close = fail_once
        with pytest.raises(asyncio.CancelledError) as stopped: await owner.aclose()
        receipt = stopped.value.cleanup_pending
        assert isinstance(receipt, CleanupPendingError)
        assert receipt.owners == (owner,)
        assert not provider.closed
        inspection_close.assert_not_awaited()
        await receipt.retry()
        assert provider.closed
        inspection_close.assert_awaited_once()
        assert seen[-1][0] == 'provider_close'
    asyncio.run(run())
