"""Fail-closed primitives for provider-owned credential protection.

Providers must join the operation's authority, register credentials before use,
validate the final adapter payload and protect all returned content. These
primitives do not themselves establish those provider or transport obligations.
"""
from typing import Protocol
import asyncio
import json
import math

from .contracts import ContractError
from api_agent.telemetry.redaction import (_credential_views, registered_values, serialized_credential,
                                           _json_registrations, _contains_json_registration, _json_object, AmbiguousJSON)


class CredentialProtectedProvider(Protocol):
    """Explicit extension contract; joining must precede any inference operation.

    Implementations are responsible for registering every acquired and refreshed
    credential and guarding their final wire payload, result, errors and streams.
    Returning normally acknowledges those obligations for this authority.
    """
    credential_protection_version: int

    def bind_credential_authority(self, authority: object) -> None: ...


class CredentialProtectionError(ContractError):
    """Content cannot be safely released under the credential contract."""


def require_joined_provider(provider, authority):
    """Obtain a provider's explicit commitment for this operation authority.

    This checks the shape of the commitment, not provider compliance. The caller
    must invoke it before authentication or generation and retain the same
    authority until all operation work has stopped. A subprocess that acquires
    credentials needs its own protection before content leaves that process.
    """
    from api_agent.telemetry.privacy import EmissionBoundary
    if not isinstance(authority, EmissionBoundary):
        raise CredentialProtectionError('LLM operation credential authority is unavailable.')
    try:
        version = getattr(provider, 'credential_protection_version', None)
        bind = getattr(provider, 'bind_credential_authority', None)
    except Exception:
        raise CredentialProtectionError('Provider could not join credential protection.') from None
    if type(version) is not int or version != 1 or not callable(bind):
        raise CredentialProtectionError(
            'LLM provider must implement credential protection version 1 and bind the operation authority.')
    try:
        result = bind(authority)
    except Exception:
        raise CredentialProtectionError('Provider could not join credential protection.') from None
    if result is not None:
        # An async binding never executes; dispose the unstarted coroutine and
        # reject it rather than accepting an unfulfilled commitment.
        if asyncio.iscoroutine(result):
            result.close()
        raise CredentialProtectionError('Provider could not join credential protection.')


def admit_llm_provider(provider, authority):
    """Bind an LLM before authentication; return whether evidence must be withheld.

    Providers join before authentication and register credentials before use.
    """
    require_joined_provider(provider, authority)
    return False



def _charge_carrier_work(current, amount):
    """Bound cumulative JSON-carrier parsing work across one operation."""
    total = current + amount
    if total > 16 * 1024 * 1024:
        raise ValueError()
    return total


def guard_json(value, secrets):
    """Return an independent, unchanged JSON value or reject unsafe content.

    This conservative guard rejects rather than changing structural or typed
    values. Callers may implement contract-aware redaction and validation.
    Arrays' text leaves are also checked as a joined carrier. This function
    does not synchronize publication with concurrent credential registration.
    """
    try:
        known = registered_values(secrets)
        if any(type(secret) is not str or not secret for secret in known):
            raise ValueError()
        parts = []
        count = 0
        size = 0
        def text_part(item):
            nonlocal size
            # Bound before copying, encoding, concatenating, or normalizing.
            size += len(item)
            if size > 8 * 1024 * 1024:
                raise ValueError()
            parts.append(item)
        def clone(item, depth):
            nonlocal count
            count += 1
            if count > 100000 or depth > 64:
                raise ValueError()
            if type(item) is str:
                text_part(item)
                return item
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise ValueError()
                result = {}
                for key, child in item.items():
                    text_part(key)
                    result[key] = clone(child, depth + 1)
                return result
            if type(item) is list:
                result = [clone(child, depth + 1) for child in item]
                # Only a sequence of text fragments is one carrier. Object
                # members and keys have independent meanings.
                if result and all(type(child) is str for child in result):
                    parts.append(''.join(result))
                return result
            if type(item) in (int, bool, type(None)):
                return item
            if type(item) is float and math.isfinite(item):
                return item
            raise ValueError()
        result = clone(value, 0)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 8 * 1024 * 1024:
            raise ValueError()
        # Check fields and explicitly joined text sequences, including escaped
        # representations and controls inserted into credential text.
        for candidate in (*parts, encoded):
            for normalized, _, _ in _credential_views(candidate.encode('utf-8')):
                if any(secret.encode('utf-8') in normalized for secret in known):
                    raise ValueError()
        if serialized_credential(encoded, known):
            raise ValueError()
        # Compare JSON-shaped registrations against decoded leaf spellings too.
        # Decoding the whole serialized document can change JSON syntax; walk
        # the already validated structure instead, preserving its types.
        def normalized(item):
            if type(item) is str:
                views = _credential_views(item.encode('utf-8'))
                last = item.encode('utf-8')
                for last, _, _ in views:
                    pass
                return last.decode('utf-8')
            if type(item) is list:
                return [normalized(child) for child in item]
            if type(item) is dict:
                result = {}
                for key, child in item.items():
                    decoded_key = normalized(key)
                    if decoded_key in result:
                        raise ValueError()
                    result[decoded_key] = normalized(child)
                return result
            return item
        registrations = _json_registrations(known)
        decoded = normalized(result) if registrations else None
        if registrations and _contains_json_registration(decoded, registrations):
            raise ValueError()
        # Inspect JSON text recursively before decoding its string leaves.
        # A parsed object can itself contain another JSON document in a key
        # or value. Bound both parsing work and nesting across all carriers.
        if registrations:
            work = 0
            nodes = 0
            def inspect_carrier(item, depth):
                nonlocal work, nodes
                nodes += 1
                if nodes > 100000 or depth > 20:
                    raise ValueError()
                if type(item) is str:
                    if not any(mark in item for mark in ('[', '{', '"')):
                        return
                    work = _charge_carrier_work(work, len(item))
                    try:
                        parsed = json.loads(item, object_pairs_hook=_json_object)
                    except AmbiguousJSON:
                        raise
                    except (ValueError, TypeError, RecursionError):
                        # An invalid or unfinished outer carrier must not hide
                        # a later valid one. Recover at every possible start;
                        # charge the remaining length to bound worst-case work.
                        decoder = json.JSONDecoder(object_pairs_hook=_json_object)
                        start = 0
                        while start < len(item):
                            if item[start] not in ('{', '[', '"'):
                                start += 1
                                continue
                            try:
                                nested, end = decoder.raw_decode(item, start)
                            except AmbiguousJSON:
                                raise
                            except (ValueError, TypeError, RecursionError):
                                # Failed parses may have scanned the whole tail.
                                work = _charge_carrier_work(work, len(item) - start)
                                start += 1
                                continue
                            work = _charge_carrier_work(work, end - start)
                            if _contains_json_registration(normalized(nested), registrations):
                                raise ValueError()
                            inspect_carrier(nested, depth + 1)
                            # A prose quote can end inside a later object;
                            # recover object starts in that raw quoted span.
                            if type(nested) is str and any(mark in item[start + 1:end] for mark in ("{", "[")):
                                start += 1
                            else:
                                start = end
                        return
                    if _contains_json_registration(normalized(parsed), registrations):
                        raise ValueError()
                    inspect_carrier(parsed, depth + 1)
                elif type(item) is dict:
                    for key, child in item.items():
                        inspect_carrier(key, depth + 1)
                        inspect_carrier(child, depth + 1)
                elif type(item) is list:
                    for child in item:
                        inspect_carrier(child, depth + 1)
            for candidate in parts:
                inspect_carrier(candidate, 0)
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise CredentialProtectionError('Content cannot be safely released.') from None


async def async_guard_json(value, secrets):
    """Run bounded normalization away from the event loop."""
    return await asyncio.to_thread(guard_json, value, secrets)
