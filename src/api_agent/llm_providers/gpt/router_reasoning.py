"""Bounded OpenRouter Responses reasoning; preserve native replay fields verbatim.

Contract: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
"""
import json
from itertools import chain
from api_agent.providers.contracts import ContractError

MAX_CARRIERS = 4096


def reasoning_text(item):
    if (type(item) is not dict or item.get('type') != 'reasoning'
            or set(item) - {'type', 'id', 'status', 'summary', 'content',
                            'signature', 'format', 'encrypted_content'}):
        raise ContractError('Invalid OpenRouter reasoning fields')
    texts = []
    summaries = []
    all_text = []
    for name, kind, destination in (('summary', 'summary_text', summaries),
                                    ('content', 'reasoning_text', texts)):
        rows = item.get(name, [])
        if rows is None and name == 'content':
            rows = []
        if type(rows) is not list:
            raise ContractError('Invalid OpenRouter reasoning text')
        for row in rows:
            if (type(row) is not dict or set(row) != {'type', 'text'}
                    or row['type'] != kind or type(row['text']) is not str):
                raise ContractError('Invalid OpenRouter reasoning text')
            all_text.append(row['text'])
            if row['text'].strip():
                destination.append(row['text'])
    for name, limit in (('signature', 32768), ('format', 100), ('encrypted_content', 32768)):
        value = item.get(name)
        if value is not None and (type(value) is not str or not value or len(value) > limit
                                  or not value.isprintable()):
            raise ContractError('Invalid OpenRouter reasoning metadata')
    try:
        size = sum(len(value.encode('utf-8')) for value in all_text)
    except UnicodeEncodeError:
        raise ContractError('Invalid OpenRouter reasoning text') from None
    if size > 65536:
        raise ContractError('OpenRouter reasoning exceeds byte limit')
    return '\n'.join(summaries or texts), tuple(all_text)


def carriers(value, continuation=None, *, arguments=(), maximum=MAX_CARRIERS):
    items = value.get('output') if type(value) is dict else None
    if type(items) is not list:
        if continuation is None:
            return ()
        envelope = json.loads(continuation)
        if envelope['provider'] != 'openrouter.responses':
            return ()
        items = envelope['state']
    def strings(value, include_keys=False):
        if type(value) is str:
            yield value
        elif type(value) is dict:
            for key, child in value.items():
                if include_keys:
                    yield key
                yield from strings(child, include_keys)
        elif type(value) is list:
            for child in value:
                yield from strings(child, include_keys)
    collected = []
    fragments = chain(
        (text for item in items if type(item) is dict and item.get('type') == 'reasoning'
         for text in strings(item)),
        (text for argument in arguments for text in strings(argument, True)))
    for text in fragments:
        if len(collected) >= maximum:
            raise ContractError('Credential protection carrier limit exceeded')
        collected.append(text)
    return tuple(collected)


def evidence_payload(value):
    """Exclude opaque Responses state from evidence without changing replay."""
    return {name: [
        {key: child for key, child in item.items()
         if key not in {'signature', 'format', 'encrypted_content'}}
        if type(item) is dict and item.get('type') == 'reasoning' else item
        for item in content]
        if name in ('input', 'output') and type(content) is list else content
        for name, content in value.items()}


def guard_semantic_carriers(fragments, secrets):
    """Check semantic JSON credential pairs, including tool keys and values.

    Unresolved JSON snippets are withheld when JSON credentials are registered:
    arbitrary fragments cannot establish object member order or numeric spelling.
    Complete carriers retain the common semantic guard. Opening-token filtering
    avoids pairing thousands of ordinary tool fields with one another. Bound
    normalized input and repeated pair text together.
    """
    from api_agent.telemetry.redaction import _json_registrations, _credential_views
    from api_agent.providers.credential_guard import guard_json
    registrations = _json_registrations(secrets)
    if not registrations:
        return
    from api_agent.telemetry.redaction import _json_carriers
    numeric = any(type(json.loads(registration)) in (int, float) for registration in registrations)
    prefixes = {registration[0].encode('utf-8') for registration in registrations}
    anchors = set()
    work = 0
    for index, fragment in enumerate(fragments):
        for view, _, _ in _credential_views(fragment.encode('utf-8')):
            work += len(view)
            if work > 8 * 1024 * 1024:
                raise ContractError('Credential protection limit exceeded')
            text = view.decode('utf-8', errors='surrogateescape')
            # A partial JSON document can be completed by arbitrarily many
            # other fields, with different whitespace or object-key order.
            # Withhold unresolved carriers rather than enumerate permutations.
            if (numeric and any(character in '0123456789' for character in text)
                    or any(incomplete for _, _, incomplete in _json_carriers(text))):
                raise ContractError('Credential protection cannot resolve fragmented JSON')
            if any(prefix in view for prefix in prefixes):
                anchors.add(index)
    for left in sorted(anchors):
        for right in range(len(fragments)):
            if left == right or (right in anchors and right < left):
                continue
            first, second = fragments[left], fragments[right]
            work += 2 * (len(first) + len(second))
            if work > 8 * 1024 * 1024:
                raise ContractError('Credential protection limit exceeded')
            guard_json(first + second, secrets)
            guard_json(second + first, secrets)
