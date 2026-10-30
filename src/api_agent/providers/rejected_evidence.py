"""Bounded, credential-aware admission of rejected SDK response evidence."""
import json

from .contracts import ContractError
from .credential_guard import guard_json
from api_agent.telemetry.redaction import registered_values, _json_carriers, _json_object

_MAX_WORK = 8 * 1024 * 1024
_MAX_LEAVES = 128
_MAX_BYTES = 524288
_MAX_DEPTH = 20


def _guard_response_fragments(leaves, secrets):
    """Conservatively recognize credentials assembled from independent leaves.

    Reachable credential offsets admit fragments in any order, even with other
    fields between them. Reusing a fragment can only cause extra withholding.
    Bound work rather than enumerating permutations or releasing a partial body.
    """
    import re
    from api_agent.telemetry.redaction import _credential_views, trailing_secret_prefix
    work = 0
    def charge(amount):
        nonlocal work
        work += amount
        if work > _MAX_WORK:
            raise ContractError('Response evidence exceeds carrier protection limit.')
    if len(leaves) > _MAX_LEAVES:
        raise ContractError('Response evidence exceeds carrier protection limit.')
    fragments = set()
    for leaf in leaves:
        for fragment, _, _ in _credential_views(leaf.encode('utf-8')):
            charge(len(fragment))
            # An escape may be completed by another field before decoding.
            # Independent decoded views cannot prove such evidence safe.
            if re.search(rb'\\(?:u[0-9a-fA-F]{0,3})?$', fragment):
                raise ContractError('Response evidence contains an ambiguous escape.')
            if fragment:
                fragments.add(bytes(fragment))
    for secret in registered_values(secrets):
        needle = secret.encode('utf-8')
        reachable = set()
        for fragment in fragments:
            charge(len(needle) + len(fragment))
            if needle in fragment:
                raise ContractError('Content cannot be safely released.')
            matched = trailing_secret_prefix(fragment, needle)
            for length in range(1, matched + 1):
                charge(length)
                if fragment.endswith(needle[:length]):
                    reachable.add(length)
        for position in range(1, len(needle)):
            if position not in reachable:
                continue
            for fragment in fragments:
                remaining = len(needle) - position
                charge(1 + min(len(fragment), remaining))
                if len(fragment) >= remaining:
                    if fragment.startswith(needle[position:]):
                        raise ContractError('Content cannot be safely released.')
                elif needle.startswith(fragment, position):
                    reachable.add(position + len(fragment))


def guard_rejected_evidence(value, secrets):
    """Return unchanged SDK JSON only when its optional evidence is safe.

    Recheck with the final frozen credential snapshot before publication, not
    only the SDK worker's earlier snapshot. This does not validate a completion.
    """
    if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode()) > _MAX_BYTES:
        raise ContractError('Response evidence exceeds capture limit.')
    safe = guard_json(value, secrets)
    leaves = []
    work = 0
    def visit(item, depth=0):
        nonlocal work
        if depth > _MAX_DEPTH or len(leaves) > _MAX_LEAVES:
            raise ContractError('Response evidence exceeds carrier protection limit.')
        if type(item) is str:
            leaves.append(item)
            work += len(item)
            if work > _MAX_WORK:
                raise ContractError('Response evidence exceeds carrier protection limit.')
            for start, end, incomplete in _json_carriers(item):
                work += end - start
                if incomplete or work > _MAX_WORK:
                    raise ContractError('Response evidence contains an ambiguous JSON carrier.')
                try:
                    decoded = json.loads(item[start:end], object_pairs_hook=_json_object)
                except (ValueError, RecursionError):
                    raise ContractError('Response evidence contains an ambiguous JSON carrier.') from None
                visit(guard_json(decoded, secrets), depth + 1)
        elif type(item) is dict:
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
    visit(safe)
    _guard_response_fragments(leaves, secrets)
    return safe
