"""Workbench-owned Allure navigation, independent of test execution."""
import asyncio
from functools import cached_property
from uuid import uuid4
from rich.segment import Segment
from rich.style import Style as RichStyle
from textual.content import Content
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.style import Style
from textual.widgets import DataTable, Input, Static

from .clipboard import ClipboardError, copy_text

MAX_RETAINED_ALLURE_BYTES = 128 * 1024 * 1024


class AllureLinkContent(Content):
    """Retain one action identity after Content adds per-line selection offsets."""
    @cached_property
    def _link_styles(self):
        return {}

    @cached_property
    def _link_scope(self):
        return uuid4().hex

    def render_strips(self, width, height, style, options):
        links = self._link_styles
        result = []
        for strip in super().render_strips(width, height, style, options):
            segments = []
            for segment in strip:
                action = segment.style.meta.get('@click')
                if action is not None:
                    shared = links.setdefault(action, RichStyle(meta={
                        '@click': action, 'allure_link_scope': self._link_scope}))
                    segment = Segment(segment.text, segment.style + shared, segment.control)
                segments.append(segment)
            result.append(Strip(segments, strip.cell_length))
        return result


class AllureNavigation:
    def initialize_allure(self):
        self.allure_viewer = None
        self.allure_snapshots = {}
        self._allure_budget_unavailable = set()
        self._allure_retained_bytes = 0
        self._allure_generation = object()
        self._allure_opening = False

    def receive_allure_snapshot(self, snapshot, generation):
        if generation is self._inspection_generation and not self._inspection_shutdown:
            # A new attempt supersedes its old binding even if the new copy is
            # too large to retain. Final snapshots share one copy across tests.
            for identity in snapshot.result_files:
                self.allure_snapshots.pop(identity, None)
                self._allure_budget_unavailable.discard(identity)
            unique = {id(item): item for item in self.allure_snapshots.values()}
            retained = sum(len(body) for item in unique.values() for body in item.files.values())
            incoming = 0 if id(snapshot) in unique else sum(len(body) for body in snapshot.files.values())
            if retained + incoming <= MAX_RETAINED_ALLURE_BYTES:
                for identity in snapshot.result_files:
                    self.allure_snapshots[identity] = snapshot
            else:
                self._allure_budget_unavailable.update(snapshot.result_files)
            self._allure_retained_bytes = retained + (incoming if incoming and
                retained + incoming <= MAX_RETAINED_ALLURE_BYTES else 0)
            self.screen.refresh_bindings()

    def clear_allure_snapshots(self):
        self.allure_snapshots.clear()
        self._allure_budget_unavailable.clear()
        self._allure_retained_bytes = 0

    def allure_runner_options(self, runner, generation):
        import inspect
        parameters = inspect.signature(runner).parameters
        if 'on_snapshot' in parameters or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            return {'on_snapshot': lambda snapshot: self.call_from_thread(
                self.receive_allure_snapshot, snapshot, generation)}
        return {}

    def selected_allure_key(self, screen):
        if (self.mode not in {'integration', 'e2e'} or getattr(screen, 'kind', None) not in {'test-results', 'test-failure'}
                or self.screen is not screen or isinstance(self.focused, Input)
                or self._allure_opening):
            return None
        if screen.kind == 'test-failure':
            result, key, _ = getattr(screen, 'result_return', (None, None, None))
            if result is None or result is not self.unit_result:
                return None
        else:
            if not screen.query(DataTable):
                return None
            keys = screen.query_one(DataTable).selected_row_keys
            if len(keys) != 1:
                return None
            key = keys[0]
        if self.unit_rows.get(key, ('PENDING',))[0] not in {
                'PASSED', 'FAILED', 'ERROR', 'BROKEN', 'SKIPPED', 'CANCELLED', 'INTERRUPTED'}:
            return None
        return key

    def selected_allure_result(self, screen):
        key = self.selected_allure_key(screen)
        if key is None:
            return None
        snapshot = self.allure_snapshots.get(key)
        return (snapshot, snapshot.result_files[key]) if snapshot is not None else (None, None)

    def _allure(self):
        if self.allure_viewer is None:
            from api_agent.testing.allure_viewer import ManagedAllureViewer
            values = self.session.result().values
            self.allure_viewer = ManagedAllureViewer(
                values.get('ALLURE_VIEWER_PORT', 5252), cli=values.get('ALLURE_CLI', 'allure'))
        return self.allure_viewer

    async def open_selected_allure(self):
        selected = self.selected_allure_result(self.screen)
        if selected is None:
            return
        if selected[0] is None:
            key = self.selected_allure_key(self.screen)
            message = ('Allure live preview is unavailable for this test attempt: the retained preview memory budget was reached. If this run finalizes and publishes, browse it from Allure history.'
                       if key in self._allure_budget_unavailable else
                       'Allure evidence is unavailable for this test attempt; check publication details.')
            self.notify(message,
                        severity='error', timeout=10)
            return
        generation = self._allure_generation
        self._allure_opening = True
        self.screen.refresh_bindings()
        try:
            location = await self._allure().show_preview(*selected, source=self.allure_source)
            if generation is self._allure_generation:
                self.show_allure_location(location)
        except Exception as error:
            if generation is self._allure_generation:
                self.notify(str(error), severity='error', timeout=10)
        finally:
            if generation is self._allure_generation:
                self._allure_opening = False
                self.screen.refresh_bindings()

    def allure_page(self):
        screen = self.screen
        if isinstance(screen, ModalScreen):
            screen = self.screen_stack[-2]
        return screen if getattr(screen, 'kind', None) == 'allure' else None

    def show_allure_location(self, location):
        page = self.allure_page()
        def link(url, action):
            style = Style.from_meta({'@click': ('app.' + action, (url,))})
            return Content(url).stylize(style)
        parts = ['Allure (click to open + copy URL):\n', link(location.url, 'allure_open_url')]
        spacing = '\n\n' if page is not None else '\n'
        if location.inspection_endpoint:
            parts.extend([spacing + 'Browser CDP (click to copy; not a web page):\n',
                          link(location.inspection_endpoint, 'allure_copy_url')])
        if location.browser_error:
            parts.extend(['\n', location.browser_error])
        if getattr(location, 'history_error', None):
            parts.extend([spacing, location.history_error])
        if getattr(location, 'archive_url', None):
            parts.extend([spacing + 'Browse previous runs: ', link(location.archive_url, 'allure_open_url'),
                          ' (open + copy URL)'])
        content = Content.assemble(*parts)
        message = AllureLinkContent(str(content), content.spans)
        if page is not None:
            page.query_one('#body', Static).update(message)
        else:
            self.notify(message, timeout=15)

    async def action_allure_copy_url(self, url, opening=False):
        try:
            native = await copy_text(self, url)
        except ClipboardError:
            self.notify(Content.assemble('Could not copy the URL; copy manually:\n', url),
                        severity='error', timeout=15)
        else:
            message = 'URL copied.' if native else 'URL sent to terminal clipboard.'
            if opening:
                message += ' If no browser opens, paste the URL into a browser.'
            self.notify(message, timeout=10 if opening else 2)

    async def action_allure_open_url(self, url):
        await self.action_allure_copy_url(url, opening=True)
        try:
            self.open_url(url)
        except Exception:
            self.notify(Content.assemble('Could not open the link automatically; open manually:\n', url),
                        severity='error', timeout=15)

    def allure_source(self):
        from api_agent.testing.allure_viewer import RunSource
        from api_agent.testing.storage import RunStorage
        from api_agent.testing.collection import repository_label
        values = self.session.result().values
        storage = None
        secondary = values.get('ALLURE_VIEW_SECONDARY', False)
        if values.get('ALLURE_S3_BUCKET') and not secondary:
            storage = RunStorage(values['ALLURE_S3_BUCKET'],
                prefix=values.get('ALLURE_S3_PREFIX', 'allure'),
                endpoint_url=values.get('ALLURE_S3_ENDPOINT'),
                region_name=values.get('ALLURE_S3_REGION'),
                localstack_port=values.get('ALLURE_LOCALSTACK_PORT', 4566),
                managed_local=values.get('ALLURE_S3_USE_LOCALSTACK', False))
        local_root = values.get('ALLURE_SECONDARY_DIR' if secondary else 'ALLURE_RESULTS_DIR')
        return RunSource(repository_label(self.context, self.context.owner_root),
                           local_root=local_root or None, storage=storage)

    async def show_allure_history(self):
        generation = self._allure_generation
        self._allure_opening = True
        try:
            await self.show('allure', 'Allure history', 'Preparing recorded integration/E2E runs…',
                            (('refresh-allure', 'Refresh history'),))
            if generation is not self._allure_generation:
                return
            location = await self._allure().show_history(self.allure_source())
            if generation is self._allure_generation:
                self.operation_status = 0
                self.show_allure_location(location)
        except Exception as error:
            page = self.allure_page() if generation is self._allure_generation else None
            if page is not None:
                self.operation_status = 3
                page.query_one('#error', Static).update(str(error))
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
