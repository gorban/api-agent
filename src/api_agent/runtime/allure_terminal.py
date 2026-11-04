"""Terminal lifetime for the shared Allure viewer and reporting leases."""
import asyncio
import sys
from types import SimpleNamespace

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.allure_history import AllureHistoryOwner
from api_agent.testing.diagnostics import CredentialScrubber, format_exception, terminal_exception
from api_agent.tui.reporting import ReportingSessions


class TerminalAllureOwner(AllureHistoryOwner):
    def __init__(self, context, result):
        self.context = context
        self.session = SimpleNamespace(result=lambda: result)
        self.allure_viewer = None
        self.reporting_sessions = ReportingSessions()
        self._closed_owners = set()

    async def start(self):
        await self.prepare_allure_reporting()
        return await self._allure().show_archive(self.allure_source())

    async def aclose(self):
        failures = []
        for owner in (self.allure_viewer, self.reporting_sessions):
            if owner is None or id(owner) in self._closed_owners:
                continue
            try:
                await owner.aclose()
            except BaseException as error:
                failures.append(error)
            else:
                self._closed_owners.add(id(owner))
        if failures:
            pending = CleanupPendingError((self,), self.aclose)
            error = failures[0]
            if isinstance(error, CleanupPendingError):
                raise pending from None
            error.cleanup_pending = pending
            raise error


async def run_allure_terminal(context, result, *, owner_factory=TerminalAllureOwner,
                              output=None, diagnostic=None, wait=None):
    """Keep real SIGINT ownership until same-loop disposal is acknowledged."""
    from api_agent.runtime.local_terminal import _terminal_stop_signals
    output = output or (lambda text: print(text, flush=True))
    diagnostic = diagnostic or (lambda text: print(text, file=sys.stderr, flush=True))
    delivery_errors = []
    def stop_notice(text):
        try:
            diagnostic(text)
        except Exception as error:
            delivery_errors.append(error)
    with _terminal_stop_signals(stop_notice):
        status = await _run_allure_terminal(context, result, owner_factory=owner_factory,
            output=output, diagnostic=diagnostic, wait=wait)
    if delivery_errors:
        raise delivery_errors[0]
    return status


async def _run_allure_terminal(context, result, *, owner_factory, output, diagnostic, wait):
    """Keep owned resources alive until interruption; never read stdin."""
    scrub = CredentialScrubber(result.values)
    owner = owner_factory(context, result)
    status = 0
    try:
        try:
            location = await owner.start()
        except (asyncio.CancelledError, KeyboardInterrupt):
            return 130
        except Exception as error:
            diagnostic(scrub(format_exception(error, boundary='Allure history startup')))
            return 1
        output(scrub(f'Allure history: {location.url}'))
        if location.inspection_endpoint:
            output(scrub(f'Browser CDP: {location.inspection_endpoint}'))
        if location.browser_error:
            diagnostic('Allure browser could not open; use the displayed history URL.')
        output('Press Ctrl+C to stop Allure and close owned resources.')
        try:
            await (wait() if wait is not None else asyncio.Event().wait())
        except (asyncio.CancelledError, KeyboardInterrupt):
            status = 130
    finally:
        async def close_until_acknowledged():
            failure = None
            delivery_failure = None
            while True:
                try:
                    await owner.aclose()
                    break
                except BaseException as error:
                    pending = error if isinstance(error, CleanupPendingError) else getattr(error, 'cleanup_pending', None)
                    if pending is None:
                        raise terminal_exception(error, scrub, boundary='Allure cleanup') from None
                    if failure is None:
                        failure = error
                        try:
                            diagnostic(scrub(format_exception(error, boundary='Allure cleanup pending; retrying owned resources')))
                        except Exception as error:
                            delivery_failure = error
                    await asyncio.sleep(1)
            if delivery_failure is not None:
                raise delivery_failure
            if failure is not None:
                wrapped = terminal_exception(failure, scrub, boundary='Allure cleanup completed after failure')
                del wrapped.cleanup_pending
                wrapped.terminal_cleanup_acknowledged = True
                raise wrapped from None
        cleanup = asyncio.create_task(close_until_acknowledged())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                status = 130
        cleanup.result()
    return status
