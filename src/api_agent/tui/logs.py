"""Reusable finite log reader with selectable, sanitized text and owned disposal."""
import asyncio
from threading import Event

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from api_agent.providers.control import CleanupPendingError
from api_agent.runtime.tasks import offload, settle
from .clipboard import ClipboardError, copy_text


class LogScreen(ModalScreen):
    BINDINGS = [Binding('escape,c', 'close', 'Close', priority=True),
                Binding('q,ctrl+q', 'quit_request', 'Quit', priority=True),
                Binding('ctrl+c', 'copy_selected', 'Copy selection', priority=True)]
    DEFAULT_CSS = '''
    LogScreen { align: center middle; }
    #log-page { width: 95%; height: 95%; border: round $accent; background: $surface; }
    #log-text { height: 1fr; }
    #log-actions { height: auto; }
    #log-actions > Button { width: 1fr; }
    '''

    def __init__(self, title, loader):
        super().__init__()
        self.title_text, self.loader = title, loader
        self.cancelled = Event()
        self.reader = None
        self.cleanup_task = None
        self.pending_cleanup = None
        self.closing = False

    def compose(self):
        with Vertical(id='log-page'):
            yield Static(self.title_text, markup=False)
            yield TextArea('Reading bounded log tail…', read_only=True, soft_wrap=False, id='log-text')
            yield Static('', id='log-error', markup=False)
            with Horizontal(id='log-actions'):
                for label, identifier in (('Copy all', 'log-copy'), ('Close', 'log-close')):
                    button = Button(label, id=identifier)
                    button.styles.width = '1fr'
                    yield button

    def on_mount(self):
        self.query_one(TextArea).focus()
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            text = await offload(self.loader, cancelled=self.cancelled.is_set,
                                 cancellation=self.cancelled)
        except BaseException as error:
            self.pending_cleanup = (error if isinstance(error, CleanupPendingError)
                                    else getattr(error, 'cleanup_pending', None))
            text = 'Container logs unavailable or cancelled.'
        if self.is_mounted and not self.closing:
            self.query_one(TextArea).load_text(text)

    async def aclose(self):
        self.closing = True
        self.cancelled.set()
        if (self.cleanup_task is None or self.cleanup_task.done()
                and (self.pending_cleanup is not None or self.cleanup_task.cancelled()
                     or self.cleanup_task.exception() is not None)):
            self.cleanup_task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self.cleanup_task)
            if self.pending_cleanup is not None:
                raise self.pending_cleanup
        except asyncio.CancelledError:
            # Retain the exact caller interruption after owned disposal settles.
            try:
                await settle(self.cleanup_task)
            except BaseException:
                pass
            raise

    async def _close(self):
        if self.reader is not None:
            await settle(self.reader)
        if self.pending_cleanup is not None:
            pending = self.pending_cleanup
            await pending.retry()
            if self.pending_cleanup is pending:
                self.pending_cleanup = None

    async def action_close(self):
        try:
            await self.aclose()
        except BaseException as error:
            self.closing = False
            self.query_one('#log-error', Static).update('Reader cleanup is pending; retry Close.')
            if not isinstance(error, Exception) or self.pending_cleanup is None:
                raise
            return
        self.dismiss()

    async def on_unmount(self):
        await self.aclose()

    async def _copy(self, text):
        if not text:
            return
        try:
            await copy_text(self.app, text)
        except ClipboardError:
            self.query_one('#log-error', Static).update('Could not copy log text.')

    async def action_copy_selected(self):
        await self._copy(self.query_one(TextArea).selected_text)

    async def on_button_pressed(self, event):
        if event.button.id == 'log-copy':
            await self._copy(self.query_one(TextArea).text)
        else:
            await self.action_close()

    async def action_quit_request(self):
        from .app import QuitDialog
        self.app.push_screen(QuitDialog(), lambda yes: self.app.confirm_quit() if yes else None)
