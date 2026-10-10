"""Credential-descriptor labels for operation-specific account selection."""
from collections.abc import Mapping

from api_agent.auth.profiles import normalize_auth, parse_profiles


def account_options(auth, roles, *, paired=False, registry=None):
    """Tests need the primary account; local UI needs all declared roles paired.

    Optional role descriptors enrich labels without blocking primary-only tests.
    This never retrieves usernames, passwords or codes from a password manager.
    """
    if not roles:
        return ()
    canonical = normalize_auth(auth, registry=registry)
    required = roles if paired else roles[:1]
    profiles = parse_profiles({'password_manager': canonical['password_manager'],
        'roles': {role: canonical['roles'].get(role, []) for role in required}}, required,
        registry=registry)
    count = min(len(profiles.for_role(role)) for role in required)
    options = []
    for index in range(count):
        names = [profiles.for_role(role)[index].user for role in required]
        for role in roles[len(required):]:
            entries = canonical['roles'].get(role, [])
            if isinstance(entries, list) and index < len(entries):
                entry = entries[index]
                if isinstance(entry, Mapping) and isinstance(entry.get('user'), str) and entry['user'].strip():
                    names.append(entry['user'])
        options.append((index, ' / '.join(names)))
    return tuple(options)
