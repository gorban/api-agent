"""Literal local presentation of destination-admitted workflow observations.

This renderer is downstream of privacy admission. It cannot grant capture or
recover content omitted by the source. Full output changes presentation only.
"""
from collections.abc import Mapping
import json

_PREVIEW_CHARACTERS = 512
_KEYS = ('event', 'category', 'outcome', 'owner', 'kind', 'node', 'component',
         'sequence', 'event_id', 'invocation_id', 'turn_id', 'scope_id', 'parent_id',
         'visit_id', 'attempt_id', 'call_id', 'operation_id', 'direction', 'reason',
         'elapsed', 'attempt', 'max_passes', 'declared_timeout', 'effective_timeout',
         'timeout_key', 'source', 'target', 'condition', 'implicit', 'usage',
         'selection', 'accounting', 'started_ms', 'stopped_ms', 'accepted', 'cause_id')


def _plain(value):
    """Thaw the plain immutable admitted schema, without object introspection."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if type(value) in (tuple, list):
        return [_plain(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError('Unsupported admitted diagnostic value')


class LocalDiagnosticProjection:
    """Project already scrubbed values; terminal controls remain literal JSON."""
    def __init__(self, *, full_content=False):
        self.full_content = full_content is True

    def format(self, row):
        if not isinstance(row, Mapping):
            raise ValueError('Diagnostic observation must be an admitted mapping')
        result = {key: _plain(row[key]) for key in _KEYS if key in row}
        if row.get('event') == 'capture':
            if 'values' not in row:
                result['content'] = row.get('reason', 'withheld')
            else:
                # Source admission currently supports explicitly classified scalars.
                # Unknown nested values are omitted instead of walking tool state.
                values = row['values']
                if not isinstance(values, Mapping):
                    result['content'] = 'unsupported_schema'
                else:
                    permitted, omitted, truncated = {}, [], []
                    for key, value in values.items():
                        if type(value) not in (str, bool, int, float, type(None)):
                            omitted.append(key)
                        elif type(value) is str and not self.full_content and len(value) > _PREVIEW_CHARACTERS:
                            permitted[key] = value[:_PREVIEW_CHARACTERS]
                            truncated.append(key)
                        else:
                            permitted[key] = value
                    result['values'] = permitted
                    result['presentation'] = 'full_admitted' if self.full_content else 'preview_512_characters'
                    result['source_reason'] = row.get('reason', 'captured')
                    source_truncated = list(row.get('truncated_fields', ()))
                    if source_truncated or truncated:
                        result['truncated_fields'] = sorted(set(source_truncated + truncated))
                    if omitted:
                        result['omitted_fields'] = sorted(omitted)
                        result['content'] = 'unsupported_schema'
        return json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\n'
