"""Independently authored structured policy and actual local browser controls."""
import pytest

from api_agent.llm_providers.gpt.contracts import AuthError
from api_agent.llm_providers.gpt.passkey_policy import restrict_webauthn_headers

GET, CREATE = 'publickey-credentials-get', 'publickey-credentials-create'


def test_preserves_all_original_headers_and_other_policy_directives():
    headers = [{'name': 'Set-Cookie', 'value': 'a=1'}, {'name': 'Set-Cookie', 'value': 'b=2'},
        {'name': 'pErMiSsIoNs-PoLiCy', 'value': 'camera=(), geolocation=(self "https://example.test")'},
        {'name': 'Permissions-Policy', 'value': 'microphone=*;report-to="group,one"'}]
    result = restrict_webauthn_headers(headers)
    assert result[:-1] == headers and result[0] is not headers[0]
    assert result[-1] == {'name': 'Permissions-Policy', 'value': f'{GET}=(self), {CREATE}=(self)'}
    assert len(headers) == 4


@pytest.mark.parametrize('existing', [f'{GET}=()', f'{GET}=(self)', f'{GET}=( self );report-to="group"'])
def test_existing_restrictive_feature_is_preserved_and_only_missing_feature_added(existing):
    original = {'name': 'Permissions-Policy', 'value': existing}
    assert restrict_webauthn_headers([original]) == [original, {'name': 'Permissions-Policy', 'value': f'{CREATE}=(self)'}]


def test_both_existing_restrictive_features_need_no_duplicate_header():
    headers = [{'name': 'Permissions-Policy', 'value': f'{GET}=(self), {CREATE}=()'}]
    assert restrict_webauthn_headers(headers) == headers


@pytest.mark.parametrize('value', [f'{GET}=*', f'{GET}=(self "https://example.test")',
    f'{GET}', f'{GET}=?0', f'{GET}=(self), {GET}=()', 'camera=(),', 'camera=() garbage',
    'camera=("unterminated)', 'Camera=()', 'camera=(self\tself)', 'camera="bad\\q"',
    'camera=(),,microphone=()', 'camera=:a:', 'camera=:a===:', 'camera=\u00e9', 'camera=' + 'x' * 65536])
def test_ambiguous_or_invalid_combined_policy_fails_with_static_error(value):
    with pytest.raises(AuthError, match='^passkey_permissions_policy$'):
        restrict_webauthn_headers([{'name': 'Permissions-Policy', 'value': value}])


def test_duplicate_web_authn_feature_across_field_lines_is_rejected():
    with pytest.raises(AuthError):
        restrict_webauthn_headers([{'name': 'Permissions-Policy', 'value': f'{GET}=(self)'},
                                  {'name': 'permissions-policy', 'value': f'{GET}=()'}])


@pytest.mark.parametrize('headers', [None, {}, [None], [{'name': 'Bad name', 'value': 'x'}],
    [{'name': 'X', 'value': 1}], [{'name': 'X', 'value': 'line\r\nInjected: 1'}],
    [{'name': 'X', 'value': '\0'}], [{'name': 'X'}], [{'name': 'X', 'value': 'x', 'extra': 1}]])
def test_malformed_cdp_headers_are_rejected(headers):
    with pytest.raises(AuthError): restrict_webauthn_headers(headers)


@pytest.mark.parametrize('value', ['', ' \t ', 'camera="https://example.test/a,b"',
    'camera=?0, microphone=123, geolocation=-1.25, fullscreen=:YWJj:',
    'camera=(self;enabled "https://example.test");report-to="group"', 'camera;enabled'])
def test_valid_structured_dictionary_forms_keep_their_original_bytes(value):
    headers = [{'name': 'Permissions-Policy', 'value': value}]
    assert restrict_webauthn_headers(headers)[0] == headers[0]


@pytest.mark.parametrize('restrict,expected', [(True, 'NotAllowedError'), (False, 'signed')])
def test_real_chromium_policy_controls_passkey_while_foreign_javascript_loads(restrict, expected):
    import asyncio
    import base64
    from types import SimpleNamespace
    from cryptography.hazmat.primitives.asymmetric import ec
    from playwright.async_api import async_playwright
    from api_agent.auth.login_credentials import PasskeyCredential
    from api_agent.llm_providers.gpt.passkeys import SoftwarePasskey

    async def exercise():
        async def serve(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                body = b'<!doctype html><title>Ordinary foreign document</title><script>window.ordinaryLoaded=true</script>'
                writer.write(b'HTTP/1.1 200 OK\r\nPermissions-Policy: camera=()\r\n'
                    b'Permissions-Policy: microphone=()\r\nContent-Type: text/html\r\nConnection: close\r\nContent-Length: '
                    + str(len(body)).encode() + b'\r\n\r\n' + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        servers = [await asyncio.start_server(serve, '127.0.0.1', 0) for _ in range(2)]
        origin, foreign = [f'http://localhost:{server.sockets[0].getsockname()[1]}' for server in servers]
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
        credential = PasskeyCredential('localhost', 'synthetic@example.test', encode(b'policy-credential'),
            encode(b'policy-user'), 0, ec.generate_private_key(ec.SECP256R1()))
        try:
            async with async_playwright() as driver:
                browser = await driver.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()
                owner = SoftwarePasskey(SimpleNamespace(page=page, context=context, browser=browser),
                                        credential, trusted_origin=origin)
                # Isolate policy enforcement from the coordinator-owned request
                # guard: install the real authenticator, then allow documents.
                try:
                    await owner.prepare()
                    await owner.channel.send('Fetch.disable')
                    owner.channel.remove_listener('Fetch.requestPaused', owner._paused)
                    owner.intercepting = False
                    channel = owner.channel
                    tasks = set()
                    async def respond(event):
                        await channel.send('Fetch.continueResponse', {
                            'requestId': event['requestId'], 'responseCode': event['responseStatusCode'],
                            'responsePhrase': event.get('responseStatusText', ''),
                            'responseHeaders': (restrict_webauthn_headers(event.get('responseHeaders', []))
                                                if restrict else event.get('responseHeaders', []))})
                    def paused(event):
                        task = asyncio.create_task(respond(event))
                        tasks.add(task)
                    channel.on('Fetch.requestPaused', paused)
                    await channel.send('Fetch.enable', {'patterns': [
                        {'resourceType': 'Document', 'requestStage': 'Response'}]})
                    await page.goto(origin)
                    await page.evaluate('''url => new Promise(resolve => {
                        const frame=document.createElement('iframe');
                        frame.allow='publickey-credentials-get *; publickey-credentials-create *';
                        frame.onload=resolve; frame.src=url; document.body.append(frame);
                    })''', foreign)
                    frame = page.frames[1]
                    assert await frame.evaluate('window.ordinaryLoaded') is True
                    assert await page.evaluate("document.featurePolicy.allowsFeature('camera')") is False
                    assert await page.evaluate("document.featurePolicy.allowsFeature('microphone')") is False
                    assert await page.evaluate("document.featurePolicy.allowsFeature('publickey-credentials-get')") is True
                    result = await asyncio.wait_for(frame.evaluate('''async id => {
                        const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),3000);
                        try { await navigator.credentials.get({signal:controller.signal,publicKey:{
                            rpId:'localhost',challenge:new Uint8Array(32),userVerification:'required',
                            allowCredentials:[{type:'public-key',id:Uint8Array.from(atob(id),c=>c.charCodeAt(0))}]}});
                            return 'signed';
                        } catch(error) { return error.name; } finally {clearTimeout(timer);}
                    }''', base64.b64encode(b'policy-credential').decode()), 10)
                    assert result == expected
                    assert await frame.evaluate("document.featurePolicy.allowsFeature('publickey-credentials-get')") is (not restrict)
                    await channel.send('Fetch.disable')
                    await asyncio.gather(*tasks)
                    channel.remove_listener('Fetch.requestPaused', paused)
                finally:
                    await owner.aclose()
                    await browser.close()
        finally:
            for server in servers: server.close()
            await asyncio.gather(*(server.wait_closed() for server in servers))
    asyncio.run(exercise())
