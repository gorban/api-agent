"""Document redirects are guarded before their destination reaches local HTTP."""
import asyncio
import base64
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from playwright.async_api import Error, async_playwright

from api_agent.auth.login_credentials import PasskeyCredential
from api_agent.llm_providers.gpt.passkeys import SoftwarePasskey


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


@pytest.mark.parametrize('empty_phrase', [False, True])
@pytest.mark.parametrize('document', ['main', 'iframe'])
@pytest.mark.parametrize('destination', ['foreign', 'callback', 'wrong-callback-path'])
def test_redirect_checks_each_document_and_only_exact_owned_callback(document, destination, empty_phrase):
    async def exercise():
        requests = []
        target = ''
        async def serve(reader, writer):
            try:
                headers = (await reader.readuntil(b'\r\n\r\n')).decode().split('\r\n')
                path = headers[0].split()[1]
                host = next(line for line in headers if line.lower().startswith('host:'))
                requests.append((path, host))
                if path == '/redirect':
                    body = b''
                    response = f'HTTP/1.1 302 Found\r\nLocation: {target}\r\n'.encode()
                else:
                    body = b'<!doctype html><title>Disposable local document</title>'
                    response = (b'HTTP/1.1 200 \r\n' if empty_phrase else b'HTTP/1.1 200 OK\r\n') + b'Content-Type: text/html\r\n'
                writer.write(response + b'Connection: close\r\nContent-Length: '
                             + str(len(body)).encode() + b'\r\n\r\n' + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        origin, foreign = f'http://localhost:{port}', f'http://127.0.0.1:{port}'
        target = foreign + ('/wrong' if destination == 'wrong-callback-path' else '/callback')
        callback = None if destination == 'foreign' else foreign + '/callback'
        key = PasskeyCredential('localhost', 'synthetic@example.test', encoded(b'disposable-credential'),
                                encoded(b'disposable-user'), 0, ec.generate_private_key(ec.SECP256R1()))
        try:
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                context = await browser.new_context(service_workers='block')
                page = await context.new_page()
                owner = SoftwarePasskey(SimpleNamespace(page=page, context=context, browser=browser), key,
                                        trusted_origin=origin, callback_url=callback)
                try:
                    await owner.prepare()
                    await page.goto(origin)
                    permitted = document == 'iframe' or destination == 'callback'
                    if document == 'main':
                        if permitted:
                            await page.goto(origin + '/redirect')
                            assert page.url == target
                        else:
                            with pytest.raises(Error):
                                await page.goto(origin + '/redirect')
                    else:
                        # The frame load/error completion is the barrier after
                        # the redirect attempt, rather than a scheduling sleep.
                        await asyncio.wait_for(page.evaluate('''url => new Promise(resolve => {
                            const frame = document.createElement('iframe');
                            frame.onload = () => resolve('loaded');
                            frame.onerror = () => resolve('failed');
                            frame.src = url; document.body.append(frame);
                        })''', origin + '/redirect'), 10)
                    foreign_requests = [path for path, host in requests if f'127.0.0.1:{port}' in host]
                    assert foreign_requests == ([('/wrong' if destination == 'wrong-callback-path' else '/callback')] if permitted else [])
                    assert sum(path == '/redirect' for path, host in requests) == 1
                    assert not owner.navigation_failed
                finally:
                    await owner.aclose()
                    await browser.close()
        finally:
            server.close()
            await server.wait_closed()
    asyncio.run(exercise())
