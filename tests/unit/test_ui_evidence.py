"""Trusted Studio masking policy with synthetic browser content."""
import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.ui.evidence import capture_studio_viewport
from api_agent.ui.provider import DemoUIProvider


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


@pytest.mark.parametrize('rows,verified,expected', [
    (None, True, None),
    ([{'index': 0, 'parent': -1, 'forced': False, 'texts': ['ordinary']}], False, None),
    ([{'index': 0, 'parent': -1, 'forced': False, 'texts': ['ordinary']}], True, b'png'),
])
def test_policy_checks_document_identity_and_cleans_markers(rows, verified, expected):
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1],
        evaluate=AsyncMock(side_effect=[rows, rows if verified else None, None]), screenshot=AsyncMock(return_value=b'png'), locator=Mock())
    assert asyncio.run(capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)), 'http://localhost:9000')) == expected
    assert page.evaluate.await_count == (2 if rows is None else 3)


def test_policy_rejects_oversized_document_and_cleans_markers():
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1],
        evaluate=AsyncMock(side_effect=[[{}] * 10001, None]), screenshot=AsyncMock())
    assert asyncio.run(capture_studio_viewport(page, EmissionBoundary(ContentPolicy(True)), 'http://localhost:9000')) is None
    assert page.evaluate.await_count == 2
    page.screenshot.assert_not_called()


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
                    <input value="form-secret"><div id="split"><span>synthetic-</span><span>secret</span></div>'''))
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
    page = SimpleNamespace(url='http://localhost:9000/', frames=[1], evaluate=evaluate,
        screenshot=screenshot, locator=Mock())
    assert asyncio.run(capture_studio_viewport(page,boundary,'http://localhost:9000')) is None


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
    policy.assert_awaited_once_with(page, boundary, 'http://localhost:9000')
