"""Read-only container visibility; destructive actions keep existing stop guards."""
import asyncio
import os

from textual.widgets import Button, DataTable, Static

from api_agent.runtime.tasks import offload
from api_agent.testing.diagnostics import CredentialScrubber


VOLUME_PREFIX = 'volume:'


def _engine_display(label, key):
    return f"{label} [{key[:12]}]"


class ContainerManagement:
    def container_engine_display(self):
        label = getattr(self.container_runtime, 'selected_label', None)
        return _engine_display(label, self.container_runtime.selected_key) if label is not None else None

    def container_log_scrubber(self):
        return CredentialScrubber({**os.environ, **self.session.result(_review=True).values})

    def show_container_management(self):
        scrub = self.container_log_scrubber()
        rows = [(item.id, ('✓' if item.id in self.container_marked else '', item.id,
                          scrub(item.name), scrub(item.image), scrub(item.status))) for item in self.containers]
        running = {item.id for item in self.containers if item.running}
        if running:
            rows.insert(0, ('container-all', ('✓' if running <= self.container_marked else '', 'All running', '', '', '')))
        rows.extend((VOLUME_PREFIX + item.name, ('✓' if item.name in self.volume_marked else '', 'volume', scrub(item.name),
                                                 scrub(f'{item.project} ({item.profile})'),
                                                 'in use' if item.in_use else 'unused')) for item in self.volumes)
        targets = getattr(self.container_runtime, 'known_targets', lambda: ())()
        selected = getattr(self.container_runtime, 'selected_key', None)
        actions = tuple(('container-engine-' + key,
                         ('✓ ' if key == selected else '') + 'Select engine: ' + _engine_display(label, key))
                        for key, label in targets)
        self.show('containers', 'Container management', self.container_summary +
                  '\nEnter marks running rows for Stop and unused volume rows for removal. Highlight a container to view its '
                  'bounded logs. Stopping keeps volumes; a stopped container is removed by the next managed start or '
                  'release. Remove unused deletes other checkouts\' containers that no live lease protects and their unused '
                  'volumes once the checkout is gone or past retention; Remove selected volumes deletes the volumes you '
                  'marked when nothing references them and no live lease protects them.',
                  (('container-logs', 'View highlighted logs'), ('stop-containers', 'Stop selected containers (s)'),
                   ('remove-volumes', 'Remove selected volumes'), ('reap-containers', 'Remove unused'),
                   ('refresh-containers', 'Refresh inventory')) + actions, rows)

    def select_container_engine(self, key):
        self.container_runtime.select_target(key)
        self.containers = self.volumes = ()
        self.container_marked.clear()
        self.volume_marked.clear()
        self.container_summary = 'Selected engine: ' + _engine_display(
            self.container_runtime.selected_label, self.container_runtime.selected_key) + '; checking…'
        self.show_container_management()
        self.refresh_managed_containers()

    def request_container_reap(self):
        from .app import ReapUnusedDialog
        runtime = getattr(self.container_runtime, 'selected_runtime', self.container_runtime)
        self.push_screen(ReapUnusedDialog(self.container_engine_display()), lambda yes: self.run_worker(
            self._reap_containers(runtime), group='containers', exclusive=True) if yes else None)

    async def _reap_containers(self, runtime):
        try:
            report = await asyncio.to_thread(runtime.reap_unused)
            message = (f'Removed {report.containers} unused container(s) of {report.scopes} scope(s), '
                       f'{report.volumes} volume(s) and {report.networks} network(s).'
                       if report.scopes or report.volumes else 'Nothing unused to remove.')
            if report.diagnostic:
                message += ' Cleanup ' + report.diagnostic + '.'
        except Exception as error:
            message = str(error)
        await self._refresh_managed_containers()
        self.call_after_refresh(self.show_container_stop_error, message)

    def refresh_managed_containers(self):
        self.run_worker(self._refresh_managed_containers(), group='containers', exclusive=True)

    async def _refresh_managed_containers(self):
        origin = self.screen
        selection = getattr(self.container_runtime, 'selected_key', None)
        label = getattr(self.container_runtime, 'selected_label', None)
        prefix = ('Selected engine: ' + _engine_display(label, selection) + '\n') if label is not None else ''
        try:
            items = await offload(self.container_runtime.inventory)
            if selection != getattr(self.container_runtime, 'selected_key', None):
                return
            volumes = await self._managed_volumes()
            if selection != getattr(self.container_runtime, 'selected_key', None):
                return
            self.containers, self.volumes = items, volumes
            self.container_marked.intersection_update(item.id for item in items if item.running)
            self.volume_marked.intersection_update(item.name for item in volumes if not item.in_use)
            count = sum(item.running for item in items)
            unused = sum(not item.in_use for item in volumes)
            self.container_summary = (prefix + f'Managed containers: {count} running, {len(items)-count} stopped'
                                      + (f'; managed volumes: {len(volumes)} ({unused} unused)' if volumes else ''))
        except Exception:
            if selection != getattr(self.container_runtime, 'selected_key', None):
                return
            self.containers = self.volumes = ()
            self.container_marked.clear()
            self.volume_marked.clear()
            self.container_summary = prefix + 'Container inventory unavailable.'
        # A late read cannot replace a modal or a different screen instance.
        if self.screen is origin:
            if getattr(origin, 'kind', None) == 'containers':
                self.show_container_management()
            elif getattr(origin, 'kind', None) == 'menu':
                self.update_managed_container_menu()

    async def _managed_volumes(self):
        """Volumes are secondary to containers: an engine that cannot list them still lists containers."""
        runtime = getattr(self.container_runtime, 'selected_runtime', self.container_runtime)
        lister = getattr(runtime, 'volumes', None)
        if lister is None:
            return ()
        try:
            return await offload(lister)
        except Exception:
            return ()

    def update_managed_container_menu(self):
        screen = self.screen
        if getattr(screen, 'kind', None) == 'menu':
            # A menu that has not composed yet renders this body when it does.
            screen.body = self.container_summary
            if not screen.query('#body'):
                return
            screen.query_one('#body', Static).update(self.container_summary)
            button = screen.query_one('#mode-cleanup-containers', Button)
            button.label = 'Container management'
            button.disabled = False

    def toggle_managed_container(self, key):
        running = {item.id for item in self.containers if item.running}
        if key.startswith(VOLUME_PREFIX):
            name = key[len(VOLUME_PREFIX):]
            if name in {item.name for item in self.volumes if not item.in_use}:
                self.volume_marked.symmetric_difference_update({name})
                table = self.screen.query_one(DataTable)
                table.update_cell(key, next(iter(table.columns)), '✓' if name in self.volume_marked else '')
            return
        if key == 'container-all':
            self.container_marked = set() if running <= self.container_marked else running
        elif key in running:
            self.container_marked.symmetric_difference_update({key})
        table = self.screen.query_one(DataTable)
        column = next(iter(table.columns))
        for item in self.containers:
            table.update_cell(item.id, column, '✓' if item.id in self.container_marked else '')
        if running:
            table.update_cell('container-all', column, '✓' if running <= self.container_marked else '')

    def request_container_logs(self):
        from .logs import LogScreen
        if not self.screen.query(DataTable):
            raise ValueError('Highlight a managed container to view logs.')
        table = self.screen.query_one(DataTable)
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        item = next((item for item in self.containers if item.id == key), None)
        if item is None:
            raise ValueError('Highlight a managed container to view logs.')
        scrubber = self.container_log_scrubber()
        runtime = getattr(self.container_runtime, 'selected_runtime', self.container_runtime)
        def load(*, cancelled):
            return runtime.logs(item.id, cancelled=cancelled, scrubber=scrubber)
        title = 'Container logs: ' + item.name
        engine = self.container_engine_display()
        if engine is not None:
            title += ' | Engine: ' + engine
        self.container_log_screen = LogScreen(scrubber(title), load)
        self.push_screen(self.container_log_screen)

    async def close_container_logs(self):
        screen = getattr(self, 'container_log_screen', None)
        if screen is not None:
            await screen.aclose()
            if self.container_log_screen is screen:
                self.container_log_screen = None
