"""Real managed Chromium checks for captured SVG styling and navigation."""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from api_agent.runtime.viewer_host import start_viewer_host
from api_agent.workflows.viewer_build import _projection, _repository_inputs
from api_agent.workflows.viewer_render import build_view_bundle


def test_managed_standalone_svg_keeps_admitted_plantuml_styles_and_links():
    root = Path(__file__).resolve().parents[2]
    projection = _projection(root)
    repositories, sources = _repository_inputs(root)
    bundle = build_view_bundle(projection, roots={"base": root, "pack": root},
                               verify=True, repositories=repositories,
                               repository_sources=sources)
    captured_config = (root / "cfg/config.toml").read_bytes()

    async def exercise():
        host = await start_viewer_host(bundle, projection=projection,
                                       repository_sources=sources)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(host.entry_url)
                    image = page.locator('main img[alt="Workflow diagram"]')
                    await image.wait_for()
                    assert await image.evaluate("element => element.naturalWidth > 0")
                    await page.goto(host.entry_url.replace('flow.md.html', '.private_resources/diagrams/flow.svg'))
                    assert page.url.endswith('/r/base/.private_resources/diagrams/flow.svg')
                    rectangle = page.locator('g.entity rect').first
                    edge = page.locator('g.link path').first
                    assert await page.locator('svg').evaluate(
                        'element => getComputedStyle(element).backgroundColor') == 'rgb(255, 255, 255)'
                    assert await rectangle.evaluate('element => getComputedStyle(element).stroke') == 'rgb(24, 24, 24)'
                    assert await rectangle.evaluate('element => getComputedStyle(element).strokeWidth') == '0.5px'
                    assert await edge.evaluate('element => getComputedStyle(element).stroke') == 'rgb(24, 24, 24)'
                    assert await edge.evaluate('element => getComputedStyle(element).strokeDasharray') == '7px, 7px'

                    # Restoring the old fixed HTML-only policy on this one SVG
                    # response must lose its style, proving the cause of the bug.
                    legacy = await browser.new_page()
                    async def old_policy(route):
                        response = await route.fetch()
                        headers = dict(response.headers)
                        headers['content-security-policy'] = (
                            "default-src 'self'; object-src 'none'; base-uri 'none'; "
                            "style-src 'self' 'unsafe-hashes' "
                            "'sha256-oh7Gx0FkcA1mNom2ETU41Zi2LVeZci7ocogSJroEPis=' "
                            "'sha256-tG8slQ0asE6MlAE2csEakfk6pO1QfspgSsr25hj415E='")
                        await route.fulfill(response=response, headers=headers)
                    await legacy.route('**/flow.svg', old_policy)
                    await legacy.goto(page.url)
                    assert await legacy.locator('g.entity rect').first.evaluate(
                        'element => getComputedStyle(element).stroke') == 'none'
                    await legacy.close()

                    await page.locator('g.entity[data-qualified-name="timeout0"] a').click()
                    assert page.url.endswith('/r/base/cfg/config.toml')
                    response = await page.request.get(page.url)
                    assert await response.body() == captured_config

                    await page.goto(host.entry_url.replace('flow.md.html', '.private_resources/diagrams/flow.svg'))
                    await page.locator('g.entity[data-qualified-name="n0"] a').click()
                    assert page.url.endswith('/r/base/workflows/components/agent.md.html')
                    await page.locator('main.workflow-document').wait_for()
                finally:
                    await browser.close()
        finally:
            await host.aclose()

    asyncio.run(exercise())
