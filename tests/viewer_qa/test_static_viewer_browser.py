"""Real Chromium checks for owner-local static diagram links and root help."""

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


def test_static_root_and_nested_images_stay_in_owner_and_root_help_survives(static_view):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--allow-file-access-from-files"])
        try:
            root_page = browser.new_page()
            outside_events = _watch_outside_file(root_page, (static_view.parent / "outside.svg").as_uri())
            root_page.goto((static_view / "flow.md.html").as_uri())
            root_page.locator("main.workflow-document img").first.wait_for()
            root_page.wait_for_function("() => [...document.querySelectorAll('main img')].every(img => img.complete)")
            assert root_page.locator("aside.viewer-help").count() == 1
            assert root_page.locator("#workflow-fallback").count() == 0
            assert root_page.locator("h2", has_text="Notes").count() == 1
            assert len(_image_state(root_page)) == 1
            assert _image_state(root_page)[0]["alt"] == "Workflow diagram"
            assert _image_state(root_page)[0]["width"] > 0
            assert outside_events == []

            nested_page = browser.new_page()
            nested_page.goto((static_view / "workflows/components/renderer.md.html").as_uri())
            nested_page.locator("main.workflow-document img").first.wait_for()
            assert nested_page.locator("aside.viewer-help").count() == 0
            assert len(_image_state(nested_page)) == 1
            assert _image_state(nested_page)[0]["width"] > 0
        finally:
            browser.close()


def test_root_help_depends_on_exact_carrier_newline(static_view):
    from playwright.sync_api import sync_playwright

    script = static_view / ".private_resources/viewer/viewer.js"
    original = script.read_text()
    assert 'const source = carrier.textContent.startsWith("\\n") ? carrier.textContent.slice(1) : carrier.textContent;' in original
    script.write_text(original.replace(
        'const source = carrier.textContent.startsWith("\\n") ? carrier.textContent.slice(1) : carrier.textContent;',
        'const source = carrier.textContent;', 1))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--allow-file-access-from-files"])
        try:
            page = browser.new_page()
            page.goto((static_view / "flow.md.html").as_uri())
            page.locator("main.workflow-document").wait_for()
            assert page.locator("aside.viewer-help").count() == 0
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
