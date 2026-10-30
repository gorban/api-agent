"""Real offline Chromium assertions with independently generated disposable keys."""
import asyncio
import base64
import hashlib
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from api_agent.auth.login_credentials import PasskeyCredential
from api_agent.llm_providers.gpt.passkeys import SoftwarePasskey


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def decoded(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


ASSERTION = '''async ({challenge, credential}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    try {
        const result = await navigator.credentials.get({signal: controller.signal, publicKey: {
            challenge: Uint8Array.from(challenge), rpId: 'localhost', userVerification: 'required',
            allowCredentials: [{type: 'public-key', id: Uint8Array.from(atob(credential), c => c.charCodeAt(0))}]}});
        const b64 = value => btoa(String.fromCharCode(...new Uint8Array(value)));
        return {id: result.id, auth: b64(result.response.authenticatorData),
            client: b64(result.response.clientDataJSON), signature: b64(result.response.signature),
            user: b64(result.response.userHandle)};
    } catch (error) { return {error: error.name}; }
    finally { clearTimeout(timer); }
}'''


def test_software_passkey_real_signatures_verification_scope_and_removal():
    from playwright.async_api import async_playwright
    async def exercise():
        async def serve(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                body = b'<!doctype html><title>Disposable local authenticator test</title>'
                writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: '
                             + str(len(body)).encode() + b'\r\n\r\n' + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        origin = f'http://localhost:{server.sockets[0].getsockname()[1]}'
        private = ec.generate_private_key(ec.SECP256R1())
        credential_id, user_handle = b'disposable-credential', b'disposable-user'
        credential = PasskeyCredential('localhost', 'synthetic@example.test', encoded(credential_id),
                                      encoded(user_handle), 0, private)
        arguments = dict(challenge=[1] * 32, credential=base64.b64encode(credential_id).decode())
        try:
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()
                owner = SoftwarePasskey(SimpleNamespace(page=page, context=context, browser=browser),
                                        credential, trusted_origin=origin)
                try:
                    await owner.prepare()
                    await page.goto(origin)
                    signatures = []
                    for number in (1, 2):
                        arguments['challenge'] = [number] * 32
                        result = await asyncio.wait_for(page.evaluate(ASSERTION, arguments), 10)
                        assert 'error' not in result
                        auth, client = base64.b64decode(result['auth']), base64.b64decode(result['client'])
                        data = json.loads(client)
                        assert auth[:32] == hashlib.sha256(b'localhost').digest()
                        assert auth[32] & 5 == 5  # Presence and verification are software signals.
                        assert auth[32] & 24 == 0  # No backup eligibility/state claims.
                        assert int.from_bytes(auth[33:37], 'big') == 0
                        assert data['origin'] == origin and data['type'] == 'webauthn.get'
                        assert decoded(data['challenge']) == bytes(arguments['challenge'])
                        assert decoded(result['id']) == credential_id
                        assert base64.b64decode(result['user']) == user_handle
                        signature = base64.b64decode(result['signature'])
                        private.public_key().verify(signature, auth + hashlib.sha256(client).digest(),
                                                    ec.ECDSA(hashes.SHA256()))
                        signatures.append(signature)
                    assert signatures[0] != signatures[1]
                    await owner.channel.send('WebAuthn.setUserVerified',
                        {'authenticatorId': owner.authenticator, 'isUserVerified': False})
                    assert 'error' in await asyncio.wait_for(page.evaluate(ASSERTION, arguments), 10)
                    await owner.channel.send('WebAuthn.setUserVerified',
                        {'authenticatorId': owner.authenticator, 'isUserVerified': True})
                    sibling = await context.new_page()
                    await sibling.goto(origin)
                    assert 'error' in await asyncio.wait_for(sibling.evaluate(ASSERTION, arguments), 10)
                    await sibling.close()
                    await owner.aclose()
                    assert owner.closed and owner.authenticator is None
                    assert 'error' in await asyncio.wait_for(page.evaluate(ASSERTION, arguments), 10)
                finally:
                    await owner.aclose()
                    await browser.close()
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(exercise())
