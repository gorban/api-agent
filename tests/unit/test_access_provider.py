"""Access checks are independently configured and cannot advertise inference."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from api_agent.config import load_shared_schema
from api_agent.providers.contracts import ProviderCapabilities, OperationalError
from api_agent.providers.loading import aload_provider, bootstrap_schema, validate_capabilities, ProviderError


def test_access_declaration_bootstrap_without_factory_execution(tmp_path):
    (tmp_path / 'cfg').mkdir()
    (tmp_path / 'cfg/config.toml').write_text('version=1\n[fields.ACCESS_LIMIT]\ntype="int"\ndefault=3\n')
    path = tmp_path / 'provider.py'
    path.write_text("raise AssertionError('factory discovery executed')")
    schema = bootstrap_schema(load_shared_schema(), ['--access-provider=' + str(path) + ':create'], {}, {})
    assert schema.fields['ACCESS_LIMIT'].default == 3
    assert schema.fields['ACCESS_PROVIDER'].acknowledge_default is False


def test_access_runtime_independent_of_inference(tmp_path):
    path = tmp_path / 'provider.py'
    path.write_text('''from api_agent.providers.contracts import ProviderCapabilities
class Access:
    version=1
    capabilities=ProviderCapabilities('access', ('describe','usage'))
    async def adescribe(self, *, auth=None): return ('available',)
    async def aread_usage(self, *, auth=None): return {'used': 0}
def create(config, services): return Access()
''')
    async def run():
        owner = await aload_provider(str(path) + ':create', {}, {}, kind='access')
        assert await owner.require('access', 'describe').adescribe() == ('available',)
        assert await owner.require('access', 'usage').aread_usage() == {'used': 0}
        for kind, operation in [('access', 'complete'), ('access','stream'), ('llm','complete')]:
            with pytest.raises(ProviderError, match='operation_unavailable'):
                owner.require(kind, operation)
        await owner.aclose()
        assert owner.disposed
    asyncio.run(run())


@pytest.mark.parametrize('features,kind', [(('unknown',), 'llm'), (('tools','tools'), 'llm'),
    (['tools'], 'llm'), ((1,), 'llm'), (('tools',), 'access')])
def test_invalid_feature_descriptions_rejected(features, kind):
    with pytest.raises(ProviderError, match='capability_invalid'):
        validate_capabilities(SimpleNamespace(capabilities=ProviderCapabilities(kind, features=features)))


@pytest.mark.parametrize('operation', ['complete','stream'])
def test_access_adapter_cannot_declare_generation(operation):
    with pytest.raises(ProviderError, match='capability_invalid'):
        validate_capabilities(SimpleNamespace(capabilities=ProviderCapabilities('access', (operation,))))


def test_stream_factory_preserves_approved_safe_failure():
    from api_agent.providers.loading import _StreamView
    from api_agent.providers.llm import LLMRequest,CompletionRequest,Message,TextContent
    request = LLMRequest('synthetic',CompletionRequest((Message('user',(TextContent('Question'),)),)))
    expected = OperationalError('rate_limited', retry_after=2)
    def stream(*args, **kwargs): raise expected
    async def scenario():
        iterator = _StreamView(SimpleNamespace(astream=stream,
            credential_protection_version=1, bind_credential_authority=lambda authority: None)).astream(request)
        try:
            with pytest.raises(OperationalError) as caught:
                await anext(iterator)
            assert caught.value is expected
            assert caught.value.retry_after == 2
        finally:
            await iterator.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['kind', 'operations', 'credential_roles', 'llm_role', 'features'])
def test_classifications_reject_non_string_and_unsafe_string_subclasses(field):
    class Unsafe(str):
        def __repr__(self): return 'private-payload'
    for unsafe in ([], Unsafe('llm')):
        kwargs = {'kind': 'llm'}
        kwargs[field] = (unsafe,) if field in {'operations', 'credential_roles', 'features'} else unsafe
        with pytest.raises(ProviderError) as caught:
            validate_capabilities(SimpleNamespace(capabilities=ProviderCapabilities(**kwargs)))
        assert str(caught.value) == 'provider_capability_invalid'
        assert 'private-payload' not in repr(caught.value)
