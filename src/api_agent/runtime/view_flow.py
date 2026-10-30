"""Standalone, nonexecuting workflow browser lifecycle."""
import asyncio
import os
from dataclasses import replace
from pathlib import Path

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
            and definition.entry.name == 'flow.md.html'
            and definition.entry.is_relative_to(definition.roots[owner])
            and all(document.logical_path.startswith(owner + '/')
                    for document in projection.documents)
            and all(resource.logical_path.startswith((owner + '/', 'inline/' + owner + '/'))
                    for resource in projection.resources))


def _view_context(context, entry):
    """Select an explicit source graph using its declared resource ownership."""
    from api_agent.workflows.definition import _read_bounded
    from api_agent.workflows.viewer_build import read_view_roots, VIEW_ROOTS_PATH
    from api_agent.runtime.resource_pack import ResourcePackError
    path = Path(os.path.abspath(Path(entry).expanduser()))
    roots = tuple(dict.fromkeys((context.base_root,
                  context.workflow_base_root or context.base_root, context.pack_root)))
    containing = sorted((root for root in roots if path.is_relative_to(root)),
                        key=lambda root: len(root.parts), reverse=True)
    if not containing:
        raise ResourcePackError('Viewer flow must be inside the selected application or resource pack')
    _read_bounded(path, containing[0])
    path = path.resolve()
    library = context.workflow_base_root or context.base_root
    owned = context.workflow_library_files
    mapping = None
    for owner in containing:
        mapping_path = owner / VIEW_ROOTS_PATH
        if (owned is not None and mapping_path.is_relative_to(library)
                and (mapping_path.exists() or mapping_path.is_symlink())
                and mapping_path.relative_to(library).as_posix() not in owned):
            raise ResourcePackError('Workflow view mapping is not owned by installed distribution')
        mapping = read_view_roots(owner).get(path)
        if mapping is not None:
            break
    if mapping is not None:
        mapped_owned = None
        if owned is not None and mapping.base_root.is_relative_to(library):
            prefix = mapping.base_root.relative_to(library)
            mapped_owned = frozenset(Path(item).relative_to(prefix).as_posix()
                                    for item in owned if Path(item).is_relative_to(prefix))
        return replace(context, workflow_entry=path, workflow_root=mapping.entry_root,
                       base_root=mapping.base_root, workflow_base_root=None, pack_root=mapping.pack_root,
                       workflow_replacements=(), workflow_library_files=mapped_owned)
    if path == (context.workflow_entry or (context.workflow_base_root or context.base_root) / 'flow.md.html'):
        return context
    root_id = 'pack' if path.is_relative_to(context.pack_root) else 'base'
    return replace(context, workflow_entry=path, workflow_root=root_id, workflow_replacements=())


def _prepare_view(context, *, entry=None):
    from api_agent.workflows.viewer_projection import project_view
    from api_agent.workflows.viewer_render import (StaleViewError, build_view_bundle,
                                                    capture_repository_sources)
    original = context
    if entry is not None:
        context = _view_context(context, entry)
    definition = load_selected_definition(context)
    if entry is not None and original.workflow_library_files is not None:
        library = original.workflow_base_root or original.base_root
        selected = {path.relative_to(library).as_posix() for path in definition.contents
                    if isinstance(path, Path) and path.is_relative_to(library)}
        if not selected <= original.workflow_library_files:
            from api_agent.runtime.resource_pack import ResourcePackError
            raise ResourcePackError('Selected workflow file is not owned by installed distribution')
    projection = project_view(definition)
    canonical = _canonical_selection(context, definition, projection)
    repositories, repository_sources = capture_repository_sources(definition.roots)
    try:
        bundle = build_view_bundle(projection, roots=definition.roots, verify=canonical,
                                   check_orphans=False,
                                   repositories=repositories, repository_sources=repository_sources)
    except StaleViewError:
        # Build a temporary view of the captured working tree. The saved static
        # diagrams and the commit-time check remain untouched.
        from api_agent.workflows.viewer_build import prepare_renderer
        import os
        from api_agent.workflows.viewer_render import PlantUMLRenderer, default_renderer
        print('Workflow diagrams changed; preparing a temporary view.', flush=True)
        renderer = (default_renderer() if os.environ.get('API_AGENT_PLANTUML_JAR')
                    else PlantUMLRenderer(prepare_renderer()))
        bundle = build_view_bundle(projection, roots=definition.roots, verify=False,
                                   renderer=renderer, check_orphans=False,
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
                        settle = getattr(owner, 'settle_terminated_inspection', None)
                        if owner is inspection and settle is not None and await settle():
                            return
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


async def view_flow(context, *, entry=None):
    from api_agent.runtime.frontend import prepare_source_frontend
    await prepare_source_frontend()
    await _serve_view(_prepare_view(context) if entry is None else _prepare_view(context, entry=entry))


def run_view_flow(context, *, entry=None):
    try:
        from api_agent.runtime.frontend import ensure_source_frontend
        ensure_source_frontend()
        prepared = _prepare_view(context) if entry is None else _prepare_view(context, entry=entry)
        asyncio.run(_serve_view(prepared))
    except KeyboardInterrupt:
        return 130
    return 0
