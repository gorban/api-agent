"""Rotating credentials are scrubbed only while their owner can still emit them."""
import asyncio

import pytest

from api_agent.telemetry.privacy import ContentPolicy, CredentialRegistrationBlocked, EmissionBoundary


def scrubbed(boundary, secret):
    """Whether the boundary currently removes ``secret`` from content it emits."""
    projection = boundary.for_policy(ContentPolicy(True))
    return secret not in projection.content_export({'prompt': 'before ' + secret + ' after'})['prompt']


@pytest.fixture
def boundary():
    return EmissionBoundary()


@pytest.fixture
def owner(boundary):
    return boundary.credential_owner()


def test_an_owner_registration_is_scrubbed(boundary, owner):
    owner.register('jwt-1')

    assert scrubbed(boundary, 'jwt-1')


def test_a_slot_rotation_releases_the_superseded_credential(boundary, owner):
    owner.register('jwt-1', slot='jwt')

    owner.register('jwt-2', slot='jwt')

    assert scrubbed(boundary, 'jwt-2')
    assert not scrubbed(boundary, 'jwt-1')


def test_a_slot_rotation_to_the_same_value_keeps_it_scrubbed(boundary, owner):
    owner.register('jwt-1', slot='jwt')

    owner.register('jwt-1', 'side-value', slot='jwt')
    owner.register('jwt-1', slot='jwt')

    assert scrubbed(boundary, 'jwt-1')
    assert not scrubbed(boundary, 'side-value')


def test_a_rotation_replaces_every_value_of_the_credential(boundary, owner):
    owner.register('key-1', 'token-1', slot='aws')

    owner.register('key-2', 'token-2', slot='aws')

    assert [scrubbed(boundary, value) for value in ('key-1', 'token-1', 'key-2', 'token-2')] == [
        False, False, True, True]


def test_slots_rotate_independently(boundary, owner):
    owner.register('jwt-1', slot='jwt')
    owner.register('identity-1', slot='identity')

    owner.register('jwt-2', slot='jwt')

    assert scrubbed(boundary, 'identity-1')


def test_unslotted_registrations_accumulate_until_the_owner_closes(boundary, owner):
    owner.register('first')
    owner.register('second')

    assert scrubbed(boundary, 'first') and scrubbed(boundary, 'second')

    owner.close()

    assert not scrubbed(boundary, 'first') and not scrubbed(boundary, 'second')


def test_a_hold_keeps_a_superseded_credential_until_it_is_released(boundary, owner):
    owner.register('jwt-1', slot='jwt')
    hold = owner.hold()

    owner.register('jwt-2', slot='jwt')

    assert scrubbed(boundary, 'jwt-1')
    hold.release()
    assert not scrubbed(boundary, 'jwt-1')
    assert scrubbed(boundary, 'jwt-2')


def test_a_hold_also_keeps_credentials_registered_while_it_is_open(boundary, owner):
    hold = owner.hold()
    owner.register('jwt-1', slot='jwt')
    owner.register('jwt-2', slot='jwt')
    owner.register('jwt-2', slot='jwt')

    assert scrubbed(boundary, 'jwt-1') and scrubbed(boundary, 'jwt-2')
    hold.release()
    assert not scrubbed(boundary, 'jwt-1') and scrubbed(boundary, 'jwt-2')


def test_overlapping_holds_each_keep_the_credential(boundary, owner):
    owner.register('jwt-1', slot='jwt')
    with owner.hold():
        with owner.hold():
            owner.register('jwt-2', slot='jwt')
        assert scrubbed(boundary, 'jwt-1')
    assert not scrubbed(boundary, 'jwt-1')


def test_a_hold_is_a_context_manager_and_release_is_idempotent(boundary, owner):
    owner.register('jwt-1', slot='jwt')
    with owner.hold() as hold:
        owner.register('jwt-2', slot='jwt')
        assert scrubbed(boundary, 'jwt-1')
    hold.release()

    assert not scrubbed(boundary, 'jwt-1')


def test_a_hold_outlives_the_owner_that_issued_it(boundary, owner):
    owner.register('jwt-1', slot='jwt')
    hold = owner.hold()

    owner.close()

    assert scrubbed(boundary, 'jwt-1')
    hold.release()
    assert not scrubbed(boundary, 'jwt-1')


def test_closing_an_owner_twice_is_harmless(boundary, owner):
    owner.register('jwt-1')

    owner.close()
    owner.close()

    assert not scrubbed(boundary, 'jwt-1')


def test_a_closed_owner_accepts_no_registrations_or_holds(owner):
    owner.close()

    with pytest.raises(ValueError, match='closed'):
        owner.register('late')
    with pytest.raises(ValueError, match='closed'):
        owner.hold()


@pytest.mark.parametrize('secrets', [(), ('',), (7,), ('ok', None)])
def test_an_owner_registers_only_nonempty_strings(owner, secrets):
    with pytest.raises(ValueError, match='nonempty strings'):
        owner.register(*secrets)
    with pytest.raises(ValueError, match='nonempty strings'):
        asyncio.run(owner.aregister(*secrets))


def test_an_unscoped_registration_outlives_an_owner_that_also_registered_it(boundary, owner):
    owner.register('shared', slot='jwt')
    boundary.register_secret('shared')

    owner.close()

    assert scrubbed(boundary, 'shared')


def test_an_unscoped_registration_made_first_is_never_released_by_an_owner(boundary, owner):
    boundary.register_secret('shared')
    owner.register('shared', slot='jwt')

    owner.register('other', slot='jwt')

    assert scrubbed(boundary, 'shared')


def test_a_value_registered_by_two_owners_is_scrubbed_until_both_release_it(boundary):
    first, second = boundary.credential_owner(), boundary.credential_owner()
    first.register('shared')
    second.register('shared')

    first.close()
    assert scrubbed(boundary, 'shared')
    second.close()

    assert not scrubbed(boundary, 'shared')


def test_a_policy_projection_shares_the_owner_registrations(boundary):
    projected = boundary.for_policy(ContentPolicy(True))
    owner = projected.credential_owner()
    owner.register('jwt-1', slot='jwt')

    assert scrubbed(boundary, 'jwt-1')
    owner.close()
    assert not scrubbed(boundary, 'jwt-1')


def test_many_rotations_leave_only_the_current_credential_registered(boundary, owner):
    for index in range(500):
        owner.register(f'jwt-{index}', slot='jwt')

    assert boundary._secrets == {'jwt-499'}


def test_a_release_during_publication_waits_for_the_evidence_lease(boundary, owner):
    owner.register('jwt-1', slot='jwt')
    lease = boundary.begin_evidence_lease()

    owner.register('jwt-1', slot='jwt')
    owner.close()
    assert scrubbed(boundary, 'jwt-1')
    lease.release()

    assert not scrubbed(boundary, 'jwt-1')


def test_a_release_deferred_by_a_lease_does_not_drop_a_credential_claimed_again(boundary, owner):
    owner.register('jwt-1', slot='jwt')
    lease = boundary.begin_evidence_lease()
    other = boundary.credential_owner()
    owner.close()
    other.register('jwt-1')
    lease.release()

    assert scrubbed(boundary, 'jwt-1')


def test_a_new_credential_cannot_be_registered_synchronously_during_publication(boundary, owner):
    owner.register('jwt-1', slot='jwt')
    lease = boundary.begin_evidence_lease()

    with pytest.raises(CredentialRegistrationBlocked):
        owner.register('jwt-2', slot='jwt')
    owner.register('jwt-1', slot='jwt')
    lease.release()

    assert scrubbed(boundary, 'jwt-1')
    assert not scrubbed(boundary, 'jwt-2')


def test_asynchronous_registration_waits_for_the_publication_lease_then_rotates(boundary, owner):
    async def scenario():
        owner.register('jwt-1', slot='jwt')
        lease = boundary.begin_evidence_lease()
        pending = asyncio.create_task(owner.aregister('jwt-2', slot='jwt'))
        await asyncio.sleep(0)
        assert not pending.done()
        lease.release()
        await pending

    asyncio.run(scenario())

    assert boundary._secrets == {'jwt-2'}


def test_asynchronous_registration_times_out_while_publication_holds_its_lease(boundary, owner):
    async def scenario():
        boundary.begin_evidence_lease()
        await owner.aregister('jwt-1', timeout=.01)

    with pytest.raises(TimeoutError):
        asyncio.run(scenario())

    assert not scrubbed(boundary, 'jwt-1')
