"""Withheld content must not change surrounding layout when nothing is focused."""

import base64

import pytest
from playwright.async_api import async_playwright

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.ui.evidence import capture_studio_viewport


@pytest.mark.asyncio
async def test_unfocused_mask_preserves_flex_layout_and_hides_original_pixels():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 640, 'height': 480})
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
                <body style="margin:0;background:white"><main id="panel" style="display:flex">
                <article style="flex:1"><section id="private"><p style="margin:20px 0;
                  height:80px;background:red">Confidential answer</p></section>
                <button id="send">Send</button></article></main></body>'''))
            await page.goto('http://localhost:9000/')
            before = await page.locator('#send').bounding_box()
            private = await page.locator('#private').bounding_box()
            assert await page.evaluate('document.activeElement === document.body')

            reasons = []
            png = await capture_studio_viewport(
                page, EmissionBoundary(ContentPolicy(False)), 'http://localhost:9000',
                content_mask_selector='#private', content_mask_anchor='#panel',
                unavailable=reasons.append, retry_window_seconds=1,
            )

            assert png is not None and reasons == []
            assert await page.locator('#send').bounding_box() == before
            assert await page.locator('#private').get_attribute('style') is None
            assert await page.evaluate('document.activeElement === document.body')
            black = await page.evaluate('''async ({png, box}) => {
                const image = new Image(); image.src = 'data:image/png;base64,' + png;
                await image.decode(); const canvas = document.createElement('canvas');
                canvas.width = image.width; canvas.height = image.height;
                const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
                const pixels = ctx.getImageData(box.x, box.y, box.width, box.height).data;
                return pixels.every((value, index) => value === (index % 4 === 3 ? 255 : 0));
            }''', {'png': base64.b64encode(png).decode(), 'box': private})
            assert black
        finally:
            await browser.close()
