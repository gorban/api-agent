"""Masking preserves the live browser's focus without overriding newer intent."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


async def _at_raw_capture(page, action):
    session = await page.context.new_cdp_session(page)
    send = session.send
    async def evaluate(expression):
        result = await send('Runtime.evaluate', {'expression':expression, 'returnByValue':True})
        assert 'exceptionDetails' not in result
        return result['result'].get('value')
    async def intercepted(method, params=None):
        if method == 'Page.captureScreenshot':
            await action(evaluate)
        return await send(method, params or {})
    session.send = intercepted
    return SimpleNamespace(url=page.url, frames=page.frames, once=page.once,
        context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)))


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancellation', 'preparation_failure'])
async def test_masked_capture_restores_focused_textarea_and_selection(outcome):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            display = 'display:contents' if outcome == 'preparation_failure' else ''
            await page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=f'''
              <body style="margin:0"><main id="panel"><section id="private" style="{display}">
                <p style="margin:0">Response</p>
                <textarea id="input" style="display:block;height:40px">Confidential draft</textarea>
              </section><button id="other" style="outline:none">Other control</button></main></body>'''))
            await page.goto('http://localhost:9000/')
            await page.locator('#input').focus()
            await page.locator('#input').evaluate("node => node.setSelectionRange(2, 5, 'backward')")
            before = await _state(page)
            started = asyncio.Event()
            async def screenshot(_):
                started.set()
                if outcome == 'cancellation':
                    await asyncio.Event().wait()
                if outcome == 'failure':
                    raise RuntimeError('capture failed')
            proxy = await _at_raw_capture(page, screenshot)
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
              <body><main id="panel"><section id="private" style="height:40px">
                <textarea id="input" style="display:block;height:40px">Confidential draft</textarea>
              </section><button id="other" style="outline:none">Other control</button></main></body>'''))
            await page.goto('http://localhost:9000/')
            await page.locator('#input').focus()
            await page.locator('#input').evaluate("node => node.setSelectionRange(2, 5, 'backward')")
            async def screenshot(evaluate):
                expressions = {
                    'selection': "input.setSelectionRange(6,9,'forward')",
                    'focus_handler': "input.addEventListener('focus',()=>input.setSelectionRange(10,12,'forward'))",
                    'pointer': "document.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));input.blur()",
                    'keyboard': "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))",
                    'removed': "input.remove()",
                    'other_focus': "document.querySelector('#other').focus()",
                    'other_focus_then_blur': "document.querySelector('#other').focus();document.querySelector('#other').blur()",
                }
                await evaluate("const input=document.querySelector('#input');" + expressions[change])
            proxy = await _at_raw_capture(page, screenshot)
            assert await capture_studio_viewport(proxy, EmissionBoundary(ContentPolicy(False)),
                'http://localhost:9000', content_mask_selector='#private', content_mask_anchor='#panel') is not None
            if change == 'removed':
                assert await page.evaluate('document.activeElement.id') == ''
                assert await page.locator('#input').count() == 0
                return
            expected = {'other_focus': 'other', 'other_focus_then_blur': '', 'selection': 'input',
                'pointer': '', 'keyboard': 'input', 'focus_handler': 'input'}[change]
            state = await _state(page)
            assert state['active'] == expected
            assert (state['start'], state['end'], state['direction']) == (
                {'selection': (6, 9, 'forward')}.get(
                    change, (2, 5, 'backward')))
            if change == 'focus_handler':
                await page.locator('#input').evaluate('node=>node.blur()')
                await page.locator('#input').focus()
                assert (await _state(page))['start'] == 10
                assert (await _state(page))['end'] == 12
        finally:
            await browser.close()
