"""Terminal presentation for one same-loop local operation."""
import asyncio
import sys


def format_log(event, source):
    """Keep single-source bytes intact and label each combined-stream line."""
    if source != 'all':
        return event.text if event.source == source else ''
    return ''.join(f'[{event.source}] {line}' for line in event.text.splitlines(keepends=True))


async def run_local_terminal(context, resolve, *, argv=(), schema=None, owner_factory=None,
                             output=None, diagnostic=None):
    """Run without stdin; retain cleanup receipts when disposal cannot finish."""
    output = output or (lambda text: print(text, end='', flush=True))
    diagnostic = diagnostic or (lambda text: print(text, file=sys.stderr, flush=True))
    source = resolve().values['RUN_LOG_SOURCE']
    def event_received(event):
        if event.kind == 'log':
            output(format_log(event, source))
        elif event.kind == 'status' and event.snapshot is not None:
            snapshot = event.snapshot
            diagnostic(f'Run local: {snapshot.phase}')
            for destination in snapshot.destinations:
                diagnostic(f'{destination.name}: {destination.url}')
            if snapshot.inspection_endpoint:
                diagnostic(f'Browser CDP: {snapshot.inspection_endpoint}')
    options = {'schema': schema} if schema is not None else {}
    owner = None
    status = 0
    try:
        if owner_factory is None:
            from .local_run import LocalRunOwner
            owner_factory = LocalRunOwner
        owner = owner_factory(context, resolve, argv=argv, on_event=event_received, **options)
        await owner.start()
        status = await owner.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        if owner is not None:
            owner.request_stop()
        status = 130
    except Exception as error:
        diagnostic(owner.diagnostic(error) if owner is not None else 'Local operation could not start.')
        status = 1
    finally:
        try:
            if owner is not None:
                await owner.aclose()
        except BaseException as error:
            pending = owner.pending_cleanup or getattr(error, 'cleanup_pending', None)
            if not isinstance(error, Exception):
                if pending is not None:
                    error.cleanup_pending = pending
                raise
            if pending is not None:
                from api_agent.providers.control import CleanupPendingError
                safe = CleanupPendingError((owner,), pending.retry)
                safe.cleanup_pending = pending
                raise safe from None
            raise RuntimeError(owner.diagnostic(error)) from None
    return status


async def open_host_url(url):
    """Delegate a verified destination to the host's default URL handler."""
    import os
    import shutil
    import subprocess
    import webbrowser
    from api_agent.providers.progress import component_timeout
    from api_agent.runtime.tasks import offload
    async with component_timeout('Open local browser destination', 15):
        if os.environ.get('DISTROBOX_ENTER_PATH'):
            host = shutil.which('distrobox-host-exec')
            if host is None:
                raise RuntimeError('Host browser forwarding is unavailable; open the displayed URL on the host.')
            await offload(subprocess.run, [host, 'xdg-open', url], check=True, timeout=10,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif not await offload(webbrowser.open, url, new=2):
            raise RuntimeError('The default browser could not open the displayed URL.')
