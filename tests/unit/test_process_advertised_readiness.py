"""An advertised TLS identity never changes the readiness connection address."""
import asyncio
from datetime import datetime, timedelta, timezone
import ssl
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from api_agent.ui import process


@pytest.mark.parametrize('origin,scheme,port', [
    ('https://agent.example:8443', 'https', 8443),
    ('https://agent.example:8443/', 'https', 8443),
    ('https://agent.example', 'https', 443),
    ('https://[::1]:8443', 'https', 8443),
])
def test_advertised_identity_is_independent_of_connection(origin, scheme, port):
    authority, hostname = process._readiness_identity(origin, scheme=scheme, port=port)
    assert authority == origin.removeprefix('https://').rstrip('/')
    assert hostname == ('::1' if '[' in origin else 'agent.example')


@pytest.mark.parametrize('origin', [
    None, False, 1, {}, 'http://agent.example:8443',
    'https://agent.example:8444', 'https://agent.example:0',
    'https://user@agent.example:8443', 'https://user:pass@agent.example:8443',
    'https://@agent.example:8443', 'https://:@agent.example:8443',
    'https://agent.example:8443/path', 'https://agent.example:8443/?q=1',
    'https://agent.example:8443/#fragment', 'https://agent.example:8443?x=1',
    'https://agent.example:8443#x', 'https://AGENT.example:8443',
    'https://agent.example:8443?', 'https://agent.example:8443#',
    'https://agent.example:bad', 'https://agent.example:65536',
    'https://agent.example:8443//', 'https://agent.example:8443\\bad',
    'https://agent.example:8443/\n', 'https://agént.example:8443',
    'https://:8443', 'https://[bad]:8443', 'https://agent.example:8443\r\nX: a',
])
def test_invalid_advertised_identity_is_rejected(origin):
    with pytest.raises(ValueError, match='readiness origin'):
        process._readiness_identity(origin, scheme='https', port=8443)


def test_explicit_zero_port_is_not_an_advertised_origin():
    with pytest.raises(ValueError, match='readiness origin'):
        process._readiness_identity('https://agent.example:0', scheme='https', port=0)


def test_advertised_http_rejected_before_spawn(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid readiness identity reached process creation')
    monkeypatch.setattr(process.subprocess, 'Popen', forbidden)
    async def run():
        launcher = process.StudioProcesses(ssl_context=ssl.create_default_context())
        with pytest.raises(ValueError, match='readiness origin'):
            await launcher.start(('unused',), cwd='.', host='127.0.0.1', port=8443,
                scheme='http', env={}, readiness_origin='http://agent.example:8443')
    asyncio.run(run())


@pytest.fixture
def advertised_tls(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'agent.example')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('agent.example')]), critical=False)
        .sign(key, hashes.SHA256()))
    certificate = tmp_path / 'server.pem'
    private_key = tmp_path / 'key.pem'
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate, private_key)
    return server, ssl.create_default_context(cafile=str(certificate))


@pytest.mark.parametrize('trusted,advertised', [
    (True, 'agent.example'), (False, 'agent.example'), (True, 'other.example'),
])
def test_real_loopback_tls_uses_advertised_host_and_checks_certificate(
        advertised_tls, monkeypatch, trusted, advertised):
    server_context, trusted_context = advertised_tls
    client_context = trusted_context if trusted else ssl.create_default_context()
    token = 'owned-token'
    child = SimpleNamespace(code=None)
    child.poll = lambda: child.code
    child.terminate = lambda: setattr(child, 'code', 0)
    child.kill = lambda: setattr(child, 'code', -9)
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(process, 'token_urlsafe', lambda _: token)
    server_names, requests = [], []
    server_context.set_servername_callback(lambda connection, name, context: server_names.append(name))

    async def run():
        async def serve(reader, writer):
            data = await reader.readuntil(b'\r\n\r\n')
            requests.append(data.decode('ascii'))
            # Reconnect after a non-ready response; identity must survive retries.
            status = '503 Unavailable' if len(requests) == 1 else '200 OK'
            writer.write((f'HTTP/1.1 {status}\r\nContent-Length: 0\r\n'
                f'X-UI-Launch-Token: {token}\r\nConnection: close\r\n\r\n').encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0, ssl=server_context,
            ssl_handshake_timeout=1, ssl_shutdown_timeout=1)
        port = server.sockets[0].getsockname()[1]
        valid_identity = trusted and advertised == 'agent.example'
        launcher = process.StudioProcesses(ssl_context=client_context,
            timeout=5 if valid_identity else .6)
        try:
            if trusted and advertised == 'agent.example':
                owner = await launcher.start(('unused',), cwd='.', host='127.0.0.1',
                    port=port, scheme='https', env={}, readiness_origin=f'https://{advertised}:{port}')
                try:
                    assert len(requests) == 2
                    assert all(f'Host: {advertised}:{port}\r\n' in request for request in requests)
                    assert all(request.startswith('HEAD / HTTP/1.1\r\n') for request in requests)
                finally:
                    await owner.aclose()
            else:
                with pytest.raises(TimeoutError):
                    await launcher.start(('unused',), cwd='.', host='127.0.0.1',
                        port=port, scheme='https', env={}, readiness_origin=f'https://{advertised}:{port}')
                assert requests == []
            assert server_names and set(server_names) == {advertised}
            assert child.code == 0
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(run())
