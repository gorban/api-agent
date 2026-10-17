"""Managed publication uses the same literal address as its verified listener."""
import pytest

from api_agent.testing.storage import RunStorage


@pytest.mark.parametrize('endpoint', [None, 'http://localhost:4566', 'http://127.0.0.1:4566/'])
def test_managed_storage_pins_the_verified_ipv4_address(endpoint):
    storage = RunStorage('synthetic-results', endpoint_url=endpoint, managed_local=True)
    assert storage.endpoint_url == 'http://127.0.0.1:4566'
    assert storage._client().meta.endpoint_url == 'http://127.0.0.1:4566'


def test_managed_storage_preserves_the_configured_port():
    storage = RunStorage('synthetic-results', endpoint_url='http://localhost:9876',
                         localstack_port=9876, managed_local=True)
    assert storage.endpoint_url == 'http://127.0.0.1:9876'


@pytest.mark.parametrize('endpoint', ['http://localhost:4566', 'https://storage.example.test'])
def test_publication_only_preserves_the_selected_endpoint(endpoint):
    assert RunStorage('synthetic-results', endpoint_url=endpoint).endpoint_url == endpoint


@pytest.mark.parametrize('endpoint', ['http://localhost:9876', 'http://other.example.test:4566'])
def test_managed_storage_rejects_an_unverified_endpoint(endpoint):
    with pytest.raises(ValueError, match='configured loopback endpoint'):
        RunStorage('synthetic-results', endpoint_url=endpoint, managed_local=True)


def test_managed_storage_selection_requires_a_boolean():
    with pytest.raises(ValueError, match='must be a boolean'):
        RunStorage('synthetic-results', managed_local='True')


def test_explicit_empty_managed_endpoint_is_not_defaulted():
    with pytest.raises(ValueError, match='Compose endpoint'):
        RunStorage('synthetic-results', endpoint_url='', managed_local=True)
