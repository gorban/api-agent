"""Selected manifest lookup follows the provider startup owner rules."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api_agent.api.application import Runtime
from api_agent.providers.distribution import selected_manifests
from api_agent.providers.loading import ProviderError, provider_location


@pytest.fixture
def provider_roots(tmp_path):
    api = tmp_path / 'api'
    for directory in (api, api / 'upstream', tmp_path / 'upstream'):
        directory.mkdir(exist_ok=True)
        (directory / 'factory.py').write_text('raise AssertionError("must remain inert")\n')
        (directory / 'provider-dependencies.toml').write_text(
            f'version = 1\ndependencies = ["{directory.parent.name}-{directory.name}==1"]\n')
    return tmp_path, api


def test_selected_upstream_uses_api_owner_not_pack_root(provider_roots):
    root, api = provider_roots
    values = {'API_PROVIDER': 'api/factory.py:create', 'UPSTREAM_API_PROVIDER': 'upstream/factory.py:create'}

    manifest, = selected_manifests(values, owner_root=root, active_kinds={'upstream'})

    assert manifest.root == api / 'upstream'
    assert manifest.requirements() == (f'{api.name}-upstream==1',)


def test_absolute_upstream_path_keeps_its_explicit_owner(provider_roots):
    root, api = provider_roots
    values = {'API_PROVIDER': 'api/factory.py:create',
              'UPSTREAM_API_PROVIDER': str(root / 'upstream/factory.py') + ':create'}

    manifest, = selected_manifests(values, owner_root=root, active_kinds={'upstream'})

    assert manifest.root == root / 'upstream'


def test_upstream_without_api_uses_general_owner(provider_roots):
    root, _ = provider_roots
    manifest, = selected_manifests({'UPSTREAM_API_PROVIDER': 'upstream/factory.py:create'},
                                   owner_root=root, active_kinds={'upstream'})
    assert manifest.root == root / 'upstream'


def test_missing_api_for_selected_upstream_rejects_before_manifest(provider_roots):
    root, _ = provider_roots
    with pytest.raises(ProviderError, match='provider_location_invalid'):
        selected_manifests({'API_PROVIDER': 'missing.py:create',
                            'UPSTREAM_API_PROVIDER': 'upstream/factory.py:create'},
                           owner_root=root, active_kinds={'upstream'})


def test_missing_selected_upstream_rejects_after_api_resolution(provider_roots):
    root, _ = provider_roots
    with pytest.raises(ProviderError, match='provider_location_invalid'):
        selected_manifests({'API_PROVIDER': 'api/factory.py:create',
                            'UPSTREAM_API_PROVIDER': 'missing.py:create'},
                           owner_root=root, active_kinds={'upstream'})


def test_inactive_or_absent_upstream_does_not_resolve_api(provider_roots):
    root, _ = provider_roots
    values = {'API_PROVIDER': 'missing.py:create', 'UPSTREAM_API_PROVIDER': 'missing.py:create'}
    assert selected_manifests(values, owner_root=root, active_kinds=set()) == ()
    assert selected_manifests({'API_PROVIDER': 'missing.py:create'}, owner_root=root,
                              active_kinds={'upstream'}) == ()


def test_runtime_upstream_start_uses_same_path_as_selected_manifest(provider_roots, monkeypatch):
    from api_agent.providers import loading
    root, api = provider_roots
    values = {'API_PROVIDER': 'api/factory.py:create', 'UPSTREAM_API_PROVIDER': 'upstream/factory.py:create'}
    resolved = []
    owner = SimpleNamespace(aclose=AsyncMock())
    async def load(location, config, services, *, kind):
        resolved.append(provider_location(location, owner_root=services['owner_root'])[0])
        return owner
    monkeypatch.setattr(loading, 'aload_provider', load)

    async def run():
        runtime = Runtime('demo', factory_plan=[('upstream', values['UPSTREAM_API_PROVIDER'], values,
                                                {'owner_root': root})])
        await runtime.astart()
        await runtime.aclose()
    asyncio.run(run())

    manifest, = selected_manifests(values, owner_root=root, active_kinds={'upstream'})
    assert resolved == [api / 'upstream/factory.py']
    assert resolved[0].parent == manifest.root
