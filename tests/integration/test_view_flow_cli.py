"""Early standalone CLI rejection does not prepare providers or a browser."""
from pathlib import Path
import os
import subprocess
import sys
import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.feature('Workflow viewer')


def test_view_flow_rejects_test_mode_before_browser_setup():
    program = ('from pathlib import Path; from api_agent.runtime.bootstrap import main; '
               'raise SystemExit(main(["--view-flow", "--tui-mode=unit"], '
               'base_root=Path.cwd(), _worktrees_verified=True))')
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(ROOT / 'src')
    result = subprocess.run([sys.executable, '-c', program], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert '--view-flow conflicts with --tui-mode' in result.stderr
    assert 'Preparing' not in result.stderr


def test_view_flow_rendered_links(record_property):
    """Crawl actual browser links for every included graph, with no inference."""
    import asyncio
    import json
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.runtime.view_flow import _prepare_view
    from api_agent.workflows.viewer_build import discover_view_entries
    context = load_launch_context(ROOT)
    entries = discover_view_entries(ROOT)
    assert entries, 'No workflow entries were discovered.'
    prepared = [(entry, _prepare_view(context, entry=entry)) for entry in entries]
    summary = asyncio.run(_audit_rendered_workflows(prepared))
    record_property('workflow_link_audit', json.dumps(summary, sort_keys=True))


async def _audit_rendered_workflows(prepared):
    from api_agent.providers.control import finish_cleanup
    from api_agent.providers.progress import component_timeout
    from api_agent.runtime.managed_browser import managed_browser
    from api_agent.runtime.viewer_host import start_viewer_host
    browser = managed_browser(headless=True)
    summary = []
    try:
        async with component_timeout('Workflow rendered-link audit', 180):
            session = await browser.start()
            page = session.page
            for entry, (_, projection, _, bundle, repository_sources) in prepared:
                host = await start_viewer_host(bundle, projection=projection,
                                               repository_sources=repository_sources)
                try:
                    row = await _audit_rendered_routes(page, host)
                    row['source'] = entry.relative_to(ROOT).as_posix()
                    summary.append(row)
                finally:
                    await finish_cleanup(host.aclose())
    finally:
        await finish_cleanup(browser.aclose())
    return summary


async def _audit_rendered_routes(page, host):
    from urllib.parse import unquote, urldefrag, urlsplit
    origin = f'http://127.0.0.1:{host.port}'
    documents, targets, fragments = {}, set(), []
    routes = [(path, kind) for path, (kind, _) in host.routes.items()
              if kind.startswith('text/html') or kind == 'image/svg+xml']
    for path, kind in routes:
        url = origin + '/' + path
        response = await page.goto(url, wait_until='load')
        assert response is not None and response.status == 200, url
        if kind.startswith('text/html'):
            await page.locator('main.workflow-document').wait_for()
            selector = 'main.workflow-document a[href]'
            documents[url] = set(await page.locator('[id]').evaluate_all('(items) => items.map(item => item.id)'))
        else:
            selector = 'a'
        links = await page.locator(selector).evaluate_all('''items => items.map(item => {
            const href = item.getAttribute('href') || item.getAttributeNS('http://www.w3.org/1999/xlink', 'href');
            return href ? new URL(href, document.baseURI).href : null;
        }).filter(Boolean)''')
        for link in links:
            parsed = urlsplit(link)
            assert parsed.scheme != 'file', f'{url} links to an unavailable file: {link}'
            assert '_____switch_branch_____' not in link, f'SVG navigation did not initialize: {url} -> {link}'
            if parsed.scheme not in ('http', 'https') or parsed.netloc != f'127.0.0.1:{host.port}':
                continue
            target, fragment = urldefrag(link)
            targets.add(target)
            if fragment:
                fragments.append((url, target, unquote(fragment)))
    assert any(urlsplit(target).path.endswith('.py') for target in targets), 'Captured Python source links are absent'
    for target in sorted(targets):
        response = await page.request.get(target)
        assert response.status == 200, f'Broken rendered workflow link: {target} ({response.status})'
    for source, target, fragment in fragments:
        if target in documents:
            assert fragment in documents[target], f'Broken workflow anchor: {source} -> {target}#{fragment}'
    return {'entry': host.entry_path, 'rendered_pages': len(routes), 'local_targets': len(targets)}
