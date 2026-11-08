"""Trusted Studio masking policy with synthetic browser content."""
import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.ui.evidence import (OwnedFrameContract, _owned_application_frames,
    _renderer_literals, _sensitive, capture_studio_viewport)
from api_agent.ui.provider import DemoUIProvider


async def _painted_baseline(page):
    # Navigation/load does not guarantee the first compositor frame exists.
    # Allow font layout and two rendering-frame turns before pixel preconditions.
    await page.evaluate("""async () => {
      await document.fonts.ready;
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")
    return await page.screenshot()


async def _region_pixels(page, png, box):
    return await page.evaluate('''async ({png,box}) => {
      const image = new Image(); image.src = 'data:image/png;base64,' + png;
      await image.decode();
      const canvas = document.createElement('canvas');
      canvas.width = image.width; canvas.height = image.height;
      const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
      const data = context.getImageData(box.x, box.y, box.width, box.height).data;
      let ink = false, red = false, blue = false, black = true, blackPixel = false;
      for (let index = 0; index < data.length; index += 4) {
        const [r,g,b] = data.slice(index, index + 3);
        ink ||= r < 245 || g < 245 || b < 245;
        red ||= r > 200 && g < 80 && b < 80;
        blue ||= r < 80 && g < 80 && b > 180;
        black &&= r === 0 && g === 0 && b === 0;
        blackPixel ||= r < 50 && g < 50 && b < 50;
      }
      return {ink, red, blue, black, blackPixel};
    }''', {'png': base64.b64encode(png).decode(), 'box': box})


def _mock_cdp():
    async def send(method, params):
        if method == 'DOM.getDocument':
            return {'root': {'nodeId': 1}}
        if method == 'DOM.querySelectorAll':
            return {'nodeIds': []}
        raise AssertionError('Unexpected shadow audit request')
    session = SimpleNamespace(send=AsyncMock(side_effect=send), detach=AsyncMock())
    return SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)), session


def _proxied_context(page):
    return SimpleNamespace(new_cdp_session=lambda _: page.context.new_cdp_session(page))


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['popover', 'dialog'])
async def test_content_mask_hides_transient_top_layer_descendants(surface):
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    element = ('<div id="escape" popover' if surface == 'popover' else '<dialog id="escape"')
    close = '</div>' if surface == 'popover' else '</dialog>'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 600, 'height': 360})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=
                '<body style="margin:0;background:white"><main id="panel">'
                '<div id="private"><p style="margin:0">Confidential answer</p>' + element +
                ' style="position:fixed;inset:auto;left:300px;top:250px;margin:0;border:0;'
                'width:200px;height:50px;background:red">Confidential escape' + close +
                '</div><button id="send">Send</button></main></body>'))
            await page.goto(origin + '/')
            before = await page.locator('#private').bounding_box()
            controls = await page.locator('#send').bounding_box()
            async def screenshot(**kwargs):
                assert await page.locator('#private').bounding_box() == before
                assert await page.locator('#send').bounding_box() == controls
                await page.locator('#escape').evaluate('''(node, surface) => {
                  if (surface === 'popover') node.showPopover(); else node.showModal();
                }''', surface)
                image = await page.screenshot(**kwargs)
                await page.locator('#escape').evaluate('''(node, surface) => {
                  if (surface === 'popover') node.hidePopover(); else node.close();
                }''', surface)
                return image
            proxy = SimpleNamespace(url=page.url, frames=page.frames, screenshot=screenshot,
                evaluate=page.evaluate, evaluate_handle=page.evaluate_handle,
                locator=page.locator, context=_proxied_context(page))
            image = await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_selector='#private', content_mask_anchor='#panel')
            assert image is not None
            region = await _region_pixels(page, image,
                {'x': 300, 'y': 250, 'width': 200, 'height': 50})
            assert not region['red'] and not region['ink']
            assert (await _region_pixels(page, image, before))['black']
            assert await page.locator('#private').get_attribute('style') is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_content_mask_rejects_size_containment_that_moves_outer_controls():
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body style="margin:0"><main id="panel"><section id="private">
                <p style="margin:20px 0">Confidential answer</p>
              </section><button id="send">Send</button></main></body>'''))
            await page.goto(origin + '/')
            before = await page.locator('#send').bounding_box()
            reasons = []
            assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_selector='#private', content_mask_anchor='#panel',
                unavailable=reasons.append) is None
            assert reasons == ['content_layout']
            assert await page.locator('#send').bounding_box() == before
            assert await page.locator('#private').get_attribute('style') is None
            assert await page.locator('html > div').count() == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('clip', [
    'inset(var(--agent-chat-clip-top, 0px) 0 0 0 round 0 0 1rem 1rem)',
    'inset(10% 5%)',
    'inset(20px 10px 5px)',
    'inset(20px 10px 5px 15px round 8px / 4px 6px)',
])
async def test_rounded_nx_panel_inset_masks_only_visible_content_and_keeps_composer(clip):
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 600, 'height': 360})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body style="margin:0;background:white"><main id="panel">
                <div id="surface" style="--agent-chat-clip-top:20px;width:300px;height:120px;clip-path:CLIP">
                  <div id="private" style="height:600px">Confidential answer</div>
                </div><button id="send" style="display:block;height:40px;width:300px;background:red">Send</button>
              </main></body>'''.replace('CLIP', clip)))
            await page.goto(origin + '/')
            reasons = []
            image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_selector='#private', content_mask_anchor='#panel',
                unavailable=reasons.append)
            assert image is not None, reasons
            assert (await _region_pixels(page, image,
                {'x': 30, 'y': 30, 'width': 240, 'height': 70}))['black']
            assert not (await _region_pixels(page, image,
                {'x': 0, 'y': 0, 'width': 300, 'height': 5}))['ink']
            assert (await _region_pixels(page, image,
                await page.locator('#send').bounding_box()))['red']
            assert await page.locator('html > div').count() == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_unsupported_ancestor_clip_withholds_pixels_and_restores_styles():
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body><main id="panel"><div style="clip-path:circle(40%)">
                <div id="private" style="height:40px">Confidential answer</div>
              </div><button>Send</button></main></body>'''))
            await page.goto(origin + '/')
            reasons = []
            assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_selector='#private', content_mask_anchor='#panel',
                unavailable=reasons.append) is None
            assert reasons == ['content_clip']
            assert await page.locator('#private').get_attribute('style') == 'height:40px'
            assert await page.locator('html > div').count() == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['proxy_move', 'content_resize', 'proxy_remove', 'capture_failure'])
async def test_content_mask_proxies_reject_changed_geometry_and_always_cleanup(mutation):
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body><main id="panel"><div id="scroll" style="height:80px;overflow:auto">
                <section id="private" style="height:400px">Confidential</section>
              </div><button>Send</button></main></body>'''))
            await page.goto(origin + '/')
            async def screenshot(**kwargs):
                image = await page.screenshot(**kwargs)
                if mutation == 'proxy_move':
                    await page.locator('html > div').evaluate(
                        "node => node.style.setProperty('left', '400px', 'important')")
                elif mutation == 'proxy_remove':
                    await page.locator('html > div').evaluate('node => node.remove()')
                elif mutation == 'content_resize':
                    await page.locator('#scroll').evaluate("node => node.style.height = '40px'")
                else:
                    raise RuntimeError('capture failed')
                return image
            proxy = SimpleNamespace(url=page.url, frames=page.frames,
                screenshot=screenshot, evaluate=page.evaluate,
                evaluate_handle=page.evaluate_handle, locator=page.locator,
                context=_proxied_context(page))
            reasons = []
            if mutation == 'capture_failure':
                with pytest.raises(RuntimeError, match='capture failed'):
                    await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(False)),
                        origin, content_mask_selector='#private', content_mask_anchor='#panel')
            else:
                assert await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(False)),
                    origin, content_mask_selector='#private', content_mask_anchor='#panel',
                    unavailable=reasons.append) is None
                assert reasons == ['content_mask_geometry']
            assert await page.locator('html > div').count() == 0
            assert await page.locator('#private').get_attribute('style') == 'height:400px'
            assert await page.evaluate("[...document.querySelectorAll('*')].every(node => [...node.attributes].every(attr => !attr.name.startsWith('data-evidence-')))")
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_content_mask_requires_an_interior_in_every_authored_card():
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body><main id="panel"><article><div class="private">First answer</div></article>
                <article>Unmarked second answer</article></main></body>'''))
            await page.goto(origin + '/')
            reasons = []
            assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_selector='.private', content_mask_anchor='#panel',
                content_mask_requirements=(('article', '.private'),),
                unavailable=reasons.append) is None
            assert reasons == ['content_mask_missing']
            assert await page.locator('html > div').count() == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_masked_message_interiors_are_black_and_card_borders_and_controls_survive():
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 640, 'height': 360})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body style="margin:0;background:white"><main id="panel">
                  <fieldset id="card" style="margin:20px;width:350px;border:4px solid red;padding:10px">
                    <legend>Response</legend>
                    <div id="response" class="private" style="height:60px;background:blue">Confidential response</div>
                    <details open><summary id="reasoning-toggle">Reasoning</summary>
                      <div id="reasoning" class="private" style="height:30px;background:blue">Confidential reasoning</div>
                    </details>
                  </fieldset>
                  <section id="prompt-card" style="margin:20px;border:4px solid red;padding:8px">
                    <textarea id="prompt" class="private" style="display:block;width:300px;height:30px;background:blue">Confidential draft</textarea>
                    <button id="send">Send</button><select id="model"><option>Model</option></select>
                  </section>
                </main></body>'''))
            await page.goto(origin + '/')
            before = await page.locator('#card').bounding_box()
            image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_selector='.private', content_mask_anchor='#panel')
            assert image is not None
            assert await page.locator('#card').bounding_box() == before
            for selector in ('#response', '#reasoning', '#prompt'):
                box = await page.locator(selector).bounding_box()
                interior = {**box, 'x': box['x'] + 3, 'y': box['y'] + 3,
                    'width': box['width'] - 6, 'height': box['height'] - 6}
                assert (await _region_pixels(page, image, interior))['black']
            for selector in ('#card', '#prompt-card'):
                assert (await _region_pixels(page, image,
                    await page.locator(selector).bounding_box()))['red']
            for selector in ('#reasoning-toggle', '#send', '#model'):
                region = await _region_pixels(page, image,
                    await page.locator(selector).bounding_box())
                assert region['ink'] and not region['black']
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_omitted_content_masks_chat_dialog_and_preserves_shell_pixels():
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 640, 'height': 360})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body style="margin:0;background:white">
                  <nav id="shell" style="height:60px;background:rgb(255,0,0)">Nx shell</nav>
                  <dialog open data-testid="agent-chat-panel" style="position:absolute;left:100px;top:80px;width:300px;height:200px;background:white">
                    <div id="toolbar" style="height:20px;background:red">Maximize</div>
                    <div data-testid="agent-conversation-content" style="height:40px;background:blue">User request and assistant response
                      <div id="escaped" style="position:fixed;left:10px;top:320px;width:100px;height:20px;background:blue">escaped secret</div>
                    </div>
                    <section aria-label="Clarification questions"><div data-testid="clarification-content" style="height:30px;background:blue">Clarification content</div></section>
                    <section aria-label="Mutation confirmation"><div data-testid="mutation-confirmation-content"><div style="height:30px;background:blue">Approval summary</div><div style="height:30px;background:blue">Mutation request</div></div></section>
                    <div id="model-picker" style="height:20px;background:red">Model</div>
                    <textarea data-testid="agent-prompt-input" style="display:block;height:30px;background:blue">private draft</textarea>
                    <iframe src="about:blank" style="width:40px;height:30px"></iframe>
                  </dialog>
                </body>'''))
            await page.goto(origin + '/')
            boundary = EmissionBoundary(ContentPolicy(False))
            image = await capture_studio_viewport(page, boundary, origin,
                allow_frames=True, content_mask_anchor='[data-testid="agent-chat-panel"]',
                content_mask_selector=('[data-testid="agent-conversation-content"], '
                    '[data-testid="clarification-content"], '
                    '[data-testid="mutation-confirmation-content"], '
                    '[data-testid="agent-prompt-input"]'),
                content_mask_requirements=(
                    ('[data-testid="agent-chat-panel"]', '[data-testid="agent-conversation-content"]'),
                    ('[aria-label="Clarification questions"]', '[data-testid="clarification-content"]'),
                    ('[aria-label="Mutation confirmation"]', '[data-testid="mutation-confirmation-content"]')))
            assert image is not None
            shell = await page.locator('#shell').bounding_box()
            assert (await _region_pixels(page, image, shell))['red'] is True
            for selector in ('[data-testid="agent-conversation-content"]',
                    '[data-testid="clarification-content"]',
                    '[data-testid="mutation-confirmation-content"]',
                    '[data-testid="agent-prompt-input"]'):
                region = await _region_pixels(page, image,
                    await page.locator(selector).bounding_box())
                assert region['black'] is True
            for selector in ('#toolbar', '#model-picker'):
                assert (await _region_pixels(page, image,
                    await page.locator(selector).bounding_box()))['red'] is True
            escaped = await page.locator('#escaped').bounding_box()
            assert (await _region_pixels(page, image, escaped))['ink'] is False
            assert await page.locator('[data-testid="agent-chat-panel"]').evaluate(
                '(node) => node.style.background') == 'white'
            assert await page.locator('[data-testid="agent-conversation-content"]').evaluate(
                '(node) => node.style.clipPath') == ''
            boundary.register_secret('registered-outside-secret')
            await page.locator('#shell').evaluate(
                '(node) => { node.textContent = "registered-outside-secret"; }')
            assert await capture_studio_viewport(page, boundary, origin,
                allow_frames=True, content_mask_anchor='[data-testid="agent-chat-panel"]',
                content_mask_selector='[data-testid="agent-conversation-content"]') is None
            await page.locator('#shell').evaluate('(node) => { node.textContent = "Nx shell"; }')
            for section, required in (
                    ('[data-testid="agent-conversation-content"]',
                     ('[data-testid="agent-chat-panel"]', '[data-testid="agent-conversation-content"]')),
                    ('[data-testid="clarification-content"]',
                     ('[aria-label="Clarification questions"]', '[data-testid="clarification-content"]')),
                    ('[data-testid="mutation-confirmation-content"]',
                     ('[aria-label="Mutation confirmation"]', '[data-testid="mutation-confirmation-content"]'))):
                target = await page.locator(section).element_handle()
                await target.evaluate('(node) => node.removeAttribute("data-testid")')
                assert await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(False)), origin, allow_frames=True,
                    content_mask_anchor='[data-testid="agent-chat-panel"]',
                    content_mask_selector='[data-testid="agent-prompt-input"]',
                    content_mask_requirements=(required,)) is None
                await target.evaluate('(node, value) => node.setAttribute("data-testid", value)',
                    section.split('"')[1])
            await page.locator('[data-testid="agent-chat-panel"]').evaluate(
                '(node) => node.remove()')
            assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_anchor='[data-testid="agent-chat-panel"]',
                content_mask_selector='[data-testid="agent-conversation-content"]') is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_studio_default_content_policy_masks_conversation_and_composer():
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 640, 'height': 360})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body style="margin:0;background:white">
                  <header id="chrome" style="height:60px;background:red">Studio</header>
                  <div class="content"><div id="chat-stage" style="height:200px;background:blue">
                    <div id="conversation" class="message-body">Request, response, clarification and approval</div>
                    <textarea id="prompt">secret draft</textarea>
                    <div id="model-control" style="height:20px;background:red">Model and send</div>
                  </div>
                  <div id="status">private error</div></div>
                  <dialog id="failure">private failure</dialog>
                </body>'''))
            await page.goto(origin + '/')
            provider = DemoUIProvider({}, {})
            provider.process = object()
            provider._evidence_origin = origin
            image = await provider.acapture_evidence(page=page,
                boundary=EmissionBoundary(ContentPolicy(False)))
            assert image is not None
            for selector in ('#conversation', '#prompt', '#status'):
                box = await page.locator(selector).bounding_box()
                region = await _region_pixels(page, image, {'x': box['x'] + 2,
                    'y': box['y'] + 2, 'width': box['width'] - 4,
                    'height': box['height'] - 4})
                assert region['black'] is True, (selector, region)
            assert (await _region_pixels(page, image,
                await page.locator('#chrome').bounding_box()))['red'] is True
            assert (await _region_pixels(page, image,
                await page.locator('#model-control').bounding_box()))['red'] is True
            original = await provider.acapture_evidence(page=page,
                boundary=EmissionBoundary(ContentPolicy(True)))
            assert original is not None
            assert (await _region_pixels(page, original,
                await page.locator('#conversation').bounding_box()))['black'] is False
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['conversation', 'message_body', 'prompt'])
async def test_studio_content_policy_rejects_missing_authored_content_hooks(missing):
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body><main id="chat-stage"><section id="conversation">
                <article class="message"><div class="message-body">First answer</div></article>
                <article class="message"><div id="second" class="message-body">Second answer</div></article>
              </section><textarea id="prompt">Private draft</textarea></main></body>'''))
            await page.goto(origin + '/')
            selector, attribute = {'conversation': ('#conversation', 'id'),
                'message_body': ('#second', 'class'), 'prompt': ('#prompt', 'id')}[missing]
            await page.locator(selector).evaluate('(node, name) => node.removeAttribute(name)', attribute)
            provider = DemoUIProvider({}, {})
            provider.process = object()
            provider._evidence_origin = origin
            assert await provider.acapture_evidence(page=page,
                boundary=EmissionBoundary(ContentPolicy(False))) is None
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_masked_content_open_popover_cannot_escape_into_screenshot():
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 600, 'height': 300})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body><div id="panel"><div id="conversation" style="width:100px;height:40px;background:blue">
                  <div id="secret-popover" popover style="position:fixed;left:300px;top:100px;background:red">registered-secret</div>
                </div><div id="toolbar">Static controls</div></div></body>'''))
            await page.goto(origin + '/')
            await page.locator('#secret-popover').evaluate('(node) => node.showPopover()')
            assert await page.locator('#secret-popover').evaluate(
                '(node) => node.matches(":popover-open")') is True
            boundary = EmissionBoundary(ContentPolicy(False))
            boundary.register_secret('registered-secret')
            reasons = []
            assert await capture_studio_viewport(page, boundary, origin,
                content_mask_selector='#conversation', content_mask_anchor='#panel',
                unavailable=reasons.append) is None
            assert reasons == ['content_top_layer']
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('clipping', ['overflow:auto', 'contain:paint', 'clip-path:inset(0)'])
async def test_tall_masked_transcript_preserves_controls_outside_scroll_clip(clipping):
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 600, 'height': 300})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body style="margin:0;background:white">
                  <div id="panel" style="position:absolute;left:100px;top:20px;width:300px;background:white">
                    <div id="toolbar" style="height:30px;background:red">Static toolbar</div>
                    <div style="height:100px;CLIPPING">
                      <section id="conversation" style="height:600px;background:blue">Private transcript</section>
                    </div>
                    <div id="model" style="height:30px;background:red">Static model picker</div>
                    <div id="composer" style="height:30px;background:red">Static composer</div>
                  </div>
                </body>'''.replace('CLIPPING', clipping)))
            await page.goto(origin + '/')
            image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_selector='#conversation', content_mask_anchor='#panel')
            assert image is not None
            for selector in ('#toolbar', '#model', '#composer'):
                box = await page.locator(selector).bounding_box()
                assert (await _region_pixels(page, image, box))['red'] is True
            transcript = await page.locator('#conversation').bounding_box()
            visible = {**transcript, 'height': 50}
            region = await _region_pixels(page, image, visible)
            assert region['black'] is True
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_masked_capture_keeps_static_controls_with_live_transcript():
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 600, 'height': 300})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body style="background:white"><div id="panel">
                  <div id="toolbar" style="height:30px;background:red">Static controls</div>
                  <div id="conversation" style="height:50px;background:blue"><span id="live">0</span></div>
                </div><script>
                  let count = 0;
                  setInterval(() => {
                    document.querySelector('#live').textContent = String(++count);
                    if (count % 5 === 0) document.querySelector('#conversation')
                      .appendChild(document.createElement('span')).textContent = ' extra';
                  }, 1);
                </script></body>'''))
            await page.goto(origin + '/')

            async def slow_screenshot(**kwargs):
                image = await page.screenshot(**kwargs)
                await asyncio.sleep(.1)
                return image

            proxy = SimpleNamespace(url=page.url, frames=page.frames,
                screenshot=slow_screenshot, evaluate=page.evaluate,
                evaluate_handle=page.evaluate_handle, locator=page.locator,
                context=_proxied_context(page))
            image = await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_selector='#conversation', content_mask_anchor='#panel')
            assert image is not None
            assert (await _region_pixels(page, image,
                await page.locator('#toolbar').bounding_box()))['red'] is True
            assert (await _region_pixels(page, image,
                await page.locator('#conversation').bounding_box()))['blue'] is False
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['outside_text', 'outside_clone', 'portal', 'root_style'])
async def test_masked_capture_rejects_changes_outside_hidden_transcript(mutation):
    from playwright.async_api import async_playwright
    origin = 'http://localhost:9000'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body><div id="panel"><div id="toolbar">Static controls</div>
                <div id="conversation"><span id="private">Private response</span></div>
                </div></body>'''))
            await page.goto(origin + '/')

            async def changing_screenshot(**kwargs):
                image = await page.screenshot(**kwargs)
                if mutation == 'outside_text':
                    await page.locator('#toolbar').evaluate(
                        "node => node.textContent = 'Changed controls'")
                elif mutation == 'outside_clone':
                    await page.locator('#toolbar').evaluate(
                        'node => node.replaceWith(node.cloneNode(true))')
                elif mutation == 'portal':
                    await page.locator('#private').evaluate(
                        'node => document.body.appendChild(node)')
                else:
                    await page.locator('#conversation').evaluate(
                        "node => node.style.setProperty('opacity', '1', 'important')")
                return image

            proxy = SimpleNamespace(url=page.url, frames=page.frames,
                screenshot=changing_screenshot, evaluate=page.evaluate,
                evaluate_handle=page.evaluate_handle, locator=page.locator,
                context=_proxied_context(page))
            reasons = []
            assert await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(False)),
                origin, content_mask_selector='#conversation', content_mask_anchor='#panel',
                unavailable=reasons.append) is None
            assert reasons == ['isolation_verify' if mutation == 'root_style' else 'document_changed']
        finally:
            await browser.close()


_OWNED_FRAME = OwnedFrameContract('iframe[data-testid="chat-message-html"]',
    '/assets/agent-html-runtime/renderer.html', 'allow-scripts')


@pytest.mark.parametrize('failure', ['nested_frame', 'missing_document', 'shadow_root'])
def test_owned_frame_cdp_audit_rejects_unverifiable_inner_document(failure):
    origin = 'http://localhost:9000'
    async def send(method, params=None):
        if method == 'DOM.getDocument':
            return {'root': {'nodeId': 1}}
        if method == 'DOM.querySelectorAll':
            return {'nodeIds': [2]}
        if method == 'Page.getFrameTree':
            child = {'frame': {'id': 'child', 'parentId': 'top',
                'loaderId': 'loader', 'url': origin + _OWNED_FRAME.path}}
            if failure == 'nested_frame':
                child['childFrames'] = [{'frame': {'id': 'nested'}}]
            return {'frameTree': {'frame': {'id': 'top'}, 'childFrames': [child]}}
        if method == 'DOM.describeNode' and params['depth'] == 0:
            return {'node': {'localName': 'iframe', 'frameId': 'child',
                'backendNodeId': 3, 'attributes': ['data-testid', 'chat-message-html',
                    'sandbox', 'allow-scripts', 'src', _OWNED_FRAME.path]}}
        if method == 'DOM.describeNode':
            return {'node': {} if failure == 'missing_document' else {
                'contentDocument': {'shadowRoots': [{'shadowRootType': 'closed'}]}}}
        raise AssertionError(method)
    session = SimpleNamespace(send=send)
    assert asyncio.run(_owned_application_frames(session, origin, _OWNED_FRAME)) is None


def test_owned_frame_literal_audit_rejects_invalid_isolated_world_result():
    async def send(method, _):
        if method == 'Page.createIsolatedWorld':
            return {'executionContextId': 7}
        return {'result': {'type': 'object'}}
    session = SimpleNamespace(send=send)
    records = ((1, 'child', 'loader', 'http://localhost:9000/renderer.html'),)
    assert asyncio.run(_renderer_literals(session, records,
        EmissionBoundary(ContentPolicy(True)))) is None


def test_owned_frame_contract_must_be_explicit_and_complete():
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1])
    reasons = []
    result = asyncio.run(capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
        'http://localhost:9000', owned_frames=OwnedFrameContract('', '/renderer.html', 'allow-scripts'),
        unavailable=reasons.append))
    assert result is None and reasons == ['frame']


def test_parent_text_audit_uses_one_snapshot_per_pass_and_sees_new_secrets():
    boundary = EmissionBoundary(ContentPolicy(True))
    original = boundary.snapshot_content
    calls = []
    def snapshot():
        calls.append(True)
        return original()
    boundary.snapshot_content = snapshot
    rows = [{'forced': False, 'texts': ['ordinary ' + str(index)]}
            for index in range(100)]
    rows.append({'forced': True, 'texts': ['masked frame-secret-123']})
    assert _sensitive(rows, boundary) is False
    assert len(calls) == 1
    boundary.register_secret('ordinary 55')
    assert _sensitive(rows, boundary) is True
    assert len(calls) == 2


def test_parent_text_audit_deduplicates_only_exact_strings_per_pass():
    boundary = EmissionBoundary(ContentPolicy(True))
    original = boundary.snapshot_content
    exports = []
    class Snapshot:
        def __init__(self, delegate):
            self.delegate = delegate
        def _export(self, payload):
            exports.append(payload['text'])
            return self.delegate._export(payload)
    def snapshot():
        generation, delegate = original()
        return generation, Snapshot(delegate)
    boundary.snapshot_content = snapshot
    rows = [{'forced': False, 'texts': ['ordinary', '{"safe":"value"}', 'ordinary']}
            for _ in range(100)]
    assert _sensitive(rows, boundary) is False
    assert exports == ['ordinary', '{"safe":"value"}']
    exports.clear()
    boundary.register_secret('value')
    assert _sensitive(rows, boundary) is True
    assert exports == ['ordinary', '{"safe":"value"}']


def test_parent_text_audit_rejects_credential_registered_during_export():
    boundary = EmissionBoundary(ContentPolicy(True))
    original = boundary.snapshot_content
    class Snapshot:
        def __init__(self, delegate):
            self.delegate = delegate
        def _export(self, payload):
            clean = self.delegate._export(payload)
            boundary.register_secret('new-secret')
            return clean
    def snapshot():
        generation, delegate = original()
        return generation, Snapshot(delegate)
    boundary.snapshot_content = snapshot
    rows = [{'forced': False, 'texts': ['ordinary', 'new-secret']}]
    assert _sensitive(rows, boundary) is True


def test_provider_requires_running_owned_instance():
    async def run():
        provider = DemoUIProvider({}, {})
        assert await provider.acapture_evidence(page=object(), boundary=object()) is None
        provider.process = object()
        provider.closed = True
        assert await provider.acapture_evidence(page=object(), boundary=object()) is None
    asyncio.run(run())


@pytest.mark.parametrize('enabled,origin,url,frames', [
    (False, 'http://localhost:9000', 'http://localhost:9000/', [1]),
    (True, None, 'http://localhost:9000/', [1]),
    (True, 'http://localhost:9000', 'https://other.invalid/', [1]),
    (True, 'http://localhost:9000', 'http://localhost:9000/login', [1]),
    (True, 'http://localhost:9000', 'http://localhost:9000/', [1, 2]),
])
def test_policy_rejects_unowned_or_disabled_document(enabled, origin, url, frames):
    page = SimpleNamespace(url=url, frames=frames, evaluate=AsyncMock(), screenshot=AsyncMock())
    assert asyncio.run(capture_studio_viewport(page, EmissionBoundary(ContentPolicy(enabled)), origin)) is None
    page.evaluate.assert_not_called()
    page.screenshot.assert_not_called()


@pytest.mark.parametrize('mask_options', [
    {'content_mask_selector': '#conversation'},
    {'content_mask_selector': 7, 'content_mask_anchor': '#panel'},
    {'content_mask_selector': 'x' * 513, 'content_mask_anchor': '#panel'},
    {'content_mask_selector': '#conversation', 'content_mask_anchor': 7},
    {'content_mask_selector': '#conversation', 'content_mask_anchor': 'x' * 257},
    {'content_mask_selector': '#conversation', 'content_mask_anchor': '#panel',
     'content_mask_requirements': []},
    {'content_mask_selector': '#conversation', 'content_mask_anchor': '#panel',
     'content_mask_requirements': (('#panel', '#conversation'),) * 9},
    {'content_mask_selector': '#conversation', 'content_mask_anchor': '#panel',
     'content_mask_requirements': (['#panel', '#conversation'],)},
    {'content_mask_selector': '#conversation', 'content_mask_anchor': '#panel',
     'content_mask_requirements': (('#panel', ''),)},
    {'content_mask_selector': '#conversation', 'content_mask_anchor': '#panel',
     'content_mask_requirements': (('#panel', 'x' * 257),)},
])
def test_invalid_content_mask_contract_rejects_before_browser_access(mask_options):
    class NoBrowserAccessPage:
        @property
        def url(self):
            raise AssertionError('invalid mask reached browser state')

    reasons = []
    assert asyncio.run(capture_studio_viewport(NoBrowserAccessPage(),
        EmissionBoundary(ContentPolicy(False)), 'http://localhost:9000',
        unavailable=reasons.append, **mask_options)) is None
    assert reasons == ['policy']


@pytest.mark.parametrize('rows,verified,expected', [
    (None, True, None),
    ([{'index': 0, 'parent': -1, 'forced': False, 'texts': ['ordinary']}], False, None),
    ([{'index': 0, 'parent': -1, 'forced': False, 'texts': ['ordinary']}], True, b'png'),
])
def test_policy_checks_document_identity_and_cleans_markers(rows, verified, expected):
    handle = SimpleNamespace(evaluate=AsyncMock(return_value=None), dispose=AsyncMock())
    context, session = _mock_cdp()
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1],
        context=context,
        evaluate_handle=AsyncMock(return_value=handle),
        evaluate=AsyncMock(side_effect=[rows, rows if verified else None, None]), screenshot=AsyncMock(return_value=b'png'), locator=Mock())
    assert asyncio.run(capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)), 'http://localhost:9000')) == expected
    assert page.evaluate.await_count == (2 if rows is None else 3)
    assert handle.evaluate.await_count == 2
    handle.dispose.assert_awaited_once()
    session.detach.assert_awaited_once()


def test_policy_rejects_oversized_document_and_cleans_markers():
    handle = SimpleNamespace(evaluate=AsyncMock(return_value=None), dispose=AsyncMock())
    context, session = _mock_cdp()
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1],
        context=context,
        evaluate_handle=AsyncMock(return_value=handle),
        evaluate=AsyncMock(side_effect=[[{}] * 10001, None]), screenshot=AsyncMock())
    assert asyncio.run(capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)), 'http://localhost:9000')) is None
    assert page.evaluate.await_count == 2
    page.screenshot.assert_not_called()
    handle.dispose.assert_awaited_once()
    session.detach.assert_awaited_once()


def test_shadow_audit_unavailable_before_capture_fails_closed():
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1],
        context=SimpleNamespace(new_cdp_session=AsyncMock(side_effect=RuntimeError('synthetic CDP failure'))),
        evaluate_handle=AsyncMock(), screenshot=AsyncMock())
    reasons = []
    assert asyncio.run(capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
        'http://localhost:9000', unavailable=reasons.append)) is None
    assert reasons == ['shadow_audit']
    page.evaluate_handle.assert_not_awaited()
    page.screenshot.assert_not_awaited()


@pytest.mark.parametrize('audits,reason,screenshot_count', [
    ([RuntimeError('synthetic CDP failure')], 'shadow_audit', 0),
    (['closed'], 'closed_shadow', 0),
    (['clear', RuntimeError('synthetic CDP failure')], 'shadow_audit', 1),
    (['clear', 'closed'], 'closed_shadow', 1),
])
def test_shadow_audit_before_and_after_pixels(audits, reason, screenshot_count):
    sequence = iter(audits)
    current = None
    async def send(method, params):
        nonlocal current
        if method == 'DOM.getDocument':
            current = next(sequence)
            if isinstance(current, Exception):
                raise current
            return {'root': {'nodeId': 1}}
        if method == 'DOM.querySelectorAll':
            return {'nodeIds': [2] if current == 'closed' else []}
        if method == 'DOM.describeNode':
            return {'node': {'shadowRoots': [{'shadowRootType': 'closed'}]}}
        raise AssertionError('Unexpected shadow audit request')
    session = SimpleNamespace(send=AsyncMock(side_effect=send), detach=AsyncMock())
    handle = SimpleNamespace(evaluate=AsyncMock(return_value=None), dispose=AsyncMock())
    rows = [{'index': 0, 'parent': -1, 'forced': False, 'texts': ['ordinary']}]
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1],
        context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)),
        evaluate_handle=AsyncMock(return_value=handle), evaluate=AsyncMock(side_effect=[rows, rows, None]),
        screenshot=AsyncMock(return_value=b'png'), locator=Mock())
    reasons = []
    assert asyncio.run(capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
        'http://localhost:9000', unavailable=reasons.append)) is None
    assert reasons == [reason]
    assert page.screenshot.await_count == screenshot_count
    session.detach.assert_awaited_once()


def test_browser_inspection_rejection_still_restores_prepared_page():
    context, session = _mock_cdp()
    handle = SimpleNamespace(evaluate=AsyncMock(return_value=None), dispose=AsyncMock())
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1], context=context,
        evaluate_handle=AsyncMock(return_value=handle),
        evaluate=AsyncMock(side_effect=[{'unavailable': 'isolation'}, None]),
        screenshot=AsyncMock())
    reasons = []
    assert asyncio.run(capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
        'http://localhost:9000', unavailable=reasons.append)) is None
    assert reasons == ['isolation']
    page.screenshot.assert_not_awaited()
    handle.dispose.assert_awaited_once()
    session.detach.assert_awaited_once()


def test_controlled_chromium_masks_original_pixels():
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 600, 'height': 400})
                await page.route('http://localhost:9000/', lambda route: route.fulfill(
                    content_type='text/html', body='''<!doctype html><style>
                    body {margin:0;background:white} div,input {position:absolute;width:180px;height:30px}
                    #plain {top:10px;left:10px;background:lime} #secret {top:60px;left:10px;background:red}
                    input {top:110px;left:10px} #split {top:160px;left:10px;background:red}
                    </style><div id="plain">ordinary answer</div><div id="secret">synthetic-secret</div>
                    <input type="password" value="form-secret"><div id="split"><span>synthetic-</span><span>secret</span></div>'''))
                await page.goto('http://localhost:9000/')
                boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-secret',))
                assert await capture_studio_viewport(page, boundary, 'http://localhost:9000') is None
                await page.locator('#secret, #split').evaluate_all('nodes => nodes.forEach(node => node.remove())')
                pixels = await capture_studio_viewport(page, boundary, 'http://localhost:9000')
                colors = await page.evaluate("""async encoded => {
                    const image = new Image(); image.src = 'data:image/png;base64,' + encoded;
                    await image.decode(); const canvas = document.createElement('canvas');
                    canvas.width = image.width; canvas.height = image.height;
                    const context = canvas.getContext('2d'); context.drawImage(image, 0, 0);
                    return [20,120].map(y => [...context.getImageData(170,y,1,1).data]);
                }""", base64.b64encode(pixels).decode())
                assert colors == [[0,255,0,255], [0,0,0,255]]
                assert await page.locator('input').input_value() == 'form-secret'
                assert await page.locator('#plain').inner_text() == 'ordinary answer'
                assert await page.evaluate("[...document.querySelectorAll('*')].every(node => [...node.attributes].every(attr => !attr.name.startsWith('data-evidence-')))")
                await page.evaluate("document.body.insertAdjacentHTML('beforeend', '<button id=send disabled>Send</button>')")
                assert await capture_studio_viewport(page, boundary, 'http://localhost:9000') is None
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('registration_phase', ['screenshot','cleanup'])
def test_new_registration_during_capture_invalidates_pixels(registration_phase):
    boundary = EmissionBoundary(ContentPolicy(True))
    rows = [{'index':0,'parent':-1,'forced':False,'texts':['late-secret']}]
    async def evaluate(script, arguments):
        if isinstance(arguments, str):
            if registration_phase == 'cleanup': boundary.register_secret('late-secret')
            return None
        return rows
    async def screenshot(**kwargs):
        if registration_phase == 'screenshot': boundary.register_secret('late-secret')
        return b'png'
    async def handle_evaluate(script):
        if registration_phase == 'cleanup' and 'previous' in script:
            boundary.register_secret('late-secret')
        return None
    handle = SimpleNamespace(evaluate=handle_evaluate, dispose=AsyncMock())
    context, session = _mock_cdp()
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1], evaluate=evaluate,
        context=context, evaluate_handle=AsyncMock(return_value=handle), screenshot=screenshot, locator=Mock())
    assert asyncio.run(capture_studio_viewport(page,boundary,'http://localhost:9000')) is None
    handle.dispose.assert_awaited_once()
    session.detach.assert_awaited_once()


@pytest.mark.parametrize('body',['<div></div>'*10001,'<div>'+'x'*1048577+'</div>'], ids=['node-limit', 'text-limit'])
def test_browser_policy_bounds_dom_data_before_export(body):
    async def exercise():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.route('http://localhost:9000/',lambda route:route.fulfill(content_type='text/html',body=body))
                await page.goto('http://localhost:9000/')
                assert await capture_studio_viewport(page,EmissionBoundary(ContentPolicy(True)),'http://localhost:9000') is None
                assert await page.evaluate("[...document.querySelectorAll('*')].every(node => [...node.attributes].every(attr => !attr.name.startsWith('data-evidence-')))")
            finally:await browser.close()
    asyncio.run(exercise())


def test_running_provider_delegates_capture_to_owned_policy(monkeypatch):
    policy = AsyncMock(return_value=b'owned-png')
    monkeypatch.setattr('api_agent.ui.evidence.capture_studio_viewport', policy)
    provider = DemoUIProvider({}, {})
    provider.process = object()
    provider._evidence_origin = 'http://localhost:9000'
    page, boundary = object(), object()
    assert asyncio.run(provider.acapture_evidence(page=page, boundary=boundary)) == b'owned-png'
    policy.assert_awaited_once_with(page, boundary, 'http://localhost:9000',
        content_mask_selector=('.message-body, .reasoning-preview, .reasoning-summary, .error-note, '
            '.response-question > *, #prompt, #reply-context, #prompt-error, #status, #failure-message'),
        content_mask_anchor='#chat-stage',
        content_mask_requirements=(('#chat-stage', '#conversation'),
                                   ('#chat-stage textarea', '#prompt'),
                                   ('.message', '.message-body'),))


def test_browser_content_enabled_preserves_prompt_response_and_only_owned_icons():
    from api_agent.ui.evidence import _TRUSTED_ICONS

    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 600, 'height': 400})
                body = '''<!doctype html><style>
                  body {margin:0;background:white} textarea,input,div {position:absolute;width:180px;height:30px}
                  textarea {top:10px;left:10px;background:lime} #answer {top:60px;left:10px;background:cyan}
                  input {top:110px;left:10px;background:red} #owned {top:160px;left:10px;color:red}
                  #untrusted {position:absolute;top:210px;left:10px}
                  </style><textarea id="prompt">ordinary prompt</textarea><div id="answer">ordinary response</div>
                  <input autocomplete="current-password" value="synthetic-credential">
                  <div id="owned">''' + _TRUSTED_ICONS[0] + '''</div>
                  <svg id="untrusted" width="22" height="22"><rect width="22" height="22" fill="red"/></svg>'''
                await page.route('http://localhost:9000/', lambda route: route.fulfill(content_type='text/html', body=body))
                await page.goto('http://localhost:9000/')
                boundary = EmissionBoundary(ContentPolicy(True), secrets=('synthetic-credential',))
                original = await _painted_baseline(page)
                pixels = await capture_studio_viewport(page, boundary, 'http://localhost:9000')
                assert pixels is not None
                unchanged = await page.evaluate('''async images => {
                  const contexts = await Promise.all(images.map(async encoded => {
                    const image = new Image(); image.src='data:image/png;base64,'+encoded; await image.decode();
                    const canvas=document.createElement('canvas'); canvas.width=image.width; canvas.height=image.height;
                    const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);return ctx;
                  }));
                  return [[10,10,180,30],[10,60,180,30],[10,160,22,22]].map(region => {
                    const data=contexts.map(ctx=>ctx.getImageData(...region).data);
                    return data[0].every((value,index)=>value===data[1][index]);
                  });
                }''', [base64.b64encode(image).decode() for image in (original,pixels)])
                assert unchanged == [True,True,True]
                colors = await page.evaluate('''async encoded => {
                  const image = new Image(); image.src = 'data:image/png;base64,' + encoded; await image.decode();
                  const canvas = document.createElement('canvas'); canvas.width=image.width;canvas.height=image.height;
                  const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);
                  return [[170,20],[170,70],[170,120],[21,171],[21,221]].map(([x,y])=>[...ctx.getImageData(x,y,1,1).data]);
                }''', base64.b64encode(pixels).decode())
                assert colors[:3] == [[0,255,0,255],[0,255,255,255],[0,0,0,255]]
                assert colors[3][0] > 150 and colors[3][1] < 100
                assert colors[4] == [0,0,0,255]
                # Values set through the DOM property are not reflected in attributes.
                await page.locator('#prompt').fill('synthetic-credential')
                assert await capture_studio_viewport(page, boundary, 'http://localhost:9000') is None
                await page.locator('#prompt').fill('ordinary prompt')
                await page.locator('input').evaluate("node => { node.autocomplete='off'; node.value='synthetic-credential'; node.removeAttribute('value'); }")
                assert await capture_studio_viewport(page, boundary, 'http://localhost:9000') is None
            finally:
                await browser.close()
    asyncio.run(run())


def test_plain_static_button_path_icon_remains_visible_but_unsupported_svg_is_masked():
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 240, 'height': 120})
                body = '''<!doctype html><style>
                  body {margin:0;background:white}
                  button {position:absolute;top:20px;width:42px;height:42px;background:lime;border:0}
                  #plain {left:20px;color:red} #unsafe {left:110px}
                  </style>
                  <button id="plain" type="button" aria-label="Expand panel">
                    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                      <path d="M2 2L18 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                    </svg>
                  </button>
                  <button id="unsafe" type="button" aria-label="Other action">
                    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
                      <foreignObject width="20" height="20"><div>unknown</div></foreignObject>
                    </svg>
                  </button>'''
                await page.route('http://localhost:9000/',
                    lambda route: route.fulfill(content_type='text/html', body=body))
                await page.goto('http://localhost:9000/')
                reasons = []
                pixels = await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(True)), 'http://localhost:9000',
                    unavailable=reasons.append)
                assert pixels is not None, reasons
                assert (await _region_pixels(page, pixels,
                    {'x': 24, 'y': 24, 'width': 34, 'height': 34}))['red']
                assert (await _region_pixels(page, pixels,
                    {'x': 122, 'y': 32, 'width': 18, 'height': 18}))['black']
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['screenshot', 'cleanup', 'document-change'])
def test_browser_capture_races_fail_closed(phase):
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('http://localhost:9000/', lambda route: route.fulfill(
                    content_type='text/html', body='<div id="answer">newly-sensitive-value</div>'))
                await page.goto('http://localhost:9000/')
                boundary = EmissionBoundary(ContentPolicy(True))
                async def screenshot(**kwargs):
                    pixels = await page.screenshot(**kwargs)
                    if phase == 'screenshot':
                        boundary.register_secret('newly-sensitive-value')
                    if phase == 'document-change':
                        await page.locator('#answer').evaluate("node => node.textContent = 'changed value'")
                    return pixels
                async def evaluate(script, arguments):
                    result = await page.evaluate(script, arguments)
                    if phase == 'cleanup' and isinstance(arguments, str):
                        boundary.register_secret('newly-sensitive-value')
                    return result
                proxy = SimpleNamespace(url=page.url, frames=page.frames, screenshot=screenshot,
                                        evaluate=evaluate, evaluate_handle=page.evaluate_handle,
                                        locator=page.locator, context=_proxied_context(page))
                assert await capture_studio_viewport(proxy, boundary, 'http://localhost:9000') is None
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('body,setup',[
    ('<input id="left"><input id="right">',"left.value='review-';right.value='credential'"),
    ('<input id="left"><span>credential</span>',"left.value='review-'"),
    ('<span>review-</span><input id="right">',"right.value='credential'"),
])
def test_credentials_split_across_live_form_values_and_text_are_withheld(body,setup):
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.route('http://localhost:9000/',lambda route:route.fulfill(content_type='text/html',body=body))
                await page.goto('http://localhost:9000/')
                await page.evaluate(setup)
                boundary=EmissionBoundary(ContentPolicy(True),secrets=('review-credential',))
                assert await capture_studio_viewport(page,boundary,'http://localhost:9000') is None
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('label,masked',[('API key',True),('OTP',True),('Token count',False)])
def test_authentication_labels_are_masked_without_hiding_ordinary_token_counts(label,masked):
    async def run():
        import base64
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser=await driver.chromium.launch(headless=True)
            try:
                page=await browser.new_page(viewport={'width':300,'height':100})
                body='<style>input{position:absolute;top:30px;left:10px;width:180px;height:30px;background:lime}</style><label>'+label+'<input id="field"></label>'
                await page.route('http://localhost:9000/',lambda route:route.fulfill(content_type='text/html',body=body))
                await page.goto('http://localhost:9000/')
                await page.locator('input').fill('sample value')
                image=await capture_studio_viewport(page,EmissionBoundary(ContentPolicy(True)),'http://localhost:9000')
                assert image is not None
                color=await page.evaluate('''async encoded=>{
                  const image=new Image();image.src='data:image/png;base64,'+encoded;await image.decode();
                  const canvas=document.createElement('canvas');canvas.width=300;canvas.height=100;
                  const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);return [...ctx.getImageData(170,40,1,1).data];
                }''',base64.b64encode(image).decode())
                assert color==([0,0,0,255] if masked else [0,255,0,255])
            finally:
                await browser.close()
    asyncio.run(run())


def test_nx_sized_inert_bundle_keeps_visible_pixels_and_masks_frame():
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 600, 'height': 300})
                body = ('<html><body style="background:white;color:black;font:24px Arial">'
                    '<div>Visible answer</div><script type="application/json">'
                    + 'x' * 400_000 + '</script>'
                    '<iframe src="about:blank" style="width:100px;height:40px"></iframe>'
                    '</body></html>')
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body=body))
                await page.goto(origin + '/da2/Home')
                image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True)
                assert image is not None and image.startswith(b'\x89PNG\r\n\x1a\n')
                frame = await page.locator('iframe').bounding_box()
                result = await page.evaluate('''async ({png, frame}) => {
                  const image = new Image(); image.src = 'data:image/png;base64,' + png;
                  await image.decode(); const canvas = document.createElement('canvas');
                  canvas.width = image.width; canvas.height = image.height;
                  const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
                  const pixel = (x,y) => [...ctx.getImageData(x,y,1,1).data].slice(0,3);
                  return {text: Array.from({length:250}, (_,x) => pixel(x,20))
                    .some(rgb => rgb.some(channel => channel < 200)),
                    frame: pixel(Math.floor(frame.x+frame.width/2),Math.floor(frame.y+frame.height/2))};
                }''', {'png': base64.b64encode(image).decode(), 'frame': frame})
                assert result == {'text': True, 'frame': [0, 0, 0]}
            finally:
                await browser.close()
    asyncio.run(run())


def test_nx_viewport_keeps_split_secret_and_unique_size_limit():
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        body = ['<html><body><span>alpha</span><span>omega</span></body></html>']
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body=body[0]))
                await page.goto(origin + '/da2/Home')
                assert await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(True), secrets=('alphaomega',)),
                    origin, path='/da2/Home', allow_frames=True) is None
                body[0] = ('<html><body><script type="application/json">'
                    + 'x' * 1_100_000 + '</script></body></html>')
                await page.reload()
                assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True) is not None
                body[0] = ('<html><body><span>' + 'x' * 1_100_000 + '</span></body></html>')
                await page.reload()
                assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True) is None
            finally:
                await browser.close()
    asyncio.run(run())


def test_nx_viewport_rejects_registered_secret_in_css_image():
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        css = ('#card{width:300px;height:80px;background-image:url("data:image/svg+xml,'
            '%3Csvg xmlns=%22http://www.w3.org/2000/svg%22%3E%3Ctext x=%2210%22 y=%2230%22%3E'
            'secret-marker%3C/text%3E%3C/svg%3E")}')
        body = ['<html><head><style>' + css +
            '</style></head><body><div id="card"></div></body></html>']
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 500, 'height': 200})
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body=body[0]))
                await page.goto(origin + '/da2/Home')
                masked = await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(True), secrets=('secret-marker',)),
                    origin, path='/da2/Home', allow_frames=True)
                assert masked is not None
                assert (await _region_pixels(page, masked,
                    {'x': 16, 'y': 16, 'width': 280, 'height': 60}))['black']
                body[0] = ('<html><head><style>' + css.replace('secret-marker', 'ordinary-mark') +
                    '</style></head><body><div id="card"></div>'
                    '<p id="adjacent">Visible adjacent text</p></body></html>')
                await page.reload()
                reasons = []
                masked = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True,
                    unavailable=reasons.append)
                assert masked is not None and reasons == []
                assert (await _region_pixels(page, masked,
                    {'x': 16, 'y': 16, 'width': 280, 'height': 60}))['black']
                body[0] = ('<html><body><div id="card"></div>'
                    '<p id="adjacent">Visible adjacent text</p></body></html>')
                await page.reload()
                image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True)
                assert image is not None
                adjacent = await page.locator('#adjacent').bounding_box()
                visible = await page.evaluate('''async ({png,adjacent}) => {
                  const image = new Image(); image.src = 'data:image/png;base64,' + png;
                  await image.decode(); const canvas = document.createElement('canvas');
                  canvas.width=image.width; canvas.height=image.height;
                  const ctx=canvas.getContext('2d'); ctx.drawImage(image,0,0);
                  const pixel=(x,y)=>[...ctx.getImageData(x,y,1,1).data].slice(0,3);
                  return Array.from({length:Math.floor(adjacent.width)},(_,dx)=>
                      pixel(Math.floor(adjacent.x+dx),Math.floor(adjacent.y+adjacent.height/2)))
                      .some(rgb=>rgb.some(channel=>channel<200));
                }''', {'png':base64.b64encode(image).decode(), 'adjacent':adjacent})
                assert visible is True
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('pseudo_surface', ['background', 'content'])
@pytest.mark.parametrize('pseudo', ['before', 'after'])
def test_nx_viewport_withholds_pseudo_url_outside_host(pseudo, pseudo_surface):
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        encoded = ''.join(f'%{ord(char):02X}' for char in 'secret-marker')
        svg = ('data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 '
            'width=%22120%22 height=%2250%22%3E%3Crect width=%22120%22 '
            'height=%2250%22 fill=%22red%22/%3E%3Ctext x=%2210%22 y=%2230%22%3E'
            + encoded + '%3C/text%3E%3C/svg%3E')
        drawing = ('content:"";width:120px;height:50px;background-image:url("' + svg + '")'
            if pseudo_surface == 'background' else 'content:url("' + svg + '")')
        css = ('#host{position:relative;width:20px;height:20px}'
            '#host::' + pseudo + '{position:absolute;left:300px;top:20px;' + drawing + '}')
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 500, 'height': 200})
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='<html><head><style>' + css +
                    '</style></head><body><div id="host"></div></body></html>'))
                await page.goto(origin + '/da2/Home')
                assert await page.evaluate('''() => {
                    const pseudo=getComputedStyle(document.querySelector('#host'),'::%s');
                    return pseudo.content.includes('url(') || pseudo.backgroundImage.includes('url(');
                }''' % pseudo)
                escaped = {'x': 308, 'y': 28, 'width': 112, 'height': 46}
                raw = await _painted_baseline(page)
                assert (await _region_pixels(page, raw, escaped))['ink']
                masked = await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(True), secrets=('secret-marker',)),
                    origin, path='/da2/Home', allow_frames=True)
                assert masked is not None
                assert not (await _region_pixels(page, masked, escaped))['ink']
            finally:
                await browser.close()
    asyncio.run(run())


def test_nx_viewport_withholds_border_image_outside_host():
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        encoded = ''.join(f'%{ord(char):02X}' for char in 'secret-marker')
        svg = ('data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 '
            'width=%2240%22 height=%2240%22%3E%3Crect width=%2240%22 height=%2240%22 '
            'fill=%22red%22/%3E%3Ctext x=%222%22 y=%2220%22%3E' + encoded +
            '%3C/text%3E%3C/svg%3E')
        css = ('body{margin:0;background:white}'
            '#host{position:absolute;left:200px;top:120px;width:40px;height:40px;'
            'border:1px solid transparent;border-image-source:url("' + svg + '");'
            'border-image-slice:30 fill;border-image-outset:80px}')
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 500, 'height': 300})
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='<html><head><style>' + css +
                    '</style></head><body><div id="host"></div></body></html>'))
                await page.goto(origin + '/da2/Home')
                assert await page.evaluate("() => getComputedStyle(document.querySelector('#host')).borderImageSource.includes('url(')")
                raw = await _painted_baseline(page)
                outside = await page.evaluate('''async png => {
                  const image=new Image(); image.src='data:image/png;base64,'+png;
                  await image.decode(); const canvas=document.createElement('canvas');
                  canvas.width=image.width; canvas.height=image.height;
                  const ctx=canvas.getContext('2d'); ctx.drawImage(image,0,0);
                  return [...ctx.getImageData(120,40,1,1).data].slice(0,3);
                }''', base64.b64encode(raw).decode())
                assert outside == [255, 0, 0]
                masked = await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(True), secrets=('secret-marker',)),
                    origin, path='/da2/Home', allow_frames=True)
                assert masked is not None
                assert not (await _region_pixels(page, masked,
                    {'x': 105, 'y': 25, 'width': 85, 'height': 85}))['red']
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('escape_style', [
    'filter:drop-shadow(200px 0 0 red)',
    '-webkit-box-reflect:right 20px',
])
def test_nx_viewport_withholds_css_image_reflection_outside_host(escape_style):
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        encoded = ''.join(f'%{ord(char):02X}' for char in 'secret-marker')
        svg = ('data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 '
            'width=%22120%22 height=%2240%22%3E%3Ctext x=%222%22 y=%2228%22 '
            'font-size=%2220%22 fill=%22black%22%3E' + encoded + '%3C/text%3E%3C/svg%3E')
        css = ('body{margin:0;background:white}#host{position:absolute;left:40px;top:40px;'
            'width:120px;height:40px;background-image:url("' + svg + '");' + escape_style + '}')
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 500, 'height': 160})
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='<html><head><style>' + css +
                    '</style></head><body><div id="host"></div></body></html>'))
                await page.goto(origin + '/da2/Home')
                assert await page.evaluate("() => getComputedStyle(document.querySelector('#host')).backgroundImage.includes('url(')")
                escaped = ({'x': 240, 'y': 40, 'width': 120, 'height': 40}
                    if escape_style.startswith('filter:') else
                    {'x': 180, 'y': 40, 'width': 120, 'height': 40})
                raw = await _painted_baseline(page)
                assert (await _region_pixels(page, raw, escaped))['ink']
                reasons = []
                masked = await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(True), secrets=('secret-marker',)),
                    origin, path='/da2/Home', allow_frames=True,
                    unavailable=reasons.append)
                if masked is not None:
                    assert reasons == []
                    assert not (await _region_pixels(page, masked, escaped))['ink']
                else:
                    assert reasons == ['css_effect']
            finally:
                await browser.close()
    asyncio.run(run())


def test_top_layer_backdrop_url_withholds_independent_pixels():
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 300, 'height': 120})
                encoded = ''.join(f'%{ord(char):02X}' for char in 'backdrop-secret')
                svg = ('data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 '
                    'width=%2250%22 height=%2250%22%3E%3Crect width=%2250%22 height=%2250%22 '
                    'fill=%22red%22/%3E%3Ctext x=%222%22 y=%2220%22%3E' + encoded +
                    '%3C/text%3E%3C/svg%3E')
                body = '<style>dialog::backdrop{background-image:url("' + svg + '")}</style><dialog>ordinary</dialog>'
                await page.route('http://localhost:9000/', lambda route: route.fulfill(
                    content_type='text/html', body=body))
                await page.goto('http://localhost:9000/')
                await page.locator('dialog').evaluate('node => node.showModal()')
                assert (await _region_pixels(page, await _painted_baseline(page),
                    {'x': 2, 'y': 2, 'width': 40, 'height': 40}))['red']
                reasons = []
                assert await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(True), secrets=('backdrop-secret',)),
                    'http://localhost:9000', unavailable=reasons.append) is None
                assert reasons == ['backdrop']
            finally:
                await browser.close()
    asyncio.run(run())


def test_registered_shadow_host_with_display_contents_fails_closed():
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('http://localhost:9000/', lambda route: route.fulfill(
                    content_type='text/html', body='''<style>x-secret{display:contents}</style>
                    <x-secret></x-secret><script>
                    customElements.define('x-secret', class extends HTMLElement {
                      constructor() { super(); this.attachShadow({mode:'open'}).innerHTML =
                        '<span>shadow-secret</span>'; }
                    });</script>'''))
                await page.goto('http://localhost:9000/')
                assert await page.locator('x-secret span').inner_text() == 'shadow-secret'
                reasons = []
                assert await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(True), secrets=('shadow-secret',)),
                    'http://localhost:9000', unavailable=reasons.append) is None
                assert reasons == ['display_contents']
            finally:
                await browser.close()
    asyncio.run(run())


def test_closed_shadow_host_with_display_contents_fails_closed():
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 300, 'height': 100})
                await page.route('http://localhost:9000/', lambda route: route.fulfill(
                    content_type='text/html', body='''<style>x-secret{display:contents}</style>
                    <x-secret></x-secret><script>
                    customElements.define('x-secret', class extends HTMLElement {
                      constructor() { super(); this.attachShadow({mode:'closed'}).innerHTML =
                        '<span style="background:red">closed-shadow-secret</span>'; }
                    });</script>'''))
                await page.goto('http://localhost:9000/')
                assert await page.evaluate("document.querySelector('x-secret').shadowRoot === null")
                raw = await _painted_baseline(page)
                assert (await _region_pixels(page, raw,
                    {'x': 8, 'y': 8, 'width': 200, 'height': 24}))['red']
                reasons = []
                assert await capture_studio_viewport(page,
                    EmissionBoundary(ContentPolicy(True), secrets=('closed-shadow-secret',)),
                    'http://localhost:9000', unavailable=reasons.append) is None
                assert reasons == ['closed_shadow']
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('host', ['unregistered', 'nested-open-closed'])
def test_closed_shadow_audit_covers_unregistered_and_nested_hosts(host):
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 300, 'height': 100})
                if host == 'unregistered':
                    body = '''<x-secret style="display:contents"></x-secret><script>
                      document.querySelector('x-secret').attachShadow({mode:'closed'}).innerHTML =
                        '<span style="background:red">closed-secret</span>';
                    </script>'''
                else:
                    body = '''<x-open></x-open><span id="adjacent">ordinary answer</span><script>
                      const root = document.querySelector('x-open').attachShadow({mode:'open'});
                      root.innerHTML = '<x-secret style="display:contents"></x-secret>';
                      root.querySelector('x-secret').attachShadow({mode:'closed'}).innerHTML =
                        '<span style="background:red">closed-secret</span>';
                    </script>'''
                await page.route('http://localhost:9000/', lambda route: route.fulfill(
                    content_type='text/html', body=body))
                await page.goto('http://localhost:9000/')
                assert (await _region_pixels(page, await _painted_baseline(page),
                    {'x': 8, 'y': 8, 'width': 200, 'height': 24}))['red']
                reasons = []
                captured = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    'http://localhost:9000', unavailable=reasons.append)
                if host == 'unregistered':
                    assert captured is None and reasons == ['closed_shadow']
                else:
                    assert captured is not None and reasons == []
                    secret_region = {'x': 8, 'y': 8, 'width': 200, 'height': 24}
                    assert not (await _region_pixels(page, captured, secret_region))['red']
                    adjacent = await page.locator('#adjacent').bounding_box()
                    assert (await _region_pixels(page, captured, {
                        'x': int(adjacent['x']), 'y': int(adjacent['y']),
                        'width': max(1, int(adjacent['width'])),
                        'height': max(1, int(adjacent['height']))}))['ink']
            finally:
                await browser.close()
    asyncio.run(run())


def test_visible_inert_megabyte_is_isolated_before_budget_exclusion():
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 300, 'height': 100})
                body = ('''<style>body{margin:0}script{display:block;background:red;
                    width:200px;height:30px;overflow:hidden}</style><body><script type="application/json">'''
                    + 'x' * 1_100_000 + '</script><p>ordinary answer</p>')
                await page.route('http://localhost:9000/', lambda route: route.fulfill(
                    content_type='text/html', body=body))
                await page.goto('http://localhost:9000/')
                raw = await _painted_baseline(page)
                assert (await _region_pixels(page, raw,
                    {'x': 10, 'y': 5, 'width': 180, 'height': 20}))['red']
                image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    'http://localhost:9000')
                assert image is not None
                assert (await _region_pixels(page, image,
                    {'x': 10, 'y': 5, 'width': 180, 'height': 20}))['black']
                assert (await _region_pixels(page, image,
                    {'x': 10, 'y': 40, 'width': 180, 'height': 25}))['ink']
            finally:
                await browser.close()
    asyncio.run(run())


def test_prepare_failure_restores_earlier_isolation_without_screenshot():
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('http://localhost:9000/', lambda route: route.fulfill(
                    content_type='text/html', body='''<img id="first" style="color:red" src="data:,one">
                    <img id="second" src="data:,two">'''))
                await page.goto('http://localhost:9000/')
                original = await page.locator('#first').get_attribute('style')
                await page.evaluate('''() => {
                  const original = window.getComputedStyle;
                  window.getComputedStyle = function(node, pseudo) {
                    if (node.id === 'second' && !pseudo) throw new Error('synthetic preparation failure');
                    return original.call(this, node, pseudo);
                  };
                }''')
                screenshot = AsyncMock()
                proxy = SimpleNamespace(url=page.url, frames=page.frames,
                    evaluate=page.evaluate, evaluate_handle=page.evaluate_handle,
                    screenshot=screenshot, locator=page.locator, context=_proxied_context(page))
                reasons = []
                assert await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(True)),
                    'http://localhost:9000', unavailable=reasons.append) is None
                assert reasons == ['prepare_error']
                screenshot.assert_not_awaited()
                assert await page.locator('#first').get_attribute('style') == original
            finally:
                await browser.close()
    asyncio.run(run())


def test_app_style_mutation_during_capture_survives_isolation_cleanup():
    async def run():
        from playwright.async_api import async_playwright
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route('http://localhost:9000/', lambda route: route.fulfill(
                    content_type='text/html', body='<img id="card" style="color:red" src="data:,one">'))
                await page.goto('http://localhost:9000/')

                async def screenshot(**kwargs):
                    image = await page.screenshot(**kwargs)
                    await page.locator('#card').evaluate("node => node.style.color = 'blue'")
                    return image

                proxy = SimpleNamespace(url=page.url, frames=page.frames,
                    evaluate=page.evaluate, evaluate_handle=page.evaluate_handle,
                    screenshot=screenshot, locator=page.locator, context=_proxied_context(page))
                assert await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(True)),
                    'http://localhost:9000') is None
                style = await page.locator('#card').evaluate('''node => ({
                  inline:node.getAttribute('style'), color:node.style.color,
                  opacity:node.style.opacity, contentVisibility:node.style.contentVisibility,
                  animation:node.style.animation, transition:node.style.transition
                })''')
                assert style == {'inline': 'color: blue;', 'color': 'blue', 'opacity': '',
                    'contentVisibility': '', 'animation': '', 'transition': ''}
            finally:
                await browser.close()
    asyncio.run(run())


def test_owned_application_frame_keeps_ordinary_answer_pixels():
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 600, 'height': 300})
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='''<body style="background:white">
                    <p>Application answer</p><iframe data-testid="chat-message-html"
                    sandbox="allow-scripts" src="/assets/agent-html-runtime/renderer.html"
                    style="width:350px;height:120px;border:0"></iframe></body>'''))
                await page.route(origin + _OWNED_FRAME.path, lambda route: route.fulfill(
                    content_type='text/html', body='''<body style="background:white">
                    <div style="font:24px Arial;color:black">Rendered answer</div></body>'''))
                await page.goto(origin + '/da2/Home')
                await page.frame_locator(_OWNED_FRAME.selector).locator('div').wait_for()
                box = await page.locator(_OWNED_FRAME.selector).bounding_box()
                stages = []
                image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True, owned_frames=_OWNED_FRAME,
                    stage=lambda name, elapsed: stages.append((name, elapsed)))
                assert image is not None and image.startswith(b'\x89PNG\r\n\x1a\n')
                assert [name for name, _ in stages] == [
                    'frame_admitted', 'frame_literals', 'parent_audit', 'prepared',
                    'inspected', 'screenshot', 'verified', 'parent_reaudit', 'frame_rechecked',
                    'cleanup']
                assert all(type(elapsed) is int and elapsed >= 0 for _, elapsed in stages)
                assert [elapsed for _, elapsed in stages] == sorted(elapsed for _, elapsed in stages)
                pixels = await _region_pixels(page, image,
                    {'x': int(box['x'] + 4), 'y': int(box['y'] + 4),
                     'width': 240, 'height': 40})
                assert pixels['ink'] and not pixels['black']
            finally:
                await browser.close()
    asyncio.run(run())


def test_registered_light_dom_host_keeps_frame_pixels_and_checks_secret_child():
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width': 600, 'height': 300})
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='''<body style="background:white">
                    <script>customElements.define('light-card', class extends HTMLElement {})</script>
                    <light-card style="display:block"><iframe data-testid="chat-message-html"
                    sandbox="allow-scripts" src="/assets/agent-html-runtime/renderer.html"
                    style="width:350px;height:120px;border:0"></iframe>
                    <span id="child">ordinary child</span></light-card></body>'''))
                await page.route(origin + _OWNED_FRAME.path, lambda route: route.fulfill(
                    content_type='text/html', body='''<body style="background:white">
                    <div style="font:24px Arial;color:black">Rendered answer</div></body>'''))
                await page.goto(origin + '/da2/Home')
                await page.frame_locator(_OWNED_FRAME.selector).locator('div').wait_for()
                box = await page.locator(_OWNED_FRAME.selector).bounding_box()
                boundary = EmissionBoundary(ContentPolicy(True), secrets=('secret-marker',))
                image = await capture_studio_viewport(page, boundary, origin,
                    path='/da2/Home', allow_frames=True, owned_frames=_OWNED_FRAME)
                assert image is not None
                pixels = await _region_pixels(page, image,
                    {'x': int(box['x'] + 4), 'y': int(box['y'] + 4),
                     'width': 240, 'height': 40})
                assert pixels['ink'] and not pixels['black']
                await page.locator('#child').evaluate(
                    "node => node.textContent = 'secret-marker'")
                assert await capture_studio_viewport(page, boundary, origin,
                    path='/da2/Home', allow_frames=True, owned_frames=_OWNED_FRAME) is None
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('frame_html', [
    '<iframe data-testid="other" sandbox="allow-scripts" src="/assets/agent-html-runtime/renderer.html"></iframe>',
    '<iframe data-testid="chat-message-html" sandbox="allow-same-origin allow-scripts" src="/assets/agent-html-runtime/renderer.html"></iframe>',
    '<iframe data-testid="chat-message-html" sandbox="allow-scripts" src="/assets/agent-html-runtime/renderer.html?extra=1"></iframe>',
    '<iframe data-testid="chat-message-html" sandbox="allow-scripts" src="/assets/agent-html-runtime/renderer.html"></iframe><iframe src="about:blank"></iframe>',
])
def test_owned_application_frame_rejects_unknown_document(frame_html):
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='<body>Visible application' + frame_html + '</body>'))
                await page.route(origin + '/assets/agent-html-runtime/renderer.html*',
                    lambda route: route.fulfill(content_type='text/html', body='Ordinary answer'))
                await page.goto(origin + '/da2/Home')
                assert await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True, owned_frames=_OWNED_FRAME) is None
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('frame_body', ['Visible answer: frame-secret-123',
    'Visible answer: frame%2Dsecret%2D123'])
def test_owned_application_frame_rejects_registered_literal_secret(frame_body):
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='''<body>Visible application
                    <iframe data-testid="chat-message-html" sandbox="allow-scripts"
                    src="/assets/agent-html-runtime/renderer.html"></iframe></body>'''))
                await page.route(origin + _OWNED_FRAME.path, lambda route: route.fulfill(
                    content_type='text/html', body='<body>' + frame_body + '</body>'))
                await page.goto(origin + '/da2/Home')
                await page.frame_locator(_OWNED_FRAME.selector).locator('body').wait_for()
                boundary = EmissionBoundary(ContentPolicy(True))
                boundary.register_secret('frame-secret-123')
                reasons = []
                assert await capture_studio_viewport(page, boundary, origin, path='/da2/Home',
                    allow_frames=True, owned_frames=_OWNED_FRAME, unavailable=reasons.append) is None
                assert reasons == ['frame_content']
            finally:
                await browser.close()
    asyncio.run(run())


def test_owned_application_frame_navigation_during_capture_invalidates_pixels():
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='''<body>Visible application
                    <iframe data-testid="chat-message-html" sandbox="allow-scripts"
                    src="/assets/agent-html-runtime/renderer.html"></iframe></body>'''))
                await page.route(origin + _OWNED_FRAME.path, lambda route: route.fulfill(
                    content_type='text/html', body='Ordinary answer'))
                await page.route(origin + '/unexpected', lambda route: route.fulfill(
                    content_type='text/html', body='Other document'))
                await page.goto(origin + '/da2/Home')
                await page.frame_locator(_OWNED_FRAME.selector).locator('body').wait_for()

                async def screenshot(**kwargs):
                    image = await page.screenshot(**kwargs)
                    await page.frames[1].goto(origin + '/unexpected')
                    return image

                proxy = SimpleNamespace(url=page.url, frames=page.frames,
                    evaluate=page.evaluate, evaluate_handle=page.evaluate_handle,
                    screenshot=screenshot, locator=page.locator, context=_proxied_context(page))
                reasons = []
                assert await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True, owned_frames=_OWNED_FRAME,
                    unavailable=reasons.append) is None
                assert reasons == ['frame_changed']
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure_call', [1, 2])
def test_owned_application_frame_cdp_failure_withholds_pixels(failure_call):
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='''<body>Visible application
                    <iframe data-testid="chat-message-html" sandbox="allow-scripts"
                    src="/assets/agent-html-runtime/renderer.html"></iframe></body>'''))
                await page.route(origin + _OWNED_FRAME.path, lambda route: route.fulfill(
                    content_type='text/html', body='Ordinary answer'))
                await page.goto(origin + '/da2/Home')
                await page.frame_locator(_OWNED_FRAME.selector).locator('body').wait_for()
                session = await page.context.new_cdp_session(page)
                calls = 0
                async def send(method, params=None):
                    nonlocal calls
                    if method == 'Page.getFrameTree':
                        calls += 1
                        if calls == failure_call:
                            raise RuntimeError('synthetic CDP inspection failure')
                    return await session.send(method, params or {})
                proxy = SimpleNamespace(url=page.url, frames=page.frames,
                    evaluate=page.evaluate, evaluate_handle=page.evaluate_handle,
                    screenshot=page.screenshot, locator=page.locator,
                    context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=
                        SimpleNamespace(send=send, detach=session.detach))))
                reasons = []
                assert await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True, owned_frames=_OWNED_FRAME,
                    unavailable=reasons.append) is None
                assert reasons == ['frame_inspection'] and calls == failure_call
            finally:
                await browser.close()
    asyncio.run(run())


def test_owned_application_frame_new_secret_during_capture_invalidates_pixels():
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='''<body>Visible application
                    <iframe data-testid="chat-message-html" sandbox="allow-scripts"
                    src="/assets/agent-html-runtime/renderer.html"></iframe></body>'''))
                await page.route(origin + _OWNED_FRAME.path, lambda route: route.fulfill(
                    content_type='text/html', body='Ordinary frame-token-123 answer'))
                await page.goto(origin + '/da2/Home')
                await page.frame_locator(_OWNED_FRAME.selector).locator('body').wait_for()
                boundary = EmissionBoundary(ContentPolicy(True))
                async def evaluate(expression, argument=None):
                    if isinstance(expression, str) and 'removeAttribute(marker)' in expression:
                        boundary.register_secret('frame-token-123')
                    return await page.evaluate(expression, argument)
                proxy = SimpleNamespace(url=page.url, frames=page.frames,
                    evaluate=evaluate, evaluate_handle=page.evaluate_handle,
                    screenshot=page.screenshot, locator=page.locator, context=_proxied_context(page))
                reasons = []
                assert await capture_studio_viewport(proxy, boundary, origin, path='/da2/Home',
                    allow_frames=True, owned_frames=_OWNED_FRAME, unavailable=reasons.append) is None
                assert reasons == ['registered_secret']
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('field,accepted', [
    ('<label>Search <input value="ordinary query"></label><select><option>North</option></select>', True),
    ('<label>API token <input name="api_token" value="credential-value"></label>', False),
])
def test_owned_application_frame_admits_ordinary_controls_but_rejects_credentials(field, accepted):
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='''<body>Visible application
                    <iframe data-testid="chat-message-html" sandbox="allow-scripts"
                    src="/assets/agent-html-runtime/renderer.html"></iframe></body>'''))
                await page.route(origin + _OWNED_FRAME.path, lambda route: route.fulfill(
                    content_type='text/html', body='<body>' + field + '</body>'))
                await page.goto(origin + '/da2/Home')
                await page.frame_locator(_OWNED_FRAME.selector).locator('input').wait_for()
                reasons = []
                image = await capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)),
                    origin, path='/da2/Home', allow_frames=True, owned_frames=_OWNED_FRAME,
                    unavailable=reasons.append)
                if accepted:
                    assert image is not None and image.startswith(b'\x89PNG\r\n\x1a\n')
                    assert not reasons
                else:
                    assert image is None and reasons == ['frame_content']
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('attack', ['retroactive_sandbox', 'main_world_patch'])
def test_owned_application_frame_rejects_untrusted_loaded_content(attack):
    async def run():
        from playwright.async_api import async_playwright
        origin = 'http://localhost:9000'
        sandbox = '' if attack == 'retroactive_sandbox' else 'sandbox="allow-scripts"'
        frame_body = '<body>Visible frame-secret-123 answer</body>'
        if attack == 'main_world_patch':
            frame_body += '''<script>
              Object.defineProperty(document.documentElement, 'textContent',
                {get: () => ''});
              Element.prototype.querySelectorAll = () => [];
            </script>'''
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(origin + '/da2/Home', lambda route: route.fulfill(
                    content_type='text/html', body='''<body>Visible application
                    <iframe data-testid="chat-message-html" ''' + sandbox + '''
                    src="/assets/agent-html-runtime/renderer.html"></iframe></body>'''))
                await page.route(origin + _OWNED_FRAME.path, lambda route: route.fulfill(
                    content_type='text/html', body=frame_body))
                await page.goto(origin + '/da2/Home')
                await page.frame_locator(_OWNED_FRAME.selector).locator('body').wait_for()
                if attack == 'retroactive_sandbox':
                    await page.locator(_OWNED_FRAME.selector).evaluate(
                        "node => node.setAttribute('sandbox', 'allow-scripts')")
                boundary = EmissionBoundary(ContentPolicy(True))
                boundary.register_secret('frame-secret-123')
                reasons = []
                assert await capture_studio_viewport(page, boundary, origin, path='/da2/Home',
                    allow_frames=True, owned_frames=_OWNED_FRAME,
                    unavailable=reasons.append) is None
                assert reasons == ['frame_content']
            finally:
                await browser.close()
    asyncio.run(run())
