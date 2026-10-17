"""Workbench-owned Allure navigation, independent of test execution."""
import asyncio
from textual.widgets import DataTable, Input, Static


class AllureNavigation:
    def initialize_allure(self):
        self.allure_viewer = None
        self.allure_snapshots = {}
        self._allure_generation = object()
        self._allure_opening = False

    def receive_allure_snapshot(self, snapshot, generation):
        if generation is self._inspection_generation and not self._inspection_shutdown:
            for identity in snapshot.result_files:
                self.allure_snapshots[identity] = snapshot
            self.screen.refresh_bindings()

    def allure_runner_options(self, runner, generation):
        import inspect
        parameters = inspect.signature(runner).parameters
        if 'on_snapshot' in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            return {'on_snapshot': lambda snapshot: self.call_from_thread(
                self.receive_allure_snapshot, snapshot, generation)}
        return {}

    def selected_allure_result(self, screen):
        if (self.mode not in {'integration', 'e2e'} or screen.kind != 'test-results'
                or self.screen is not screen or isinstance(self.focused, Input)
                or self._allure_opening):
            return None
        if not screen.query(DataTable):
            return None
        table = screen.query_one(DataTable)
        keys = table.selected_row_keys
        if len(keys) != 1:
            return None
        key = keys[0]
        snapshot = self.allure_snapshots.get(key)
        if self.unit_rows.get(key, ('PENDING',))[0] not in {
                'PASSED', 'FAILED', 'ERROR', 'BROKEN', 'SKIPPED', 'CANCELLED', 'INTERRUPTED'}:
            return None
        return (snapshot, snapshot.result_files[key]) if snapshot is not None else (None, None)

    def _allure(self):
        if self.allure_viewer is None:
            from api_agent.testing.allure_viewer import ManagedAllureViewer
            self.allure_viewer = ManagedAllureViewer(
                self.session.result().values.get('ALLURE_VIEWER_PORT', 5252))
        return self.allure_viewer

    async def open_selected_allure(self):
        selected = self.selected_allure_result(self.screen)
        if selected is None:
            return
        if selected[0] is None:
            self.notify('Allure evidence is unavailable for this test attempt; check publication details.',
                        severity='error', timeout=10)
            return
        generation = self._allure_generation
        self._allure_opening = True
        self.screen.refresh_bindings()
        try:
            location = await self._allure().show_preview(*selected)
            if generation is self._allure_generation:
                self.show_allure_location(location)
        except Exception as error:
            if generation is self._allure_generation:
                self.notify(str(error), severity='error', timeout=10)
        finally:
            if generation is self._allure_generation:
                self._allure_opening = False
                self.screen.refresh_bindings()

    def show_allure_location(self, location):
        message = 'Allure: ' + location.url
        if location.inspection_endpoint:
            message += '\nBrowser inspection: ' + location.inspection_endpoint
        if location.browser_error:
            message += '\n' + location.browser_error
        if self.screen.kind == 'allure':
            self.screen.query_one('#body', Static).update(message)
        else:
            self.notify(message, timeout=15)

    async def show_allure_history(self):
        from api_agent.testing.allure_viewer import RunSource
        from api_agent.testing.storage import RunStorage
        from api_agent.testing.collection import repository_label
        generation = self._allure_generation
        self._allure_opening = True
        try:
            await self.show('allure', 'Allure history', 'Preparing recorded integration/E2E runs…',
                            (('refresh-allure', 'Refresh history'),))
            if generation is not self._allure_generation:
                return
            values = self.session.result().values
            storage = None
            if values.get('ALLURE_S3_BUCKET'):
                storage = RunStorage(values['ALLURE_S3_BUCKET'],
                    prefix=values.get('ALLURE_S3_PREFIX', 'allure'),
                    endpoint_url=values.get('ALLURE_S3_ENDPOINT'),
                    region_name=values.get('ALLURE_S3_REGION'),
                    localstack_port=values.get('ALLURE_LOCALSTACK_PORT', 4566),
                    managed_local=values.get('ALLURE_S3_USE_LOCALSTACK', False))
            source = RunSource(repository_label(self.context, self.context.owner_root),
                               local_root=values.get('ALLURE_RESULTS_DIR') or None, storage=storage)
            location = await self._allure().show_history(source)
            if generation is self._allure_generation:
                self.operation_status = 0
                self.show_allure_location(location)
        except Exception as error:
            if generation is self._allure_generation and self.screen.kind == 'allure':
                self.operation_status = 3
                self.screen.query_one('#error', Static).update(str(error))
        finally:
            if generation is self._allure_generation:
                self._allure_opening = False

    async def close_allure(self):
        self._allure_generation = object()
        viewer = self.allure_viewer
        if viewer is None:
            self._allure_opening = False
            return True
        try:
            await viewer.aclose()
        except Exception as error:
            self.notify('Allure cleanup incomplete: ' + str(error), severity='error', timeout=10)
            return False
        self.allure_viewer = None
        self._allure_opening = False
        return True
