"""Fail-closed export boundary; application and test-artifact values stay intact.

Adapters must call ``export`` before recording/exporting payloads. Attach
``OwnedLogFilter`` only to handlers owned by this application. This module does
not install logging hooks or instrument providers automatically.
"""
from dataclasses import dataclass
import json
import logging
import math
from pathlib import PurePosixPath
import re
from uuid import UUID

from api_agent.config.resolve import ConfigResult
from api_agent.telemetry.redaction import overlapping_ranges, printable_value, registered_values, concealed_bearer_ranges, serialized_credential, registered_scalar, registered_json_value, _json_registrations, _contains_json_registration, _json_carriers, JSONDepthError


@dataclass(frozen=True)
class ContentPolicy:
    allow_content: bool = False

    @classmethod
    def from_config(cls, result: ConfigResult | None, *, field_name="LOG_CONTENT"):
        if not isinstance(result, ConfigResult) or result.errors or result.missing:
            return cls()
        field = result.fields.get(field_name)
        if field is None:
            return cls()
        source = field.source
        external = source in ('dotenv', 'environment', 'cli')
        if isinstance(source, str) and source.startswith('../'):
            external = PurePosixPath(source).name == '.env'
        return cls(field.value is True and field.explicit is True and external
                   and result.values.get(field_name) is True)


_DROP = object()
# Interoperable JSON integer range; bounds also prevent Python's integer-string
# digit limit and float-conversion overflow from breaking diagnostic emission.
_MAX_JSON_INTEGER = (1 << 53) - 1
_SECRET_KEYS = frozenset({
    'authorization', 'proxy_authorization', 'cookie', 'set_cookie', 'password',
    'passwd', 'pwd', 'totp', 'otp', 'token', 'access_token', 'refresh_token',
    'id_token', 'api_key', 'apikey', 'secret', 'client_secret', 'signing_secret',
    'signing_key', 'private_key', 'credentials', 'credential', 'unattended_auth',
    'extension_secret', 'jwt', 'bearer', 'tokens',
})
_STATUSES = frozenset({'ok', 'success', 'error', 'failed', 'cancelled', 'pending',
                       'running', 'completed', 'retry', 'timeout'})
_EVENTS = frozenset({'start', 'end', 'retry', 'error', 'cancel', 'stream', 'flush'})
_COUNTS = frozenset({'input_tokens', 'output_tokens', 'total_tokens', 'token_count',
                     'retry_count', 'chunk_count', 'attempt', 'http_status',
                     'cached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'cache_creation_tokens', 'reasoning_tokens'})
_DURATIONS = frozenset({'duration_ms', 'duration_seconds'})
_IDS = frozenset({'correlation_id', 'request_id', 'trace_id', 'span_id', 'run_id'})

_BEARER = re.compile(r'(?i)\bbearer\s+(?!\[redacted\](?=$|[\s,;"\'}]))[^\s,;"\'}]+')


# Explicit field/carrier names identify non-user-visible content. Ordinary
# visible strings containing these words are not classified by their text.
_EXCLUDED_FIELDS = frozenset({'hidden_reasoning', 'chain_of_thought',
    'reasoning_content', 'encrypted_reasoning', 'encrypted_content',
    'internal_reasoning', 'internal_analysis', 'hidden_analysis', 'private_reasoning',
    'thinking_content', 'redacted_thinking', 'hidden_thoughts', 'scratchpad',
    'continuation_state', 'continuation_id', 'previous_response_id',
    'conversation_state', 'resume_state', 'checkpoint_state'})
_HIDDEN_TYPES = frozenset({'reasoning', 'analysis', 'thinking', 'redacted_thinking',
    'continuation', 'compaction'})
_EFFORTS = frozenset({'', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'auto', 'off'})
_EFFORT_KEYS = frozenset({'reasoning_effort', 'gen_ai_request_reasoning_effort'})
_GENAI_COUNT_KEYS = frozenset('gen_ai_usage_' + key for key in _COUNTS if 'tokens' in key)
_COUNT_KEYS = _COUNTS | _GENAI_COUNT_KEYS | frozenset({'max_output_tokens'})


def _normalized_key(key):
    return re.sub(r'(?<=[a-z])(?=[A-Z])', '_', printable_value(key)).lower().replace('-', '_').replace('.', '_')


def _excluded_carrier(value):
    # Callers have already admitted an exact dictionary.
    kind, channel = value.get('type'), value.get('channel')
    return ((type(kind) is str and _normalized_key(kind) in _HIDDEN_TYPES)
            or (type(channel) is str and _normalized_key(channel) in ('analysis', 'reasoning')))


def _credential_key(key):
    normalized = _normalized_key(key)
    if normalized in _COUNT_KEYS or normalized in _EFFORT_KEYS:
        return False
    return (normalized in _SECRET_KEYS or normalized in _EXCLUDED_FIELDS
            or any(normalized.endswith('_' + field) for field in _EXCLUDED_FIELDS)
            or any(part in _SECRET_KEYS for part in normalized.split('_')))


def _permitted_metadata_value(key, value):
    normalized = _normalized_key(key)
    if normalized in _GENAI_COUNT_KEYS or normalized == 'max_output_tokens':
        return value is None or (type(value) is int and 0 <= value <= _MAX_JSON_INTEGER)
    if normalized == 'gen_ai_request_reasoning_effort':
        return value is None or (type(value) is str and value in _EFFORTS)
    return True


class _DuplicateJSONKey(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey()
        result[key] = value
    return result


def _parse_json(value):
    return json.loads(value, object_pairs_hook=_unique_object)


class EmissionBoundary:
    """Serialize only permitted JSON values, scrubbing registered secrets always.

    Secret producers must register values before diagnostics are emitted.
    Unknown objects, exceptions, cycles, and excessive nesting are omitted;
    their ``str``/``repr`` methods are never invoked.
    """

    def __init__(self, policy=ContentPolicy(), *, secrets=(), assigned_ids=(), preserve_json_text=False):
        self.policy = policy
        self.preserve_json_text = preserve_json_text
        self._secrets = set()
        self._registered_json = None
        self._assigned_ids = set()
        for secret in secrets:
            self.register_secret(secret)
        for identifier in assigned_ids:
            self.register_id(identifier)

    def for_policy(self, policy):
        """Project a destination policy while sharing live credential registrations."""
        boundary = EmissionBoundary(policy, preserve_json_text=self.preserve_json_text)
        boundary._secrets = self._secrets
        boundary._assigned_ids = self._assigned_ids
        return boundary

    def register_id(self, identifier):
        """Register an identifier generated by the runtime, never by user content."""
        if type(identifier) is not str or str(UUID(identifier)) != identifier:
            raise ValueError('Assigned identifier must be a canonical UUID')
        self._assigned_ids.add(identifier)

    def register_secret(self, secret):
        if type(secret) is not str or not secret:
            raise ValueError('Secret must be a nonempty string')
        self._secrets.add(secret)

    def _text(self, value):
        # Collect every range from the original text. Replacing a registered
        # scheme or an inner Bearer token first can conceal the other guard.
        registered = []
        for secret in registered_values(self._secrets):
            registered.extend((start, end, None)
                              for start, end in overlapping_ranges(value, secret))
        # Detect inserted controls consistently with provider credential checks.
        # Source offsets preserve ordinary controls outside matching ranges.
        if not value.isprintable():
            visible_offsets = [index for index, character in enumerate(value)
                               if character.isprintable()]
            visible = ''.join(value[index] for index in visible_offsets)
            for secret in registered_values(self._secrets):
                registered.extend((visible_offsets[start], visible_offsets[end - 1] + 1, None)
                                  for start, end in overlapping_ranges(visible, secret))
            del visible_offsets, visible
        spans = list(registered)
        spans.extend(concealed_bearer_ranges(value))
        for match in _BEARER.finditer(value):
            visible = ''.join(character for character in match.group()
                              if character.isprintable() or character.isspace())
            # A trailing format/control character does not turn the public
            # redaction sentinel into a new credential. A real suffix does.
            if re.fullmatch(r'(?i)bearer\s+\[redacted\]', visible) is None:
                spans.append((match.start(), match.end(), match.start() + 6))
        # The printable view is detection-only. Original offsets preserve all
        # unrelated controls and whitespace when a concealed scheme matches.
        characters = []
        offsets = []
        for index, character in enumerate(value):
            if character.isprintable() or character.isspace():
                characters.append(character)
                offsets.append(index)
        printable = ''.join(characters)
        spans.extend((offsets[match.start()], offsets[match.end() - 1] + 1,
                      offsets[match.start() + 5] + 1)
                     for match in _BEARER.finditer(printable))
        merged = []
        for start, end, scheme_end in sorted(spans, key=lambda span: (span[0], span[1])):
            if merged and start < merged[-1][1]:
                previous_start, previous_end, previous_scheme = merged[-1]
                if start == previous_start and scheme_end is not None:
                    previous_scheme = scheme_end
                merged[-1] = (previous_start, max(previous_end, end), previous_scheme)
            else:
                merged.append((start, end, scheme_end))
        for start, end, scheme_end in reversed(merged):
            replacement = '[REDACTED]'
            if (scheme_end is not None and
                    not any(secret_start < scheme_end and secret_end > start
                            for secret_start, secret_end, _ in registered) and
                    not any(secret in 'Bearer [REDACTED]' for secret in self._secrets)):
                replacement = 'Bearer [REDACTED]'
            value = value[:start] + replacement + value[end:]
        return value

    def _serialized_credential(self, encoded):
        """Share registered JSON representation checks across emission paths.

        Bearer syntax belongs to decoded text, not its escaped JSON carrier.
        """
        if any(secret in encoded for secret in self._secrets):
            return True
        try:
            return serialized_credential(encoded, self._secrets)
        except (ValueError, RecursionError):
            return True

    def _decoded_text_credential(self, value, depth):
        """Check joined JSON representations without rewriting ordinary parts."""
        if depth > 20:
            return True
        if any(secret in value for secret in self._secrets):
            return True
        try:
            decoded = _parse_json(value)
        except _DuplicateJSONKey:
            return True
        except RecursionError:
            return True
        except ValueError:
            return self._text(value) != value or self._embedded_json_credential(value, depth)
        try:
            encoded = json.dumps(decoded, ensure_ascii=False)
        except RecursionError:
            return True
        return (self._serialized_credential(encoded) or
                self._decoded_value_credential(decoded, depth + 1))

    def _embedded_json_credential(self, value, depth, *, as_key=False):
        """Recognize complete carriers without repairing malformed syntax."""
        try:
            for left, right, unfinished in _json_carriers(value):
                if unfinished:
                    continue
                carrier = value[left:right]
                try:
                    decoded = _parse_json(carrier)
                except (_DuplicateJSONKey, RecursionError):
                    return True
                except ValueError:
                    continue
                if as_key and type(decoded) is str and self._key_credential(decoded, depth + 1):
                    return True
                if self._decoded_text_credential(carrier, depth + 1):
                    return True
        except (ValueError, RecursionError):
            return True
        return False

    def _key_credential(self, key, depth):
        if depth > 20:
            return True
        if _credential_key(key):
            return True
        # Decode only the printable detection view. Keep the original key for
        # ordinary output, but do not let a nonprinting shell hide JSON syntax.
        candidate = printable_value(key)
        if not candidate.lstrip().startswith(('"', '{', '[')):
            return self._embedded_json_credential(candidate, depth, as_key=True)
        try:
            decoded = _parse_json(candidate)
        except (_DuplicateJSONKey, RecursionError):
            return True
        except ValueError:
            return self._embedded_json_credential(candidate, depth, as_key=True)
        return ((type(decoded) is str and self._key_credential(decoded, depth + 1)) or
                self._decoded_text_credential(candidate, depth + 1))

    def _decoded_value_credential(self, value, depth):
        if depth > 20:
            return True
        if type(value) is str:
            return self._decoded_text_credential(value, depth)
        if type(value) is dict:
            if _excluded_carrier(value):
                return True
            return any(self._key_credential(key, depth) or not _permitted_metadata_value(key, item) or self._text(key) != key or
                       self._decoded_value_credential(item, depth + 1)
                       for key, item in value.items())
        if type(value) is list:
            joined = ''.join(item for item in value if type(item) is str)
            if self._decoded_text_credential(joined, depth + 1):
                return True
            if any(self._decoded_value_credential(item, depth + 1) for item in value):
                return True
            # Apply existing part canonicalization for detection only. Numeric
            # text omission can join a credential absent from the raw array.
            parts = [self._json(item, set(), depth + 1) for item in value
                     if type(item) is str]
            normalized = ''.join(item for item in parts if type(item) is str)
            return self._decoded_text_credential(normalized, depth + 1)
        return registered_scalar(value, self._secrets)

    def _json_registration_snapshot(self):
        secrets = frozenset(self._secrets)
        cached = self._registered_json
        if cached is None or cached[0] != secrets:
            cached = (secrets, _json_registrations(secrets))
            self._registered_json = cached
        return cached

    def _registered_native(self, value, seen, depth):
        if not self._secrets:
            return False
        try:
            secrets, registrations = self._json_registration_snapshot()
            if registered_json_value(value, secrets, registrations=registrations):
                return True
            if not registrations:
                return False
            changed = [False]
            admissible = self._admissible_original(value, seen, depth, changed)
            return (changed[0] and admissible is not _DROP and
                    registered_json_value(admissible, secrets, registrations=registrations))
        except (ValueError, RecursionError):
            return True

    def _admissible_original(self, value, seen, depth, changed):
        """Compare admitted original members before credential-key filtering.

        Unsupported native members are omitted exactly as during export. This
        is a detection view, not an exported value or an arbitrary subset match.
        """
        if depth > 20:
            changed[0] = True
            return _DROP
        if type(value) is str:
            # Reuse ordinary export for text admission: JSON decoding can
            # consume depth or discard an inadmissible numeric value. The
            # empty-secret boundary never enters registered comparison again.
            ordinary = EmissionBoundary(ContentPolicy(True), preserve_json_text=self.preserve_json_text)
            if ordinary._json(value, set(), depth) is _DROP:
                changed[0] = True
                return _DROP
            return value
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            if -_MAX_JSON_INTEGER <= value <= _MAX_JSON_INTEGER:
                return value
            changed[0] = True
            return _DROP
        if type(value) is float:
            if math.isfinite(value):
                return value
            changed[0] = True
            return _DROP
        if type(value) not in (dict, list, tuple) or id(value) in seen:
            changed[0] = True
            return _DROP
        seen = seen | {id(value)}
        if type(value) is dict:
            result = {}
            for key, item in value.items():
                if type(key) is not str:
                    changed[0] = True
                    continue
                admitted = self._admissible_original(item, seen, depth + 1, changed)
                if admitted is not _DROP:
                    result[key] = admitted
            return result
        result = []
        for item in value:
            admitted = self._admissible_original(item, seen, depth + 1, changed)
            if admitted is not _DROP:
                result.append(admitted)
        return result

    def _json(self, value, seen, depth):
        if depth > 20:
            return _DROP
        # Compare each original JSON node before key filtering or syntax
        # masking can fragment a registered container or quoted string.
        if self._registered_native(value, seen, depth):
            return '[REDACTED]' if type(value) is str else _DROP
        if type(value) is str:
            # Serialized JSON receives the same credential-key treatment.
            scrubbed = self._text(value)
            if value in self._secrets:
                return '[REDACTED]'
            try:
                parsed = _parse_json(value)
            except _DuplicateJSONKey:
                return '[REDACTED]'
            except RecursionError:
                return _DROP
            except ValueError:
                return '[REDACTED]' if self._embedded_json_credential(value, depth) else scrubbed
            # Preserve semantic serialized credentials before native child
            # omission can turn their enclosing carrier into ordinary JSON.
            try:
                _, registrations = self._json_registration_snapshot()
                if type(parsed) is not str and registrations and _contains_json_registration(parsed, registrations):
                    return '[REDACTED]'
            except JSONDepthError:
                pass
            except (ValueError, RecursionError):
                return '[REDACTED]'
            if type(parsed) is str and self._registered_native(parsed, set(), depth + 1):
                return '[REDACTED]'
            # Numeric JSON text is still text: redact secret substrings before
            # interpreting it as a primitive. Keep original object keys until
            # credential-key filtering, and decode escaped strings recursively.
            if type(parsed) in (int, float) and scrubbed != value:
                return scrubbed
            if type(parsed) in (dict, list, str, int, float):
                clean = self._json(parsed, set(), depth + 1)
                if clean is _DROP:
                    return _DROP
                encoded = json.dumps(clean, ensure_ascii=False)
                # Secret substrings spanning JSON syntax cannot safely be
                # replaced inside the serialized container. Drop that whole
                # text value conservatively, after credential-key filtering.
                if type(parsed) in (dict, list) and any(
                        secret in value and any(char in secret for char in '"{}[],:')
                        for secret in self._secrets):
                    return '[REDACTED]'
                if self._serialized_credential(encoded):
                    return '[REDACTED]'
                return value if self.preserve_json_text and _parse_json(encoded) == parsed else encoded
            return scrubbed
        if type(value) is int and not -_MAX_JSON_INTEGER <= value <= _MAX_JSON_INTEGER:
            return _DROP
        if value is None or type(value) in (bool, int):
            return _DROP if registered_scalar(value, self._secrets) else value
        if type(value) is float:
            return value if math.isfinite(value) and not registered_scalar(value, self._secrets) else _DROP
        if type(value) not in (dict, list, tuple) or id(value) in seen:
            return _DROP
        seen = seen | {id(value)}
        if type(value) is dict:
            if _excluded_carrier(value):
                return _DROP
            result = {}
            for key, item in value.items():
                if (type(key) is not str or self._key_credential(key, depth)
                        or not _permitted_metadata_value(key, item)):
                    continue
                clean = self._json(item, seen, depth + 1)
                if clean is not _DROP:
                    result[self._text(key)] = clean
            return _DROP if self._registered_native(result, seen, depth) else result
        # Ordered text parts can reconstruct credentials absent from each part.
        text_parts = [item for item in value if type(item) is str]
        original_joined = ''.join(text_parts)
        # Match contiguous JSON policy before interpreting incomplete parts.
        # A harmless serialized redaction marker may contain escaped format
        # bytes that resemble a credential suffix until the whole is decoded.
        unchanged_json = False
        original_json = False
        try:
            parsed = _parse_json(original_joined)
        except (_DuplicateJSONKey, RecursionError):
            original_json = True
        except ValueError:
            original_json = self._embedded_json_credential(original_joined, depth)
        else:
            original_json = True
            if type(parsed) in (dict, list, str) and re.search(r'(?i)\[redacted\]', original_joined):
                checker = EmissionBoundary(ContentPolicy(True), secrets=tuple(self._secrets),
                                           preserve_json_text=True)
                unchanged_json = checker._json(original_joined, set(), depth + 1) == original_joined
        joined = original_joined if unchanged_json else ''.join(self._text(item) for item in text_parts)
        # Preserve positive structured verdicts before masking can damage JSON.
        # Ordinary text still retains safe per-part masking where possible.
        if (not unchanged_json and self._decoded_text_credential(original_joined, depth) and
                (original_json or self._text(original_joined) != joined)):
            return ['[REDACTED]']
        if not unchanged_json and any(self._embedded_json_credential(item, depth + 1) for item in text_parts):
            return ['[REDACTED]']
        if self._decoded_text_credential(joined, depth):
            return ['[REDACTED]']
        result = []
        for item in value:
            clean = item if unchanged_json and type(item) is str else self._json(item, seen, depth + 1)
            if clean is not _DROP:
                result.append(clean)
        # Recursive JSON canonicalization can make previously separated text join.
        sanitized_text = ''.join(item for item in result if type(item) is str)
        if any(secret in sanitized_text for secret in self._secrets):
            return ['[REDACTED]']
        if self._decoded_text_credential(sanitized_text, depth):
            return ['[REDACTED]']
        return _DROP if self._registered_native(result, seen, depth) else result

    def _metadata(self, key, value):
        normalized = _normalized_key(key)
        if normalized in _EFFORT_KEYS and type(value) is str and value in _EFFORTS:
            return value
        if normalized in _COUNT_KEYS:
            key = normalized.removeprefix('gen_ai_usage_')
        if key == 'status' and type(value) is str and value in _STATUSES:
            return value
        if key == 'event' and type(value) is str and value in _EVENTS:
            return value
        if key == 'error_kind' and type(value) is str and value in ('timeout', 'invalid_value', 'io_error', 'error'):
            return value
        if key in _COUNTS and type(value) is int and 0 <= value <= _MAX_JSON_INTEGER:
            if key != 'http_status' or 100 <= value <= 599:
                return value
        if key in _DURATIONS and type(value) in (int, float) and 0 <= value <= _MAX_JSON_INTEGER:
            return value
        if key in _IDS and type(value) is str and value in self._assigned_ids:
            return self._text(value)
        return _DROP

    def export(self, payload):
        if type(payload) is not dict or _excluded_carrier(payload):
            return {}
        # Each child starts at zero, so the containing export root is at -1.
        if self._registered_native(payload, set(), -1):
            return {}
        result = {}
        for key, value in payload.items():
            if (type(key) is not str or self._key_credential(key, 0)
                    or not _permitted_metadata_value(key, value)):
                continue
            clean = (self._json(value, {id(payload)}, 0) if self.policy.allow_content
                     else self._metadata(key, value))
            if clean is not _DROP and not self.policy.allow_content:
                clean = self._json(clean, set(), 0)
            if clean is not _DROP:
                result[self._text(key)] = clean
        return {} if self._registered_native(result, set(), -1) else result

    def content_export(self, payload):
        """Explicit evidence callers retain content but share credential rules."""
        return EmissionBoundary(ContentPolicy(True), secrets=tuple(self._secrets),
                                preserve_json_text=True).export(payload)

    @staticmethod
    def exception(error):
        """Fixed classifications only: no messages, class names, locals or stacks."""
        if isinstance(error, TimeoutError):
            kind = 'timeout'
        elif isinstance(error, ValueError):
            kind = 'invalid_value'
        elif isinstance(error, OSError):
            kind = 'io_error'
        else:
            kind = 'error'
        return {'status': 'error', 'error_kind': kind}


class OwnedLogFilter(logging.Filter):
    """Sanitize an owned handler's output; never install on user/UI channels."""

    def __init__(self, boundary):
        super().__init__()
        self.boundary = boundary

    def filter(self, record):
        payload = record.msg if type(record.msg) is dict else {}
        if self.boundary.policy.allow_content and type(record.msg) is str:
            # Avoid formatting arbitrary args (which could call repr/str).
            payload = {'message': record.msg}
        clean = self.boundary.export(payload)
        if record.exc_info:
            clean.update(self.boundary.exception(record.exc_info[1]))
        # Formatters can access every LogRecord attribute. Remove arbitrary extras
        # and stack text/interpolation arguments; source metadata needs opt-in.
        level = record.levelno if record.levelno in (10, 20, 30, 40, 50) else 20
        safe = logging.LogRecord('api_agent', level, '', 0,
                                 json.dumps(clean, ensure_ascii=False), (), None)
        if self.boundary.policy.allow_content:
            for name in ('pathname', 'filename', 'module', 'funcName'):
                value = record.__dict__.get(name)
                if type(value) is str:
                    safe.__dict__[name] = self.boundary._text(value)
            if type(record.lineno) is int and record.lineno >= 0:
                safe.lineno = record.lineno
        safe.threadName = 'runtime'
        safe.processName = 'runtime'
        safe.taskName = None
        record.__dict__.clear()
        record.__dict__.update(safe.__dict__)
        return True
