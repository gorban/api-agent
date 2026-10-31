"""Standalone managed local trace dashboard with operation-owned cleanup."""
import asyncio
from dataclasses import replace
from threading import Event
from urllib.parse import urlsplit

from api_agent.config.declarations import ConfigSchema
from api_agent.config.resolve import ConfigError, resolve_config
from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import component_timeout, report_timeout
from api_agent.providers.sessions import InspectionOwner
from api_agent.runtime.compose import ComposeError, selected_profiles
from api_agent.runtime.local_tracing import DashboardError, LocalTracing
from api_agent.runtime.managed_browser import managed_browser
from api_agent.telemetry.privacy import EmissionBoundary
from api_agent.testing.prerequisites import local_trace_prerequisites

_FIELDS = frozenset({'TRACE_MODE', 'TRACE_TARGET', 'TRACE_VIEWER_URL', 'JAEGER_VIEWER_PORT',
                     'LANGFUSE_ADMIN_EMAIL', 'LANGFUSE_ADMIN_PASSWORD', 'LANGFUSE_PUBLIC_KEY',
                     'LANGFUSE_SECRET_KEY', 'CLIENT_CA_FILE'})


def resolve_viewer_config(schema, *, argv, environ, dotenv, anchors):
    """Use normal typed precedence without unrelated execution requirements."""
    fields = {}
    for name in _FIELDS:
        if name in schema.fields:
            declaration = schema.fields[name]
            applies = {key: value for key, value in declaration.applies_when.items() if key == 'TRACE_MODE'}
            required = {key: value for key, value in declaration.required_when.items() if key == 'TRACE_MODE'}
            fields[name] = replace(declaration, applies_when=applies, required_when=required)
    result = resolve_config(ConfigSchema(fields), argv=argv, environ=environ, dotenv=dotenv, anchors=anchors, interactive=True)
    result.raise_for_errors()
    if result.positionals:
        raise ConfigError('Trace viewer does not accept positional arguments.')
    values = result.values
    for name in ('TRACE_TARGET', 'TRACE_VIEWER_URL'):
        value = values.get(name)
        if value and (len(value) > 2048 or any(ord(character) < 33 or ord(character) > 126 for character in value)):
            raise ConfigError('Invalid trace destination configuration.')
    try:
        profiles = selected_profiles(values)
    except ComposeError:
        raise ConfigError('Invalid trace destination configuration.') from None
    if profiles not in (('langfuse',), ('otel',)):
        raise ConfigError('Trace viewer requires an enabled managed local tracing destination.')
    if result.missing:
        raise ConfigError('Trace viewer requires the configured local dashboard credentials.')
    if values.get('TRACE_MODE') == 'OTEL' and values.get('TRACE_VIEWER_URL'):
        try:
            url = urlsplit(values['TRACE_VIEWER_URL'])
            port = url.port
        except ValueError:
            raise ConfigError('Trace viewer requires the managed local Jaeger dashboard.') from None
        if (url.scheme != 'http' or url.hostname not in {'localhost', '127.0.0.1'}
                or port != values.get('JAEGER_VIEWER_PORT', 16686)
                or url.path not in ('', '/') or url.query or url.fragment or url.username is not None):
            raise ConfigError('Trace viewer requires the managed local Jaeger dashboard.')
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
    cancelled = Event()
    manager = local_trace_prerequisites(context, values, cancel_event=cancelled)
    if manager is None:
        raise ConfigError('Trace viewer requires an enabled managed local tracing destination.')
    inspection = InspectionOwner()
    dashboard = LocalTracing(values, emissions=EmissionBoundary())
    browser = None
    acquisition = None
    try:
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
            await _dispose(manager, label='collector')
        await finish_cleanup(cleanup())


def run_trace_viewer(context, values):
    try:
        asyncio.run(view_trace(context, values))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('Trace viewer could not start; check the managed tracing configuration and dashboard readiness.', flush=True)
        return 2
    return 0
