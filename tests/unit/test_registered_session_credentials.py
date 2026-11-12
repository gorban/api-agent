"""A session scrubs each call's authentication while the call runs and releases it after it rotates."""
import pytest

from api_agent.runtime.local_llm import _RegisteredSession
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


def scrubbed(boundary, secret):
    projection = boundary.for_policy(ContentPolicy(True))
    return secret not in projection.content_export({'prompt': 'before ' + secret + ' after'})['prompt']


class Authentication:
    """Hands out a new token on every call, like a provider that refreshes."""
    def __init__(self):
        self.issued = 0

    async def __call__(self):
        self.issued += 1
        return f'token-{self.issued}'

    async def aclose(self):
        pass


class Provider:
    def __init__(self):
        self.authentication = Authentication()

    async def asetup(self, **options):
        return self.authentication


@pytest.fixture
def boundary():
    return EmissionBoundary()


@pytest.fixture
def session(boundary):
    return _RegisteredSession(Provider(), emissions=boundary, on_authentication_failure=lambda: None)


@pytest.mark.asyncio
async def test_a_call_that_began_before_a_rotation_still_scrubs_its_token(boundary, session):
    observed = []

    async def overlapping(*, auth):
        async def rotated(*, auth):
            return auth
        assert await session.call(rotated) == 'token-2'
        observed.append((auth, scrubbed(boundary, 'token-1'), scrubbed(boundary, 'token-2')))

    await session.call(overlapping)

    assert observed == [('token-1', True, True)]
    assert not scrubbed(boundary, 'token-1')
    assert scrubbed(boundary, 'token-2')
    await session.aclose()


@pytest.mark.asyncio
async def test_the_registry_stays_bounded_across_many_rotations(boundary, session):
    async def operation(*, auth):
        return auth

    for _ in range(300):
        await session.call(operation)

    assert boundary._secrets == {'token-300'}
    await session.aclose()


@pytest.mark.asyncio
async def test_a_call_without_string_authentication_registers_nothing(boundary):
    class Anonymous:
        async def __call__(self):
            return None

        async def aclose(self):
            pass

    class AnonymousProvider:
        async def asetup(self, **options):
            return Anonymous()

    session = _RegisteredSession(AnonymousProvider(), emissions=boundary, on_authentication_failure=lambda: None)

    async def operation(*, auth):
        return auth

    assert await session.call(operation) is None
    assert boundary._secrets == set()
    await session.aclose()


@pytest.mark.asyncio
async def test_closing_the_session_ends_its_registration(boundary, session):
    async def operation(*, auth):
        return auth

    await session.call(operation)
    await session.aclose()

    assert boundary._secrets == set()
