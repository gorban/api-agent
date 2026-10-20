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
    for relative in ("flow.svg", "workflows/components/renderer.svg"):
        shutil.copy2(ROOT / ".private_resources/diagrams" / relative,
                     owner / ".private_resources/diagrams" / relative)
    root_html = (ROOT / "flow.md.html").read_text()
    notes = ("\n\n## Notes\n\n"
             "![Outside](../.private_resources/diagrams/../../outside.svg)\n\n"
             "![Sibling](../sibling/.private_resources/diagrams/other.svg)\n\n"
             "![Encoded](.private_resources/diagrams/%2e%2e/outside.svg)\n\n"
             "![Encoded separator](.private_resources/diagrams/%2foutside.svg)\n\n"
             "![Encoded backslash](.private_resources/diagrams/%5coutside.svg)\n\n"
             "![Extra prefix](../../.private_resources/diagrams/flow.svg)\n\n"
             "![Absolute](/outside.svg)\n\n"
             "![Scheme](file:///outside.svg)\n\n")
    root_html = root_html.replace("\n\n## Generated Diagram v1", notes + "## Generated Diagram v1", 1)
    (owner / "flow.md.html").write_text(root_html)
    shutil.copy2(ROOT / "workflows/components/renderer.md.html",
                 owner / "workflows/components/renderer.md.html")
    (tmp_path / "outside.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="23" height="10" viewBox="0 0 23 10"/>')
    return owner


def _image_state(page):
    return page.locator("main.workflow-document img").evaluate_all(
        "images => images.map(image => ({alt: image.alt, width: image.naturalWidth}))")


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
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=static_view))
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
    assert page.locator("main > h1 + p + details > summary").inner_text() == "Diagram links"


@pytest.mark.parametrize("path,target", [
    ("flow.md.html", ".private_resources/diagrams/flow.svg"),
    ("workflows/components/renderer.md.html", "../../.private_resources/diagrams/workflows/components/renderer.svg"),
])
def test_static_diagram_follows_title_and_opens_svg(view_url, path, target):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--allow-file-access-from-files"])
        try:
            page = browser.new_page()
            page.goto(view_url(path))
            page.locator("main.workflow-document").wait_for()
            page.wait_for_function("() => [...document.querySelectorAll('main img')].every(img => img.complete)")
            _assert_layout(page, target)
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
            page.get_by_text("Diagram links", exact=True).click()
            navigation = page.locator("details a")
            assert navigation.get_attribute("href") == "workflows/components/renderer.md.html"
            navigation.click()
            page.locator("h1", has_text="Node v2: renderer").wait_for()
            _assert_layout(page, "../../.private_resources/diagrams/workflows/components/renderer.svg")
        finally:
            browser.close()


@pytest.mark.parametrize("failure", ["disabled", "viewer.js", "marked.js", "dompurify.js"])
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
        finally:
            browser.close()


@pytest.mark.parametrize("old,new", [
    ('title.after(picture);', 'main.append(picture);'),
    ('picture.replaceChildren(clickable);', 'picture.replaceChildren(image);'),
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
    source = source.replace("# Workflow v2", "Workflow v2", 1) if change == "no-title" else source.split("\n\n## Generated Diagram v1")[0]
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
