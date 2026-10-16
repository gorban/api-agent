"""Restrict WebAuthn delegation without removing unrelated response policies."""
import re

from .contracts import AuthError

_FEATURES = ('publickey-credentials-get', 'publickey-credentials-create')
_KEY = r'[a-z*][a-z0-9_.*-]*'
_STRING = r'"(?:[\x20-\x21\x23-\x5b\x5d-\x7e]|\\["\\])*"'
_TOKEN = r"[A-Za-z*][!#$%&'*+.^_`|~0-9A-Za-z:/-]*"
_BARE = rf'(?:{_STRING}|{_TOKEN}|-?[0-9]{{1,12}}\.[0-9]{{1,3}}|-?[0-9]{{1,15}}|:(?:[A-Za-z0-9+/]{{4}})*(?:[A-Za-z0-9+/]{{2}}(?:==)?|[A-Za-z0-9+/]{{3}}=?)?:|\?[01])'
_PARAMS = rf'(?:; *{_KEY}(?:={_BARE})?)*'
_ITEM = rf'{_BARE}{_PARAMS}'
_VALUE = rf'(?:\((?: *{_ITEM}(?: +{_ITEM})*)? *\)|{_BARE})'
_MEMBER = re.compile(rf'(?P<key>{_KEY})(?:=(?P<value>{_VALUE}))?{_PARAMS}')


def restrict_webauthn_headers(headers):
    """Return CDP HeaderEntries; refuse policy ambiguity with a fixed error.

    Permissions-Policy uses an RFC 8941 dictionary across combined field lines.
    Keep original fields verbatim and append only absent WebAuthn features.
    Existing WebAuthn declarations must already forbid foreign delegation.
    """
    if not isinstance(headers, list):
        raise AuthError('passkey_permissions_policy')
    copied, policies = [], []
    for entry in headers:
        if (not isinstance(entry, dict) or set(entry) != {'name', 'value'}
                or not isinstance(entry['name'], str) or not isinstance(entry['value'], str)
                or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", entry['name']) is None
                or any(character in entry['value'] for character in '\r\n\0')):
            raise AuthError('passkey_permissions_policy')
        copied.append(dict(entry))
        if entry['name'].lower() == 'permissions-policy':
            policies.append(entry['value'])
    combined = ', '.join(policies)
    if len(combined) > 65536 or any(ord(character) > 127 for character in combined):
        raise AuthError('passkey_permissions_policy')
    remaining, present = combined.strip(' \t'), set()
    while remaining:
        member = _MEMBER.match(remaining)
        if member is None:
            raise AuthError('passkey_permissions_policy')
        key, value = member.group('key', 'value')
        if key in _FEATURES:
            if key in present or value is None or re.fullmatch(r'\( *(?:self)? *\)', value) is None:
                raise AuthError('passkey_permissions_policy')
            present.add(key)
        remaining = remaining[member.end():].lstrip(' \t')
        if remaining:
            if not remaining.startswith(',') or not remaining[1:].strip(' \t'):
                raise AuthError('passkey_permissions_policy')
            remaining = remaining[1:].lstrip(' \t')
    additions = ', '.join(f'{feature}=(self)' for feature in _FEATURES if feature not in present)
    if additions:
        copied.append({'name': 'Permissions-Policy', 'value': additions})
    return copied
