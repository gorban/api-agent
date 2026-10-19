"""Workbench-owned Allure navigation, independent of test execution."""
import asyncio
import re
from functools import cached_property, wraps
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
_HISTORY_OPEN_ERROR = 'Could not open Allure history; check reporting setup and storage.'
_KNOWN_HISTORY_VIEWER_MESSAGES = frozenset({
    'No finalized integration or E2E Allure runs are available',
    'Allure history retrieval timed out',
    'Could not discover finalized Allure history',
    'Could not fetch the exact Allure run; check reporting storage',
    'Conflicting immutable Allure run manifests',
    'Exact finalized Allure run is unavailable locally or in S3',
    'Finalized Allure manifest binding changed',
    'Symlinked Allure history is forbidden',
    'Allure manifest exceeds viewer limit',
    'Allure history directory identity mismatch',
    'Allure artifact digest mismatch or size limit exceeded',
    'Allure result references a missing attachment',
    'Allure viewer is closed',
    'Allure server cleanup remains pending',
    'Allure could not start; install Allure 2 and check Java on PATH',
    'Allure timed out; check Java and the viewer generation timeout',
    'Invalid ALLURE_CLI; provide a command and quote arguments containing spaces',
    'Unsupported Allure CLI; an externally installed Allure 2 version is required',
    'Allure 2 CLI command is missing from PATH; configure ALLURE_CLI or install with brew install allure; see https://allurereport.org/docs/v2/install/',
    'Owned Allure report server did not become ready',
    'The exact selected Allure result is unavailable',
    'Allure did not preserve the exact selected attempt',
    'Allure generation produced no report',
    'Allure generated an invalid selected-result link',
})


def _known_history_viewer_message(error):
    from api_agent.testing.allure_source import ViewerError
    if (type(error) is ViewerError and len(error.args) == 1
            and type(error.args[0]) is str):
        message = error.args[0]
        if message in _KNOWN_HISTORY_VIEWER_MESSAGES:
            return message
        port = re.fullmatch(r'Allure viewer port ([0-9]{1,5}) is occupied; stop its owner or configure ALLURE_VIEWER_PORT', message)
        if port and 1 <= int(port[1]) <= 65535:
            return f'Allure viewer port {int(port[1])} is occupied; stop its owner or configure ALLURE_VIEWER_PORT'
        if re.fullmatch(r'Allure command failed \(exit -?[0-9]{1,3}\); check ALLURE_CLI, its host PATH and Java installation', message):
            return 'Allure command failed; check ALLURE_CLI, its host PATH and Java installation'
    return None


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


def _owned_opener(method):
    @wraps(method)
    async def open_owned(self, *args, **kwargs):
        if not self._allure_accepting:
            return
        task = asyncio.current_task()
        generation = self._allure_generation
        self._allure_tasks.add(task)
        try:
            return await method(self, *args, **kwargs)
        except asyncio.CancelledError:
            if generation is self._allure_generation:
                raise
        finally:
            self._allure_tasks.discard(task)
    return open_owned


class AllureNavigation:
    def initialize_allure(self):
        self.allure_viewer = None
        self.allure_snapshots = {}
        self._allure_budget_unavailable = set()
        self._allure_retained_bytes = 0
        self._allure_generation = object()
        self._allure_opening = False
        self._allure_loading = ''
        self._allure_restore_worker = None
        self._allure_tasks = set()
        self._allure_cancel_requested = set()
        self._allure_cleanup = None
        self._allure_accepting = True
        self._allure_shutdown = False

    def refresh_allure_loading(self):
        for screen in self.screen_stack:
            for status in screen.query('#allure-loading'):
                status.update(self._allure_loading)
                status.display = bool(self._allure_loading)

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
                values.get('ALLURE_VIEWER_PORT', 5252), cli=values.get('ALLURE_CLI', 'allure'),
                history_count=values.get('ALLURE_HISTORY_DISPLAY_COUNT', 20))
        return self.allure_viewer

    @_owned_opener
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
        mode = 'E2E' if self.mode == 'e2e' else 'integration'
        self._allure_loading = f'Preparing selected {mode} run in Allure…'
        self.refresh_allure_loading()
        self.screen.refresh_bindings()
        try:
            await self.prepare_allure_reporting()
            if generation is not self._allure_generation:
                return
            location = await self._allure().show_preview(*selected, source=self.allure_source)
            if generation is self._allure_generation:
                self.show_allure_location(location)
        except Exception as error:
            if generation is self._allure_generation:
                self.notify(_known_history_viewer_message(error) or _HISTORY_OPEN_ERROR,
                            severity='error', timeout=10)
        finally:
            if generation is self._allure_generation:
                self._allure_opening = False
                self._allure_loading = ''
                self.refresh_allure_loading()
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
            parts.extend([spacing + 'All runs: ', link(location.archive_url, 'allure_open_url'),
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

    async def prepare_allure_reporting(self, *, for_restore=False):
        values = self.session.result().values
        if (values.get('ALLURE_S3_BUCKET') and values.get('ALLURE_S3_USE_LOCALSTACK')
                and (for_restore or not values.get('ALLURE_VIEW_SECONDARY', False))):
            import os
            from api_agent.runtime.compose import plan_compose, ENGINE_ENVIRONMENT_KEYS
            from api_agent.runtime.ownership import StackScope
            from api_agent.runtime.tasks import offload
            environment = {key: os.environ[key] for key in ENGINE_ENVIRONMENT_KEYS if key in os.environ}
            config = {key: value for key, value in values.items()
                      if key.startswith('ALLURE_S3_') or key == 'ALLURE_LOCALSTACK_PORT'}
            plan = plan_compose(config, StackScope.from_roots(self.context.base_root, self.context.pack_root),
                                engine_environment=environment)
            await offload(self.reporting_sessions.acquire, plan, engine_environment=environment)

    def allure_source(self, *, primary=False):
        from api_agent.testing.allure_viewer import RunSource
        from api_agent.testing.storage import RunStorage
        from api_agent.testing.collection import repository_label
        values = self.session.result().values
        storage = None
        secondary = values.get('ALLURE_VIEW_SECONDARY', False) and not primary
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

    @_owned_opener
    async def show_allure_history(self):
        generation = self._allure_generation
        self._allure_opening = True
        try:
            values = self.session.result().values
            actions = [('refresh-allure', 'Refresh history')]
            if values.get('ALLURE_SECONDARY_DIR') and values.get('ALLURE_S3_BUCKET'):
                actions.append(('allure-restore-secondary', 'Restore missing secondary history'))
            await self.show('allure', 'Allure history', 'Preparing recorded integration/E2E runs…', tuple(actions))
            if generation is not self._allure_generation:
                return
            await self.prepare_allure_reporting()
            if generation is not self._allure_generation:
                return
            location = await self._allure().show_archive(self.allure_source())
            if generation is self._allure_generation:
                self.operation_status = 0
                self.show_allure_location(location)
        except Exception as error:
            page = self.allure_page() if generation is self._allure_generation else None
            if page is not None:
                self.operation_status = 3
                page.query_one('#error', Static).update(_known_history_viewer_message(error) or _HISTORY_OPEN_ERROR)
        finally:
            if generation is self._allure_generation:
                self._allure_opening = False

    @_owned_opener
    async def restore_allure_secondary_history(self):
        """Run the explicit restore outside the UI loop, then refresh primary history."""
        from threading import Event
        from api_agent.runtime.tasks import offload
        from api_agent.testing.collection import repository_label
        from api_agent.testing.history_restore import known_restore_message, restore_missing_history

        if self._allure_opening:
            return
        unexpected = 'Could not restore Allure history; check reporting setup and storage.'
        try:
            values = self.session.result().values
            missing = not values.get('ALLURE_SECONDARY_DIR') or not values.get('ALLURE_S3_BUCKET')
        except Exception:
            self.notify(unexpected, severity='error', timeout=10)
            return
        if missing:
            self.notify('Configure secondary history and an S3 bucket before restoring.',
                        severity='error', timeout=10)
            return
        generation = self._allure_generation
        cancellation = Event()
        self._allure_opening = True
        self._allure_loading = 'Checking finalized secondary history…'
        page = self.allure_page()
        if page is not None:
            page.query_one('#error', Static).update('')
        self.refresh_allure_loading()

        def progress(done, total):
            self.call_from_thread(self._restore_allure_progress, generation, done, total)

        try:
            await self.prepare_allure_reporting(for_restore=True)
            if generation is not self._allure_generation:
                return
            source = self.allure_source(primary=True)
            repo = repository_label(self.context, self.context.owner_root)
            restored = await offload(restore_missing_history, repo, values['ALLURE_SECONDARY_DIR'],
                                     source.storage, progress=progress, cancelled=cancellation.is_set,
                                     cancellation=cancellation)
            if generation is self._allure_generation:
                location = await self._allure().show_archive(source)
                self.operation_status = 0
                self.show_allure_location(location)
                self.notify(f'Verified {restored} finalized secondary runs in S3.', timeout=10)
        except asyncio.CancelledError:
            cancellation.set()
            raise
        except Exception as error:
            if generation is self._allure_generation:
                self.operation_status = 3
                message = known_restore_message(error) or _known_history_viewer_message(error) or unexpected
                page = self.allure_page()
                if page is not None:
                    page.query_one('#error', Static).update(message)
                else:
                    self.notify(message, severity='error', timeout=10)
        finally:
            if generation is self._allure_generation:
                self._allure_opening = False
                self._allure_loading = ''
                self.refresh_allure_loading()

    def _restore_allure_progress(self, generation, done, total):
        if generation is self._allure_generation:
            self._allure_loading = f'Restoring secondary history: {done}/{total} finalized runs'
            self.refresh_allure_loading()

    def stop_allure_admission(self, *, shutdown=False):
        self._allure_shutdown |= shutdown
        self._allure_accepting = False
        self._allure_generation = object()
        self._allure_loading = ''
        self.refresh_allure_loading()

    def allure_cleanup_active(self):
        return bool(self._allure_tasks) or (self._allure_cleanup is not None
                                           and 'runtime' not in self._allure_cleanup.done)

    def begin_allure_cleanup(self):
        from types import SimpleNamespace
        from api_agent.runtime.operation import OperationCleanup
        self.stop_allure_admission()
        if self._allure_cleanup is None or 'runtime' in self._allure_cleanup.done:
            self._allure_cleanup = OperationCleanup(None, SimpleNamespace(aclose=self._dispose_allure),
                                                    None, None, False, 15)
        return self._allure_cleanup

    async def _dispose_allure(self):
        from api_agent.providers.control import _cancel_once
        tasks = tuple(self._allure_tasks)
        for task in tasks:
            _cancel_once(task, self._allure_cancel_requested)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._allure_cancel_requested.difference_update(tasks)
        # Openers can be draining a thread/provider while cancellation settles.
        # Only now is their final viewer owner known and safe to dispose.
        viewer = self.allure_viewer
        if viewer is not None:
            await viewer.aclose()
            if self.allure_viewer is not viewer:
                raise RuntimeError('Allure viewer changed during cleanup; retry its retained owner.')
            self.allure_viewer = None
        self._allure_opening = False
        self._allure_accepting = not self._allure_shutdown

    async def wait_allure_cleanup(self, cleanup):
        from api_agent.providers.control import CleanupPendingError
        from api_agent.providers.progress import independent_timeout_scope, report_timeout
        with independent_timeout_scope():
            async with report_timeout('Allure navigation cleanup', 15):
                try:
                    await cleanup.retry()
                except CleanupPendingError:
                    self.notify('Allure cleanup incomplete; retry to finish closing the viewer.',
                                severity='error', timeout=10)
                    return False
        return True

    async def close_allure(self):
        return await self.wait_allure_cleanup(self.begin_allure_cleanup())
