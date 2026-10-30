"""Synthetic provider admission precedes authentication and capture."""
import asyncio

import pytest

from api_agent.providers.credential_guard import CredentialProtectionError
from api_agent.providers.loading import LoadedProvider
from api_agent.providers.contracts import ProviderCapabilities
from api_agent.providers.llm import CompletionRequest, LLMRequest, Message, TextContent
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.inference_capture import collect_inference, capture_inference, withholding_inference_content


def test_direct_loaded_provider_rejects_unjoined_before_generation():
    class Provider:
        capabilities = ProviderCapabilities(kind='llm', operations=('complete',))
        async def acomplete(self, request, *, auth):
            pytest.fail('Unjoined provider was invoked')

    async def run():
        provider = LoadedProvider(Provider(), 'synthetic')
        request = LLMRequest('sample', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        with pytest.raises(CredentialProtectionError, match='must implement'):
            await provider.require('llm', 'complete').acomplete(request)
    asyncio.run(run())


def test_withheld_capture_preserves_phase_without_content():
    boundary = EmissionBoundary(ContentPolicy(True))
    with collect_inference(boundary) as rows:
        with withholding_inference_content():
            capture_inference('input', {'value': 'synthetic sensitive text'}, 'call')
    assert rows['rows'][0]['content'] == {}
    assert rows['rows'][0]['reason'] == 'withheld'


@pytest.mark.parametrize('operation,method', [('describe', 'adescribe'), ('complete', 'asetup')])
def test_direct_facade_rejects_unjoined_before_setup_or_discovery(operation, method):
    class Provider:
        capabilities = ProviderCapabilities(kind='llm', operations=(operation,))
        async def asetup(self, **kwargs): pytest.fail('setup ran')
        async def adescribe(self, **kwargs): pytest.fail('discovery ran')

    async def run():
        view = LoadedProvider(Provider(), 'synthetic').require('llm', operation)
        with pytest.raises(CredentialProtectionError, match='must implement'):
            await getattr(view, method)()
    asyncio.run(run())


def test_direct_facade_joins_active_collector_before_provider_registers():
    from api_agent.providers.llm import LLMResult
    secret = 'synthetic-provider-private-value'
    boundary = EmissionBoundary(ContentPolicy(True))
    class Provider:
        capabilities = ProviderCapabilities(kind='llm', operations=('complete',))
        credential_protection_version = 1
        def bind_credential_authority(self, authority): self.authority = authority
        async def acomplete(self, request, *, auth):
            await self.authority.aregister_secret(secret)
            capture_inference('input', {'text': secret}, 'call')
            return LLMResult(Message('assistant', (TextContent('safe answer'),)))

    async def run():
        adapter = Provider()
        view = LoadedProvider(adapter, 'synthetic').require('llm', 'complete')
        request = LLMRequest('sample', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        with collect_inference(boundary) as collector:
            await view.acomplete(request)
        assert adapter.authority._authority is boundary._authority
        assert secret not in repr(collector['rows'])
        assert collector['rows']
    asyncio.run(run())


def test_direct_facade_withholds_if_already_bound_to_different_collector():
    from api_agent.providers.llm import LLMResult
    first = EmissionBoundary(ContentPolicy(True))
    second = EmissionBoundary(ContentPolicy(True))
    class Provider:
        capabilities = ProviderCapabilities(kind='llm', operations=('complete',))
        credential_protection_version = 1
        def bind_credential_authority(self, authority): self.authority = authority
        async def acomplete(self, request, *, auth):
            capture_inference('input', {'text': 'content'}, 'call')
            return LLMResult(Message('assistant', (TextContent('safe'),)))

    async def run():
        view = LoadedProvider(Provider(), 'synthetic', services={'emissions': first}).require('llm', 'complete')
        request = LLMRequest('sample', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        with collect_inference(second) as collector:
            await view.acomplete(request)
        assert collector['authority_conflict']
        assert all(not row['content'] for row in collector['rows'])
    asyncio.run(run())


def test_replaced_provider_cannot_reuse_previous_admission_receipt():
    from api_agent.providers.llm import LLMResult
    class Joined:
        capabilities = ProviderCapabilities(kind='llm', operations=('complete',))
        credential_protection_version = 1
        def bind_credential_authority(self, authority): self.authority = authority
        async def acomplete(self, request, *, auth=None):
            return LLMResult(Message('assistant', (TextContent('safe'),)))
    class Unjoined:
        capabilities = ProviderCapabilities(kind='llm', operations=('complete',))
        async def acomplete(self, request, *, auth=None): pytest.fail('replacement ran')
    async def run():
        owner = LoadedProvider(Joined(), 'synthetic')
        view = owner.require('llm', 'complete')
        request = LLMRequest('sample', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        await view.acomplete(request)
        owner.implementation = Unjoined()
        with pytest.raises(CredentialProtectionError, match='changed'):
            await owner.require('llm', 'complete').acomplete(request)
        with pytest.raises(CredentialProtectionError, match='changed'):
            await view.acomplete(request)
    asyncio.run(run())


def test_changed_authority_cannot_reuse_previous_admission_receipt():
    from api_agent.providers.llm import LLMResult
    class Joined:
        capabilities = ProviderCapabilities(kind='llm', operations=('complete',))
        credential_protection_version = 1
        def bind_credential_authority(self, authority): self.authority = authority
        async def acomplete(self, request, *, auth=None):
            return LLMResult(Message('assistant', (TextContent('safe'),)))
    async def run():
        owner = LoadedProvider(Joined(), 'synthetic')
        request = LLMRequest('sample', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        await owner.require('llm', 'complete').acomplete(request)
        owner._credential_authority = EmissionBoundary()
        with pytest.raises(CredentialProtectionError, match='changed'):
            await owner.require('llm', 'complete').acomplete(request)
    asyncio.run(run())


def test_stale_stream_view_never_calls_provider_validation_or_binds():
    class Provider:
        capabilities = ProviderCapabilities(kind='llm', operations=('stream',))
        credential_protection_version = 1
        def __init__(self): self.calls = []
        def bind_credential_authority(self, authority): self.calls.append('bind')
        def validate_request(self, request):
            self.calls.append('validate')
            raise AssertionError('stale validation ran')
        async def astream(self, request, *, auth=None):
            self.calls.append('stream')
            yield None
    async def run():
        original = Provider()
        owner = LoadedProvider(original, 'synthetic')
        view = owner.require('llm', 'stream')
        owner.implementation = Provider()
        request = LLMRequest('sample', CompletionRequest((Message('user', (TextContent('hello'),)),)))
        stream = view.astream(request)
        assert original.calls == []
        with pytest.raises(CredentialProtectionError, match='changed'):
            await anext(stream)
        assert original.calls == []
        await stream.aclose()
    asyncio.run(run())
