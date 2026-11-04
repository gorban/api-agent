"""Bounded OpenRouter native reasoning; preserve replay fields verbatim.

Contract: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
"""
import json
import re
from itertools import chain
from api_agent.providers.contracts import ContractError

MAX_CARRIERS = 4096


_NATIVE_METADATA_FIELDS = frozenset(('reasoning', 'reasoning_content', 'reasoning_details',
                                     'signature', 'encrypted_content'))
_NATIVE_TYPES = frozenset(('reasoning', 'reasoning.text', 'reasoning.summary', 'reasoning.encrypted'))
_ESCAPED_ASCII = re.compile(r'\\u00([0-9a-fA-F]{2})')
_NATIVE_MARKER = re.compile(r"reasoning\.(?:encrypted|text|summary)|[\"'](?:reasoning|reasoning_content|reasoning_details|signature|encrypted_content)[\"']\s*:")


def safe_error_text(value):
    """Withhold native metadata diagnostics, including prefixed serialized JSON.

    Only provider diagnostics and unrecognized metadata use this function.
    Actual assistant/user/tool text is admitted by its own complete schema.
    """
    if type(value) is not str or len(value) > 65536:
        return None
    visible_keys = value
    for _ in range(4):
        decoded = _ESCAPED_ASCII.sub(lambda match: chr(int(match[1], 16)), visible_keys)
        if decoded == visible_keys:
            break
        visible_keys = decoded
    if _ESCAPED_ASCII.search(visible_keys):
        return None
    visible_keys = visible_keys.replace('\\', '')
    return None if _NATIVE_MARKER.search(visible_keys) else value


def metadata_projection(value):
    """Bound unknown wrappers without promoting opaque native fields to evidence."""
    remaining = [100000, 1048576]
    def project(item, depth):
        remaining[0] -= 1
        if depth > 32 or remaining[0] < 0:
            raise ValueError
        if type(item) is dict:
            if type(item.get('type')) is str and item['type'] in _NATIVE_TYPES:
                return {}
            return {key: project(child, depth + 1) for key, child in item.items()
                    if key not in _NATIVE_METADATA_FIELDS}
        if type(item) is list:
            return [project(child, depth + 1) for child in item]
        if type(item) is str:
            remaining[1] -= len(item)
            if remaining[1] < 0:
                raise ValueError
            return safe_error_text(item)
        return item
    try:
        return project(value, 0)
    except ValueError:
        return {}


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
    if type(value) is dict and type(value.get('choices')) is list:
        items = [row.get('message') for row in value['choices'] if type(row) is dict]
    if type(items) is not list:
        if continuation is None:
            items = []
        else:
            envelope = json.loads(continuation)
            if envelope['provider'] not in ('openrouter.responses', 'openrouter.completions'):
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
        (text for item in items if type(item) is dict
         for key, child in item.items()
         if item.get('type') == 'reasoning' or key in ('reasoning', 'reasoning_content', 'reasoning_details')
         for text in strings(child)),
        (text for argument in arguments for text in strings(argument, True)))
    for text in fragments:
        if len(collected) >= maximum:
            raise ContractError('Credential protection carrier limit exceeded')
        collected.append(text)
    return tuple(collected)


def projected_carriers(value, *, semantic=False, normalized=False, maximum=MAX_CARRIERS):
    """Bound a second representation: every key/string evidence can release.

    Semantic admission already checks complete parsed output arguments. Omit
    their redundant encoded strings only from the semantic pair pass; literal
    admission retains both representations together under its shared work bound.
    """
    if normalized:
        from api_agent.providers.inference import result_from_wire
        result_from_wire(value)
        projected = dict(value)
        if 'message' in value:
            projected['message'] = {key: child for key, child in value['message'].items()
                                    if key != 'continuation'}
    else:
        projected = evidence_payload(value)
    argument_objects = set()
    if semantic:
        if normalized and 'message' in projected:
            argument_objects.update(id(call) for call in projected['message']['calls'])
        for item in projected.get('output', []):
            if item.get('type') == 'function_call':
                argument_objects.add(id(item))
        for choice in projected.get('choices', []):
            for call in choice.get('message', {}).get('tool_calls', []):
                argument_objects.add(id(call['function']))
    def strings(item):
        if type(item) is str:
            yield item
        elif type(item) is dict:
            for key, child in item.items():
                yield key
                if key != 'arguments' or id(item) not in argument_objects:
                    yield from strings(child)
        elif type(item) is list:
            for child in item:
                yield from strings(child)
    collected = []
    for text in strings(projected):
        if len(collected) >= maximum:
            raise ContractError('Credential protection carrier limit exceeded')
        collected.append(text)
    return tuple(collected)


def evidence_payload(value, *, source='response'):
    """Project native carriers with explicit authority for the validated request.

    Only the transport's outgoing capture site supplies request authority. A
    provider response cannot acquire it by echoing request controls or schemas.
    """
    if source not in ('request', 'response'):
        raise ContractError('Invalid native evidence source')
    native_fields = {'reasoning', 'reasoning_content', 'reasoning_details'}
    def chat_projection(item):
        if type(item) is not dict:
            return {}
        candidate = dict(item, **({} if native_fields.intersection(item) else {'reasoning': None}))
        try:
            chat_state(candidate)
        except ContractError:
            # Native assistant history uses the same complete message grammar
            # in responses, outgoing requests and echoed rejection bodies.
            return {}
        return {key: [
                    {name: child for name, child in detail.items() if name not in {'signature', 'data'}}
                    for detail in child] if key == 'reasoning_details' and type(child) is list else child
                for key, child in item.items()}
    def response_items(items):
        from .rich import _native_state
        try:
            _native_state(items, openrouter=True, outcome='length', require_replay=False)
        except ContractError:
            return []
        return [{key: child for key, child in item.items()
                 if item['type'] != 'reasoning' or key not in {'signature', 'format', 'encrypted_content'}}
                for item in items]
    def request_item(item):
        from api_agent.providers.rich_inference import _identifier, _text
        if type(item) is not dict:
            return {}
        kind = item.get('type')
        if (kind in ('reasoning', 'message', 'function_call')
                and not (kind == 'message' and item.get('role') in ('user', 'system', 'developer'))):
            candidate = dict(item)
            if kind == 'function_call' and 'id' not in candidate:
                candidate['id'] = 'projection'
            validated = response_items([candidate])
            if not validated:
                return {}
            return {key: child for key, child in validated[0].items() if key in item}
        try:
            if kind == 'function_call_output':
                if set(item) != {'type', 'call_id', 'output'}:
                    raise ContractError('Invalid native tool output')
                _identifier(item['call_id'])
                output = item['output']
                if type(output) is list:
                    for part in output:
                        if (type(part) is not dict or set(part) - {'type', 'text', 'prompt_cache_breakpoint'}
                                or part.get('type') != 'input_text'
                                or part.get('prompt_cache_breakpoint', {'mode': 'explicit'}) != {'mode': 'explicit'}):
                            raise ContractError('Invalid native tool output text')
                        _text(part.get('text'))
                else:
                    _text(output)
                return item
            role = item.get('role')
            if (role == 'assistant' and type(item.get('content')) is not list
                    or native_fields.intersection(item) or 'tool_calls' in item):
                return chat_projection(item)
            allowed = {'role', 'content', 'tool_call_id'} if role == 'tool' else {'role', 'content'}
            if kind == 'message':
                allowed.add('type')
            if role not in ('user', 'system', 'developer', 'assistant', 'tool') or set(item) != allowed:
                raise ContractError('Invalid native request message')
            if role == 'tool':
                _identifier(item['tool_call_id'])
            content = item['content']
            if type(content) is list:
                for part in content:
                    if (type(part) is not dict or set(part) - {'type', 'text', 'prompt_cache_breakpoint'}
                            or part.get('type') not in ('text', 'input_text')
                            or part.get('prompt_cache_breakpoint', {'mode': 'explicit'}) != {'mode': 'explicit'}):
                        raise ContractError('Invalid native request text')
                    _text(part.get('text'))
            else:
                _text(content)
        except ContractError:
            return {}
        return item
    projected = dict(value) if source == 'request' else metadata_projection(value)
    if 'output' in value:
        projected['output'] = response_items(value['output'])
    for name in ('input', 'messages'):
        if name not in value:
            continue
        rows = value[name]
        if type(rows) is list:
            projected[name] = []
            for item in rows:
                projected[name].append(request_item(item))
        else:
            projected[name] = rows if name == 'input' and type(rows) is str else []
    if 'choices' in value:
        projected['choices'] = []
        if type(value['choices']) is list:
            for row in value['choices']:
                if type(row) is not dict:
                    projected['choices'].append({})
                    continue
                choice = {}
                if type(row.get('index')) is int and 0 <= row['index'] < 64:
                    choice['index'] = row['index']
                if row.get('finish_reason') in ('stop', 'length', 'tool_calls', 'content_filter'):
                    choice['finish_reason'] = row['finish_reason']
                if 'message' in row:
                    choice['message'] = chat_projection(row['message'])
                # Deltas are unsupported on this route; their complete carrier
                # is withheld rather than accepting unvalidated fragments.
                projected['choices'].append(choice)
    return projected


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


def chat_state(message, expected=None):
    """Validate the documented Completions carrier without rewriting its order."""
    from api_agent.providers.rich_inference import object_json, _identifier
    from api_agent.providers.llm import ToolCall
    if (type(message) is not dict or message.get('role') != 'assistant'
            or set(message) - {'role', 'content', 'tool_calls', 'refusal', 'reasoning',
                               'reasoning_content', 'reasoning_details'}
            or message.get('refusal') is not None
            or message.get('content') is not None and type(message['content']) is not str):
        raise ContractError('Invalid OpenRouter assistant state')
    texts, summaries, aliases = [], [], []
    for key in ('reasoning', 'reasoning_content'):
        value = message.get(key)
        if value is not None:
            if type(value) is not str:
                raise ContractError('Invalid OpenRouter reasoning text')
            aliases.append(value)
    details = message.get('reasoning_details')
    if details is not None:
        if type(details) is not list or len(details) > 64:
            raise ContractError('Invalid OpenRouter reasoning details')
        detail_ids = set()
        for position, detail in enumerate(details):
            if type(detail) is not dict:
                raise ContractError('Invalid OpenRouter reasoning detail')
            kind = detail.get('type')
            field = {'reasoning.text': 'text', 'reasoning.summary': 'summary',
                     'reasoning.encrypted': 'data'}.get(kind) if type(kind) is str else None
            if (field is None or set(detail) - {'type', field, 'id', 'format', 'index', 'signature'}
                    or 'signature' in detail and kind != 'reasoning.text'
                    or type(detail.get(field)) is not str):
                raise ContractError('Invalid OpenRouter reasoning detail')
            for key, maximum in (('id', 200), ('format', 100), ('signature', 32768)):
                value = detail.get(key)
                if value is not None and (type(value) is not str or not value
                        or len(value) > maximum or not value.isprintable()):
                    raise ContractError('Invalid OpenRouter reasoning metadata')
            identifier = detail.get('id')
            if identifier is not None:
                if identifier in detail_ids:
                    raise ContractError('Duplicate OpenRouter reasoning detail identity')
                detail_ids.add(identifier)
            index = detail.get('index')
            if 'index' in detail and (type(index) is not int or index != position):
                raise ContractError('Invalid OpenRouter reasoning index')
            if kind == 'reasoning.encrypted':
                if not detail[field] or len(detail[field]) > 32768 or not detail[field].isprintable():
                    raise ContractError('Invalid OpenRouter encrypted reasoning')
            else:
                (summaries if kind == 'reasoning.summary' else texts).append(detail[field])
    try:
        if sum(len(text.encode('utf-8')) for text in [*texts, *summaries, *aliases]) > 65536:
            raise ContractError('OpenRouter reasoning exceeds byte limit')
    except UnicodeEncodeError:
        raise ContractError('Invalid OpenRouter reasoning text') from None
    calls = []
    if 'tool_calls' in message:
        if type(message['tool_calls']) is not list or not 1 <= len(message['tool_calls']) <= 64:
            raise ContractError('Invalid OpenRouter assistant tools')
        for position, call in enumerate(message['tool_calls']):
            if (type(call) is not dict or set(call) not in ({'id', 'type', 'function'}, {'id', 'type', 'function', 'index'})
                    or call['type'] != 'function' or type(call['function']) is not dict
                    or set(call['function']) != {'name', 'arguments'}
                    or 'index' in call and (type(call['index']) is not int or call['index'] != position)):
                raise ContractError('Invalid OpenRouter assistant tools')
            function = call['function']
            object_json(function['arguments'])
            calls.append(ToolCall(_identifier(call['id']), _identifier(function['name'], name=True), function['arguments']))
    content = message.get('content') or ''
    if (not any(key in message for key in ('reasoning', 'reasoning_content', 'reasoning_details'))
            or not (content or calls) or len({call.call_id for call in calls}) != len(calls)
            or expected is not None and (tuple(calls) != expected.calls
                or content != ''.join(part.text for part in expected.content))):
        raise ContractError('OpenRouter continuation does not match assistant output')
    shown = summaries or texts or aliases[:1]
    return '\n'.join(text for text in shown if text.strip()) or None
