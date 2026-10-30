"""Shared consumer ownership for completed runs with retained inspection."""
import asyncio
from threading import Lock


class ReceiptInspection:
    """Expose a retained cleanup receipt through the existing consumer actions."""
    lost = False
    retains_browser = False

    def __init__(self, error):
        from .suite_lock import _cleanup_receipts
        self.error = error
        self._receipts = tuple(_cleanup_receipts(error))
        self.closed = False
        self._guard = Lock()

    @property
    def pending(self):
        return not self.closed

    async def aclose(self):
        from api_agent.providers.contracts import ContractError
        from .suite_lock import _cleanup_receipts
        if not self._guard.acquire(blocking=False):
            raise ContractError('Cleanup receipt recovery is already running.')
        try:
            if self.closed:
                return
            recording = getattr(self.error, 'recording_prerequisites', None)
            seen = set()
            receipts = (*self._receipts, *_cleanup_receipts(self.error),
                *_cleanup_receipts(getattr(recording, 'pending_cleanup', None)))
            for pending in receipts:
                group = getattr(pending, 'inspection_group', None)
                # Cancellation can attach this consumer's own group retry.
                # Retrying it from here would recursively revisit ourselves.
                if (id(pending) in seen or group is not None and
                        any(handle is self for handle in group.handles)):
                    continue
                seen.add(id(pending))
                await pending.retry()
            self.closed = True
        finally:
            self._guard.release()

    def end_inspection(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(self.aclose())
            except Exception:
                return False
            return True
        return False  # Synchronous consumers must offload from an active loop.

    def abort(self):
        return self.end_inspection()

    def wait_closed(self):
        if not self.end_inspection():
            raise self.error


def preserve_cleanup_receipts(error):
    from .suite_lock import _cleanup_receipts
    if not any(_cleanup_receipts(error)):
        return
    handles = tuple(getattr(error, 'inspections', ()))
    if not any(isinstance(handle, ReceiptInspection) and handle.error is error for handle in handles):
        error.inspections = (*handles, ReceiptInspection(error))


def cleanup_active(handle):
    """Resource closure does not acknowledge a pending suite release."""
    return (not handle.closed or getattr(handle, 'pending', False)
            or bool(getattr(handle, '_cleanup_callbacks', ())))


class InspectionGroup:
    def __init__(self, handles=()):
        self.handles = tuple(handles)
        self.diagnostic_errors = []

    @property
    def active(self):
        return any(cleanup_active(handle) for handle in self.handles)

    @property
    def endpoints(self):
        """Deduplicate retained loopback browsers, without assigning them to tests."""
        import re
        endpoints = []
        for handle in self.handles:
            if handle.closed:
                continue
            for endpoint in getattr(handle, 'inspection_endpoints', ()):
                if (isinstance(endpoint, str)
                        and re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}', endpoint)
                        and int(endpoint.rsplit(':', 1)[1]) <= 65535
                        and endpoint not in endpoints):
                    endpoints.append(endpoint)
        return tuple(endpoints)

    @property
    def pending(self):
        return any(getattr(handle, 'pending', False) or (handle.closed and cleanup_active(handle))
                   for handle in self.handles)

    @property
    def lost(self):
        return any(getattr(handle, 'lost', False) for handle in self.handles)

    def cleanup_receipt(self):
        from api_agent.providers.control import CleanupPendingError
        error = CleanupPendingError((self,), self.aclose)
        error.inspection_group = self
        error.inspections = self.handles
        error.diagnostic_errors = tuple(self.diagnostic_errors)
        return error

    async def aclose(self):
        from api_agent.runtime.tasks import offload
        if not await offload(self.close, force=True):
            raise self.cleanup_receipt() from None

    def close(self, *, force=False):
        """End inspection synchronously; retain every unacknowledged handle."""
        interruption = None
        for handle in self.handles:
            if not cleanup_active(handle):
                continue
            try:
                acknowledged = handle.end_inspection()
            except BaseException as error:
                self.diagnostic_errors.append(error)
                if not isinstance(error, Exception):
                    interruption = interruption or error
                acknowledged = False
            if not acknowledged and force:
                try:
                    handle.abort()
                except BaseException as error:
                    self.diagnostic_errors.append(error)
                    if not isinstance(error, Exception):
                        interruption = interruption or error
        if interruption is not None:
            interruption.inspection_group = self
            interruption.inspections = self.handles
            interruption.diagnostic_errors = tuple({id(item): item for item in (
                *getattr(interruption, 'diagnostic_errors', ()), *self.diagnostic_errors)
                if item is not interruption}.values())
            raise interruption
        return not self.active


def finish_terminal_inspection(recorded, output, *, diagnostic_output=None):
    """Print completed results before waiting for the debug windows to close."""
    execution = getattr(recorded, 'execution', None)
    if execution is None:
        return True
    from .contracts import format_test_summary
    group = InspectionGroup(getattr(execution, 'inspections', ()))
    try:
        primary = None
        inspection_interrupt = None
        try:
            if diagnostic_output is not None:
                from .diagnostics import result_diagnostics
                for details in result_diagnostics(execution.tests):
                    diagnostic_output(details)
            output(format_test_summary('integration', execution.tests,
                state='CANCELLED' if execution.cancelled else None, elapsed=getattr(recorded, 'elapsed', None)))
            publication = ('SKIPPED' if getattr(recorded, 'reporting_skipped', False)
                           else 'PASS' if recorded.persisted else 'FAIL')
            output('Allure publication: ' + publication)
            if getattr(recorded, 'usage', None):
                from api_agent.telemetry.usage_output import final_usage_text
                output(final_usage_text(recorded.usage))
            for endpoint in group.endpoints:
                output('Browser CDP (retained suite browser): ' + endpoint)
            if group.active:
                if any(not handle.closed and getattr(handle, 'retains_browser', True) for handle in group.handles):
                    output('Debug inspection remains open. Tests and publication have finished; close the browser windows or press Ctrl+C to end inspection.')
                else:
                    output('Shared authentication cleanup remains pending. Tests and publication have finished; retrying owned cleanup.')
                try:
                    for handle in group.handles:
                        handle.wait_closed()
                except KeyboardInterrupt as error:
                    # Results and publication are finalized. Ctrl+C now requests
                    # owned inspection cleanup, preserving the suite outcome.
                    inspection_interrupt = error
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                cleaned = group.close(force=True)
            except BaseException:
                if primary is not None:
                    raise primary
                if inspection_interrupt is not None:
                    raise inspection_interrupt
                raise
        if inspection_interrupt is not None and group.diagnostic_errors:
            raise inspection_interrupt
        if not cleaned or group.lost:
            output('Inspection cleanup: FAIL — worker exited unexpectedly or cleanup remains pending.')
            if not cleaned:
                raise group.cleanup_receipt() from None
            return False
        return True
    except BaseException as error:
        error.inspection_group = group
        error.inspections = group.handles
        error.diagnostic_errors = tuple({id(item): item for item in (
            *getattr(error, 'diagnostic_errors', ()), *group.diagnostic_errors)
            if item is not error}.values())
        raise
