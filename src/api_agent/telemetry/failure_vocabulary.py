"""Closed vocabulary of provider failure facts that traces may carry.

Provider failure attributes are enumerations, never text. The defaults here are
public, provider-independent names. An embedding application that talks to a
provider with its own failure types, codes, recommendations or categories
registers those fixed tokens once at startup; nothing outside the union of the
defaults and the registered tokens is ever admitted to a trace.

Registration is additive and idempotent. The admitted set is the union of the
defaults and every registered token, so registration order never matters and a
host cannot remove a default or widen the shape of an attribute. Readers use an
immutable snapshot and take no lock.
"""
from contextlib import contextmanager
import re
from threading import Lock
from types import MappingProxyType

TOKEN_PATTERN = re.compile(r'[a-z][a-z0-9_]*(?:[/-][a-z0-9_]+)*')
MAX_TOKEN_LENGTH = 64
MAX_REGISTERED_TOKENS = 64

_FIELDS = ('type', 'code', 'recommendation', 'categories', 'category_source')
_REGISTRABLE = {'types': 'type', 'codes': 'code', 'categories': 'categories',
                'recommendations': 'recommendation'}

DEFAULTS = MappingProxyType({
    'type': frozenset(('content_policy_violation', 'invalid_request_error', 'rate_limit_error',
                       'authentication_error', 'permission_error', 'server_error')),
    'code': frozenset(('content_filter', 'context_length_exceeded', 'rate_limit_exceeded',
                       'invalid_api_key', 'insufficient_quota')),
    'recommendation': frozenset(('block', 'allow', 'review')),
    'categories': frozenset(('hate', 'hate/threatening', 'harassment', 'harassment/threatening',
                             'self-harm', 'self-harm/intent', 'self-harm/instructions', 'sexual',
                             'sexual/minors', 'violence', 'violence/graphic', 'illicit',
                             'illicit/violent')),
    'category_source': frozenset(('reported', 'inferred')),
})

_lock = Lock()
_registered = MappingProxyType({field: frozenset() for field in _FIELDS})
_admitted = DEFAULTS


def _publish(registered):
    global _registered, _admitted
    _registered = registered
    _admitted = MappingProxyType({field: DEFAULTS[field] | registered[field] for field in _FIELDS})


def _tokens(name, values):
    if type(values) not in (tuple, list, set, frozenset):
        raise ValueError(f'{name} must be a tuple, list or set of tokens')
    for value in values:
        if (type(value) is not str or not 0 < len(value) <= MAX_TOKEN_LENGTH
                or TOKEN_PATTERN.fullmatch(value) is None):
            raise ValueError(f'{name} accepts only lowercase identifier tokens of at most '
                             f'{MAX_TOKEN_LENGTH} characters')
    return frozenset(values)


def register_provider_failure_vocabulary(*, types=(), codes=(), categories=(), recommendations=()):
    """Admit more fixed failure tokens; invalid input changes nothing and raises ValueError."""
    supplied = {'types': types, 'codes': codes, 'categories': categories,
                'recommendations': recommendations}
    additions = {_REGISTRABLE[name]: _tokens(name, values) - DEFAULTS[_REGISTRABLE[name]]
                 for name, values in supplied.items()}
    with _lock:
        merged = {field: _registered[field] | additions.get(field, frozenset()) for field in _FIELDS}
        for field, tokens in merged.items():
            if len(tokens) > MAX_REGISTERED_TOKENS:
                raise ValueError(f'At most {MAX_REGISTERED_TOKENS} {field} tokens may be registered')
        _publish(MappingProxyType(merged))


def admits(field, value):
    """True only for a str that is a default or registered token of ``field``; never raises."""
    return type(field) is str and type(value) is str and value in _admitted.get(field, ())


@contextmanager
def provider_failure_vocabulary_scope(**tokens):
    """Test helper: register for the block, then withdraw only what the block added.

    Tokens that were already registered when the block opened, and tokens other
    threads register while it is open, are left alone.
    """
    with _lock:
        before = _registered
    register_provider_failure_vocabulary(**tokens)
    with _lock:
        added = {field: _registered[field] - before[field] for field in _FIELDS}
    try:
        yield
    finally:
        with _lock:
            _publish(MappingProxyType({field: _registered[field] - added[field] for field in _FIELDS}))
