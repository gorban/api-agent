"""Read-only container visibility; destructive actions keep existing stop guards."""
import os

from textual.widgets import Button, DataTable, Static

from api_agent.runtime.tasks import offload
from api_agent.testing.diagnostics import CredentialScrubber


class ContainerManagement:
    def container_log_scrubber(self):
        return CredentialScrubber({**os.environ, **self.session.result(_review=True).values})

    def show_container_management(self):
        scrub = self.container_log_scrubber()
        rows = [(item.id, ('✓' if item.id in self.container_marked else '', item.id,
                          scrub(item.name), scrub(item.image), scrub(item.status))) for item in self.containers]
        running = {item.id for item in self.containers if item.running}
        if running:
            rows.insert(0, ('container-all', ('✓' if running <= self.container_marked else '', 'All running', '', '', '')))
        self.show('containers', 'Container management', self.container_summary +
                  '\nEnter marks running rows for Stop. Highlight a container to view its bounded logs. Containers and volumes are retained.',
                  (('container-logs', 'View highlighted logs'), ('stop-containers', 'Stop selected containers (s)'),
                   ('refresh-containers', 'Refresh inventory')), rows)

    def refresh_managed_containers(self):
        self.run_worker(self._refresh_managed_containers(), group='containers', exclusive=True)

    async def _refresh_managed_containers(self):
        origin = self.screen
        try:
            items = await offload(self.container_runtime.inventory)
            self.containers = items
            self.container_marked.intersection_update(item.id for item in items if item.running)
            count = sum(item.running for item in items)
            self.container_summary = f'Managed containers: {count} running, {len(items)-count} stopped'
        except Exception:
            self.containers = ()
            self.container_marked.clear()
            self.container_summary = 'Container inventory unavailable.'
        # A late read cannot replace a modal or a different screen instance.
        if self.screen is origin:
            if getattr(origin, 'kind', None) == 'containers':
                self.show_container_management()
            elif getattr(origin, 'kind', None) == 'menu':
                self.update_managed_container_menu()

    def update_managed_container_menu(self):
        if getattr(self.screen, 'kind', None) == 'menu':
            self.screen.query_one('#body', Static).update(self.container_summary)
            button = self.screen.query_one('#mode-cleanup-containers', Button)
            button.label = 'Container management'
            button.disabled = False

    def toggle_managed_container(self, key):
        running = {item.id for item in self.containers if item.running}
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
        def load(*, cancelled):
            return self.container_runtime.logs(item.id, cancelled=cancelled, scrubber=scrubber)
        self.container_log_screen = LogScreen(scrubber('Container logs: ' + item.name), load)
        self.push_screen(self.container_log_screen)

    async def close_container_logs(self):
        screen = getattr(self, 'container_log_screen', None)
        if screen is not None:
            await screen.aclose()
            if self.container_log_screen is screen:
                self.container_log_screen = None
