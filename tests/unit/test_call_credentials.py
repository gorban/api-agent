"""The credential a call authenticates with is scrubbed for that call and released when the calls that used it end."""
import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary, HeldCredentials
from api_agent.workflows.inference_capture import _register_call_auth, collect_inference, prepare_capture_auth
from api_agent.workflows.journal import _JournalBoundary


def scrubbed(boundary, secret):
    projection = boundary.for_policy(ContentPolicy(True))
    return secret not in projection.content_export({'prompt': 'before ' + secret + ' after'})['prompt']


@pytest.fixture
def boundary():
    return EmissionBoundary(ContentPolicy(True))


def test_every_projection_of_an_authority_shares_one_call_owner(boundary):
    assert boundary.call_credentials() is boundary.call_credentials()
    assert boundary.for_policy(ContentPolicy(False)).call_credentials() is boundary.call_credentials()


def test_holds_taken_together_are_released_together(boundary):
    owner = boundary.credential_owner()
    owner.register('token-1', slot='call')
    with HeldCredentials((owner.hold(), owner.hold())):
        owner.register('token-2', slot='call')
        assert scrubbed(boundary, 'token-1')

    assert not scrubbed(boundary, 'token-1')


@pytest.mark.asyncio
async def test_a_call_scrubs_its_credential_until_released(boundary):
    with collect_inference(boundary):
        captured = await prepare_capture_auth('token-1')

        assert scrubbed(boundary, 'token-1')
        captured.release()
        captured.release()

    assert not scrubbed(boundary, 'token-1')


@pytest.mark.asyncio
async def test_a_call_that_began_before_a_rotation_still_scrubs_its_credential(boundary):
    with collect_inference(boundary):
        first = await prepare_capture_auth('token-1')
        second = await prepare_capture_auth('token-2')

        assert scrubbed(boundary, 'token-1') and scrubbed(boundary, 'token-2')
        second.release()
        assert scrubbed(boundary, 'token-1')
        first.release()
        assert not scrubbed(boundary, 'token-1')


@pytest.mark.asyncio
async def test_the_registry_stays_bounded_across_many_calls(boundary):
    with collect_inference(boundary):
        for index in range(300):
            (await prepare_capture_auth(f'token-{index}')).release()

    assert boundary._secrets == set()


@pytest.mark.asyncio
async def test_a_failed_registration_releases_the_hold(boundary, monkeypatch):
    async def fail(*secrets, slot=None, timeout=30):
        raise RuntimeError('synthetic')

    boundary.call_credentials().register('token-0', slot='call')
    monkeypatch.setattr(type(boundary.call_credentials()), 'aregister', fail)
    with collect_inference(boundary):
        with pytest.raises(RuntimeError, match='synthetic'):
            await prepare_capture_auth('token-1')
    monkeypatch.undo()

    boundary.call_credentials().register('token-3', slot='call')
    assert not scrubbed(boundary, 'token-0')


@pytest.mark.asyncio
@pytest.mark.parametrize('auth', [None, '', b'token'])
async def test_a_call_without_a_string_credential_holds_nothing(boundary, auth):
    with collect_inference(boundary):
        (await prepare_capture_auth(auth)).release()

    assert boundary._secrets == set()


@pytest.mark.asyncio
async def test_a_call_without_a_boundary_holds_nothing():
    (await prepare_capture_auth('token-1')).release()


@pytest.mark.asyncio
async def test_a_boundary_without_call_credentials_keeps_the_credential_for_its_life():
    class Legacy:
        policy = ContentPolicy(True)

        def __init__(self):
            self.secrets = []

        def register_secret(self, secret):
            self.secrets.append(secret)

    legacy = Legacy()
    with collect_inference(legacy):
        (await prepare_capture_auth('token-1')).release()
        _register_call_auth(legacy, 'token-2')

    assert legacy.secrets == ['token-1', 'token-2']


@pytest.mark.asyncio
async def test_a_journal_boundary_scrubs_the_credential_on_every_authority_it_keeps_live():
    runtime, evidence = EmissionBoundary(ContentPolicy(True)), EmissionBoundary(ContentPolicy(True))
    journal = _JournalBoundary(ContentPolicy(True), runtime, evidence)
    with collect_inference(journal):
        first = await prepare_capture_auth('token-1')
        second = await prepare_capture_auth('token-2')
        second.release()

        assert all(scrubbed(authority, 'token-1') for authority in (runtime, evidence))
        first.release()

    assert all(not scrubbed(authority, 'token-1') and not scrubbed(authority, 'token-2') for authority in (runtime, evidence))


def test_capture_registers_the_call_credential_without_outliving_the_next_call(boundary):
    _register_call_auth(boundary, 'token-1')
    _register_call_auth(boundary, 'token-2')

    assert not scrubbed(boundary, 'token-1')
    assert scrubbed(boundary, 'token-2')
