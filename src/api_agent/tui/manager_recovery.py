"""Ephemeral password-manager interaction, separate from test evidence."""
from dataclasses import replace
import queue
import re
import threading

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Static, TextArea

from api_agent.auth.interaction import Reply
from .app import ButtonFooter, FocusNavigation, QuitDialog
from .clipboard import ClipboardError, copy_text
from . import clipboard


_LIMIT = 65536
_ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]')


def transcript_text(value):
    value = _ANSI.sub('', value)
    return ''.join(char for char in value if char in '\n\r\t' or ord(char) >= 32 and ord(char) != 127)


class _PrivateInput(Input):
    """Explicit paste reads the operating-system clipboard outside the UI loop."""

    def __init__(self, **kwargs):
        self._paste_generation = 0
        self._paste_worker = None
        super().__init__(**kwargs)

    def watch_value(self):
        self._paste_generation += 1

    def watch_selection(self):
        self._paste_generation += 1

    def on_blur(self):
        self._paste_generation += 1

    def _paste_allowed(self):
        if not self.is_attached:
            return False
        screen = self.screen
        return (self.has_focus and not self.disabled
                and self.app.screen is screen and not screen.pending
                and not screen.awaiting_output and not screen.channel.closed
                and screen.query_one('#manager-input') is self)

    def action_cut(self):
        self.delete_selection()

    def action_copy(self):
        pass  # Masked input must never copy private values out.

    def action_paste(self):
        if not self._paste_allowed():
            return
        if self._paste_worker is not None and not self._paste_worker.is_finished:
            return
        self._paste_worker = self.run_worker(
            self._read_paste(self._paste_generation), group='private-paste')

    async def _read_paste(self, generation):
        try:
            value = await clipboard.read_text()
        except ClipboardError:
            if self._paste_allowed() and generation == self._paste_generation:
                self.app.notify('Native clipboard unavailable. Use terminal Paste (commonly Ctrl+Shift+V on Linux).',
                                severity='warning', timeout=5)
            return
        if self._paste_allowed() and generation == self._paste_generation:
            self.replace(value, *self.selection)

    def _on_paste(self, event):
        event.prevent_default()
        if self._paste_allowed():
            self.replace(event.text, *self.selection)
        event.stop()


class ManagerRecoveryScreen(FocusNavigation, Screen):
    AUTO_FOCUS = '#manager-input'
    BINDINGS = [Binding('ctrl+r', 'retry', 'Retry', key_display='^r', priority=True),
                Binding('ctrl+c', 'copy_output', 'Copy selected output', key_display='^c', priority=True)]
    DEFAULT_CSS = '''
    ManagerRecoveryScreen { padding: 1 2; }
    ManagerRecoveryScreen #manager-title { text-style: bold; margin-bottom: 1; }
    ManagerRecoveryScreen #manager-output-scroll { height: 1fr; min-height: 6; }
    ManagerRecoveryScreen #manager-output { height: 1fr; min-height: 6; }
    ManagerRecoveryScreen #manager-input-label { margin-top: 1; }
    ManagerRecoveryScreen #manager-actions { height: auto; margin-top: 1; }
    ManagerRecoveryScreen Button { margin-right: 1; }
    '''

    def __init__(self, channel):
        from .private_input import install_private_input_logging
        install_private_input_logging()
        super().__init__()
        self.channel = channel
        self.transcript = ''
        self.pending = False
        self.awaiting_output = False
        self._copy_worker = None

    def compose(self):
        yield Static(Text(self.channel.label), id='manager-title')
        yield Static('Output: select text to reveal the copy shortcut. Masked input cannot be copied.')
        with VerticalScroll(id='manager-output-scroll') as scroll:
            scroll.can_focus = False
            yield TextArea('', id='manager-output', read_only=True, show_cursor=False)
        yield Static('Input:', id='manager-input-label')
        yield _PrivateInput(password=True, id='manager-input')
        with Horizontal(id='manager-actions'):
            for label, identifier in (('Send value (Enter)', 'manager-send'),
                                      ('Cancel', 'manager-cancel')):
                button = Button(label, id=identifier)
                button.styles.width = 'auto'
                yield button
        yield ButtonFooter()

    def on_mount(self):
        clipboard.clear_local_clipboard(self.app)
        self.query_one('#manager-output', TextArea).load_text(self.transcript)
        self.query_one('#manager-input', Input).focus()

    def on_screen_suspend(self):
        self.query_one('#manager-input', _PrivateInput)._paste_generation += 1

    def append_output(self, value):
        if self.channel.closed or self.pending:
            return
        text = transcript_text(value)
        self.transcript = (self.transcript + text)[-_LIMIT:]
        if self.is_mounted:
            self.query_one('#manager-output', TextArea).load_text(self.transcript)
            self.query_one('#manager-output', TextArea).scroll_end(animate=False)
            # Enter echoes and formatting-only output are not a new prompt.
            if text.strip() and self.awaiting_output:
                self.awaiting_output = False
                field = self.query_one('#manager-input', Input)
                field.value = ''
                field.disabled = False
                self.query_one('#manager-send', Button).disabled = False
                if self.app.screen is self:
                    field.focus()

    def send_value(self):
        if self.pending or self.channel.closed or self.awaiting_output:
            return
        field = self.query_one('#manager-input', _PrivateInput)
        field._paste_generation += 1
        self.awaiting_output = True
        field.disabled = True
        self.query_one('#manager-send', Button).disabled = True
        self.channel.reply(Reply('input', field.value))

    def on_input_submitted(self, event):
        self.send_value()

    async def on_button_pressed(self, event):
        if event.button.id == 'manager-send':
            self.send_value()
        else:
            self.request_cancel()

    def request_cancel(self):
        if not self.pending and not self.channel.closed:
            self.app.push_screen(QuitDialog('Cancel authentication? Enter literal y to confirm; every other response cancels.'),
                                 lambda yes: self.stop('cancel') if yes else None)

    def stop(self, action):
        if self.pending or self.channel.closed:
            return
        self.pending = True
        self.query_one('#manager-input', Input).value = ''
        for widget in self.query('Input, Button'):
            widget.disabled = True
        self.channel.reply(Reply(action))
        self.refresh_bindings()

    async def action_copy_output(self):
        # Consume Ctrl+C even without a selection: it must never become quit.
        if self.pending or self.channel.closed or self.app.screen is not self:
            return
        selected = self.query_one('#manager-output', TextArea).selected_text
        if not selected:
            return
        if self._copy_worker is not None and not self._copy_worker.is_finished:
            return
        self._copy_worker = self.run_worker(self._copy_output(selected), group='output-copy')

    async def _copy_output(self, selected):
        severity = 'information'
        try:
            native = await copy_text(self.app, selected)
            message = 'Output copied.' if native else 'Output sent to terminal clipboard.'
        except ClipboardError:
            message, severity = 'Could not copy selected output.', 'error'
        finally:
            clipboard.clear_local_clipboard(self.app)
        if not self.pending and not self.channel.closed and self.app.screen is self:
            self.app.notify(message, severity=severity, timeout=2)

    def on_text_area_selection_changed(self, event):
        self.refresh_bindings()

    @property
    def active_bindings(self):
        bindings = super().active_bindings
        copy = bindings['ctrl+c']
        # Hide the hint separately from action availability: disabling the action
        # would let Ctrl+C fall through to input copying or application quit.
        visible = (not self.pending and not self.channel.closed
                   and bool(self.query_one('#manager-output', TextArea).selected_text))
        bindings['ctrl+c'] = copy._replace(binding=replace(copy.binding, show=visible))
        return bindings

    def check_action(self, action, parameters):
        if action == 'copy_output':
            return True
        return not self.pending and not self.channel.closed

    def action_retry(self):
        self.stop('retry')


class _Channel:
    def __init__(self, interaction, label):
        self.interaction, self.label = interaction, label
        self.responses = queue.Queue()
        self.closed = False
        self.screen = ManagerRecoveryScreen(self)

    def write(self, text):
        if not self.closed:
            self.interaction.app.call_from_thread(self.screen.append_output, text)

    def read(self, timeout):
        if self.closed or self.interaction.cancel_event.is_set():
            return Reply('cancel')
        try:
            return self.responses.get(timeout=timeout)
        except queue.Empty:
            return None

    def reply(self, reply):
        if not self.closed:
            self.responses.put(reply)

    def close(self):
        with self.interaction.condition:
            if self.closed:
                return
            app = self.interaction.app
            if not app.is_running:
                self.interaction.dismiss(self)
            else:
                try:
                    app.call_from_thread(self.interaction.dismiss, self)
                except RuntimeError:
                    if app.is_running:
                        raise
                    self.interaction.dismiss(self)
            self.closed = True


class TUIInteraction:
    """Blocking CLI-thread transport; the Textual loop only presents messages."""
    def __init__(self, app, cancel_event):
        self.app, self.cancel_event = app, cancel_event
        self.lock = threading.RLock()
        self.condition = threading.Condition()
        self.active = None
        self.waiting = []

    def open(self, label):
        channel = _Channel(self, label)
        if self.cancel_event.is_set():
            channel.closed = True
        else:
            self.app.call_from_thread(self.present, channel)
        return channel

    def present(self, channel):
        if self.active is None:
            self.active = channel
            self.app.push_screen(channel.screen)
        else:
            self.waiting.append(channel)

    def dismiss(self, channel):
        # A deadline/cancellation may complete while the confirmation is open.
        if self.app.is_running and channel.screen in self.app.screen_stack:
            channel.screen.query_one('#manager-input', Input).value = ''
            channel.screen.query_one('#manager-output', TextArea).load_text('')
            while self.app.screen is not channel.screen:
                self.app.pop_screen()
            self.app.pop_screen()
        channel.screen.transcript = ''
        if channel in self.waiting:
            self.waiting.remove(channel)
        if self.active is channel:
            self.active = None
            if self.waiting and self.app.is_running:
                self.present(self.waiting.pop(0))
