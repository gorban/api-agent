"""Real Chromium checks for static presentation and owner-local diagram links."""

from pathlib import Path
import shutil
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.feature("Workflow viewer")


@pytest.fixture
def static_view(tmp_path):
    owner = tmp_path / "owner"
    (owner / ".private_resources/viewer").mkdir(parents=True)
    (owner / ".private_resources/diagrams/workflows/components").mkdir(parents=True)
    (owner / "workflows/components").mkdir(parents=True)
    for asset in (ROOT / ".private_resources/viewer").iterdir():
        shutil.copy2(asset, owner / ".private_resources/viewer" / asset.name)
    for relative in ("flow.svg", "workflows/components/agent.svg"):
        shutil.copy2(ROOT / ".private_resources/diagrams" / relative,
                     owner / ".private_resources/diagrams" / relative)
    from api_agent.workflows.document_format import wrap_document

    notes = ("\n\n## Notes\n\n"
             "![Outside](../.private_resources/diagrams/../../outside.svg)\n\n"
             "![Sibling](../sibling/.private_resources/diagrams/other.svg)\n\n"
             "![Encoded](.private_resources/diagrams/%2e%2e/outside.svg)\n\n"
             "![Encoded separator](.private_resources/diagrams/%2foutside.svg)\n\n"
             "![Encoded backslash](.private_resources/diagrams/%5coutside.svg)\n\n"
             "![Extra prefix](../../.private_resources/diagrams/flow.svg)\n\n"
             "![Absolute](/outside.svg)\n\n"
             "![Scheme](file:///outside.svg)\n\n")
    root_source = ("# Workflow v2\n" + notes + "## Héllo *world*!\n\n## Héllo *world*!\n\n"
                   "## 中文\n\n```text\n## Not a heading\n```\n").encode()
    root_generated = (b"![Workflow diagram](.private_resources/diagrams/flow.svg)\n\n"
                      b"[Open standalone diagram](.private_resources/diagrams/flow.svg)\n\n"
                      b"- [agent](workflows/components/agent.md.html)")
    root_html = wrap_document(root_source, root=True, asset_prefix=".private_resources/viewer/", generated=root_generated)
    (owner / "flow.md.html").write_bytes(root_html)
    nested_generated = (b"![Workflow diagram](../../.private_resources/diagrams/workflows/components/agent.svg)\n\n"
                        b"[Open standalone diagram](../../.private_resources/diagrams/workflows/components/agent.svg)\n\n"
                        b"- [llm_call](#node-llm_call)")
    nested = wrap_document(b"# Component v2: agent\n\n## Node: llm_call\n", root=False,
                           asset_prefix="../../.private_resources/viewer/", generated=nested_generated)
    (owner / "workflows/components/agent.md.html").write_bytes(nested)
    (tmp_path / "outside.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="23" height="10" viewBox="0 0 23 10"/>')
    return owner


def _image_state(page):
    return page.locator("main.workflow-document img").evaluate_all(
        "images => images.map(image => ({alt: image.alt, width: image.naturalWidth}))")


@pytest.mark.parametrize('view_url', ['http'], indirect=True)
def test_http_inline_fragment_navigation_is_hidden_locally(view_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url('workflows/components/agent.md.html'))
            page.locator('main.workflow-document').wait_for()

            assert page.locator('main details').count() == 0
            assert page.locator('main #node-llm_call').count() == 1
            assert page.locator('main img[alt="Workflow diagram"]').count() == 1
        finally:
            browser.close()


@pytest.mark.parametrize('view_url', ['http'], indirect=True)
def test_http_fragment_hiding_depends_on_exact_viewer_pattern(view_url, static_view):
    from playwright.sync_api import sync_playwright

    source = (static_view / '.private_resources/viewer/viewer.js').read_text()
    mutated = source.replace(r'|#node-\\2', '').replace(r'|#node-\\3', '')
    assert mutated != source

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route('**/viewer.js', lambda route: route.fulfill(
                status=200, content_type='text/javascript', body=mutated))
            page.goto(view_url('workflows/components/agent.md.html'))
            page.locator('main.workflow-document').wait_for()

            assert page.locator('main details > summary').all_inner_texts() == ['Diagram links']
        finally:
            browser.close()


@pytest.mark.parametrize('view_url', ['http'], indirect=True)
def test_http_mismatched_fragment_navigation_is_not_hidden(view_url, static_view):
    from playwright.sync_api import sync_playwright

    document = static_view / 'workflows/components/agent.md.html'
    document.write_bytes(document.read_bytes().replace(b'[llm_call](#node-llm_call)',
                                                       b'[llm_call](#node-other)'))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url('workflows/components/agent.md.html'))
            page.locator('main.workflow-document').wait_for()

            assert page.locator('main details').count() == 1
            assert page.locator('main details a[href="#node-other"]').count() == 1
        finally:
            browser.close()


def _watch_outside_file(page, outside):
    events = []
    for kind in ("request", "response", "requestfinished"):
        page.on(kind, lambda item, kind=kind: events.append(kind) if item.url == outside else None)
    return events


@pytest.fixture(params=["file", "http"])
def view_url(static_view, request):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    if request.param == "file":
        yield lambda path: (static_view / path).as_uri()
        return
    class StrictHandler(SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; "
                             "style-src 'self' 'unsafe-hashes' 'sha256-oh7Gx0FkcA1mNom2ETU41Zi2LVeZci7ocogSJroEPis=' "
                             "'sha256-tG8slQ0asE6MlAE2csEakfk6pO1QfspgSsr25hj415E='")
            super().end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StrictHandler, directory=static_view))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield lambda path: f"http://127.0.0.1:{server.server_port}/{path}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _assert_layout(page, target):
    assert page.locator("aside.viewer-help, #workflow-fallback").count() == 0
    assert page.get_by_role("heading", name="Viewing this file").count() == 0
    assert page.locator("main > h1 + p > a > img").count() == 1
    assert page.locator("main > h1 + p > a").get_attribute("href") == target
    assert page.get_by_role("heading", name="Generated Diagram v1").count() == 0
    assert page.get_by_role("link", name="Open standalone diagram").count() == 0
    assert page.locator("main details").count() == 0
    assert page.locator("plaintext").count() == 1
    assert not page.locator("plaintext").is_visible()


@pytest.mark.parametrize("path,target", [
    ("flow.md.html", ".private_resources/diagrams/flow.svg"),
    ("workflows/components/agent.md.html", "../../.private_resources/diagrams/workflows/components/agent.svg"),
])
def test_static_diagram_follows_title_and_opens_svg(view_url, path, target):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--allow-file-access-from-files"])
        try:
            page = browser.new_page()
            policy_errors = []
            page.on("console", lambda message: policy_errors.append(message.text)
                    if "Content Security Policy" in message.text else None)
            page.goto(view_url(path))
            page.locator("main.workflow-document").wait_for()
            page.wait_for_function("() => [...document.querySelectorAll('main img')].every(img => img.complete)")
            _assert_layout(page, target)
            assert policy_errors == []
            assert page.locator('meta[http-equiv="Content-Security-Policy"]').evaluate("item => item.parentElement.tagName") == "HEAD"
            assert len(_image_state(page)) == 1
            assert _image_state(page)[0]["width"] > 0
            page.locator("main > h1 + p > a").click()
            assert page.url.endswith(target.replace("../../", ""))
            assert page.locator("svg").count() == 1
        finally:
            browser.close()


def test_notes_image_stays_in_place_and_diagram_navigation_works(static_view, view_url):
    from playwright.sync_api import sync_playwright

    document = static_view / "flow.md.html"
    document.write_text(document.read_text().replace("## Notes\n", "## Notes\n\n![Notes image](.private_resources/diagrams/flow.svg)\n", 1))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url("flow.md.html"))
            page.locator("main").wait_for()
            assert page.locator('h2 + p > img[alt="Notes image"]').count() == 1
            assert page.locator('h1 + p img[alt="Workflow diagram"]').count() == 1
            assert page.locator("details").count() == 0
            assert "<details open>" in page.locator("plaintext").text_content()
        finally:
            browser.close()


@pytest.mark.parametrize("failure", ["disabled", "viewer.js", "marked.js", "dompurify.js", "heading-id.js"])
def test_fallback_survives_unavailable_scripts(static_view, view_url, failure):
    from playwright.sync_api import sync_playwright

    if failure != "disabled":
        (static_view / ".private_resources/viewer" / failure).unlink()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(java_script_enabled=failure != "disabled")
            page.goto(view_url("flow.md.html"))
            assert page.locator("#workflow-fallback").is_visible()
            assert "./launch.py --view-flow" in page.locator("#workflow-fallback").inner_text()
            assert page.locator("main").count() == 0
            assert not page.locator("plaintext").is_visible()
        finally:
            browser.close()


@pytest.mark.parametrize("old,new", [
    ('document.getElementById("workflow-fallback")?.remove();', ''),
])
def test_layout_assertions_causally_reject_regressions(static_view, old, new):
    from playwright.sync_api import sync_playwright

    script = static_view / ".private_resources/viewer/viewer.js"
    original = script.read_text()
    assert old in original
    script.write_text(original.replace(old, new, 1))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto((static_view / "flow.md.html").as_uri())
            page.locator("main").wait_for()
            with pytest.raises(AssertionError):
                _assert_layout(page, ".private_resources/diagrams/flow.svg")
        finally:
            browser.close()


@pytest.mark.parametrize("change", ["no-title", "no-generated"])
def test_missing_optional_structure_is_safe(static_view, change):
    from playwright.sync_api import sync_playwright

    document = static_view / "flow.md.html"
    source = document.read_text()
    source = source.replace("# Workflow v2", "Workflow v2", 1) if change == "no-title" else source.replace("<!-- workflow-generated-diagram-v2:start -->", "<!-- ordinary -->", 1)
    document.write_text(source)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(error))
            page.goto(document.as_uri())
            page.locator("main").wait_for()
            assert errors == []
            assert page.locator("#workflow-fallback").count() == 0
        finally:
            browser.close()


def test_static_image_guard_causally_blocks_existing_outside_svg(static_view):
    from playwright.sync_api import sync_playwright

    script = static_view / ".private_resources/viewer/viewer.js"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--allow-file-access-from-files"])
        try:
            page = browser.new_page()
            outside_events = _watch_outside_file(page, (static_view.parent / "outside.svg").as_uri())
            page.goto((static_view / "flow.md.html").as_uri())
            page.locator("main.workflow-document").wait_for()
            assert page.locator('img[alt="Outside"]').count() == 0
            assert outside_events == []

            original = script.read_text()
            assert "if (!safe) {" in original
            script.write_text(original.replace("if (!safe) {", "if (false) {", 1))
            mutant = browser.new_context()
            mutant_page = mutant.new_page()
            mutant_events = _watch_outside_file(mutant_page, (static_view.parent / "outside.svg").as_uri())
            mutant_page.goto((static_view / "flow.md.html").as_uri())
            mutant_page.locator('img[alt="Outside"]').wait_for()
            assert mutant_page.locator('img[alt="Outside"]').evaluate("image => image.naturalWidth") == 23
            assert "request" in mutant_events and "response" in mutant_events
            mutant.close()
        finally:
            browser.close()


def test_inert_parsing_prevents_outside_request_before_image_removal(static_view):
    from playwright.sync_api import sync_playwright

    script = static_view / ".private_resources/viewer/viewer.js"
    original = script.read_text()
    assert "template.innerHTML = clean;" in original
    script.write_text(original.replace("template.innerHTML = clean;",
                                       "main.innerHTML = clean;\n  template.innerHTML = clean;", 1)
                      .replace("main.append(template.content);", "main.replaceChildren(template.content);", 1))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--allow-file-access-from-files"])
        try:
            page = browser.new_page()
            events = _watch_outside_file(page, (static_view.parent / "outside.svg").as_uri())
            page.goto((static_view / "flow.md.html").as_uri())
            page.locator("main.workflow-document").wait_for()
            assert page.locator('img[alt="Outside"]').count() == 0
            deadline = time.monotonic() + 2
            while "response" not in events and time.monotonic() < deadline:
                page.wait_for_timeout(10)
            assert "request" in events and "response" in events
        finally:
            browser.close()


def test_heading_ids_title_and_initial_fragment(view_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url("flow.md.html") + "#héllo-world-1")
            page.locator("main").wait_for()
            assert page.title() == "Workflow v2"
            assert page.locator("main h2").evaluate_all("items => items.map(item => item.id)") == [
                "notes", "héllo-world", "héllo-world-1", "中文"]
            assert page.locator("main pre").inner_text() == "## Not a heading\n"
            assert page.locator('h2[id="héllo-world-1"]').evaluate("item => item.matches(':target')")
        finally:
            browser.close()


def test_svg_adapter_rebases_only_admitted_targets(static_view, view_url):
    import html
    import json
    from playwright.sync_api import sync_playwright

    svg = static_view / ".private_resources/diagrams/adapter.svg"
    prefix = "//github.com/example/workflow/blob/_____switch_branch_____/"
    other_prefix = "//github.com/example/other/blob/_____switch_branch_____/"
    roots = html.escape(json.dumps({prefix: "../../", other_prefix: "../../other%20owner/"}), quote=True)
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                   f'<a id="safe" href="{prefix}flow.md.html#notes"><text y="20">Go</text></a>'
                   f'<a id="other" href="{other_prefix}flow.md.html#notes"><text y="80">Other owner</text></a>'
                   f'<a id="escape" href="{prefix}%2e%2e/outside.md.html"><text y="40">Escape</text></a>'
                   '<a id="remote" href="https://example.org/flow.md.html"><text y="60">Other</text></a>'
                   f'<script data-workflow-links="v1" data-workflow-roots="{roots}" '
                   'href="../viewer/svg-links.js" xlink:href="../viewer/svg-links.js"></script></svg>')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--allow-file-access-from-files"])
        try:
            page = browser.new_page()
            page.goto(view_url(".private_resources/diagrams/adapter.svg"))
            assert page.locator("#safe").get_attribute("href") == view_url("flow.md.html") + "#notes"
            assert page.locator("#other").get_attribute("href") == view_url("flow.md.html").removesuffix("flow.md.html") + "other%20owner/flow.md.html#notes"
            assert page.locator("#escape").get_attribute("href") == prefix + "%2e%2e/outside.md.html"
            assert page.locator("#remote").get_attribute("href") == "https://example.org/flow.md.html"
            page.locator("#safe").click()
            page.locator("main #notes").wait_for()
            assert page.url.endswith("flow.md.html#notes")
        finally:
            browser.close()


@pytest.mark.parametrize('view_url', ['http'], indirect=True)
def test_http_svg_adapter_rebases_captured_resource_suffixes_only(static_view, view_url):
    import html
    import json
    from playwright.sync_api import sync_playwright

    prefix = '//github.com/example/workflow/blob/_____switch_branch_____/'
    roots = html.escape(json.dumps({prefix: '../../'}), quote=True)
    svg = static_view / '.private_resources/diagrams/resources.svg'
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                   f'<a id="schema" href="{prefix}contracts/input.json"><text y="20">Schema</text></a>'
                   f'<a id="config" href="{prefix}cfg/config.toml"><text y="40">Config</text></a>'
                   f'<a id="fragment" href="{prefix}cfg/config.toml#field"><text y="60">Fragment</text></a>'
                   f'<a id="encoded" href="{prefix}%2e%2e/outside.json"><text y="80">Encoded</text></a>'
                   '<a id="foreign" href="//github.com/example/foreign/blob/_____switch_branch_____/cfg/config.toml">'
                   '<text y="100">Foreign</text></a>'
                   f'<script data-workflow-links="v1" data-workflow-roots="{roots}" '
                   'href="../viewer/svg-links.js" xlink:href="../viewer/svg-links.js"></script></svg>')
    adapter = (static_view / '.private_resources/viewer/svg-links.js').read_text()
    legacy = adapter.replace('const resourceTarget = path.endsWith(".json") || path.endsWith(".toml");',
                             'const resourceTarget = false;')
    assert legacy != adapter

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url('.private_resources/diagrams/resources.svg'))
            assert page.locator('#schema').get_attribute('href') == view_url('contracts/input.json')
            assert page.locator('#config').get_attribute('href') == view_url('cfg/config.toml')
            assert page.locator('#fragment').get_attribute('href') == prefix + 'cfg/config.toml#field'
            assert page.locator('#encoded').get_attribute('href') == prefix + '%2e%2e/outside.json'
            assert page.locator('#foreign').get_attribute('href').startswith('//github.com/example/foreign/')

            legacy_page = browser.new_page()
            legacy_page.route('**/svg-links.js', lambda route: route.fulfill(
                status=200, content_type='text/javascript', body=legacy))
            legacy_page.goto(view_url('.private_resources/diagrams/resources.svg'))
            assert legacy_page.locator('#schema').get_attribute('href') == prefix + 'contracts/input.json'
            assert legacy_page.locator('#config').get_attribute('href') == prefix + 'cfg/config.toml'
        finally:
            browser.close()


def test_blocked_viewer_keeps_fallback_and_source_inert(view_url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route("**/viewer.js", lambda route: route.abort())
            page.goto(view_url("flow.md.html"))
            assert page.locator("#workflow-fallback").is_visible()
            assert not page.locator("plaintext").is_visible()
            assert page.locator("main").count() == 0
        finally:
            browser.close()


def test_rendering_retains_exact_carrier_and_sanitizes_html(static_view, view_url):
    from playwright.sync_api import sync_playwright

    document = static_view / "flow.md.html"
    document.write_text(document.read_text() + '\n<script>window.injected=true</script>\n'
                        '<img src="https://example.org/outside.svg" onerror="window.injected=true">\n'
                        '<a href="javascript:window.injected=true">Unsafe</a>\n')
    original = document.read_text().split('<plaintext id="workflow-source" hidden>', 1)[1]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            outside = []
            page.on("request", lambda request: outside.append(request.url)
                    if request.url.startswith("https://example.org/") else None)
            page.goto(view_url("flow.md.html"))
            page.locator("main").wait_for()
            assert page.locator("plaintext").text_content() == original
            assert page.locator("main script, main img[onerror], main a[href^='javascript:']").count() == 0
            assert page.evaluate("window.injected") is None
            assert outside == []
        finally:
            browser.close()


def test_legacy_v1_carrier_stays_hidden_and_safe(static_view, view_url):
    from api_agent.workflows.document_format import _CARRIER_V1, _HEAD_V1, _ROOT_FALLBACK_V1
    from playwright.sync_api import sync_playwright

    document = static_view / "flow.md.html"
    document.write_bytes(_HEAD_V1.replace(b"{assets}", b".private_resources/viewer/") +
                         _ROOT_FALLBACK_V1 + _CARRIER_V1 + b"# Legacy workflow\n\n## Notes\n")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url("flow.md.html"))
            page.locator("main").wait_for()
            assert page.title() == "Legacy workflow"
            assert not page.locator("plaintext").is_visible()
            assert page.locator("#workflow-fallback").count() == 0
        finally:
            browser.close()


def test_nested_missing_dependency_shows_readable_diagnostic(static_view, view_url):
    from playwright.sync_api import sync_playwright

    (static_view / ".private_resources/viewer/marked.js").unlink()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url("workflows/components/agent.md.html"))
            diagnostic = page.locator("body").inner_text()
            assert "Workflow viewer dependencies are unavailable" in diagnostic
            assert "run npm ci from the repository root" in diagnostic
            assert page.locator(".workflow-viewer-error").evaluate("item => getComputedStyle(item).fontSize") == "16px"
            assert page.locator("main").count() == 0
        finally:
            browser.close()


def test_nonlocal_host_simulation_keeps_repository_alternatives(static_view):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            def serve(route):
                from urllib.parse import urlsplit
                target = static_view / urlsplit(route.request.url).path.lstrip("/")
                route.fulfill(path=target) if target.is_file() else route.fulfill(status=404)
            page.route("https://workflow.example/**", serve)
            page.goto("https://workflow.example/flow.md.html")
            page.locator("main").wait_for()
            assert page.locator("main details[open]").count() == 1
            assert page.get_by_role("link", name="Open standalone diagram").count() == 1
        finally:
            browser.close()


def test_reserved_heading_fragments_survive_sanitization_without_spoofing(static_view, view_url):
    from playwright.sync_api import sync_playwright
    document = static_view / 'flow.md.html'
    document.write_text(document.read_text() + '''
<h2 data-workflow-heading="0" id="location" onclick="window.injected=true">Raw before</h2>

## Location

## Constructor

## Location

## Name

## Cookie

<h2 id="constructor">Raw after</h2>

```html
<h2 id="location">Code only</h2>
```
''')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url('flow.md.html') + '#location')
            page.locator('main #location').wait_for()
            assert page.locator('main #location').inner_text() == 'Location'
            assert page.locator('main #constructor').inner_text() == 'Constructor'
            assert page.locator('main #location-1').inner_text() == 'Location'
            assert page.locator('main #name').inner_text() == 'Name'
            assert page.locator('main #cookie').inner_text() == 'Cookie'
            assert page.get_by_role('heading', name='Raw before').get_attribute('id') is None
            assert page.get_by_role('heading', name='Raw after').get_attribute('id') is None
            assert page.locator('main [onclick], main [data-workflow-headingx]').count() == 0
            assert page.evaluate('window.injected') is None
        finally:
            browser.close()


def test_duplicate_svg_navigation_scripts_do_not_rewrite_while_parsing(static_view, view_url):
    import html
    import json
    from playwright.sync_api import sync_playwright
    prefix = '//github.com/example/workflow/blob/_____switch_branch_____/'
    roots = html.escape(json.dumps({prefix: '../../'}), quote=True)
    script = (f'<script data-workflow-links="v1" data-workflow-roots="{roots}" '
              'href="../viewer/svg-links.js" xlink:href="../viewer/svg-links.js"></script>')
    svg = static_view / '.private_resources/diagrams/duplicates.svg'
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                   f'<a id="target" href="{prefix}flow.md.html"><text y="20">Go</text></a>'
                   + script + script + '</svg>')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url('.private_resources/diagrams/duplicates.svg'))
            assert page.locator('#target').get_attribute('href') == prefix + 'flow.md.html'
        finally:
            browser.close()


@pytest.mark.parametrize('view_url', ['http'], indirect=True)
def test_http_hosted_flow_click_targets_inline_heading(view_url, static_view):
    from types import SimpleNamespace
    from api_agent.workflows.document_format import wrap_document
    from api_agent.workflows.viewer_render import _hosted_declaration
    from playwright.sync_api import sync_playwright

    raw = b'# Component: agent\n\n## Flow\n\n1. [node llm_call](#node-llm_call)\n\n## Node: llm_call\n'
    document = SimpleNamespace(logical_path='base/workflows/components/agent.md.html',
                               raw_bytes=raw, executable_bytes=raw)
    step = SimpleNamespace(id='llm_call', owner_path=document.logical_path,
                           declaration_path=document.logical_path, flow_line=5,
                           declaration_fragment='node-llm_call')
    projection = SimpleNamespace(steps=(step,), documents=(document,), resources=())
    hosted = _hosted_declaration(projection, document)
    (static_view / 'workflows/components/agent.md.html').write_bytes(wrap_document(
        hosted, root=False, asset_prefix='../../.private_resources/viewer/'))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(view_url('workflows/components/agent.md.html'))
            link = page.get_by_role('link', name='node llm_call', exact=True)
            assert link.get_attribute('href') == 'agent.md.html#node-llm_call'
            link.click()
            page.wait_for_url('**/agent.md.html#node-llm_call')
            assert page.locator('main #node-llm_call').inner_text() == 'Node: llm_call'
        finally:
            browser.close()
