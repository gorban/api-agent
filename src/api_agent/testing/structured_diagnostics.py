"""Typed local stdout records projected before rendering, never trusted text."""
import json

from api_agent.providers.scrubbed_response import decode_json, scrub_response, ResponseValidationError

_PROFILE = 'integration_test_profile'
_LABEL = 'Integration test profile'
_MAX_RECORDS = 16
_MAX_CHARACTERS = 65536


def profile_records(properties):
    """Extract only the owned pytest property, with bounded explicit loss."""
    if type(properties) not in (list, tuple) or len(properties) > 1024:
        return [None]
    records = []
    for entry in properties:
        if (type(entry) not in (list, tuple) or len(entry) != 2
                or type(entry[0]) is not str or entry[0] != _PROFILE):
            continue
        value = entry[1]
        record = value if type(value) is str and len(value) <= _MAX_CHARACTERS else None
        if record not in records:
            if len(records) == _MAX_RECORDS:
                return list(dict.fromkeys([*records, None]))
            records.append(record)
    return records


def report_records(reports):
    properties = []
    for report in reports:
        records = report.get('profile_evidence', [])
        if type(records) is not list or len(records) > _MAX_RECORDS + 1:
            return [None]
        properties.extend((_PROFILE, record) for record in records)
    return profile_records(properties)


def _profile_schema():
    from .test_profiles import SHARED_KEYS
    return {'type': 'object', 'maxProperties': 128,
        'propertyNames': {'anyOf': [
            {'enum': sorted(SHARED_KEYS)},
            {'pattern': r'^[A-Z][A-Z0-9_]*(?:API_MODE|INFERENCE_TIMEOUT_SECONDS|MAX_OUTPUT_TOKENS|REASONING_EFFORT)$'}]},
        'additionalProperties': {'type': 'object', 'required': ['value', 'source'],
            'additionalProperties': False, 'properties': {
                'source': {'type': 'string', 'minLength': 1, 'maxLength': 1024},
                'value': {'type': ['string', 'number', 'boolean', 'null'], 'maxLength': 4096}}}}


def render_record(label, value, *, schema, authority, operation):
    """Project a typed record and validate its serialized form with one owner."""
    # Keep registration synchronized through the complete render. The caller
    # reprojects raw records at finalization rather than trusting an earlier copy.
    with authority._authority.lock:
        _, snapshot = authority.snapshot_content()
        secrets = tuple(snapshot._secrets)
        clean, _ = scrub_response(value, schema=schema, secrets=secrets, operation=operation)
        rendered = label + ': ' + json.dumps(clean, sort_keys=True, ensure_ascii=False, allow_nan=False)
        rendered, _ = scrub_response(rendered, schema={'type': 'string', 'minLength': 1},
                                      secrets=secrets, operation=operation)
        return rendered


def append_profile_records(stdout, records, scrubber):
    """Append typed profile output; ordinary stdout was scrubbed as text first."""
    if not records:
        return stdout
    unavailable = _LABEL + ': [withheld: structured diagnostic unavailable]\n'
    with scrubber.boundary._authority.lock:
        lines = []
        for record in records:
            if type(record) is not str:
                lines.append(scrubber(unavailable))
                continue
            try:
                value = decode_json(record)
                line = render_record(_LABEL, value, schema=_profile_schema(),
                    authority=scrubber.boundary, operation='test_profile_diagnostic')
            except ResponseValidationError:
                lines.append(scrubber(unavailable))
            else:
                lines.append(line + '\n')
        combined = stdout + ('\n' if stdout and not stdout.endswith('\n') else '') + ''.join(lines)
        _, snapshot = scrubber.boundary.snapshot_content()
        try:
            combined, _ = scrub_response(combined, schema={'type': 'string', 'minLength': 1},
                secrets=tuple(snapshot._secrets), operation='test_stdout_diagnostic')
        except ResponseValidationError:
            return scrubber('stdout withheld: structured diagnostic credential protection.\n')
        return combined
