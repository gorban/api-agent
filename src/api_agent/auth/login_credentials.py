"""In-memory selected-login material; representations never contain credentials."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import re

from .managers import ManagerError


def validate_login_request(username, relying_party_id):
    if (not isinstance(username, str) or not username.strip() or len(username) > 1024
            or not username.isprintable()):
        raise ManagerError('Selected login requires a valid expected username.')
    if (not isinstance(relying_party_id, str) or len(relying_party_id) > 253
            or re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*', relying_party_id) is None):
        raise ManagerError('Selected login requires a canonical relying-party ID.')


def decode_id(value, *, size=None, maximum=1024):
    if (not isinstance(value, str) or not value or re.fullmatch('[A-Za-z0-9_-]+', value) is None
            or len(value) > maximum * 2):
        raise ManagerError('Selected login contains an invalid encoded identifier.')
    try:
        decoded = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    except ValueError:
        raise ManagerError('Selected login contains an invalid encoded identifier.') from None
    if (not decoded or len(decoded) > maximum or (size is not None and len(decoded) != size)
            or base64.urlsafe_b64encode(decoded).decode().rstrip('=') != value):
        raise ManagerError('Selected login contains an invalid encoded identifier.')
    return decoded


@dataclass(frozen=True)
class LoginCredentialRequest:
    expected_username: str = field(repr=False)
    relying_party_id: str = field(repr=False)


@dataclass(frozen=True)
class PasskeyCredential:
    relying_party_id: str = field(repr=False)
    username: str = field(repr=False)
    credential_id: str = field(repr=False)
    user_handle: str = field(repr=False)
    sign_count: int = field(repr=False)
    private_key: object = field(repr=False)
    _components: tuple[str, ...] = field(init=False, default=(), repr=False)

    def __post_init__(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        validate_login_request(self.username, self.relying_party_id)
        decode_id(self.credential_id, maximum=1023)
        decode_id(self.user_handle, maximum=64)
        if type(self.sign_count) is not int or not 0 <= self.sign_count <= 0xffffffff:
            raise ManagerError('Passkey signature counter is invalid.')
        if not isinstance(self.private_key, ec.EllipticCurvePrivateKey) or not isinstance(self.private_key.curve, ec.SECP256R1):
            raise ManagerError('Passkey requires a validated P-256 private key.')
        numbers = self.private_key.private_numbers()
        values = (numbers.private_value, numbers.public_numbers.x, numbers.public_numbers.y)
        object.__setattr__(self, '_components', tuple(base64.urlsafe_b64encode(value.to_bytes(32)).decode().rstrip('=')
                                                    for value in values))

    def pkcs8_base64(self):
        from cryptography.hazmat.primitives import serialization
        data = self.private_key.private_bytes(serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        return base64.b64encode(data).decode('ascii')

    def secret_values(self):
        return (self.username, self.credential_id,
                self.user_handle, *self._components, self.pkcs8_base64())


@dataclass(frozen=True)
class SelectedLoginCredentials:
    available: bool
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    passkey: PasskeyCredential | None = field(default=None, repr=False)
    _components: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if type(self.available) is not bool:
            raise ManagerError('Selected login availability is invalid.')
        if not self.available:
            if any(value is not None for value in (self.username, self.password, self.passkey)):
                raise ManagerError('Absent selected login cannot carry credentials.')
            return
        validate_login_request(self.username, 'localhost')
        if self.password is not None and (not isinstance(self.password, str) or not self.password):
            raise ManagerError('Selected login password is invalid.')
        if self.passkey is not None and (type(self.passkey) is not PasskeyCredential or self.passkey.username != self.username):
            raise ManagerError('Selected login passkey does not match its account.')

    @property
    def password_available(self):
        return self.password is not None

    @property
    def passkey_available(self):
        return self.passkey is not None

    def secret_values(self):
        values = tuple(value for value in (self.username, self.password, *self._components) if value)
        return values + (() if self.passkey is None else self.passkey.secret_values())
