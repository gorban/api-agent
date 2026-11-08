"""Fixed-schema account boundaries: reconciliation evidence, never call usage."""
from copy import deepcopy
from datetime import datetime
import json
import math
import os

TOKENS = ('input_tokens', 'output_tokens', 'cached_input_tokens',
          'cache_write_tokens', 'reasoning_tokens', 'total_tokens')


def _number(value, *, maximum=(1 << 53) - 1, integer=False):
    return value is None or (type(value) in ((int,) if integer else (int, float))
        and 0 <= value <= maximum and math.isfinite(value))


def _snapshot(value):
    if (type(value) is not dict or set(value) != {
            'available', 'captured_at', 'pct_used', 'reset_at', 'categories'}
            or type(value['available']) is not bool
            or not _number(value['captured_at'])
            or not _number(value['pct_used'], maximum=100)
            or type(value['categories']) is not dict or set(value['categories']) != set(TOKENS)
            or any(not _number(v, integer=True) for v in value['categories'].values())):
        raise ValueError('Invalid native usage snapshot')
    reset = value['reset_at']
    if reset is not None:
        try:
            if type(reset) is not str or len(reset) > 64 or datetime.fromisoformat(reset).tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ValueError('Invalid native usage reset') from None


def validate_packet(packet):
    if type(packet) is not dict or set(packet) != {'boundary', 'owner'}:
        raise ValueError('Invalid native usage packet')
    owner = packet['owner']
    if owner != {'kind': 'test'}:
        from .browser_attachments import validate_owner
        validate_owner(owner)
    value = packet['boundary']
    if (type(value) is not dict or set(value) != {
            'version', 'scope', 'status', 'graph_invocations', 'before', 'after'}
            or type(value['version']) is not int or value['version'] != 1
            or value['scope'] != 'account_boundary'
            or value['status'] not in ('complete', 'incomplete', 'interrupted')
            or not _number(value['graph_invocations'], integer=True)
            or value['graph_invocations'] is None):
        raise ValueError('Invalid native usage boundary')
    for phase in ('before', 'after'):
        _snapshot(value[phase])
    return deepcopy(packet)


def publish(boundary, *, owner=None):
    """Admit numeric counters only, independently of both content switches."""
    from api_agent.workflows.run_context import test_evidence_allowed
    if not test_evidence_allowed():
        return False
    packet = validate_packet({'boundary': boundary, 'owner': owner or {'kind': 'test'}})
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if not path or not node:
        return False
    with open(path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps({'kind': 'native_usage_boundary',
            'node': node.rsplit(' ', 1)[0], 'data': packet}, allow_nan=False) + '\n')
    return True


def reconciliation(boundary, report, test_id):
    """Present disjoint measurement scopes without substituting or adding them."""
    before, after = boundary['before'], boundary['after']
    has_boundary = (boundary['status'] == 'complete' and before['available'] and after['available']
        and before['captured_at'] is not None and after['captured_at'] is not None
        and before['captured_at'] <= after['captured_at'])
    reset = (before['reset_at'] is not None and after['reset_at'] is not None
             and before['reset_at'] != after['reset_at'])
    pct_before, pct_after = before['pct_used'], after['pct_used']
    if pct_before is not None and pct_after is not None and pct_after < pct_before:
        reset = True
    deltas = {key: (after['categories'][key] - before['categories'][key])
        if has_boundary and not reset and before['categories'][key] is not None
        and after['categories'][key] is not None and after['categories'][key] >= before['categories'][key]
        else None for key in TOKENS}
    rows = [row for row in report.get('usage_observations', []) if row['session_id'] == test_id]
    names = dict(input_tokens='input_tokens', output_tokens='output_tokens',
        cached_input_tokens='cache_read_tokens', cache_write_tokens='cache_creation_tokens',
        reasoning_tokens='reasoning_tokens', total_tokens='total_tokens')
    measured = {target: sum(row[source] for row in rows)
        if rows and all(row.get(source) is not None and source not in row.get('unknown_fields', [])
                        for row in rows) else None for target, source in names.items()}
    count = report.get('measured_calls', {}).get(test_id, 0)
    unknown = report.get('unmeasured_model_calls', {}).get(test_id, 0)
    missing_inventory = report.get('unmeasured_native_invocations', {}).get(test_id, 0)
    known = {target: sum(row[source] for row in rows if row.get(source) is not None)
        if any(row.get(source) is not None for row in rows) else None
        for target, source in names.items()}
    status = ('unavailable' if not count or not has_boundary else 'partial'
              if unknown or missing_inventory or any(value is None for value in (*measured.values(), *deltas.values()))
              or before['reset_at'] is None or after['reset_at'] is None or reset
              else 'complete')
    return {'version': 1, 'status': status,
        'scopes_are_distinct': True,
        'measured_model_calls': {'scope': 'test', 'count': count, 'categories': measured, 'known_subtotals': known},
        'unmeasured_model_calls': unknown,
        'unmeasured_native_invocations': missing_inventory,
        'account_boundary': {'scope': 'account_boundary',
            'graph_invocations': boundary['graph_invocations'],
            'available': bool(has_boundary and not reset),
            'reset_detected': reset,
            'reset_markers_consistent': (before['reset_at'] == after['reset_at'])
                if before['reset_at'] is not None and after['reset_at'] is not None else None,
            'token_deltas': deltas,
            'pct_used_delta': pct_after - pct_before if has_boundary and not reset
                and pct_before is not None and pct_after is not None else None,
            'pct_used_is_coarse': True}}
