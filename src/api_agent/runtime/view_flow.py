"""Standalone, nonexecuting workflow browser lifecycle."""
import asyncio

from api_agent.providers.progress import component_timeout
from api_agent.providers.sessions import InspectionOwner
from api_agent.providers.control import CleanupPendingError, finish_cleanup
from api_agent.runtime.managed_browser import managed_browser
from api_agent.runtime.resource_pack import load_selected_definition
from api_agent.runtime.viewer_host import start_viewer_host


def _canonical_selection(context, definition, projection):
    library = getattr(context, 'workflow_base_root', None)
    if library is not None and library.resolve() != context.base_root.resolve():
        return False
    owner = definition.entry_root
    return (not definition.replacements
            and definition.entry == definition.roots[owner] / 'flow.md.html'
            and all(document.logical_path.startswith(owner + '/')
                    for document in projection.documents)
            and all(resource.logical_path.startswith((owner + '/', 'inline/' + owner + '/'))
                    for resource in projection.resources))


def _prepare_view(context):
    from api_agent.workflows.viewer_projection import project_view
    from api_agent.workflows.viewer_render import build_view_bundle, capture_repository_sources
    definition = load_selected_definition(context)
    projection = project_view(definition)
    canonical = _canonical_selection(context, definition, projection)
    repositories, repository_sources = capture_repository_sources(definition.roots)
    bundle = build_view_bundle(projection, roots=definition.roots, verify=canonical,
                               repositories=repositories, repository_sources=repository_sources)
    return definition, projection, canonical, bundle, repository_sources


async def _serve_view(prepared):
    definition, projection, canonical, bundle, repository_sources = prepared
    host = await start_viewer_host(bundle, projection=projection, repository_sources=repository_sources)
    inspection = InspectionOwner()
    browser = managed_browser(inspection=inspection, headless=False)
    try:
        async with component_timeout('Workflow viewer browser launch', 120):
            session = await browser.start()
        async with component_timeout('Workflow viewer navigation', 30):
            response = await session.page.goto(host.entry_url, wait_until='load')
            if response is None or response.status != 200:
                raise RuntimeError('Workflow viewer entry did not load')
        if session.inspection_endpoint:
            print(f'Workflow browser CDP: {session.inspection_endpoint}', flush=True)
        print(f'Workflow definition: {definition.definition_id}' +
              ('' if canonical else ' (selected variant)'), flush=True)
        print(f'Workflow view: {host.entry_url}', flush=True)
        await inspection.wait_closed()
    finally:
        async def dispose(owner):
            reported = False
            async with component_timeout('Workflow viewer cleanup retry', None):
                while True:
                    try:
                        await owner.aclose()
                        return
                    except CleanupPendingError:
                        if not reported:
                            print('Workflow viewer cleanup remains pending; retrying owned resources.', flush=True)
                            reported = True
                        await asyncio.sleep(1)

        async def cleanup():
            try:
                await dispose(browser)
            finally:
                try:
                    await dispose(inspection)
                finally:
                    await host.aclose()
        await finish_cleanup(cleanup())


async def view_flow(context):
    await _serve_view(_prepare_view(context))


def run_view_flow(context):
    try:
        prepared = _prepare_view(context)
        asyncio.run(_serve_view(prepared))
    except KeyboardInterrupt:
        return 130
    return 0
