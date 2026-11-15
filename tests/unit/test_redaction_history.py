"""Retained run diagnostics scrub retired credentials without extending their ownership."""
import pytest

from api_agent.telemetry.privacy import ContentPolicy, CredentialRegistrationBlocked, EmissionBoundary


def test_history_tracks_multiple_owners_rotations_and_unscoped_credentials():
    source = EmissionBoundary(ContentPolicy(True), secrets=('initial-secret',))
    first = source.credential_owner()
    second = source.credential_owner()
    first.register('before-capture', slot='login')
    history = source.retain_redaction_history()
    first.register('rotated-secret', slot='login')
    second.register('other-secret')
    source.register_secret('permanent-secret')
    first.close()
    second.close()

    payload = {'text': 'initial-secret before-capture rotated-secret other-secret permanent-secret'}
    live = source.export(payload)['text']
    retained = history.snapshot().export(payload)['text']
    assert 'before-capture' in live and 'rotated-secret' in live and 'other-secret' in live
    assert 'initial-secret' not in live and 'permanent-secret' not in live
    assert all(secret not in retained for secret in (
        'initial-secret', 'before-capture', 'rotated-secret', 'other-secret', 'permanent-secret'))
    history.close()


def test_history_does_not_block_source_registration_or_share_ownership():
    source = EmissionBoundary(ContentPolicy(True))
    history = source.retain_redaction_history()
    old_snapshot = history.snapshot()
    lease = old_snapshot.begin_evidence_lease()
    owner = source.credential_owner()
    try:
        owner.register('new-secret', slot='login')
        owner.retire('login')
        assert 'new-secret' not in source._secrets
        assert 'new-secret' not in old_snapshot._secrets
        assert 'new-secret' in history.snapshot()._secrets
    finally:
        lease.release()
        history.close()


def test_closed_history_clears_retained_values_and_stops_future_capture():
    source = EmissionBoundary(ContentPolicy(True), secrets=('old-secret',))
    history = source.retain_redaction_history()
    snapshot = history.snapshot()
    history.close()
    history.close()
    source.register_secret('later-secret')
    with pytest.raises(ValueError, match='closed'):
        history.snapshot()
    assert snapshot.export({'text': 'old-secret'})['text'] == '[REDACTED]'
    assert source.export({'text': 'old-secret later-secret'})['text'] == '[REDACTED] [REDACTED]'


def test_independent_histories_release_without_affecting_source_or_each_other():
    source = EmissionBoundary(ContentPolicy(True))
    first = source.retain_redaction_history()
    second = source.retain_redaction_history()
    owner = source.credential_owner()
    owner.register('shared-secret')
    first.close()
    owner.close()
    with pytest.raises(ValueError, match='closed'):
        first.snapshot()
    assert 'shared-secret' not in source._secrets
    assert second.snapshot().export({'text': 'shared-secret'})['text'] == '[REDACTED]'
    second.close()


def test_blocked_source_registration_is_not_recorded_until_admitted():
    source = EmissionBoundary(ContentPolicy(True))
    history = source.retain_redaction_history()
    owner = source.credential_owner()
    lease = source.begin_evidence_lease()
    try:
        with pytest.raises(CredentialRegistrationBlocked):
            owner.register('pending-secret', slot='login')
        assert 'pending-secret' not in history.snapshot()._secrets
    finally:
        lease.release()
    owner.register('pending-secret', slot='login')
    assert 'pending-secret' in history.snapshot()._secrets
    owner.close()
    history.close()
