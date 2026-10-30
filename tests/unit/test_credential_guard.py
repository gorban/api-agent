import pytest

from api_agent.providers.credential_guard import (
    CredentialProtectionError, guard_json, require_joined_provider,
)


def test_guard_preserves_clean_json_types_and_copies_containers():
    original = {'text': ['hello', {'count': 3, 'ok': True, 'empty': None}]}
    result = guard_json(original, {'private-token'})
    assert result == original
    assert result is not original and result['text'] is not original['text']


@pytest.mark.parametrize('value', [
    {'text': 'private-token'},
    {'text': ['private-', 'token']},
    {'text': r'private-\u0074oken'},
    {'text': r'private-\\u0074oken'},
    {'text': 'private-\ntoken'},
    {'private-token': 1},
])
def test_guard_rejects_known_credentials_in_carriers(value):
    with pytest.raises(CredentialProtectionError, match='cannot be safely released'):
        guard_json(value, {'private-token'})


def test_guard_rejects_unsupported_values_without_coercion():
    with pytest.raises(CredentialProtectionError):
        guard_json({'value': object()}, {'private-token'})


def test_provider_must_explicitly_join_the_supplied_authority():
    from api_agent.telemetry.privacy import EmissionBoundary
    authority = EmissionBoundary()
    class Provider:
        credential_protection_version = 1
        def bind_credential_authority(self, supplied):
            self.authority = supplied
    provider = Provider()
    require_joined_provider(provider, authority)
    assert provider.authority is authority
    with pytest.raises(CredentialProtectionError):
        require_joined_provider(object(), authority)


def test_guard_rejects_large_string_before_serialization(monkeypatch):
    import api_agent.providers.credential_guard as guard
    def forbidden(*args, **kwargs):
        raise AssertionError('oversized content was serialized')
    monkeypatch.setattr(guard.json, 'dumps', forbidden)
    with pytest.raises(CredentialProtectionError):
        guard_json({'text': 'x' * (8 * 1024 * 1024 + 1)}, {'secret'})


def test_unrelated_object_members_are_not_joined():
    value = {'foo': 'bar'}
    assert guard_json(value, {'foobar'}) == value



def test_join_rejects_invalid_authority_without_calling_provider():
    class Provider:
        credential_protection_version = 1
        def bind_credential_authority(self, authority):
            raise AssertionError('must not be called')
    with pytest.raises(CredentialProtectionError, match='authority is unavailable'):
        require_joined_provider(Provider(), object())


def test_join_rejects_async_binding_and_hides_provider_failure():
    from api_agent.telemetry.privacy import EmissionBoundary
    class AsyncProvider:
        credential_protection_version = 1
        async def bind_credential_authority(self, authority):
            pass
    with pytest.raises(CredentialProtectionError, match='could not join'):
        require_joined_provider(AsyncProvider(), EmissionBoundary())
    class BrokenProvider:
        @property
        def credential_protection_version(self):
            raise ValueError('private detail')
    with pytest.raises(CredentialProtectionError, match='could not join') as error:
        require_joined_provider(BrokenProvider(), EmissionBoundary())
    assert 'private detail' not in str(error.value)


@pytest.mark.parametrize('version', [True, 0, 2, '1', None])
def test_join_requires_exact_contract_version(version):
    from api_agent.telemetry.privacy import EmissionBoundary
    class Provider:
        credential_protection_version = version
        def bind_credential_authority(self, authority):
            raise AssertionError('must not be called')
    with pytest.raises(CredentialProtectionError, match='must implement'):
        require_joined_provider(Provider(), EmissionBoundary())


def test_join_hides_even_contract_exceptions_from_provider():
    from api_agent.telemetry.privacy import EmissionBoundary
    class Provider:
        credential_protection_version = 1
        def bind_credential_authority(self, authority):
            raise CredentialProtectionError('private detail')
    with pytest.raises(CredentialProtectionError, match='could not join') as error:
        require_joined_provider(Provider(), EmissionBoundary())
    assert 'private detail' not in str(error.value)
