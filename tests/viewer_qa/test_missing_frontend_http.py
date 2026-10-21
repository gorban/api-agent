"""Fresh source viewers report explicit preparation through loopback HTTP."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import shutil

import pytest

from api_agent.workflows.document_format import wrap_document


@pytest.mark.parametrize('root_document', [True, False])
@pytest.mark.parametrize('scripts', [True, False])
def test_unprepared_source_view_has_safe_dependency_guidance(tmp_path, root_document, scripts):
    from playwright.sync_api import sync_playwright

    source = Path(__file__).resolve().parents[2]
    assets = tmp_path / '.private_resources/viewer'
    assets.mkdir(parents=True)
    # First-party bootstrap exists, while npm-installed globals intentionally do not.
    for name in ('viewer.js', 'viewer.css'):
        shutil.copyfile(source / '.private_resources/viewer' / name, assets / name)
    relative = 'flow.md.html' if root_document else 'workflows/node.md.html'
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(wrap_document(
        (b'# Workflow v2\n' if root_document else b'# Node v2: work\n') +
        b'\n<script>window.unsanitized=true</script>\n',
        root=root_document,
        asset_prefix=('.private_resources/viewer/' if root_document else '../.private_resources/viewer/'),
        generated=b'![Workflow diagram](.private_resources/diagrams/flow.svg)\n',
    ))
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(java_script_enabled=scripts)
                page.goto(f'http://127.0.0.1:{server.server_port}/{relative}')
                assert page.locator('main').count() == 0
                if scripts:
                    diagnostic = page.locator('[data-workflow-dependencies="missing"]')
                    assert 'npm ci' in diagnostic.inner_text()
                    assert 'repository root' in diagnostic.inner_text()
                    assert 'reinstall' in diagnostic.inner_text()
                    assert page.evaluate('window.unsanitized') is None
                else:
                    assert page.locator('[data-workflow-dependencies="missing"]').count() == 0
                    assert 'npm ci' not in page.locator('body').inner_text()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
