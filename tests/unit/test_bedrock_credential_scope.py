"""Rotating session credentials are scrubbed for the operations that can emit them, then released."""
import pytest
import pytest_asyncio

from api_agent.llm_providers.bedrock.factory import BedrockProvider
from api_agent.providers.call_authority import bind_call_authority
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


def scrubbed(boundary, secret):
    projection = boundary.for_policy(ContentPolicy(True))
    return secret not in projection.content_export({'prompt': 'before ' + secret + ' after'})['prompt']


@pytest.fixture
def boundary():
    return EmissionBoundary()


@pytest_asyncio.fixture
async def provider(boundary):
    provider = BedrockProvider({'BEDROCK_API_MODE': 'completions', 'BEDROCK_ENDPOINT': 'mantle'}, {'emissions': boundary})
    yield provider
    await provider.aclose()


@pytest.mark.asyncio
async def test_a_rotation_between_registration_and_the_lease_leaves_the_operation_credential_scrubbed(boundary, provider, monkeypatch):
    begin = EmissionBoundary.begin_evidence_lease
    rotations = []

    def rotate_then_lease(self):
        if not rotations:
            rotations.append(provider._credential_scope().register('key-2', slot='session'))
        return begin(self)

    monkeypatch.setattr(EmissionBoundary, 'begin_evidence_lease', rotate_then_lease)

    values, lease = await provider._join_credentials(['key-1'])

    assert 'key-1' in values and 'key-2' in values
    assert scrubbed(boundary, 'key-1')
    lease.release()
    assert not scrubbed(boundary, 'key-1')
    assert scrubbed(boundary, 'key-2')


@pytest.mark.asyncio
async def test_the_registry_stays_bounded_across_many_rotations(boundary, provider):
    for index in range(300):
        _, lease = await provider._join_credentials([f'key-{index}', f'token-{index}'])
        lease.release()

    assert boundary._secrets == {'key-299', 'token-299'}


@pytest.mark.asyncio
async def test_an_operation_without_new_credentials_keeps_the_current_ones(boundary, provider):
    _, lease = await provider._join_credentials(['key-1'])
    lease.release()

    _, catalog = await provider._join_credentials(())
    catalog.release()

    assert scrubbed(boundary, 'key-1')


@pytest.mark.asyncio
async def test_an_admitted_call_registers_rotating_credentials_through_the_provider_scope(boundary, provider):
    with bind_call_authority(boundary, ()):
        _, first = await provider._join_credentials(['key-1'])
        first.release()
        _, second = await provider._join_credentials(['key-2'])
        second.release()

        assert not scrubbed(boundary, 'key-1')
        assert scrubbed(boundary, 'key-2')


@pytest.mark.asyncio
async def test_a_failed_handoff_releases_the_hold(boundary, provider, monkeypatch):
    _, current = await provider._join_credentials(['key-1'])
    current.release()
    monkeypatch.setattr(EmissionBoundary, 'begin_evidence_lease', lambda self: (_ for _ in ()).throw(RuntimeError('synthetic')))

    with pytest.raises(RuntimeError, match='synthetic'):
        await provider._join_credentials(['key-2'])
    monkeypatch.undo()
    _, next_operation = await provider._join_credentials(['key-3'])
    next_operation.release()

    assert not scrubbed(boundary, 'key-2')


@pytest.mark.asyncio
async def test_a_failed_snapshot_releases_the_lease_and_the_hold(boundary, provider, monkeypatch):
    monkeypatch.setattr(EmissionBoundary, 'snapshot_content', lambda self: (_ for _ in ()).throw(RuntimeError('synthetic')))

    with pytest.raises(RuntimeError, match='synthetic'):
        await provider._join_credentials(['key-1'])
    monkeypatch.undo()
    _, next_operation = await provider._join_credentials(['key-2'])
    next_operation.release()

    assert not scrubbed(boundary, 'key-1')


@pytest.mark.asyncio
async def test_closing_the_provider_ends_its_credentials(boundary):
    provider = BedrockProvider({'BEDROCK_API_MODE': 'completions', 'BEDROCK_ENDPOINT': 'mantle'}, {'emissions': boundary})
    _, lease = await provider._join_credentials(['key-1'])
    lease.release()

    await provider.aclose()

    assert not scrubbed(boundary, 'key-1')
