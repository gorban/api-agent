"""Explicit test-owned observations of borrowed ordinary application runtimes.

Manual runtimes remain inert. Receipt callbacks emit content-free worker events;
the separate source scope opts into permitted, scrubbed wire publication.
Existing central publishers retain their own accounting and journal ownership.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace


def publish_terminal_receipt(receipt):
    """Publish a locally originated terminal receipt without recording it again."""
    from api_agent.workflows.run_context import test_evidence_allowed
    from .inference_usage import capture_delegated_owner, validate_event
    if not test_evidence_allowed():
        return False
    owner = capture_delegated_owner()
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    if owner is None or owner.node is None or not path:
        return False
    fields = ('call_id', 'selection', 'usage', 'actual_model', 'reasoning_effort',
              'transport_attempts', 'quote', 'response', 'service_tier', 'serving_revision',
              'started_at', 'finished_at')
    data = {key: receipt[key] for key in fields if key in receipt}
    data.update(outcome='failed' if receipt['outcome'] == 'cancelled' else receipt['outcome'],
                turn_id=owner.turn_id, turn_kind=owner.turn_kind,
                invocation_id=owner.invocation_id,
                attempt=int(os.environ.get('API_AGENT_TEST_ATTEMPT', '1')))
    if owner.conversation_id is not None:
        data['conversation_id'] = owner.conversation_id
    validate_event(SimpleNamespace(text=json.dumps(data, allow_nan=False)))
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(dict(kind='inference_usage', node=owner.node, data=data),
                                allow_nan=False) + '\n')
    return True


def publish_root_invocation(journal, event):
    """Count an admitted graph entry, then retain its observed final inventory."""
    from .inference_usage import publish_external_invocation
    if event.get('event') != 'scope_start' or event.get('kind') != 'graph' or event.get('parent_id') is not None:
        return False
    return publish_external_invocation(journal.invocation_id, turn_id=journal.turn_id)


def publish_final_inventory(packet):
    """A failed partial graph retains known calls without claiming a full inventory."""
    from api_agent.workflows.journal import validate_journal
    from .inference_usage import publish_external_invocation
    packet = validate_journal(packet)
    calls = []
    complete = packet['completeness'] == 'complete'
    for row in packet['events']:
        if row.get('event') != 'call':
            continue
        # A redacted selection still identifies a model call. A central UUID by
        # itself can also name a deterministic tool, so it proves no model type.
        model_call = (row.get('selection') is not None
            or row.get('reason') == 'selection_unavailable'
            or any(key in (row.get('usage') or {}) for key in
                   ('input_tokens', 'output_tokens', 'cached_input_tokens',
                    'cache_write_tokens', 'reasoning_tokens', 'total_tokens'))
            or any(row.get(key) is not None for key in
                   ('transport_attempts', 'response', 'actual_model')))
        if row.get('accounting') == 'unverified_tool':
            # This generated ID cannot join a provider receipt. Model evidence
            # therefore establishes a gap, rather than an empty full inventory.
            if model_call:
                complete = False
            continue
        if model_call:
            calls.append(row['call_id'])
        else:
            complete = False
    return publish_external_invocation(packet['invocation_id'], turn_id=packet['turn_id'],
        call_ids=calls, complete=complete)


def publish_quota_boundary(boundary, *, allocation_id=None):
    """Forward the selected test's explicit numeric account samples as quota events."""
    from api_agent.workflows.run_context import test_evidence_allowed
    if not test_evidence_allowed():
        return False
    from .inference_usage import _delegated_node
    node = _delegated_node()
    if node is None or not os.environ.get('API_AGENT_INTEGRATION_EVENTS'):
        return False
    from .native_usage import validate_packet
    from .quota_events import publish, validate_snapshot
    from api_agent.telemetry.usage import QuotaSnapshot, QuotaReadError
    boundary = validate_packet({'boundary': boundary, 'owner': {'kind': 'test'}})['boundary']
    if boundary['status'] != 'complete':
        return False
    if allocation_id is None:
        for phase in ('before', 'after'):
            publish(phase, QuotaReadError('identity'), node=node)
        return False
    before, after = boundary['before'], boundary['after']
    if (not before['available'] or not after['available']
            or any(row['pct_used'] is None or row['reset_at'] is None or row['captured_at'] is None
                   for row in (before, after))):
        return False
    if before['captured_at'] > after['captured_at']:
        raise ValueError('Native quota samples are not chronological')
    for phase, row in (('before', before), ('after', after)):
        reset = datetime.fromisoformat(row['reset_at']).astimezone(timezone.utc)
        snapshot = QuotaSnapshot(row['pct_used'], reset.isoformat(), reset,
            datetime.fromtimestamp(row['captured_at'], timezone.utc), allocation_id=allocation_id)
        publish(phase, validate_snapshot(snapshot), node=node)
    return True


@contextmanager
def owned_source_payloads(*, enabled):
    """Opt an already-owned test into source-authoritative, bounded wire events."""
    from api_agent.workflows.inference_capture import observe_source_captures
    from api_agent.workflows.run_context import test_evidence_allowed
    from .inference_usage import capture_delegated_owner
    from .inference_content import publish_content
    def publish(rows, call_id):
        if not test_evidence_allowed():
            return
        owner = capture_delegated_owner()
        if owner is None or owner.node is None:
            return
        fields = ('phase', 'content', 'reason', 'transport_attempt_id', 'capture_source', 'diagnostics')
        clean = [{key: row[key] for key in fields if key in row} for row in rows]
        publish_content(clean, call_id, node=owner.node)
    with observe_source_captures(publish if enabled is True else None):
        yield
