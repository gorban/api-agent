"""Actual loopback Chromium checks through the admitted finite host only."""
import asyncio
from html import escape
from pathlib import Path
from urllib.parse import quote

import pytest


@pytest.mark.parametrize("parent,name", [
    ("workflows/ordinary", "ordinary"),
    ("workflows/p%dir", "a%b"),
    ("workflows/%2e%2e", "%2f"),
    ("workflows/%2f", "%2e%2e"),
])
def test_percent_declaration_and_diagram_clicks_resolve_exact_captured_http_routes(tmp_path, parent, name):
    from api_agent.runtime.viewer_host import start_viewer_host
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.viewer_projection import project_view
    from api_agent.workflows.viewer_render import build_view_bundle
    from api_agent.workflows.viewer_repository import RepositoryIdentity
    from playwright.async_api import async_playwright

    declaration = f"{parent}/{name}.md.html"
    encoded = quote(declaration, safe="/-._~")
    child = tmp_path / declaration
    child.parent.mkdir(parents=True)
    root_source = (
        "# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| percent_paths |\n\n"
        f"## Flow\n\n1.  [node work]({encoded})\n"
    )
    (tmp_path / "flow.md.html").write_text(root_source)
    child.write_text(
        "# Node v2: work\n\n## Native\n\n| Registry | Kind | Source |\n"
        "| --- | --- | --- |\n| work | deterministic | [code](worker.py) |\n\n"
        "## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n"
        "| work | work | [input](schema.json) | [output](schema.json) |\n"
    )
    (child.parent / "worker.py").write_text("def work(inputs, context):\n    return {}\n")
    (child.parent / "schema.json").write_text('{"type":"object"}')
    definition = load_definition(tmp_path / "flow.md.html", base_root=tmp_path, pack_root=tmp_path)
    projection = project_view(definition)
    assert any(document.logical_path == "base/" + declaration for document in projection.documents)

    class LinkedRenderer:
        def render(self, source, *, allowed_links):
            anchors = "".join(
                f'<a id="node-{index}" href="{escape(link, quote=True)}">'
                f'<text x="5" y="{20 + index * 20}">Work</text></a>'
                for index, link in enumerate(sorted(allowed_links))
            )
            return (
                '<?xml version="1.0"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="80" '
                'viewBox="0 0 300 80">' + anchors + '</svg>'
            ).encode()

    bundle = build_view_bundle(
        projection, roots={"base": tmp_path, "pack": tmp_path},
        renderer=LinkedRenderer(), verify=False,
        repositories={"base": RepositoryIdentity("//github.com/example/workflow")},
    )
    expected_route = "r/base/" + encoded
    captured = next(file.body for file in bundle.files if file.path == expected_route)
    assert b"# Node v2: work" in captured

    async def exercise():
        host = await start_viewer_host(bundle, projection=projection)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    origin = f"http://127.0.0.1:{host.port}/"
                    expected_url = origin + expected_route
                    response = await page.request.get(expected_url)
                    assert await response.body() == captured
                    absent_url = expected_url.replace(".md.html", "%2525-missing.md.html")
                    assert (await page.request.get(absent_url)).status == 404
                    await page.goto(host.entry_url)
                    await page.locator("main > h1 + p > a > img").wait_for()
                    await page.locator("main > h1 + p > a").click()
                    await page.wait_for_function(
                        "expected => document.querySelector('svg a')?.getAttribute('href') === expected",
                        arg=expected_url,
                    )
                    assert await page.locator("svg a").get_attribute("href") == expected_url
                    assert await page.locator("svg a").get_attribute("xlink:href") == expected_url
                    await page.locator("svg a").click()
                    await page.locator("main > h1 + p > a > img").wait_for()
                    assert page.url == expected_url
                    await page.wait_for_function(
                        "() => document.querySelector('main img')?.naturalWidth > 0"
                    )
                    image = page.locator("main img")
                    assert await image.get_attribute("src") == "../" * len(Path(parent).parts) + ".private_resources/diagrams/" + quote(parent + "/" + name + ".svg", safe="/-._~")
                    await page.locator("main > h1 + p > a").click()
                    await page.wait_for_function(
                        "expected => document.querySelector('svg a')?.getAttribute('href') === expected",
                        arg=expected_url,
                    )
                    await page.locator("svg a").click()
                    await page.locator("main").wait_for()
                    assert page.url == expected_url
                    assert await page.locator("main h1").inner_text() == "Node v2: work"
                finally:
                    await browser.close()
        finally:
            await host.aclose()

    asyncio.run(exercise())
