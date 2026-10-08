"""Factory contracts fail closed and release isolated imports on every exit."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from api_agent.config import load_shared_schema
from api_agent.providers import loading
from test_provider_boundary import factory


@pytest.mark.parametrize('location', [None, 7, ' ', 'missing', 'file.py:bad-name'])
def test_location_requires_an_explicit_factory(location):
    with pytest.raises(loading.ProviderError, match='provider_location_'):
        loading.provider_location(location)


def test_location_rejects_missing_files_and_non_python_files(tmp_path):
    path = tmp_path / 'factory.txt'
    path.write_text('unused')
    for candidate in (path, tmp_path / 'missing.py'):
        with pytest.raises(loading.ProviderError, match='provider_location_invalid'):
            loading.provider_location(str(candidate) + ':create')


def test_schema_without_contribution_is_unchanged(tmp_path):
    schema = load_shared_schema()
    assert loading.provider_schema(factory(tmp_path), schema) is schema


def test_schema_cannot_follow_a_link_outside_provider_directory(tmp_path):
    outside = tmp_path / 'outside.toml'
    outside.write_text('version = 1\n')
    root = tmp_path / 'provider'
    root.mkdir()
    location = factory(root)
    (root / 'cfg').mkdir()
    (root / 'cfg/config.toml').symlink_to(outside)
    with pytest.raises(loading.ProviderError, match='provider_configuration_escape'):
        loading.provider_schema(location, load_shared_schema())


@pytest.mark.parametrize('spec', [None, SimpleNamespace(loader=None)])
def test_missing_import_loader_is_rejected(tmp_path, monkeypatch, spec):
    location = factory(tmp_path)
    before = set(sys.modules)
    monkeypatch.setattr(loading.importlib.util, 'spec_from_file_location', lambda *args: spec)
    with pytest.raises(loading.ProviderError, match='provider_location_invalid'):
        loading.load_provider(location, {}, {})
    assert set(sys.modules) == before


@pytest.mark.parametrize('body', [
    'create = 7',
    'def create(config, services): return None',
    'def create(config, services): return object()',
    'class Adapter: version = 2\ndef create(config, services): return Adapter()',
    'class Adapter: version = True\ndef create(config, services): return Adapter()',
    'class Adapter: version = 1.0\ndef create(config, services): return Adapter()',
    'async def create(config, services): return object()',
    'class Pending:\n    def __await__(self): yield\ndef create(config, services): return Pending()',
    *[f'class Adapter:\n    version = 1\n    {hook} = 42\ndef create(config, services): return Adapter()'
      for hook in ('authenticate', 'astart', 'aclose', 'setup_routes')],
])
def test_invalid_factories_do_not_leave_imports_or_expose_details(tmp_path, body):
    location = factory(tmp_path, body)
    before = set(sys.modules)
    with pytest.raises(loading.ProviderError) as caught:
        loading.load_provider(location, {}, {})
    if caught.value.retained_owners:
        assert str(caught.value) == 'provider_cleanup_pending'
        owner = caught.value.retained_owners[0]
        assert owner.module_name in sys.modules
        asyncio.run(owner.aclose())
    else:
        assert str(caught.value) == 'provider_initialization_failed'
    assert set(sys.modules) == before


@pytest.mark.parametrize('exception', ['KeyboardInterrupt', 'SystemExit'])
def test_process_signals_propagate_after_import_cleanup(tmp_path, exception):
    location = factory(tmp_path, f'def create(config, services): raise {exception}()')
    before = set(sys.modules)
    with pytest.raises({'KeyboardInterrupt': KeyboardInterrupt, 'SystemExit': SystemExit}[exception]):
        loading.load_provider(location, {}, {})
    assert set(sys.modules) == before


def test_optional_lifecycle_hooks_and_synchronous_registration(tmp_path):
    location = factory(tmp_path, '''class Adapter:
    version = 1
    def setup_routes(self, registrar, services): registrar.append(services)
def create(config, services): return Adapter()
''')
    owner = loading.load_provider(location, {}, {})
    registrations = []
    services = object()
    async def scenario():
        await owner.astart()
        await owner.setup_routes(registrations, services)
        assert registrations == [services]
        await owner.aclose()
        await owner.aclose()
        with pytest.raises(loading.ProviderError, match='provider_closed'):
            await owner.astart()
    asyncio.run(scenario())
    assert owner.closed and owner.module_name not in sys.modules
    empty = loading.LoadedProvider(object(), 'not-imported')
    asyncio.run(empty.setup_routes(registrations, services))
    assert registrations == [services]


def test_failed_shutdown_retains_import_until_confirmed_retry(tmp_path):
    location = factory(tmp_path, '''class Adapter:
    version = 1
    async def aclose(self): raise RuntimeError('shutdown failed')
def create(config, services): return Adapter()
''')
    owner = loading.load_provider(location, {}, {})
    from unittest.mock import AsyncMock
    async def scenario():
        with pytest.raises(loading.ProviderError, match='cleanup_pending') as error:
            await owner.aclose()
        assert error.value.retained_owners == (owner,)
        assert owner.closed and not owner.disposed and owner.module_name in sys.modules
        owner.implementation.aclose = AsyncMock()
        await owner.aclose()
        assert owner.disposed and owner.module_name not in sys.modules
        owner.implementation.aclose.assert_awaited_once()
        await owner.aclose()
        owner.implementation.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_factory_gets_an_immutable_configuration_snapshot(tmp_path):
    config = {'LIMIT': 3}
    services = object()
    owner = loading.load_provider(factory(tmp_path), config, services)
    try:
        config['LIMIT'] = 8
        assert owner.implementation.config['LIMIT'] == 3
        assert owner.implementation.services is services
        with pytest.raises(TypeError):
            owner.implementation.config['LIMIT'] = 9
    finally:
        asyncio.run(owner.aclose())


def test_bootstrap_selection_obeys_each_precedence_layer(tmp_path):
    schema = load_shared_schema()
    location = factory(tmp_path)
    bundled = loading.bootstrap_schema(schema, [], {}, {})
    assert bundled.fields['GPT_AUTH_MODE'].default == 'subscription'
    assert bundled.fields['LLM_CREDENTIAL_ROLE'].default == ''
    # Higher-precedence explicit empty selections disable the lower selection.
    assert loading.bootstrap_schema(schema, [], {'LLM_PROVIDER': ''}, {'LLM_PROVIDER': '/missing.py:create'}) is schema
    assert loading.bootstrap_schema(schema, ['--llm-provider='], {}, {'LLM_PROVIDER': '/missing.py:create'}) is schema
    assert loading.bootstrap_schema(schema, [], {}, {'LLM_PROVIDER': location}, {'LLM_PROVIDER': ''}) is schema
    assert loading.bootstrap_schema(schema, [], {}, {'LLM_PROVIDER': location}) is schema
