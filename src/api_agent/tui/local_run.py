"""Live local-run presentation; resource ownership stays in its controller."""
import asyncio
from collections import deque
from rich.text import Text

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Static, TextArea

from api_agent.runtime.local_terminal import format_log
from api_agent.runtime.local_contracts import LocalSnapshot
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.progress import component_timeout, independent_timeout_scope
from .clipboard import ClipboardError, copy_text


class LocalRunScreen(Screen):
    kind = "local-run"
    BINDINGS = [Binding('b', 'browsers', 'Browser destinations'),
                Binding('l', 'sources', 'Log sources'),
                Binding('escape,c', 'view', 'Close menu'),
                Binding('asterisk', 'all_logs', 'All logs'),
                Binding('s', 'stop', 'Stop'), Binding('q', 'quit', 'Quit'),
                Binding('ctrl+c', 'copy_choice', 'Copy selection', priority=True)]
    DEFAULT_CSS = '''
    #local-output, #local-choices { height: 1fr; }
    #local-choices { display: none; }
    #local-choices Button { width: 100%; height: auto; min-height: 3; margin: 0 0 1 0; }
    #local-choices Button:focus { border: heavy $accent; }
    '''
    VIEW_HINT = '^C Copy selected text  b Browser destinations  l Log sources  * All logs  s Stop  q Quit'
    MENU_HINT = 'Arrows/hjkl Move  1–9,0 or Enter Open\n^C Copy  Esc/c Back  s Stop  q Quit'

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
        self.menu_choices = ()
        self.menu_generation = 0
        self.failure = ''
        self.status = 0
        self.finished = False
        self.closing = False
        self.disposed = False
        self.pending_cleanup = None
        self._local_app = None
        self._local_unmounting = False

    def compose(self):
        yield Static('Starting local operation…', id='local-status', markup=False)
        yield TextArea('', id='local-output', read_only=True, soft_wrap=False)
        yield VerticalScroll(id='local-choices')
        yield Static(self.VIEW_HINT, id='local-hint', markup=False)
        yield Static('', id='local-error', markup=False)

    def on_mount(self):
        self._local_app = self.app
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
            self.reconcile_menu()
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
            self.reconcile_menu()
        self.refresh_view()

    def refresh_view(self):
        if self._local_unmounting or not self.is_mounted or self.closing or self not in self.app.screen_stack:
            return
        phase = self.snapshot.phase
        components = ', '.join(f'{item.name}: {item.state}' for item in self.snapshot.components)
        self.query_one('#local-status', Static).update(f'Run local: {phase} — logs: {self.source}\n{components}')
        self.query_one('#local-error', Static).update(self.failure)
        self.query_one('#local-hint', Static).update(self.MENU_HINT if self.menu else self.VIEW_HINT)
        text = ''.join(format_log(event, self.source) for event in self.events)
        area = self.query_one('#local-output', TextArea)
        previous = area.text
        if text != previous:
            if text.startswith(previous) and previous:
                selection = area.selection
                scroll = area.scroll_offset
                area.insert(text[len(previous):], area.document.end)
                area.selection = selection
                if area.selected_text:
                    area.scroll_to(scroll.x, scroll.y, animate=False)
                else:
                    area.scroll_end(animate=False)
            else:
                area.load_text(text)
                area.scroll_end(animate=False)
        area.display = self.menu is None
        self.query_one('#local-choices', VerticalScroll).display = self.menu is not None

    def sources(self):
        return self.snapshot.log_sources

    def destinations(self):
        return self.snapshot.destinations

    def action_browsers(self):
        self.open_menu('browsers', self.destinations())

    def action_sources(self):
        self.open_menu('logs', self.sources())

    def reconcile_menu(self):
        if self.menu is None:
            return
        choices = self.sources() if self.menu == 'logs' else self.destinations()
        if tuple(choices) != self.menu_choices:
            self.open_menu(self.menu, choices, preserve_focus=True)

    def open_menu(self, kind, choices, *, preserve_focus=False):
        selection = 0
        if preserve_focus and self.is_mounted:
            focused = self.app.focused
            if isinstance(focused, Button) and (focused.id or '').startswith(f'local-choice-{self.menu_generation}-'):
                index_text = focused.id.rsplit('-', 1)[-1]
                if index_text.isdigit():
                    old_index = int(index_text)
                    previous = self.menu_choices[old_index]
                    selection = (tuple(choices).index(previous) if previous in choices
                                 else min(old_index, max(0, len(choices) - 1)))
        self.menu = kind
        self.menu_choices = tuple(choices)
        self.menu_generation += 1
        if self.is_mounted:
            pane = self.query_one('#local-choices', VerticalScroll)
            pane.remove_children()
            buttons = []
            for index, choice in enumerate(self.menu_choices):
                hotkey = str((index + 1) % 10) if index < 10 else None
                prefix = f'{hotkey}  ' if hotkey else '   '
                label = (f'{prefix}{choice.name}\n   {choice.url}' if kind == 'browsers'
                         else f'{prefix}{choice}')
                buttons.append(Button(Text(label), id=f'local-choice-{self.menu_generation}-{index}'))
            if kind == 'logs':
                buttons.append(Button(Text('*  All logs'), id=f'local-choice-{self.menu_generation}-all'))
            pane.mount(*buttons)
            if buttons:
                generation = self.menu_generation
                target = buttons[min(selection, len(buttons) - 1)]
                self.call_after_refresh(lambda: target.focus() if self.menu_generation == generation
                                        and self.menu == kind else None)
        self.refresh_view()

    def action_view(self):
        self.menu = None
        self.menu_choices = ()
        self.refresh_view()
        if self.is_mounted:
            self.query_one('#local-output', TextArea).focus()

    def action_all_logs(self):
        self.source = 'all'
        self.action_view()

    def check_action(self, action, parameters):
        if action == 'copy_choice':
            if not self.is_mounted or self.app.screen is not self:
                return False
            focused = self.app.focused
            if self.menu is None:
                return (isinstance(focused, TextArea) and focused.id == 'local-output'
                        and focused.screen is self and focused.display and bool(focused.selected_text))
            if not isinstance(focused, Button) or focused.screen is not self:
                return False
            identifier = focused.id or ''
            prefix = f'local-choice-{self.menu_generation}-'
            if not identifier.startswith(prefix):
                return False
            index_text = identifier.removeprefix(prefix)
            return index_text.isdigit() and 0 <= int(index_text) < len(self.menu_choices)
        if self.menu is not None and action in {'sources', 'browsers', 'all_logs'}:
            return False
        return super().check_action(action, parameters)

    async def on_button_pressed(self, event: Button.Pressed):
        identifier = event.button.id or ''
        if self.menu is None or not identifier.startswith('local-choice-'):
            return
        suffix = identifier.removeprefix('local-choice-').split('-')
        if len(suffix) != 2 or int(suffix[0]) != self.menu_generation:
            return
        if suffix[1] == 'all':
            self.action_all_logs()
        else:
            await self.activate_choice(int(suffix[1]))
        event.stop()

    async def activate_choice(self, index):
        if not 0 <= index < len(self.menu_choices):
            return
        if self.menu == 'logs':
            self.source = self.menu_choices[index]
            self.action_view()
        else:
            from api_agent.runtime.local_terminal import open_host_url
            try:
                await open_host_url(self.menu_choices[index].url)
            except Exception:
                self.failure = 'Could not open the host browser. Open the displayed URL on the host.'
                self.refresh_view()

    async def action_copy_choice(self):
        if self.menu is None:
            if self.check_action('copy_choice', ()):
                await self.copy_value(self.query_one('#local-output', TextArea).selected_text)
            return
        focused = self.app.focused
        if not isinstance(focused, Button) or not (focused.id or '').startswith('local-choice-'):
            return
        suffix = focused.id.removeprefix('local-choice-').split('-')
        if len(suffix) != 2 or int(suffix[0]) != self.menu_generation or suffix[1] == 'all':
            return
        index = int(suffix[1])
        if not 0 <= index < len(self.menu_choices):
            return
        value = self.menu_choices[index]
        await self.copy_value(value if self.menu == 'logs' else value.url)

    async def copy_value(self, value):
        try:
            native = await copy_text(self.app, value)
        except ClipboardError:
            self.app.notify('Could not copy the selected value.', severity='error', timeout=2)
        else:
            self.app.notify('Value copied.' if native else 'Value sent to terminal clipboard.', timeout=2)

    async def on_key(self, event):
        if self.menu is None:
            return
        key = event.key
        if key in '1234567890' and len(key) == 1:
            index = 9 if key == '0' else int(key) - 1
            await self.activate_choice(index)
            event.stop()
            return
        directions = {'up': -1, 'k': -1, 'left': -1, 'h': -1,
                      'down': 1, 'j': 1, 'right': 1, 'l': 1}
        if key in directions:
            buttons = [button for button in self.query('#local-choices Button')
                       if (button.id or '').startswith(f'local-choice-{self.menu_generation}-')]
            focused = self.app.focused
            if buttons:
                index = buttons.index(focused) if focused in buttons else 0
                buttons[max(0, min(len(buttons) - 1, index + directions[key]))].focus()
            event.prevent_default()
            event.stop()

    async def settle_run_task(self):
        if self.run_task is None:
            return
        with independent_timeout_scope():
            async with component_timeout('Local presentation shutdown', 15) as budget:
                done, _ = await asyncio.wait({self.run_task}, timeout=budget.remaining)
                if not done:
                    raise TimeoutError('Local presentation shutdown remains pending.')
                if not self.run_task.cancelled():
                    self.run_task.result()

    async def retry_cleanup(self):
        if not await self.close_owned():
            raise self.pending_cleanup

    async def close_owned(self):
        if self.disposed:
            return True
        self.closing = True
        try:
            if self.owner is None:
                if self.run_task is not None:
                    self.run_task.cancel()
                await self.settle_run_task()
                if self.is_mounted:
                    self.retire()
            else:
                self.owner.request_stop()
                # The controller bounds startup settlement and retains late
                # acquisitions before the presentation can acknowledge Stop.
                await self.owner.aclose()
                await self.settle_run_task()
                self.retire()
        except BaseException as error:
            self.closing = False
            self.pending_cleanup = CleanupPendingError((self,), self.retry_cleanup)
            if not isinstance(error, Exception):
                error.cleanup_pending = self.pending_cleanup
                raise
            detail = self.owner.diagnostic(error) if self.owner is not None else 'Local presentation shutdown remains pending.'
            self.failure = 'Cleanup pending; retry Stop or Quit. ' + detail
            self.refresh_view()
            return False
        self.pending_cleanup = None
        return True

    def retire(self):
        self.disposed = True
        app = self._local_app or self.app
        if getattr(app, 'local_run_screen', self) is self:
            app.operation_status = self.status
            app.local_run_screen = None

    async def action_stop(self):
        if await self.close_owned():
            self.app.show_menu()

    async def action_quit(self):
        from .app import QuitDialog
        self.app.push_screen(QuitDialog(), lambda yes: self.app.run_worker(self.app.confirm_quit()) if yes else None)

    async def on_unmount(self):
        self._local_unmounting = True
        app = self._local_app or self.app
        try:
            closed = await self.close_owned()
        except BaseException as error:
            app.pending_shutdown_cleanup = app.shutdown_cleanup_receipt()
            error.cleanup_pending = app.pending_shutdown_cleanup
            raise
        if not closed:
            app.pending_shutdown_cleanup = app.shutdown_cleanup_receipt()
            raise app.pending_shutdown_cleanup
