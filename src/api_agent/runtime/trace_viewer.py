"""Standalone managed local trace dashboard with operation-owned cleanup."""
import asyncio
from threading import Event

from api_agent.config.resolve import ConfigError
from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import component_timeout, report_timeout
from api_agent.providers.sessions import InspectionOwner
from api_agent.runtime.local_tracing import DashboardError, LocalTracing
from api_agent.runtime.managed_browser import managed_browser, prepare_managed_browser
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.testing.prerequisites import local_trace_prerequisites

def resolve_viewer_config(schema, *, argv, environ, dotenv, anchors):
    """Resolve only the selected provider's typed dashboard configuration."""
    from api_agent.runtime.tracing_viewer_config import custom_viewer_config
    values = custom_viewer_config(schema, argv=argv, environ=environ, dotenv=dotenv, anchors=anchors)
    if values is None:
        raise ConfigError('Trace viewer requires an enabled tracing provider profile; TRACE_PROVIDER is omitted or empty.')
    return values


async def _dispose(owner, *, label, inspection=None):
    reported = False
    async with component_timeout('Trace viewer cleanup retry', None):
        while True:
            try:
                if label == 'collector':
                    async with report_timeout('Trace collector release', owner.cleanup_timeout) as budget:
                        task = asyncio.create_task(asyncio.to_thread(owner.release))
                        done, _ = await asyncio.wait({task}, timeout=budget.remaining)
                        if not done and not reported:
                            print('Trace viewer collector cleanup remains pending; retrying owned resources.', flush=True)
                            reported = True
                        await asyncio.shield(task)
                else:
                    await owner.aclose()
                return
            except Exception:
                if inspection is owner:
                    try:
                        if await owner.settle_terminated_inspection():
                            return
                    except Exception:
                        pass
                if not reported:
                    print(f'Trace viewer {label} cleanup remains pending; retrying owned resources.', flush=True)
                    reported = True
                await asyncio.sleep(1)


async def view_trace(context, values):
    await prepare_managed_browser(headless=False)
    cancelled = Event()
    manager = local_trace_prerequisites(context, values, cancel_event=cancelled, require_export=False)
    if manager is None and not values.get('TRACE_PROVIDER'):
        raise ConfigError('Trace viewer requires an enabled managed local tracing destination.')
    inspection = InspectionOwner()
    dashboard = LocalTracing(values, emissions=EmissionBoundary(),
        **({'owner_root': context.owner_root} if values.get('TRACE_PROVIDER') else {}))
    browser = None
    acquisition = None
    try:
        if manager is not None:
            acquisition = asyncio.create_task(asyncio.to_thread(manager.acquire))
            try:
                async with component_timeout('Trace collector acquisition', manager.timeout):
                    await asyncio.shield(acquisition)
            except BaseException:
                cancelled.set()
                async def drain_acquisition():
                    await asyncio.gather(acquisition, return_exceptions=True)
                await finish_cleanup(drain_acquisition())
                raise
        await dashboard.start()
        if dashboard.viewer_url is None:
            raise DashboardError('Managed trace dashboard is unavailable; check collector readiness.')
        browser = managed_browser(inspection=inspection, headless=False)
        async with component_timeout('Trace viewer browser launch', 120):
            session = await browser.start()
        await dashboard.login(session.page)
        if session.inspection_endpoint:
            print(f'Trace browser CDP: {session.inspection_endpoint}', flush=True)
        print(f'Trace viewer: {dashboard.viewer_url}', flush=True)
        credentials = dashboard.interactive_credentials()
        if credentials is not None:
            username, password = credentials
            print(f'  Username: {username}', flush=True)
            print(f'  Password: {password}', flush=True)
        print('Press Ctrl+C to stop the trace viewer and release its collector lease.', flush=True)
        # The dashboard may also be open in a browser that this process does
        # not own. Its lifetime belongs to the command, not the inspection tab.
        await asyncio.Event().wait()
    finally:
        async def cleanup():
            if browser is not None:
                await _dispose(browser, label='browser')
            await _dispose(inspection, label='browser inspection', inspection=inspection)
            await _dispose(dashboard, label='dashboard')
            if manager is not None:
                await _dispose(manager, label='collector')
        await finish_cleanup(cleanup())


def run_trace_viewer(context, values):
    try:
        asyncio.run(view_trace(context, values))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        if isinstance(error, ConfigError):
            hint = 'Check the selected tracing provider configuration.'
        elif isinstance(error, DashboardError):
            hint = 'Check the dashboard URL and managed collector readiness.'
        else:
            hint = 'Check managed tracing configuration, collector readiness, and browser prerequisites.'
        print('Trace viewer could not start; ' + hint, flush=True)
        return 2
    return 0
