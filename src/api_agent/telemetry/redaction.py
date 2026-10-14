"""Exact overlapping text matches with linear scans and merged ranges."""


def overlapping_ranges(text: str, secret: str):
    """Find the exact union of overlapping occurrences in linear scan time.

    The prefix table lets a mismatch or completed match reuse its known suffix
    without rescanning text. Overlapping and adjacent matches extend one range;
    only disjoint ranges are yielded, rather than one record per occurrence.
    """
    if not secret:
        raise ValueError('Matching value must be nonempty.')
    prefix = [0] * len(secret)
    matched = 0
    for index in range(1, len(secret)):
        while matched and secret[index] != secret[matched]:
            matched = prefix[matched - 1]
        if secret[index] == secret[matched]:
            matched += 1
        prefix[index] = matched
    matched = 0
    run_start, run_stop = None, 0
    index = 0
    while index < len(text):
        if not matched:
            index = text.find(secret[:1], index)
            if index < 0:
                break
        character = text[index]
        while matched and character != secret[matched]:
            matched = prefix[matched - 1]
        if character == secret[matched]:
            matched += 1
        if matched == len(secret):
            start, stop = index + 1 - len(secret), index + 1
            if run_start is None:
                run_start, run_stop = start, stop
            elif start <= run_stop:
                run_stop = stop
            else:
                yield run_start, run_stop
                run_start, run_stop = start, stop
            matched = prefix[matched - 1]
        index += 1
    if run_start is not None:
        yield run_start, run_stop


from array import array
from bisect import bisect_right
from heapq import merge
import json
import re


def printable_value(value):
    """Use the provider's strict printable credential detection contract."""
    return ''.join(character for character in value if character.isprintable())


def registered_values(secrets):
    """Retain exact credentials and their nonempty printable equivalents."""
    values = set(secrets)
    values.update(normalized for secret in tuple(values)
                  if (normalized := printable_value(secret)))
    return values


def registered_scalar(value, secrets):
    """Recognize both Python and canonical JSON spellings of a scalar."""
    values = registered_values(secrets)
    return str(value) in values or json.dumps(value, ensure_ascii=False) in values


def trailing_secret_prefix(captured, secret):
    """Recognize the longest trailing proper prefix in linear bounded space."""
    tail = captured[-(len(secret) - 1):] if len(secret) > 1 else b''
    prefix = [0] * len(secret)
    matched = 0
    for index in range(1, len(secret)):
        while matched and secret[index] != secret[matched]:
            matched = prefix[matched - 1]
        if secret[index] == secret[matched]:
            matched += 1
        prefix[index] = matched
    matched = 0
    for character in tail:
        while matched and character != secret[matched]:
            matched = prefix[matched - 1]
        if character == secret[matched]:
            matched += 1
    return matched


def detection_view(captured, *, strip_controls=True, decode_escapes=True):
    """Decode JSON escapes and retain compact original byte ranges."""
    from array import array
    text = captured.decode('utf-8', errors='surrogateescape')
    normalized, starts, ends = bytearray(), array('I'), array('I')
    trailing_start = len(text)
    while trailing_start and 0xDC80 <= ord(text[trailing_start - 1]) <= 0xDCFF:
        trailing_start -= 1
    index, position = 0, 0
    ascii_run = re.compile(r'[\x20-\x5b\x5d-\x7e]+')
    while index < len(text):
        ordinary = ascii_run.match(text, index)
        if ordinary:
            encoded = ordinary.group().encode('ascii')
            normalized.extend(encoded)
            starts.extend(range(position, position + len(encoded)))
            ends.extend(range(position + 1, position + len(encoded) + 1))
            position += len(encoded)
            index = ordinary.end()
            continue
        character = text[index]
        stop = index + 1
        decoded = character
        if decode_escapes and character == '\\':
            escape = re.compile(r'\\(?:u[dD][89aAbB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|["\\/bfnrt])').match(text, index)
            if escape:
                stop = index + len(escape.group())
                decoded = json.loads('"' + escape.group() + '"')
            elif re.compile(r'\\(?:u[0-9a-fA-F]{0,3})?"*').fullmatch(text, index):
                break
        source = text[index:stop].encode('utf-8', errors='surrogateescape')
        for item in decoded:
            # Undecoded trailing UTF-8 bytes are significant credential-prefix
            # bytes, even though surrogateescape represents them as controls.
            trailing_byte = (index >= trailing_start and 0xDC80 <= ord(item) <= 0xDCFF)
            # A decoded lone JSON surrogate is a nonprinting insertion, not
            # undecoded source UTF-8. Keeping surrogatepass bytes would hide
            # the otherwise recognizable prefix at a capture boundary.
            if decoded != character and 0xD800 <= ord(item) <= 0xDFFF:
                continue
            if item.isprintable() or trailing_byte or not strip_controls:
                encoded = item.encode('utf-8', errors='surrogateescape')
                normalized.extend(encoded)
                starts.extend([position] * len(encoded))
                ends.extend([position + len(source)] * len(encoded))
        position += len(source)
        index = stop
    return normalized, starts, ends


def _compose_offsets(starts, ends, previous_starts, previous_ends):
    if previous_starts is None:
        return starts, ends
    return (array('I', (previous_starts[index] for index in starts)),
            array('I', (previous_ends[index - 1] for index in ends)))


def _credential_views(captured):
    """Yield exact, decoded and printable views with original byte ranges."""
    yield captured, None, None
    view, starts, ends = captured, None, None
    if view.decode('utf-8', errors='surrogateescape').isprintable() and b'\\' not in view:
        return
    for _ in range(20):
        decoded, local_starts, local_ends = detection_view(view, strip_controls=False)
        decoded_starts, decoded_ends = _compose_offsets(local_starts, local_ends, starts, ends)
        if decoded != view:
            yield decoded, decoded_starts, decoded_ends
        printable, local_starts, local_ends = detection_view(decoded, decode_escapes=False)
        if printable != decoded:
            printable_starts, printable_ends = _compose_offsets(
                local_starts, local_ends, decoded_starts, decoded_ends)
            yield printable, printable_starts, printable_ends
        if decoded == view or b'\\' not in decoded:
            return
        view, starts, ends = decoded, decoded_starts, decoded_ends
    raise ValueError('Credential representation nesting exceeds the safe bound.')


class AmbiguousJSON(ValueError):
    """A representation cannot safely retain all credential-bearing members."""


class JSONDepthError(AmbiguousJSON):
    """A native candidate needs the boundary's recursive omission policy."""


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AmbiguousJSON('Duplicate JSON member.')
        result[key] = value
    return result


def normalized_json(value, depth=0):
    if depth > 20:
        raise JSONDepthError('JSON nesting exceeds the safe bound.')
    if type(value) is str:
        return printable_value(value)
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            normalized = printable_value(key)
            if normalized in result:
                raise AmbiguousJSON('Normalized JSON members collide.')
            result[normalized] = normalized_json(item, depth + 1)
        return result
    if type(value) is list:
        return [normalized_json(item, depth + 1) for item in value]
    return value


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_registrations(secrets):
    result = set()
    for secret in registered_values(secrets):
        try:
            parsed = json.loads(secret, object_pairs_hook=_json_object)
        except AmbiguousJSON:
            raise
        except (ValueError, RecursionError):
            continue
        result.add(_canonical_json(normalized_json(parsed)))
    return result


def _contains_json_registration(value, registrations, depth=0):
    if depth > 20:
        raise AmbiguousJSON('Serialized JSON nesting exceeds the safe bound.')
    value = normalized_json(value)
    if _canonical_json(value) in registrations:
        return True
    if type(value) is dict:
        return any(_contains_json_registration(item, registrations, depth + 1)
                   for pair in value.items() for item in pair)
    if type(value) is list:
        return any(_contains_json_registration(item, registrations, depth + 1) for item in value)
    if type(value) is str:
        try:
            parsed = json.loads(value, object_pairs_hook=_json_object)
        except AmbiguousJSON:
            raise
        except (ValueError, RecursionError):
            return False
        return _contains_json_registration(parsed, registrations, depth + 1)
    return False


def registered_json_value(value, secrets, *, registrations=None):
    """Compare one original native node with registered normalized JSON."""
    if registrations is None:
        registrations = _json_registrations(secrets)
    if not registrations:
        return False
    try:
        return _canonical_json(normalized_json(value)) in registrations
    except (TypeError, JSONDepthError):
        # Unknown objects/non-string keys still follow the boundary's existing
        # omission policy; JSON comparison must never stringify them.
        return False


def serialized_credential(encoded, secrets):
    registrations = _json_registrations(secrets)
    if not registrations:
        return False
    value = json.loads(encoded, object_pairs_hook=_json_object)
    return _contains_json_registration(value, registrations)


def _json_carriers(text):
    """Scan outer carriers once; nested depth is bounded before parsing."""
    start, depth, quoted, escaped = None, 0, False, False
    for index, character in enumerate(text):
        if start is None:
            if character in '{[':
                start, depth = index, 1
            elif character == '"':
                start, quoted = index, True
            continue
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
                if not depth:
                    yield start, index + 1, False
                    start = None
        elif character == '"':
            quoted = True
        elif character in '{[':
            depth += 1
            if depth > 20:
                raise AmbiguousJSON('JSON nesting exceeds the safe bound.')
        elif character in '}]':
            depth -= 1
            if not depth:
                yield start, index + 1, False
                start = None
    if start is not None:
        yield start, len(text), True


def _completed_json(text):
    closers, quoted, escaped = [], False, False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in '{[':
            closers.append('}' if character == '{' else ']')
        elif character in '}]':
            if not closers or closers.pop() != character:
                return None
    if escaped:
        text = text[:-1]
    suffix = ('"' if quoted else '') + ''.join(reversed(closers))
    try:
        return json.loads(text + suffix, object_pairs_hook=_json_object)
    except AmbiguousJSON:
        raise
    except (ValueError, RecursionError):
        return None


def _json_prefix(candidate, wanted):
    if type(candidate) is not type(wanted):
        return False
    if type(candidate) is dict:
        return bool(candidate) and all(key in wanted and _json_prefix(item, wanted[key])
                                       for key, item in candidate.items())
    if type(candidate) is list:
        return bool(candidate) and len(candidate) <= len(wanted) and all(
            _json_prefix(item, expected) for item, expected in zip(candidate, wanted))
    if type(candidate) is str:
        return bool(candidate) and wanted.startswith(candidate)
    return _canonical_json(wanted).startswith(_canonical_json(candidate))


def _contains_json_prefix(value, registrations, depth=0):
    if depth > 20:
        raise AmbiguousJSON('Serialized JSON nesting exceeds the safe bound.')
    value = normalized_json(value)
    if any(_json_prefix(value, json.loads(wanted)) for wanted in registrations):
        return True
    if type(value) is dict:
        return any(_contains_json_prefix(item, registrations, depth + 1) for item in value.values())
    if type(value) is list:
        return any(_contains_json_prefix(item, registrations, depth + 1) for item in value)
    if type(value) is str:
        parsed = _completed_json(value)
        if parsed is not None:
            return _contains_json_prefix(parsed, registrations, depth + 1)
    return False


def _contains_credential_key(value, classifier, *, concealed_only, depth=0):
    if depth > 20:
        raise AmbiguousJSON('Serialized JSON nesting exceeds the safe bound.')
    if type(value) is dict:
        return any((classifier(key) and (not concealed_only or key != printable_value(key))) or
                   _contains_credential_key(item, classifier, concealed_only=concealed_only, depth=depth + 1)
                   for key, item in value.items())
    if type(value) is list:
        return any(_contains_credential_key(item, classifier, concealed_only=concealed_only, depth=depth + 1)
                   for item in value)
    if type(value) is str:
        try:
            parsed = json.loads(value, object_pairs_hook=_json_object)
        except AmbiguousJSON:
            raise
        except (ValueError, RecursionError):
            return False
        return _contains_credential_key(parsed, classifier, concealed_only=concealed_only, depth=depth + 1)
    return False


def _registration_keys(value):
    if type(value) is dict:
        for key, item in value.items():
            yield key
            yield from _registration_keys(item)
    elif type(value) is list:
        for item in value:
            yield from _registration_keys(item)


def _partial_member_prefix(text, registrations):
    match = re.search(r'(?:^|[{,])\s*"((?:\\.|[^"\\])*)$', text)
    if match is None:
        return False
    try:
        key = printable_value(json.loads('"' + match[1] + '"', strict=False))
    except ValueError:
        return False
    return bool(key) and any(candidate.startswith(key) for registration in registrations
                             for candidate in _registration_keys(json.loads(registration)))


def _semantic_byte_ranges(captured, registrations, incomplete, classifier, mapped):
    if not registrations and classifier is None:
        return
    if not any(cue in captured for cue in (b'{', b'[', b'"')):
        return
    text = captured.decode('utf-8', errors='surrogateescape')
    positions = []
    partial = re.search(r'(?:^|[{,])\s*"((?:\\.|[^"\\])*)$', text)
    if incomplete and registrations and partial and _partial_member_prefix(text, registrations):
        positions.extend((partial.start(), len(text)))
    for left, right, unfinished in _json_carriers(text):
        if unfinished:
            if incomplete:
                parsed = _completed_json(text[left:right])
                relevant = bool(registrations) and _partial_member_prefix(text[left:right], registrations)
                if relevant or (parsed is not None and (
                        (bool(registrations) and _contains_json_prefix(parsed, registrations)) or
                        (classifier is not None and _contains_credential_key(
                            parsed, classifier, concealed_only=not mapped)))):
                    positions.extend((left, right))
            continue
        try:
            parsed = json.loads(text[left:right], object_pairs_hook=_json_object)
            sensitive = ((bool(registrations) and _contains_json_registration(parsed, registrations)) or
                         (classifier is not None and _contains_credential_key(
                             parsed, classifier, concealed_only=not mapped)))
        except AmbiguousJSON:
            sensitive = True
        except (ValueError, RecursionError):
            continue
        if sensitive:
            positions.extend((left, right))
    spans = _merge_compact_ranges(iter(sorted(zip(positions[::2], positions[1::2]))))
    byte_positions = iter(_byte_positions(text, spans))
    for left in byte_positions:
        yield left, next(byte_positions)


def _merge_compact_ranges(*ranges):
    merged = array('I')
    for left, right in merge(*ranges):
        if merged and left <= merged[-1]:
            merged[-1] = max(merged[-1], right)
        else:
            merged.extend((left, right))
    return merged


def _byte_positions(text, targets):
    character, position = 0, 0
    ascii_run = re.compile(r'[\x00-\x7f]+')
    for target in targets:
        while character < target:
            ordinary = ascii_run.match(text, character)
            if ordinary:
                length = min(ordinary.end(), target) - character
                character += length
                position += length
            else:
                position += len(text[character].encode('utf-8', errors='surrogateescape'))
                character += 1
        yield position


def _bearer_byte_ranges(captured):
    text = captured.decode('utf-8', errors='surrogateescape')
    records = []
    for start, stop, scheme_end in concealed_bearer_ranges(text):
        word = ''.join(character for character in text[start:scheme_end]
                       if character.isprintable() and not character.isspace())
        records.append((start, stop, scheme_end, word.encode('utf-8')))
    # Each endpoint stream is ordered, even for overlapping schemes. Merge the
    # three streams instead of assuming each token ends before the next starts.
    targets = tuple(merge((row[0] for row in records), (row[2] for row in records),
                          (row[1] for row in records)))
    positions = dict(zip(targets, _byte_positions(text, targets)))
    for start, stop, scheme_end, word in records:
        yield positions[start], positions[stop], positions[scheme_end], word


def registered_byte_ranges(captured, secrets, *, incomplete=False, credential_key=None):
    """Collect original ranges before any syntax rewrite can fragment them."""
    secrets = registered_values(secrets)
    try:
        values = tuple(value.encode('utf-8', errors='surrogateescape') for value in secrets)
    except UnicodeEncodeError:
        return ((0, len(captured)),)
    spans = array('I')
    try:
        registrations = _json_registrations(secrets)
        aliases = set(values)
        for registration in registrations:
            parsed = json.loads(registration)
            for ensure_ascii in (False, True):
                for separators in (None, (',', ':')):
                    alias = json.dumps(parsed, ensure_ascii=ensure_ascii, sort_keys=True, separators=separators)
                    aliases.add(alias.encode('utf-8'))
        values = tuple(aliases)
        for view, starts, ends in _credential_views(captured):
            def matches(secret):
                for left, right in overlapping_ranges(view, secret):
                    yield ((left, right) if starts is None else
                           (starts[left], ends[right - 1]))
            def semantic_matches():
                for left, right in _semantic_byte_ranges(view, registrations, incomplete, credential_key, starts is not None):
                    yield ((left, right) if starts is None else
                           (starts[left], ends[right - 1]))
            candidates = [matches(secret) for secret in values]
            candidates.append(semantic_matches())
            if incomplete and values:
                prefix_view = view.rstrip(b'"')
                longest = max(trailing_secret_prefix(prefix_view, secret) for secret in values)
                if longest:
                    left = len(prefix_view) - longest
                    candidates.append(iter((((left if starts is None else starts[left]), len(captured)),)))
            previous = ((spans[index], spans[index + 1]) for index in range(0, len(spans), 2))
            spans = _merge_compact_ranges(previous, *candidates)
            schemes = []
            for left, right, scheme_end, word in _bearer_byte_ranges(view):
                source_left = left if starts is None else starts[left]
                source_end = scheme_end if starts is None else ends[scheme_end - 1]
                index = bisect_right(spans, source_left)
                intersects = index % 2 or (index < len(spans) and spans[index] < source_end)
                if starts is not None or intersects or any(secret in word for secret in values):
                    source_right = right if starts is None else ends[right - 1]
                    schemes.append((source_left, source_right))
            previous = ((spans[index], spans[index + 1]) for index in range(0, len(spans), 2))
            spans = _merge_compact_ranges(previous, schemes)
    except ValueError:
        return ((0, len(captured)),)
    return tuple((spans[index], spans[index + 1]) for index in range(0, len(spans), 2))


def registered_text_ranges(text, secrets, *, incomplete=False, credential_key=None):
    """Translate byte recognition to source-character ranges in one scan."""
    try:
        captured = text.encode('utf-8', errors='surrogateescape')
    except UnicodeEncodeError:
        return ((0, len(text)),)
    ranges = registered_byte_ranges(captured, secrets, incomplete=incomplete, credential_key=credential_key)
    if text.isascii():
        return ranges
    result = []
    character, position = 0, 0
    ascii_run = re.compile(r'[\x00-\x7f]+')
    for left, right in ranges:
        mapped = []
        for target in (left, right):
            while position < target:
                ordinary = ascii_run.match(text, character)
                if ordinary:
                    length = min(ordinary.end() - character, target - position)
                    character += length
                    position += length
                else:
                    position += len(text[character].encode('utf-8', errors='surrogateescape'))
                    character += 1
            mapped.append(character)
        result.append(tuple(mapped))
    return result


_BEARER_TAIL = re.compile(r'\s+(?!\[redacted\](?=$|[\s,;"\'}]))[^\s,;"\'}]+', re.IGNORECASE)


def concealed_bearer_ranges(value):
    """Recognize scheme-internal whitespace/controls before masking context."""
    for candidate in re.finditer('[bB]', value):
        start = candidate.start()
        if start and (value[start - 1].isalnum() or value[start - 1] == '_'):
            continue
        position = start + 1
        for letter in 'earer':
            while position < len(value) and (not value[position].isprintable()):
                position += 1
            if position == len(value) or value[position].lower() != letter:
                break
            position += 1
        else:
            match = _BEARER_TAIL.match(value, position)
            if match:
                visible = 'Bearer' + ''.join(character for character in match.group()
                                            if character.isprintable() or character.isspace())
                if re.fullmatch(r'(?i)bearer\s+\[redacted\]', visible) is None:
                    yield start, match.end(), position
