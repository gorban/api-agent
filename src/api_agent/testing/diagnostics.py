"""Explicit terminal diagnostics: preserve failure evidence, scrub credentials only.

Captured exception/TUI evidence may be sent to the explicitly selected CI terminal
channel through this credential-only scrubber, independent of LOG_CONTENT. Runner
events and persisted reports keep their existing safe fields.
"""
from bisect import bisect_left, bisect_right
import re
import json
import shlex
import sys
import traceback

from api_agent.config.resolve import ConfigResult
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary, _credential_key, _SECRET_KEYS
from api_agent.telemetry.redaction import overlapping_ranges as _overlapping_ranges

# These public accounting settings contain paths/controls, never credentials.
_PUBLIC_KEYS = frozenset({'token_usage_csv', 'token_usage_csv_enabled', 'max_tokens',
                         'max_output_tokens', 'token_budget', 'token_limit'})
_DESCRIPTOR_SUFFIXES = ('_role', '_path', '_dir', '_file', '_manager', '_provider',
                        '_command', '_cmd', '_profile', '_type', '_enabled')
_ASSIGNMENT = re.compile(
    r'''(?<![A-Za-z0-9_.-])(?=(?P<prefix>(?P<quote>['"]?)(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)(?P=quote)\s*[:=]\s*)'''
    r'''(?P<value>\[REDACTED\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+))''')
_AUTH = re.compile(r'''(?i)(\b(?:proxy[-_]authorization|authorization)[ \t]*[:=][ \t]*['"]?)([A-Za-z][A-Za-z0-9_-]*)[ \t]+(\[REDACTED\]|[^\s,'";}\]]+)''')
# A multipart header is one credential value, including every comma-separated field.
_STRUCTURED_AUTH = re.compile(r'''(?i)(['"](?:proxy[-_]authorization|authorization)['"]\s*:\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''')
_MULTIPART_AUTH = re.compile(r'''(?im)(\b(?:proxy[-_]authorization|authorization)[ \t]*[:=][ \t]*(?!(?:Basic|Bearer)\b)[A-Za-z][A-Za-z0-9_-]*[ \t]+)([^\r\n]+)''')
_BARE_BEARER = re.compile(r'''(?i)\bbearer\s+(?!\[redacted\](?=$|[\s,;"'}]))[^\s,;"'}]+''')
_COOKIE = re.compile(r'(?im)(\b(?:Cookie|Set-Cookie)\s*:\s*)([^\r\n]*)')
_COOKIE_VALUE = re.compile(r'([A-Za-z0-9_-]+\s*=\s*)([^;\s]+)')
_URL = re.compile(r'((?:\x1b\[[0-?]*[ -/]*[@-~])|(?<![A-Za-z0-9+.-]))([A-Za-z][A-Za-z0-9+.-]*://)([^/\s@]+)@')
_QUOTED_VALUE = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', re.VERBOSE)
# Shell words may concatenate quoted segments; never mask just the first segment.
_OPTION = re.compile(r'''(?P<prefix>--(?P<key>[A-Za-z][A-Za-z0-9_-]*)(?:[ \t]+|=))(?P<value>(?:"(?:\\.|[^"\\])*"|'[^']*'|\\.|[^\s,;'"]+)+)''')
_TRUNCATED_OPTION = re.compile(r'''(?P<prefix>--(?P<key>[A-Za-z][A-Za-z0-9_-]*)(?:[ \t]+|=))(?P<value>'[^'\r\n]*|"[^"\r\n]*)(?P<marker> \[truncated: \d+ command characters omitted\])''')


def _secret_key(key):
    key = key.rsplit('.', 1)[-1]
    normalized = re.sub(r'(?<=[a-z])(?=[A-Z])', '_', key).lower().replace('-', '_')
    return (not key.endswith(('Error', 'Exception')) and normalized not in _PUBLIC_KEYS
            and not normalized.endswith(_DESCRIPTOR_SUFFIXES)
            and (_credential_key(key) or any(normalized.endswith('_' + name)
                 for name in _SECRET_KEYS if '_' in name)))


def _masked(value):
    if value in {'None', 'null', 'True', 'False', 'true', 'false', '""', "''"}:
        return value
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[0] + '[REDACTED]' + value[-1]
    return '[REDACTED]'


def _list_end(text, start):
    """Find a captured list's end without treating quoted brackets as syntax."""
    depth, quote, escaped = 0, '', False
    for position in range(start, len(text)):
        char = text[position]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = ''
        elif char in "\"'":
            quote = char
        elif char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
            if depth == 0:
                return position + 1
    return None




class _RegisteredRanges:
    """Carry original registered-secret ranges through syntax redactions.

    Replacements preserve unchanged prefixes and suffixes. Any registered range
    intersecting changed text covers its replacement as well, so neither syntax
    rewriting nor value masking can expose part of the original credential.
    """

    def __init__(self, text, secrets):
        ranges = []
        for secret in secrets:
            ranges.extend(_overlapping_ranges(text, secret))
        self.ranges = self._merge(ranges)

    @staticmethod
    def _merge(ranges):
        merged = []
        for start, stop in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
            else:
                merged.append((start, stop))
        return merged

    def sub(self, pattern, replacement, text):
        edits = [(match.start(), match.end(), replacement(match))
                 for match in pattern.finditer(text)]
        return self.apply(text, edits)

    def apply(self, text, edits):
        trimmed = []
        for start, stop, replacement in edits:
            before = text[start:stop]
            prefix = 0
            while prefix < min(len(before), len(replacement)) and before[prefix] == replacement[prefix]:
                prefix += 1
            suffix = 0
            while suffix < min(len(before), len(replacement)) - prefix and before[-1-suffix] == replacement[-1-suffix]:
                suffix += 1
            end = len(replacement) - suffix
            if prefix != len(before) or prefix != len(replacement):
                trimmed.append((start + prefix, stop - suffix, replacement[prefix:end]))
        if not trimmed:
            return text
        starts, stops, offsets, deltas, lengths = [], [], [], [], []
        pieces, cursor, offset = [], 0, 0
        for start, stop, replacement in trimmed:
            starts.append(start)
            stops.append(stop)
            offsets.append(offset)
            lengths.append(len(replacement))
            offset += len(replacement) - (stop - start)
            deltas.append(offset)
            pieces.extend((text[cursor:start], replacement))
            cursor = stop
        pieces.append(text[cursor:])

        def map_position(position, *, end=False):
            index = (bisect_left(starts, position) if end else bisect_right(starts, position)) - 1
            inside = index >= 0 and (position <= stops[index] if end else position < stops[index])
            if inside:
                return starts[index] + offsets[index] + (lengths[index] if end else 0)
            completed = bisect_right(stops, position) - 1
            return position + (deltas[completed] if completed >= 0 else 0)

        self.ranges = self._merge((map_position(start), map_position(stop, end=True))
                                  for start, stop in self.ranges)
        return ''.join(pieces)

    def redact(self, text):
        pieces, cursor = [], 0
        for start, stop in self.ranges:
            pieces.extend((text[cursor:start], '[REDACTED]'))
            cursor = stop
        pieces.append(text[cursor:])
        return ''.join(pieces)


class CredentialScrubber:
    """Callable plain-text credential scrubber, including registered runtime secrets."""

    def __init__(self, config=None, *, secret_values=(), boundary=None):
        self.boundary = boundary or EmissionBoundary(ContentPolicy(True))
        for value in tuple(self.boundary._secrets):
            self.register_secret(value)
        if isinstance(config, ConfigResult):
            config = config.values
        self._register_config(config or {})
        for value in secret_values:
            self.register_secret(value)

    def register_secret(self, value):
        self.boundary.register_secret(value)
        # Line-buffered terminal streams must also hide genuine multiline values.
        for fragment in value.splitlines():
            if fragment:
                self.boundary.register_secret(fragment)
        for escaped in (json.dumps(value, ensure_ascii=True)[1:-1], repr(value)[1:-1], shlex.quote(value)):
            self.boundary.register_secret(escaped)

    def _register_config(self, value, credential=False):
        if type(value) is str:
            if credential and value:
                self.register_secret(value)
        elif type(value) is dict:
            for key, item in value.items():
                if type(key) is str:
                    self._register_config(item, _secret_key(key))
        elif type(value) in (list, tuple):
            for item in value:
                self._register_config(item, credential)

    def __call__(self, text):
        # Recognize complete credential syntax before registered values can
        # erase a scheme, header name or other recognition boundary.
        registered = _RegisteredRanges(text, self.boundary._secrets)
        text = registered.sub(_STRUCTURED_AUTH, lambda match: match[1] + _masked(match[2]), text)
        text = registered.sub(_MULTIPART_AUTH, lambda match: match[1] + '[REDACTED]', text)
        text = registered.sub(_AUTH, lambda match: match[1] + match[2] + ' [REDACTED]', text)
        text = registered.sub(_COOKIE, lambda match: match[1] + _COOKIE_VALUE.sub(
            lambda item: item[1] + '[REDACTED]', match[2]), text)
        text = registered.sub(_URL, lambda match: match[1] + match[2] + ('[REDACTED]:[REDACTED]' if ':' in match[3] else '[REDACTED]') + '@', text)
        edits, end = [], 0
        for match in _ASSIGNMENT.finditer(text):
            key, value = match['key'].lower().replace('-', '_'), match['value']
            scheme = key in {'authorization', 'proxy_authorization'} and (
                value.lower() in {'basic', 'bearer'} or text[match.end('value'):].lstrip().startswith('[REDACTED]'))
            cookie_header = key in {'cookie', 'set_cookie'} and not value.startswith(('"', "'"))
            start, stop = match.span('value')
            container = value.startswith(('{', '[')) and value != '[REDACTED]'
            if start >= end and _secret_key(match['key']) and not (scheme or cookie_header):
                if container and value.startswith('['):
                    list_stop = _list_end(text, start)
                    if list_stop is None:
                        newline = text.find('\n', start)
                        stop = len(text) if newline == -1 else newline
                        replacement = _QUOTED_VALUE.sub(lambda item: _masked(item[0]), text[start:stop])
                    else:
                        stop, replacement = list_stop, '[REDACTED]'
                    edits.append((start, stop, replacement))
                    end = stop
                elif not container:
                    edits.append((start, stop, _masked(value)))
                    end = stop
        text = registered.apply(text, edits)
        def option(match):
            value = ('[REDACTED]' if match['value'] in self.boundary._secrets else
                     _masked(match['value']) if _secret_key(match['key']) else match['value'])
            return match['prefix'] + value
        text = registered.sub(_TRUNCATED_OPTION, lambda match: match['prefix'] + (
            '[REDACTED]' if _secret_key(match['key']) else match['value']) + match['marker'], text)
        text = registered.sub(_OPTION, option, text)
        text = registered.sub(_BARE_BEARER, lambda match: 'Bearer [REDACTED]', text)
        return self.boundary._text(registered.redact(text))


def scrub_command(command, config=None):
    """Scrub complete credential arguments before shell quoting or truncation."""
    scrubber = CredentialScrubber(config)
    result, credential_value = [], False
    for argument in command:
        argument = str(argument)
        if credential_value:
            result.append('[REDACTED]')
            credential_value = False
        elif argument.startswith('--'):
            key, separator, value = argument[2:].partition('=')
            if _secret_key(key):
                result.append('--' + key + '=[REDACTED]' if separator else argument)
                credential_value = not separator
            else:
                result.append(scrubber(argument))
        else:
            key, separator, value = argument.partition('=')
            if separator and _secret_key(key):
                result.append(key + '=[REDACTED]')
            else:
                result.append(scrubber(argument))
    return tuple(result)


def format_exception(error, boundary=''):
    """Full reachable exception chains and command evidence, without frame locals."""
    text = (boundary + '\n' if boundary else '') + ''.join(
        traceback.TracebackException.from_exception(error, capture_locals=False,
            max_group_width=sys.maxsize, max_group_depth=sys.maxsize).format(chain=True))
    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if item.__suppress_context__ and item.__context__ is not None and item.__context__ is not item.__cause__:
            text += '\nSuppressed exception context (retained locally):\n' + ''.join(
                traceback.TracebackException.from_exception(item.__context__, capture_locals=False,
                    max_group_width=sys.maxsize, max_group_depth=sys.maxsize).format(chain=True))
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        details = getattr(item, 'local_details', '')
        if type(details) is str and details:
            text += '\nLocal command evidence:\n' + details
        for secondary in getattr(item, 'diagnostic_errors', ()):
            text += '\nSecondary cleanup failure:\n' + ''.join(
                traceback.TracebackException.from_exception(secondary, capture_locals=False,
                    max_group_width=sys.maxsize, max_group_depth=sys.maxsize).format(chain=True))
            pending.append(secondary)
        pending.extend(value for value in (item.__cause__, item.__context__) if value is not None)
    return text


def result_diagnostics(tests):
    """Identify each available failure cause once, using explicit terminal fields."""
    seen = set()
    for result in tests:
        identifier = result.identity.id
        if identifier in seen:
            continue
        local = getattr(result, 'local_diagnostic', '')
        if local or result.status in {'failed', 'error', 'broken'}:
            seen.add(identifier)
            details = local or result.traceback or 'No failure diagnostic was captured for this test.'
            yield f'Failure details: {identifier}\n{details}'


class TerminalDiagnosticError(RuntimeError):
    """CI boundary type whose message contains the scrubbed original diagnostic."""


class TerminalDiagnosticInterrupt(KeyboardInterrupt):
    """Preserve interrupt/exit-130 handling without exposing the original exception."""


def terminal_exception(error, scrubber, *, boundary=''):
    """Wrap a CI rethrow, preserving cleanup capabilities without mutating its cause.

    The caller must raise this wrapper ``from None``. Original exception type and
    chains remain visible in the scrubbed message; the original cleanup owners remain attached.
    """
    diagnostic = scrubber(format_exception(error, boundary))
    if isinstance(error, KeyboardInterrupt):
        wrapped = TerminalDiagnosticInterrupt(diagnostic)
    elif isinstance(error, SystemExit):
        wrapped = SystemExit(error.code if error.code is None or isinstance(error.code, int) else 1)
        wrapped.diagnostic = diagnostic
    else:
        wrapped = TerminalDiagnosticError(diagnostic)
    for name in ('cleanup_pending', 'recording_prerequisites', 'inspection_group',
                 'inspections', 'recorded_result', 'owners', 'retry'):
        if hasattr(error, name):
            setattr(wrapped, name, getattr(error, name))
    from api_agent.providers.control import CleanupPendingError
    if isinstance(error, CleanupPendingError):
        wrapped.cleanup_pending = error
    return wrapped
