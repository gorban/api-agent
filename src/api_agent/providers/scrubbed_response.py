"""Bounded JSON response scrubbing with out-of-band, value-free diagnostics."""
from dataclasses import dataclass, field
from collections.abc import Sequence
import asyncio
import json
import logging
import re


from .contracts import ContractError, ProviderAccessError
from .credential_guard import CredentialProtectionError, _check_json, _json_document
from api_agent.telemetry.redaction import registered_text_ranges

_LIMIT = 8 * 1024 * 1024
_LOG = logging.getLogger('api_agent.scrubbing')


@dataclass(frozen=True)
class ScrubDiagnostic:
    path: tuple
    action: str
    reason: str = 'credential_or_export_policy'


@dataclass(frozen=True)
class AnonymousMember:
    path: tuple
    value: object = field(repr=False)


@dataclass(frozen=True)
class ScrubReport(Sequence):
    events: tuple
    anonymous_members: tuple = field(default=(), repr=False)
    omitted_diagnostics: int = 0

    def __len__(self):
        return len(self.events)

    def __getitem__(self, index):
        return self.events[index]


class ResponseValidationError(ContractError):
    """Safe schema failure; original rejected inputs are never retained."""
    def __init__(self, operation, diagnostics=(), *, rule='invalid_json'):
        self.diagnostics = tuple(diagnostics)
        self.operation = operation
        self.rule = rule
        detail = '; credential scrubbing changed the response' if diagnostics else ''
        super().__init__(f'{operation}: response validation failed ({rule}){detail}.')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _constant(value):
    raise ValueError()



def decode_json(value):
    """Decode a bounded, duplicate-free JSON document without credential policy."""
    if type(value) not in (str, bytes, bytearray) or len(value) > _LIMIT:
        raise ResponseValidationError('json_decode')
    try:
        result = json.loads(value, object_pairs_hook=_pairs, parse_constant=_constant)
        return _json_document(result)[0]
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        pass
    raise ResponseValidationError('json_decode')


class _DiagnosticHandler(logging.StreamHandler):
    def emit(self, record):
        from .diagnostic_output import observe_stderr
        observe_stderr(self.format(record) + self.terminator)
        super().emit(record)


def configure_diagnostics(level='warning'):
    """Configure process-local, value-free response diagnostics on stderr."""
    if level not in ('off', 'warning', 'info', 'debug'):
        raise ValueError('Invalid scrub diagnostics level')
    if not _LOG.handlers:
        _LOG.addHandler(_DiagnosticHandler())
    _LOG.setLevel(logging.CRITICAL + 1 if level == 'off' else getattr(logging, level.upper()))


def report_diagnostics(operation, diagnostics, *, omitted=0):
    if diagnostics:
        _LOG.info('Response scrubbed: operation=%s fields=%d omitted_events=%d', operation, len(diagnostics), omitted)
        for item in diagnostics:
            _LOG.debug('Response scrub: operation=%s path=%s action=%s reason=%s',
                       operation, item.path, item.action, item.reason)


def report_worker_counts(operation, diagnostics):
    """Log already validated count-only diagnostics from an isolated owner."""
    _LOG.info('Response scrubbed: operation=%s empty_strings=%d omitted_members=%d omitted_events=%d',
              operation, diagnostics['scrub_empty_strings'], diagnostics['scrub_omitted_members'],
              diagnostics['scrub_omitted_events'])


def _validate(value, schema, operation, diagnostics):
    if schema is None:
        return value
    if callable(schema):
        failed = False
        try:
            schema(_json_document(value)[0])
        except (ValueError, TypeError, KeyError, ProviderAccessError):
            failed = True
        if failed:
            raise ResponseValidationError(operation, diagnostics, rule='contract')
        return value
    from jsonschema import Draft202012Validator
    invalid = next(Draft202012Validator(schema).iter_errors(value), None)
    if invalid is not None:
        raise ResponseValidationError(operation, diagnostics, rule=invalid.validator)
    return value


def scrub_response(value, *, schema, secrets=(), operation='response', transform=True, max_depth=128):
    """Return clean JSON and diagnostics; validation owns all required fields.

    Bytes are decoded as JSON; native strings remain strings. Additional fields
    follow the supplied schema. Contract validators validate but never replace
    the screened JSON. Logging never includes raw keys or values.
    """
    from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy
    operation = operation if type(operation) is str and re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}', operation) else 'response'
    diagnostics = []
    anonymous = []
    omitted = 0
    try:
        secrets = tuple(secrets)
        if type(value) is bytes:
            if len(value) > _LIMIT:
                raise ValueError()
            value = decode_json(value)
        if type(max_depth) is not int or not 0 <= max_depth <= 128:
            raise ValueError()
        original = _json_document(value, max_depth=max_depth)[0] if transform else value
        boundary = EmissionBoundary(ContentPolicy(True), secrets=secrets, preserve_json_text=True)
        if boundary._text(operation) != operation:
            operation = 'response'
        def changed(path, action):
            nonlocal omitted
            if len(diagnostics) < 100:
                diagnostics.append(ScrubDiagnostic(path, action))
            else:
                omitted += 1
        def text_ranges(text):
            return [*registered_text_ranges(text, secrets),
                    *((left, right) for left, right, _ in boundary._text_spans(text)[1])]
        def protected(text):
            return bool(text_ranges(text))
        def visit(item, definition, path):
            if type(item) is str:
                if protected(item):
                    changed(path, 'empty_string')
                    return ''
                return item
            if type(item) is dict:
                result = {}
                properties = definition.get('properties', {}) if type(definition) is dict else {}
                for index, (key, child) in enumerate(item.items()):
                    safe = key if key in properties and not protected(key) else index
                    nested = (*path, safe)
                    child_schema = properties.get(key, definition.get('additionalProperties', {})) if type(definition) is dict else {}
                    if type(child_schema) is not dict:
                        child_schema = {}
                    cleaned = visit(child, child_schema, nested)
                    if protected(key):
                        changed(nested, 'omit_member')
                        if len(anonymous) >= 100:
                            raise ResponseValidationError(operation, diagnostics, rule='anonymous_member_budget')
                        anonymous.append(AnonymousMember(nested, cleaned))
                        continue
                    result[key] = cleaned
                return result
            if type(item) is list:
                items = definition.get('items', {}) if type(definition) is dict else {}
                result = [visit(child, items, (*path, index)) for index, child in enumerate(item)]
                # Emptying a preceding element can reveal a new word boundary
                # or join formerly separated fragments. Recheck the cleaned
                # carrier, with a fixed work bound rather than an unbounded loop.
                for pass_index in range(2):
                    carrier = item if pass_index == 0 else list(result)
                    joined = ''.join(child for child in carrier if type(child) is str)
                    ranges = text_ranges(joined)
                    if ranges:
                        offset = 0
                        for index, child in enumerate(carrier):
                            if type(child) is str:
                                stop = offset + len(child)
                                if child and any(left < stop and right > offset for left, right in ranges):
                                    if result[index] != '':
                                        changed((*path, index), 'empty_string')
                                    result[index] = ''
                                offset = stop
                if protected(''.join(child for child in result if type(child) is str)):
                    raise ResponseValidationError(operation, diagnostics, rule='carrier')
                return result
            return item
        # A wrong optional type/value must not become valid through omission.
        if transform:
            _validate(original, schema, operation, ())
            clean = visit(original, schema, ())
            # Residual encoded/cross-carrier matches fail closed.
            clean = _check_json(clean, secrets)
            if anonymous:
                values = visit([item.value for item in anonymous], {}, ())
                _check_json(values, secrets)
                anonymous = [AnonymousMember(item.path, value) for item, value in zip(anonymous, values)]
        else:
            # A functional guard requires exact preservation, with one clone
            # and the same matcher work as the original guard implementation.
            clean = _check_json(original, secrets)
    except ResponseValidationError:
        report_diagnostics(operation, diagnostics, omitted=omitted)
        raise
    except CredentialProtectionError:
        if not transform:
            try:
                visit(_json_document(original)[0], schema, ())
            except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
                pass
        if not diagnostics:
            diagnostics.append(ScrubDiagnostic((), 'reject'))
        report_diagnostics(operation, diagnostics, omitted=omitted)
        raise ResponseValidationError(operation, diagnostics) from None
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise ResponseValidationError(operation, diagnostics) from None
    report_diagnostics(operation, diagnostics, omitted=omitted)
    return _validate(clean, schema, operation, diagnostics), ScrubReport(tuple(diagnostics), tuple(anonymous), omitted)


async def async_scrub_response(value, **options):
    """Offload normalization and validation from the event loop."""
    return await asyncio.to_thread(scrub_response, value, **options)
