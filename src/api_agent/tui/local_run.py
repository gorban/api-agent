"""Live local-run presentation; resource ownership stays in its controller."""
import asyncio
from collections import deque

from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, TextArea

from api_agent.runtime.local_terminal import format_log
from api_agent.runtime.local_contracts import LocalSnapshot


class LocalRunScreen(Screen):
    kind = "local-run"
    BINDINGS = [Binding('b', 'browsers', 'Browser destinations'),
                Binding('l', 'sources', 'Log sources'),
                Binding('escape,c', 'view', 'Close menu'),
                Binding('asterisk', 'all_logs', 'All logs'),
                Binding('s', 'stop', 'Stop'), Binding('q', 'quit', 'Quit')]

    def __init__(self, context, resolve, *, argv=(), schema=None, owner_factory=None):
        super().__init__()
        self.context, self.resolve, self.argv = context, resolve, argv
        self.owner_factory = owner_factory
        self.config_schema = schema
        self.owner = None
        self.run_task = None
        self.source = resolve().values['RUN_LOG_SOURCE']
        self.events = deque(maxlen=10000)
        self.snapshot = LocalSnapshot("starting")
        self.menu = None
        self.menu_number = ""
        self.failure = ''
        self.status = 0
        self.finished = False
        self.closing = False
        self.disposed = False

    def compose(self):
        yield Static('Starting local operation…', id='local-status', markup=False)
        yield TextArea('', id='local-output', read_only=True, soft_wrap=False)
        yield Static('b Browser destinations  l Log sources  * All logs  s Stop  q Quit', markup=False)
        yield Static('', id='local-error', markup=False)

    def on_mount(self):
        self.run_task = asyncio.create_task(self.run())

    async def run(self):
        try:
            factory = self.owner_factory
            if factory is None:
                from api_agent.runtime.local_run import LocalRunOwner
                factory = LocalRunOwner
            options = {'schema': self.config_schema} if self.config_schema is not None else {}
            self.owner = factory(self.context, self.resolve, argv=self.argv, on_event=self.receive, **options)
            self.snapshot = await self.owner.start()
            self.refresh_view()
            self.status = await self.owner.wait()
        except asyncio.CancelledError:
            self.status = 130
            if self.owner is not None:
                self.owner.request_stop()
        except Exception as error:
            self.status = 1
            self.failure = (self.owner.diagnostic(error) if self.owner is not None
                            else 'Local operation could not start.')
        finally:
            self.finished = True
            self.refresh_view()

    def receive(self, event):
        if event.kind == 'log':
            self.events.append(event)
        elif event.kind == 'status':
            self.snapshot = event.snapshot
        self.refresh_view()

    def refresh_view(self):
        if not self.is_mounted or self.closing or self not in self.app.screen_stack:
            return
        phase = self.snapshot.phase
        components = ', '.join(f'{item.name}: {item.state}' for item in self.snapshot.components)
        self.query_one('#local-status', Static).update(f'Run local: {phase} — logs: {self.source}\n{components}')
        self.query_one('#local-error', Static).update(self.failure)
        if self.menu == 'logs':
            text = '\n'.join(f'{i}: {name}' for i, name in enumerate(self.sources(), 1)) + '\n*: All logs\nType a number then Enter: ' + self.menu_number
        elif self.menu == 'browsers':
            text = '\n'.join(f'{i}: {item.name} — {item.url}' for i, item in enumerate(self.destinations(), 1)) + '\nType a number then Enter: ' + self.menu_number
        else:
            text = ''.join(format_log(event, self.source) for event in self.events)
        area = self.query_one('#local-output', TextArea)
        area.load_text(text)
        area.scroll_end(animate=False)

    def sources(self):
        return self.snapshot.log_sources

    def destinations(self):
        return self.snapshot.destinations

    def action_browsers(self):
        self.menu_number = ''
        self.menu = 'browsers'
        self.refresh_view()

    def action_sources(self):
        self.menu_number = ''
        self.menu = 'logs'
        self.refresh_view()

    def action_view(self):
        self.menu = None
        self.refresh_view()

    def action_all_logs(self):
        self.source = 'all'
        self.action_view()

    async def on_key(self, event):
        if self.menu is None:
            return
        if event.character and event.character.isdigit():
            self.menu_number += event.character
            self.refresh_view()
            event.stop()
            return
        if event.key != 'enter' or not self.menu_number:
            return
        index = int(self.menu_number) - 1
        self.menu_number = ''
        choices = self.sources() if self.menu == 'logs' else self.destinations()
        if not 0 <= index < len(choices):
            self.refresh_view()
            return
        if self.menu == 'logs':
            self.source = choices[index]
            self.action_view()
        else:
            from api_agent.runtime.local_terminal import open_host_url
            try:
                await open_host_url(choices[index].url)
            except Exception:
                self.failure = 'Could not open the host browser. Open the displayed URL on the host.'
                self.refresh_view()
        event.stop()

    async def close_owned(self):
        if self.disposed:
            return True
        self.closing = True
        if self.owner is None:
            if self.run_task is not None:
                self.run_task.cancel()
                try:
                    await self.run_task
                except asyncio.CancelledError:
                    pass
            if self.is_mounted:
                self.retire()
            return True
        self.owner.request_stop()
        if self.run_task is not None:
            await self.run_task
        try:
            await self.owner.aclose()
        except Exception as error:
            self.closing = False
            self.failure = 'Cleanup pending; retry Stop or Quit. ' + self.owner.diagnostic(error)
            self.refresh_view()
            return False
        self.retire()
        return True

    def retire(self):
        self.disposed = True
        if getattr(self.app, 'local_run_screen', self) is self:
            self.app.operation_status = self.status
            self.app.local_run_screen = None

    async def action_stop(self):
        if await self.close_owned():
            self.app.show_menu()

    async def action_quit(self):
        from .app import QuitDialog
        self.app.push_screen(QuitDialog(), lambda yes: self.app.run_worker(self.app.confirm_quit()) if yes else None)

    async def on_unmount(self):
        if not await self.close_owned():
            raise self.owner.pending_cleanup
