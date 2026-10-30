"""Process-local call identities; cross-process calls cannot share authorities."""
from hashlib import sha256
import hmac
from threading import RLock

_lock = RLock()
_calls = {}


def key(capability, binding, nonce):
    return hmac.new(capability.encode(), (binding + ':' + nonce).encode(), sha256).digest()


def register(identity, scope):
    with _lock:
        if identity in _calls:
            raise RuntimeError('Duplicate local call identity')
        entry = {'scope': scope, 'lease': None}
        _calls[identity] = entry
        return entry


def lookup(identity):
    with _lock:
        return _calls.get(identity)


def assign_lease(identity, entry, lease):
    """Bind a lease only while this exact registration is still owned."""
    with _lock:
        if _calls.get(identity) is not entry:
            return False
        entry['lease'] = lease
        return True


def remove(identity, entry):
    with _lock:
        if _calls.get(identity) is entry:
            del _calls[identity]
