"""Fail-closed behavior for independently owned credential authorities."""
import asyncio
import math
import json

import pytest

from api_agent.providers.call_authority import bind_call_authority, current_call_authority
from api_agent.providers.contracts import ContractError
from api_agent.providers.credential_guard import CredentialProtectionError, guard_json
from api_agent.providers.evidence_authority import EvidenceAuthority
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary


def boundary():
    return EmissionBoundary(ContentPolicy(True))


def test_evidence_commit_rejects_stale_snapshot_and_fresh_snapshot_redacts():
    source = boundary()
    source.register_secret('synthetic-first-secret')
    authority = EvidenceAuthority((source,), ['synthetic-caller-secret'])
    generation, _ = authority.snapshot()
    source.register_secret('synthetic-later-secret')
    published = []
    assert not authority.commit_if_current(generation, lambda: published.append('stale'))
    generation, snapshot = authority.snapshot()
    assert snapshot.export({'text': 'synthetic-first-secret synthetic-caller-secret synthetic-later-secret'}) == {
        'text': '[REDACTED] [REDACTED] [REDACTED]'}
    assert authority.commit_if_current(generation, lambda: published.append('fresh'))
    assert published == ['fresh']
    with pytest.raises(ContractError):
        authority.commit_if_current((), lambda: published.append('invalid'))


@pytest.mark.parametrize('boundaries,caller', [([], []), ([boundary()], ['']), ([boundary()], ['x'] * 65)])
def test_evidence_authority_rejects_invalid_or_overlimit_caller(boundaries, caller):
    with pytest.raises(ContractError):
        EvidenceAuthority(boundaries, caller)


def test_call_authority_releases_leases_when_union_exceeds_limit():
    source, coordinator = boundary(), boundary()
    caller = [f'synthetic-secret-{index}' for index in range(65)]
    with bind_call_authority(coordinator, caller):
        with pytest.raises(ContractError):
            current_call_authority().acquire(source)
    # Both authorities can register after failure, proving no lease remained.
    source.register_secret('synthetic-after-source')
    coordinator.register_secret('synthetic-after-coordinator')


def test_call_authority_rejects_invalid_inputs_and_unadmitted_calls():
    with pytest.raises(ContractError), bind_call_authority(object(), []):
        pass
    with pytest.raises(ContractError), bind_call_authority(boundary(), 'invalid'):
        pass
    with bind_call_authority(None, None):
        authority = current_call_authority()
        with pytest.raises(ContractError):
            authority.acquire(boundary())
        with pytest.raises(ContractError):
            asyncio.run(authority.register(boundary(), ['synthetic-secret']))


@pytest.mark.parametrize('value', ['', None])
def test_async_registration_rejects_invalid_secret(value):
    instance = boundary()
    with pytest.raises(ValueError):
        asyncio.run(instance.aregister_secret(value))
    assert not instance._secrets


@pytest.mark.parametrize('timeout', [0, -1, math.inf, math.nan, 3601, '1'])
def test_async_registration_rejects_invalid_timeout(timeout):
    instance = boundary()
    with pytest.raises(ValueError):
        asyncio.run(instance.aregister_secret('synthetic-secret', timeout=timeout))
    assert not instance._secrets


def test_guard_rejects_non_string_keys_and_nested_structured_secret():
    secret = '{"first":"synthetic-one","second":"synthetic-two"}'
    for payload in ({1: 'unsafe'}, {'text': '[{"second":"synthetic-two","first":"synthetic-one"}]'}):
        with pytest.raises(CredentialProtectionError) as error:
            guard_json(payload, {secret})
        assert 'synthetic-one' not in str(error.value)
    assert guard_json({'text': '[{"first":"ordinary"}]'}, {secret}) == {'text': '[{"first":"ordinary"}]'}


def test_gate_skips_waiter_whose_event_loop_closed():
    from collections import deque
    from api_agent.providers import authority_gate

    async def scenario():
        closed_loop = asyncio.new_event_loop()
        abandoned = authority_gate._Waiter(closed_loop)
        closed_loop.close()
        live = authority_gate._Waiter(asyncio.get_running_loop())
        authority = object()
        key = id(authority)
        queue = deque((abandoned, live))
        try:
            with authority_gate._lock:
                authority_gate._states[key] = (authority, queue)
                authority_gate._advance(key, queue)
            await asyncio.wait_for(live.future, 1)
            assert live.granted
            authority_gate._release(authority, live)
            assert key not in authority_gate._states
        finally:
            with authority_gate._lock:
                authority_gate._states.pop(key, None)
    asyncio.run(scenario())


def test_releasing_lease_tolerates_waiter_with_closed_loop():
    instance = boundary()
    lease = instance.begin_evidence_lease()
    closed_loop = asyncio.new_event_loop()
    abandoned = closed_loop.create_future()
    closed_loop.close()
    instance._authority.registration_waiters.add((closed_loop, abandoned))
    lease.release()
    instance.register_secret('synthetic-after-release')
    assert instance.export({'text': 'synthetic-after-release'}) == {'text': '[REDACTED]'}


@pytest.mark.parametrize('payload', [
    {'text': '{"x":1,"x":2}'},
    {'text': 'incomplete { junk {"x":1,"x":2}'},
    {'text': '{"x":1,"\\u0078":2}'},
])
def test_guard_rejects_ambiguous_json_carriers(payload):
    with pytest.raises(CredentialProtectionError):
        guard_json(payload, {'{"token":"synthetic-credential"}'})


def test_guard_rejects_deep_embedded_structured_carrier():
    nested = 'ordinary'
    for _ in range(24):
        nested = {'child': nested}
    with pytest.raises(CredentialProtectionError):
        guard_json({'text': json.dumps(nested)}, {'{"token":"synthetic-credential"}'})


def test_guard_bounds_encoded_size_before_emission():
    # JSON escaping makes the serialized payload exceed the limit even though
    # the source string itself remains below it.
    with pytest.raises(CredentialProtectionError):
        guard_json({'text': '\n' * (4 * 1024 * 1024)}, {'synthetic-credential'})


def test_guard_rejects_invalid_empty_credential():
    with pytest.raises(CredentialProtectionError):
        guard_json({'text': 'ordinary'}, {''})


def test_guard_rejects_keys_that_collide_after_escape_normalization():
    payload = {'a': 'one', r'\u0061': 'two'}
    with pytest.raises(CredentialProtectionError):
        guard_json(payload, {'{"token":"synthetic-credential"}'})
