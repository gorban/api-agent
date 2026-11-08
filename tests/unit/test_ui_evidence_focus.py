"""Masking preserves the live browser's focus without overriding newer intent."""
import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.ui.evidence import capture_studio_viewport


async def _state(page):
    return await page.evaluate('''() => {
      const input = document.querySelector('#input');
      return {active:document.activeElement.id, start:input.selectionStart,
        end:input.selectionEnd, direction:input.selectionDirection};
    }''')


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancellation', 'preparation_failure'])
async def test_masked_capture_restores_focused_textarea_and_selection(outcome):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            margin = '20px 0' if outcome == 'preparation_failure' else '0'
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=f'''
              <body style="margin:0"><main id="panel"><section id="private">
                <p style="margin:{margin}">Response</p>
                <textarea id="input" style="display:block;height:40px">Confidential draft</textarea>
              </section><button id="other">Other control</button></main></body>'''))
            await page.goto('http://localhost:9000/')
            await page.locator('#input').focus()
            await page.locator('#input').evaluate("node => node.setSelectionRange(2, 5, 'backward')")
            before = await _state(page)
            started = asyncio.Event()
            async def screenshot(**kwargs):
                started.set()
                if outcome == 'cancellation':
                    await asyncio.Event().wait()
                if outcome == 'failure':
                    raise RuntimeError('capture failed')
                return await page.screenshot(**kwargs)
            proxy = SimpleNamespace(url=page.url, frames=page.frames, screenshot=screenshot,
                evaluate=page.evaluate, evaluate_handle=page.evaluate_handle, locator=page.locator,
                context=SimpleNamespace(new_cdp_session=lambda _: page.context.new_cdp_session(page)))
            capture = capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(False)),
                'http://localhost:9000', content_mask_selector='#private', content_mask_anchor='#panel')
            if outcome == 'cancellation':
                task = asyncio.create_task(capture)
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == 'failure':
                with pytest.raises(RuntimeError, match='capture failed'):
                    await capture
            elif outcome == 'preparation_failure':
                assert await capture is None
            else:
                assert await capture is not None
            assert await _state(page) == before
            assert await page.locator('html > div').count() == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['other_focus', 'other_focus_then_blur', 'selection',
    'pointer', 'keyboard', 'removed', 'focus_handler'])
async def test_masked_capture_preserves_newer_application_focus_or_selection(change):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='''
              <body><main id="panel"><section id="private">
                <textarea id="input" style="display:block;height:40px">Confidential draft</textarea>
              </section><button id="other">Other control</button></main></body>'''))
            await page.goto('http://localhost:9000/')
            await page.locator('#input').focus()
            await page.locator('#input').evaluate("node => node.setSelectionRange(2, 5, 'backward')")
            async def screenshot(**kwargs):
                if change == 'selection':
                    await page.locator('#input').evaluate("node => node.setSelectionRange(6, 9, 'forward')")
                elif change == 'focus_handler':
                    await page.locator('#input').evaluate('''node => node.addEventListener('focus',
                      () => node.setSelectionRange(10, 12, 'forward'))''')
                elif change == 'pointer':
                    await page.mouse.click(650, 300)
                elif change == 'keyboard':
                    await page.keyboard.press('Escape')
                elif change == 'removed':
                    await page.locator('#input').evaluate('node => node.remove()')
                else:
                    await page.locator('#other').focus()
                    if change == 'other_focus_then_blur':
                        await page.locator('#other').evaluate('node => node.blur()')
                return await page.screenshot(**kwargs)
            proxy = SimpleNamespace(url=page.url, frames=page.frames, screenshot=screenshot,
                evaluate=page.evaluate, evaluate_handle=page.evaluate_handle, locator=page.locator,
                context=SimpleNamespace(new_cdp_session=lambda _: page.context.new_cdp_session(page)))
            assert await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(False)),
                'http://localhost:9000', content_mask_selector='#private', content_mask_anchor='#panel') is not None
            if change == 'removed':
                assert await page.evaluate('document.activeElement.id') == ''
                assert await page.locator('#input').count() == 0
                return
            expected = {'other_focus': 'other', 'other_focus_then_blur': '', 'selection': 'input',
                'pointer': '', 'keyboard': '', 'focus_handler': 'input'}[change]
            state = await _state(page)
            assert state['active'] == expected
            assert (state['start'], state['end'], state['direction']) == (
                {'selection': (6, 9, 'forward'), 'focus_handler': (10, 12, 'forward')}.get(
                    change, (2, 5, 'backward')))
        finally:
            await browser.close()
