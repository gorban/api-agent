"""Managed loopback Chromium proof for fitted, interactive diagram overlays."""

import asyncio
import json
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from api_agent.runtime.viewer_host import start_viewer_host
from api_agent.workflows.viewer_build import _projection, _repository_inputs
from api_agent.workflows.viewer_render import build_view_bundle


ROOT = Path(__file__).resolve().parents[2]


async def hosted():
    projection = _projection(ROOT)
    repositories, sources = _repository_inputs(ROOT)
    bundle = build_view_bundle(projection, roots={"base": ROOT, "pack": ROOT},
                               verify=True, repositories=repositories,
                               repository_sources=sources)
    assert b'workflow-managed-svg-routes' not in next(
        file.body for file in bundle.files if file.path == bundle.entry_path)
    return await start_viewer_host(bundle, projection=projection, repository_sources=sources)


async def open_diagram(page):
    await page.locator('main a:has(img[alt="Workflow diagram"])').click()
    dialog = page.locator('dialog.workflow-flow-modal')
    await dialog.wait_for()
    await dialog.locator('iframe').wait_for(state='attached')
    await page.wait_for_function('() => !!document.querySelector("dialog.workflow-flow-modal iframe")?.contentDocument?.querySelector("g.entity rect")')
    return dialog


async def geometry(page):
    return await page.locator('dialog.workflow-flow-modal').evaluate('''dialog => {
      const stage = dialog.querySelector(".workflow-flow-stage");
      const canvas = dialog.querySelector(".workflow-flow-canvas");
      const frame = dialog.querySelector("iframe");
      const box = canvas.getBoundingClientRect();
      return {stageWidth: stage.clientWidth, stageHeight: stage.clientHeight,
        scrollWidth: stage.scrollWidth, scrollHeight: stage.scrollHeight,
        width: box.width, height: box.height, transform: getComputedStyle(frame).transform,
        zoom: dialog.querySelector("output").textContent};
    }''')


def test_managed_diagram_fits_zooms_and_follows_admitted_links():
    async def exercise():
        host = await hosted()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 375, "height": 600})
                    await page.goto(host.entry_url)
                    dialog = await open_diagram(page)
                    assert page.url == host.entry_url
                    fitted = await geometry(page)
                    assert fitted['width'] <= fitted['stageWidth'] and fitted['height'] <= fitted['stageHeight']
                    assert fitted['scrollWidth'] <= fitted['stageWidth'] and fitted['scrollHeight'] <= fitted['stageHeight']
                    assert abs(fitted['width'] / fitted['height'] - 712 / 259) < .02
                    assert fitted['zoom'] == '100%'
                    controls = await dialog.locator('.workflow-flow-controls').bounding_box()
                    assert controls['x'] >= 0 and controls['x'] + controls['width'] <= 375
                    await dialog.get_by_role('button', name='Zoom in').click()
                    enlarged = await geometry(page)
                    assert enlarged['width'] > fitted['width'] and enlarged['zoom'] == '125%'
                    await page.set_viewport_size({"width": 1280, "height": 280})
                    await page.wait_for_function('() => document.querySelector("dialog.workflow-flow-modal .workflow-flow-stage").clientHeight < 280')
                    short = await geometry(page)
                    assert short['zoom'] == '125%'
                    await dialog.get_by_role('button', name='Fit diagram').click()
                    fitted_short = await geometry(page)
                    assert fitted_short['height'] <= fitted_short['stageHeight']
                    assert fitted_short['scrollWidth'] <= fitted_short['stageWidth']
                    assert fitted_short['scrollHeight'] <= fitted_short['stageHeight']
                    assert fitted_short['zoom'] == '100%'
                    await dialog.get_by_role('button', name='Close diagram').click()
                    assert await page.locator('dialog.workflow-flow-modal').count() == 0
                    assert await page.locator('main a:has(img[alt="Workflow diagram"])').evaluate(
                        'element => document.activeElement === element')

                    dialog = await open_diagram(page)
                    frame_link = page.frame_locator('dialog.workflow-flow-modal iframe').locator(
                        'g.entity[data-qualified-name="timeout0"] a')
                    await frame_link.focus()
                    await frame_link.press('Escape')
                    assert await page.locator('dialog.workflow-flow-modal').count() == 0
                    assert await page.locator('main a:has(img[alt="Workflow diagram"])').evaluate(
                        'element => document.activeElement === element')

                    dialog = await open_diagram(page)
                    await page.frame_locator('dialog.workflow-flow-modal iframe').locator(
                        'g.entity[data-qualified-name="timeout0"] a').click()
                    assert page.url.endswith('/r/base/cfg/config.toml')
                    response = await page.request.get(page.url)
                    assert await response.body() == (ROOT / 'cfg/config.toml').read_bytes()

                    await page.goto(host.entry_url)
                    await open_diagram(page)
                    await page.frame_locator('dialog.workflow-flow-modal iframe').locator(
                        'g.entity[data-qualified-name="n0"] a').click()
                    assert page.url.endswith('/r/base/workflows/components/agent.md.html')
                    await page.locator('main.workflow-document').wait_for()
                    await open_diagram(page)
                    await page.frame_locator('dialog.workflow-flow-modal iframe').locator(
                        'g.entity[data-qualified-name="ref0"] a').click()
                    assert page.url.endswith('/r/base/workflows/contracts/llm_call_input.json')
                    schema = await page.request.get(page.url)
                    assert await schema.body() == (ROOT / 'workflows/contracts/llm_call_input.json').read_bytes()
                finally:
                    await browser.close()
        finally:
            await host.aclose()

    asyncio.run(exercise())


def test_manifest_capacity_enhances_129_paths_but_rejects_1001():
    async def exercise():
        host = await hosted()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    for count in (129, 1001):
                        page = await browser.new_page()
                        paths = ['/r/base/.private_resources/diagrams/flow.svg'] + [
                            f'/r/base/.private_resources/diagrams/synthetic-{index}.svg'
                            for index in range(count - 1)]
                        async def manifest(route):
                            response = await route.fetch()
                            source = await response.text()
                            assert source.startswith('window.__apiAgentManagedSvgRoutes = ')
                            replaced = 'window.__apiAgentManagedSvgRoutes = ' + json.dumps(paths) + ';\n' + source.split('\n', 1)[1]
                            await route.fulfill(response=response, body=replaced)
                        await page.route('**/managed-modal.js', manifest)
                        await page.goto(host.entry_url)
                        assert await page.locator('meta[name="workflow-managed-svg-routes"]').get_attribute('content') == 'v1'
                        await page.locator('main a:has(img[alt="Workflow diagram"])').click()
                        if count == 129:
                            assert await page.locator('dialog.workflow-flow-modal').count() == 1
                        else:
                            assert await page.locator('dialog.workflow-flow-modal').count() == 0
                            assert page.url.endswith('/r/base/.private_resources/diagrams/flow.svg')
                        await page.close()
                finally:
                    await browser.close()
        finally:
            await host.aclose()

    asyncio.run(exercise())


def test_managed_diagram_replaces_nested_svg_and_keeps_fragment_and_modified_clicks_local():
    async def exercise():
        host = await hosted()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(host.entry_url)
                    await open_diagram(page)
                    frame = page.locator('dialog.workflow-flow-modal iframe')
                    await frame.evaluate('frame => frame.contentDocument.querySelector("g.entity[data-qualified-name=n0] a")'
                                         '.setAttribute("href", "/r/base/.private_resources/diagrams/workflows/components/agent.svg")')
                    await page.frame_locator('dialog.workflow-flow-modal iframe').locator(
                        'g.entity[data-qualified-name="n0"] a').click()
                    await page.wait_for_function('() => document.querySelector("dialog.workflow-flow-modal iframe")?.contentDocument?.URL.endsWith("/agent.svg")')
                    assert page.url == host.entry_url
                    assert (await frame.get_attribute('src')).endswith(
                        '/r/base/.private_resources/diagrams/workflows/components/agent.svg')
                    await frame.evaluate('''frame => {
                      const target = frame.contentDocument.querySelector("g.entity[data-qualified-name=n0]");
                      target.id = "local-node";
                      frame.contentDocument.querySelector("g.entity[data-qualified-name=ref0] a")
                        .setAttribute("href", "#local-node");
                    }''')
                    await page.frame_locator('dialog.workflow-flow-modal iframe').locator(
                        'g.entity[data-qualified-name="ref0"] a').click()
                    assert page.url == host.entry_url
                    assert await page.locator('dialog.workflow-flow-modal').count() == 1
                    await frame.evaluate('frame => frame.contentDocument.querySelector("g.entity[data-qualified-name=ref0] a")'
                                         '.setAttribute("href", "/r/base/workflows/contracts/llm_call_input.json")')
                    await page.frame_locator('dialog.workflow-flow-modal iframe').locator(
                        'g.entity[data-qualified-name="ref0"] a').click(modifiers=['Control'])
                    assert page.url == host.entry_url
                    assert await page.locator('dialog.workflow-flow-modal').count() == 1
                finally:
                    await browser.close()
        finally:
            await host.aclose()

    asyncio.run(exercise())


def test_managed_diagram_invalid_dimensions_and_late_load_do_not_resurrect_dialog():
    async def exercise():
        host = await hosted()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(host.entry_url)
                    await page.route('**/flow.svg', lambda route: route.fulfill(
                        status=200, content_type='image/svg+xml',
                        body='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"/>'))
                    await page.locator('main a:has(img[alt="Workflow diagram"])').click()
                    dialog = page.locator('dialog.workflow-flow-modal')
                    await dialog.get_by_text('Diagram dimensions are unavailable.').wait_for()
                    assert await dialog.locator('iframe').count() == 0
                    assert await dialog.get_by_role('link', name='Open standalone diagram').get_attribute('href') == \
                        f'http://127.0.0.1:{host.port}/r/base/.private_resources/diagrams/flow.svg'
                    await dialog.get_by_role('button', name='Close diagram').click()
                    await page.unroute('**/flow.svg')

                    started = asyncio.Event()
                    release = asyncio.Event()
                    async def delayed(route):
                        started.set()
                        await release.wait()
                        try:
                            await route.continue_()
                        except Exception:
                            pass  # Closing the frame cancels its pending request.
                    await page.route('**/flow.svg', delayed)
                    await page.locator('main a:has(img[alt="Workflow diagram"])').click()
                    await asyncio.wait_for(started.wait(), 5)
                    await page.locator('dialog.workflow-flow-modal').get_by_role('button', name='Close diagram').click()
                    release.set()
                    await page.wait_for_timeout(100)
                    assert await page.locator('dialog.workflow-flow-modal').count() == 0
                    assert await page.locator('iframe').count() == 0
                    assert page.url == host.entry_url
                finally:
                    await browser.close()
        finally:
            await host.aclose()

    asyncio.run(exercise())


def test_managed_diagram_timeout_error_cancel_and_outside_click_dispose_frame():
    async def exercise():
        host = await hosted()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 1280, "height": 800})
                    await page.goto(host.entry_url)
                    await page.clock.install()
                    started = asyncio.Event()
                    release = asyncio.Event()

                    async def delayed(route):
                        started.set()
                        await release.wait()
                        try:
                            await route.continue_()
                        except Exception:
                            pass  # Timed-out iframe request was already canceled.

                    await page.route('**/flow.svg', delayed)
                    await page.locator('main a:has(img[alt="Workflow diagram"])').click()
                    await asyncio.wait_for(started.wait(), 5)
                    await page.clock.run_for(20001)
                    dialog = page.locator('dialog.workflow-flow-modal')
                    await dialog.get_by_text('Diagram loading timed out.').wait_for()
                    assert await dialog.locator('iframe').count() == 0
                    assert await dialog.get_by_role('link', name='Open standalone diagram').count() == 1
                    release.set()
                    await page.wait_for_timeout(100)
                    assert await dialog.get_by_text('Diagram loading timed out.').count() == 1
                    assert await dialog.locator('iframe').count() == 0
                    await page.keyboard.press('Escape')
                    assert await dialog.count() == 0
                    assert await page.locator('main a:has(img[alt="Workflow diagram"])').evaluate(
                        'element => document.activeElement === element')
                    await page.unroute('**/flow.svg')

                    await open_diagram(page)
                    dialog = page.locator('dialog.workflow-flow-modal')
                    await dialog.locator('iframe').dispatch_event('error')
                    await dialog.get_by_text('Diagram could not be loaded.').wait_for()
                    assert await dialog.locator('iframe').count() == 0
                    await page.mouse.click(5, 5)
                    assert await dialog.count() == 0
                    assert await page.locator('main a:has(img[alt="Workflow diagram"])').evaluate(
                        'element => document.activeElement === element')
                    assert page.url == host.entry_url
                finally:
                    await browser.close()
        finally:
            await host.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize('setup', [
    'delete window.ResizeObserver',
    'delete window.HTMLDialogElement',
    'delete HTMLDialogElement.prototype.showModal',
    'delete HTMLDialogElement.prototype.close',
    'delete window.AbortController',
    'window.ResizeObserver = class { constructor() { throw Error("unavailable") } }',
    'window.ResizeObserver = class { observe() { throw Error("unavailable") } disconnect() {} }',
    'HTMLDialogElement.prototype.showModal = function () { throw Error("unavailable") }',
    'window.AbortController = class { constructor() { throw Error("unavailable") } }',
])
def test_unsupported_modal_api_keeps_ordinary_svg_navigation(setup):
    async def exercise():
        host = await hosted()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.add_init_script(setup)
                    await page.goto(host.entry_url)
                    page_errors = []
                    page.on('pageerror', lambda error: page_errors.append(str(error)))
                    await page.locator('main a:has(img[alt="Workflow diagram"])').click()
                    await page.wait_for_url('**/r/base/.private_resources/diagrams/flow.svg')
                    assert await page.locator('dialog.workflow-flow-modal').count() == 0
                    response = await page.request.get(page.url)
                    assert response.status == 200
                    assert response.headers['content-type'].startswith('image/svg+xml')
                    assert b'<svg ' in await response.body()
                    assert page_errors == []
                finally:
                    await browser.close()
        finally:
            await host.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize('width', [240, 260, 375])
def test_modal_controls_fit_and_work_at_narrow_width(width):
    async def exercise():
        host = await hosted()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(viewport={'width': width, 'height': 480})
                    await page.goto(host.entry_url)
                    dialog = await open_diagram(page)
                    controls = await dialog.locator('.workflow-flow-controls').bounding_box()
                    stage = await dialog.locator('.workflow-flow-stage').bounding_box()
                    assert controls and stage and controls['y'] + controls['height'] <= stage['y']
                    for name in ('Zoom out', 'Zoom in', 'Fit diagram', 'Close diagram'):
                        button = dialog.get_by_role('button', name=name)
                        box = await button.bounding_box()
                        assert box and box['x'] >= 0 and box['x'] + box['width'] <= width
                    await dialog.get_by_role('button', name='Zoom out').click()
                    assert (await geometry(page))['zoom'] == '80%'
                    await dialog.get_by_role('button', name='Zoom in').click()
                    assert (await geometry(page))['zoom'] == '100%'
                    await dialog.get_by_role('button', name='Fit diagram').click()
                    fitted = await geometry(page)
                    assert fitted['scrollWidth'] <= fitted['stageWidth']
                    assert fitted['scrollHeight'] <= fitted['stageHeight']
                    await page.set_viewport_size({'width': 260 if width == 240 else 240, 'height': 480})
                    await dialog.get_by_role('button', name='Close diagram').click()
                    assert await dialog.count() == 0
                finally:
                    await browser.close()
        finally:
            await host.aclose()

    asyncio.run(exercise())
