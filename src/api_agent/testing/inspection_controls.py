"""Shared consumer ownership for completed runs with retained inspection."""


class InspectionGroup:
    def __init__(self, handles=()):
        self.handles = tuple(handles)

    @property
    def active(self):
        return any(not handle.closed for handle in self.handles)

    @property
    def lost(self):
        return any(getattr(handle, 'lost', False) for handle in self.handles)

    def cleanup_receipt(self):
        from api_agent.providers.control import CleanupPendingError
        error = CleanupPendingError((self,), self.aclose)
        error.inspection_group = self
        error.inspections = self.handles
        return error

    async def aclose(self):
        from api_agent.runtime.tasks import offload
        if not await offload(self.close, force=True):
            raise self.cleanup_receipt() from None

    def close(self, *, force=False):
        """End inspection synchronously; retain every unacknowledged handle."""
        interruption = None
        for handle in self.handles:
            if handle.closed:
                continue
            try:
                acknowledged = handle.end_inspection()
            except BaseException as error:
                if not isinstance(error, Exception):
                    interruption = interruption or error
                acknowledged = False
            if not acknowledged and force:
                try:
                    handle.abort()
                except BaseException as error:
                    if not isinstance(error, Exception):
                        interruption = interruption or error
        if interruption is not None:
            interruption.inspection_group = self
            interruption.inspections = self.handles
            raise interruption
        return not self.active


def finish_terminal_inspection(recorded, output):
    """Print completed results before waiting for the debug windows to close."""
    execution = getattr(recorded, 'execution', None)
    if execution is None:
        return True
    from .contracts import format_test_summary
    group = InspectionGroup(getattr(execution, 'inspections', ()))
    try:
        try:
            output(format_test_summary('integration', execution.tests,
                state='CANCELLED' if execution.cancelled else None))
            output('Allure publication: ' + ('PASS' if recorded.persisted else 'FAIL'))
            if group.active:
                output('Debug inspection remains open. Tests and publication have finished; close the browser windows or press Ctrl+C to end inspection.')
                for handle in group.handles:
                    handle.wait_closed()
        finally:
            cleaned = group.close(force=True)
        if not cleaned or group.lost:
            output('Inspection cleanup: FAIL — worker exited unexpectedly or cleanup remains pending.')
            if not cleaned:
                raise group.cleanup_receipt() from None
            return False
        return True
    except BaseException as error:
        error.inspection_group = group
        error.inspections = group.handles
        raise
