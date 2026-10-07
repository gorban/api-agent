"""Synthetic CA and loopback handshakes; no host trust or provider access."""
import asyncio
from datetime import datetime, timedelta, timezone
import socket
import ssl
from threading import Thread
from unittest.mock import AsyncMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
import pytest

from api_agent.runtime.client_tls import (
    ClientTLSError, require_verified_context, verified_client_context,
)


@pytest.fixture
def certificates(tmp_path):
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic test CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
              key_encipherment=False, data_encipherment=False, key_agreement=False,
              key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    server = (x509.CertificateBuilder().subject_name(server_name).issuer_name(ca_name)
              .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
              .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
              .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
              .sign(ca_key, hashes.SHA256()))
    ca_path, cert_path, key_path = (tmp_path / name for name in ('public-ca.pem', 'server.pem', 'server-key.pem'))
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(server_key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca_path, cert_path, key_path


def handshake(context, certificates, hostname='localhost'):
    _, cert_path, key_path = certificates
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    errors = []
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(2)
        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    with server_context.wrap_socket(connection, server_side=True) as secured:
                        secured.sendall(b'OK')
            except ssl.SSLError:
                # Rejected trust/hostname is expected to fail on the server too.
                pass
            except Exception as error:
                errors.append(error)
        worker = Thread(target=serve, daemon=True)
        worker.start()
        try:
            with socket.create_connection(listener.getsockname(), timeout=2) as connection:
                with context.wrap_socket(connection, server_hostname=hostname) as secured:
                    assert secured.recv(2) == b'OK'
        finally:
            worker.join(2)
            assert not worker.is_alive()
            assert not errors


def test_real_loopback_certificate_and_hostname_verification(certificates):
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(verified_client_context(), certificates)
    context = verified_client_context(certificates[0])
    handshake(context, certificates)
    with pytest.raises(ssl.SSLCertVerificationError, match='Hostname mismatch'):
        handshake(context, certificates, hostname='different.example')
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_system_roots_retained_and_public_ca_symlink_supported(certificates, tmp_path):
    system = set(verified_client_context().get_ca_certs(binary_form=True))
    link = tmp_path / 'ca-link.pem'
    link.symlink_to(certificates[0])
    context = verified_client_context(link)
    combined = set(context.get_ca_certs(binary_form=True))
    assert system <= combined
    assert certificates[0].read_text().startswith('-----BEGIN CERTIFICATE-----')
    assert len(combined) == len(system) + 1
    handshake(context, certificates)


@pytest.mark.parametrize('kind', ['missing', 'directory', 'private', 'malformed', 'not_certificate', 'non_ascii', 'false'])
def test_invalid_ca_rejected_without_exposing_paths_or_material(kind, certificates, tmp_path):
    path = tmp_path / 'sensitive-path.pem'
    if kind == 'directory':
        path.mkdir()
    elif kind == 'private':
        path.write_bytes(certificates[0].read_bytes() + certificates[2].read_bytes())
    elif kind == 'malformed':
        path.write_text('-----BEGIN CERTIFICATE-----\ninvalid certificate\n-----END CERTIFICATE-----')
    elif kind == 'not_certificate':
        path.write_text('sensitive material')
    elif kind == 'non_ascii':
        path.write_bytes(b'\xff')
    elif kind == 'false':
        path = False
    with pytest.raises(ClientTLSError) as caught:
        verified_client_context(path)
    assert str(caught.value) == 'Client public CA could not be loaded.'
    assert caught.value.__cause__ is None


def test_strict_context_validation_and_disabled_verification_rejected():
    verified = verified_client_context()
    assert require_verified_context(verified) is verified
    for value in (False, None, object()):
        with pytest.raises(ClientTLSError, match='verify certificates and hostnames'):
            require_verified_context(value)
    verified.check_hostname = False
    with pytest.raises(ClientTLSError):
        require_verified_context(verified)
    verified.verify_mode = ssl.CERT_NONE
    with pytest.raises(ClientTLSError):
        require_verified_context(verified)


def test_configured_provider_receives_verified_context(certificates, tmp_path):
    from types import SimpleNamespace
    from api_agent.api.composition import compose_runtime
    provider_file = tmp_path / 'provider.py'
    provider_file.write_text('''class Adapter:
    version = 1
    def __init__(self, services): self.services = services
def create(config, services): return Adapter(services)
''')
    context = SimpleNamespace(pack_id='synthetic', identity_factory_path=None)
    runtime = compose_runtime(context, {'LLM_PROVIDER': str(provider_file) + ':create'},
        callback_origin='https://agent.example', tls_material=SimpleNamespace(ca_file=certificates[0]), required_kinds={'llm'})
    async def scenario():
        await runtime.astart()
        trust = runtime.llm_provider.implementation.services['ssl_context']
        require_verified_context(trust)
        handshake(trust, certificates)
        await runtime.aclose()
    asyncio.run(scenario())


def test_loopback_helper_reports_server_worker_failure(certificates, monkeypatch):
    context = verified_client_context(certificates[0])
    original = ssl.SSLContext
    def server_context(protocol):
        server = original(protocol)
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic worker failure')
        server.wrap_socket = fail
        return server
    monkeypatch.setattr(ssl, 'SSLContext', server_context)
    with pytest.raises(AssertionError):
        handshake(context, certificates)
