"""Maintenance reuses provider readiness/accounting but treats unavailability as error."""
import pytest
from api_agent.providers.contracts import SetupPrerequisiteError
from api_agent.testing.provider_setup import IntegrationLLM


@pytest.mark.parametrize('method,internal', [('complete', '_complete'), ('describe', '_describe'), ('ensure_ready', '_ensure')])
@pytest.mark.parametrize('skip_unavailable', [True, False])
def test_standard_provider_prerequisite_policy_preserves_default_and_explicit_maintenance_error(tmp_path, monkeypatch, method, internal, skip_unavailable):
    provider = IntegrationLLM({}, tmp_path, skip_unavailable=skip_unavailable)
    error = SetupPrerequisiteError('synthetic provider prerequisite unavailable')
    async def unavailable(*args):
        raise error
    monkeypatch.setattr(provider, internal, unavailable)
    expected = pytest.skip.Exception if skip_unavailable else SetupPrerequisiteError
    try:
        with pytest.raises(expected) as caught:
            getattr(provider, method)(object()) if method == 'complete' else getattr(provider, method)()
        assert str(caught.value) == str(error)
        if not skip_unavailable:
            assert caught.value is error
    finally:
        provider.close()


def test_requirements_owner_root_is_optional_and_snapshot_fixture_supplies_it(tmp_path, monkeypatch):
    from api_agent.testing.provider_setup import IntegrationRequirements, integration_config
    assert IntegrationRequirements({}).owner_root is None
    monkeypatch.setattr('api_agent.testing.provider_setup._read_snapshot', lambda: {'config': {}, 'owner_root': str(tmp_path)})
    assert integration_config.__wrapped__().owner_root == str(tmp_path)
