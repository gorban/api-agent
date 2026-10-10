"""Ephemeral password-manager interaction, separate from test evidence."""
import queue
import re
import threading

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from api_agent.auth.interaction import Reply
from .app import ButtonFooter, FocusNavigation, QuitDialog


_LIMIT = 65536
_ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]')


def transcript_text(value):
    value = _ANSI.sub('', value)
    return ''.join(char for char in value if char in '\n\r\t' or ord(char) >= 32 and ord(char) != 127)


class ManagerRecoveryScreen(FocusNavigation, Screen):
    BINDINGS = [Binding('ctrl+r', 'retry', 'Retry', key_display='^r', priority=True)]
    DEFAULT_CSS = '''
    ManagerRecoveryScreen { padding: 1 2; }
    ManagerRecoveryScreen #manager-title { text-style: bold; margin-bottom: 1; }
    ManagerRecoveryScreen #manager-output-scroll { height: 1fr; min-height: 6; }
    ManagerRecoveryScreen #manager-output { height: auto; }
    ManagerRecoveryScreen #manager-input-label { margin-top: 1; }
    ManagerRecoveryScreen #manager-actions { height: auto; margin-top: 1; }
    ManagerRecoveryScreen Button { margin-right: 1; }
    '''

    def __init__(self, channel):
        super().__init__()
        self.channel = channel
        self.transcript = ''
        self.pending = False
        self.awaiting_output = False

    def compose(self):
        yield Static(Text(self.channel.label + ' Password Manager'), id='manager-title')
        yield Static('Output:')
        with VerticalScroll(id='manager-output-scroll') as scroll:
            scroll.can_focus = False
            yield Static('', id='manager-output', markup=False)
        yield Static('Input:', id='manager-input-label')
        yield Input(password=True, id='manager-input')
        with Horizontal(id='manager-actions'):
            yield Button('Send value (Enter)', id='manager-send')
            yield Button('Cancel', id='manager-cancel')
        yield ButtonFooter()

    def on_mount(self):
        self.query_one('#manager-output', Static).update(self.transcript)
        self.query_one('#manager-input', Input).focus()

    def append_output(self, value):
        if self.channel.closed or self.pending:
            return
        text = transcript_text(value)
        self.transcript = (self.transcript + text)[-_LIMIT:]
        if self.is_mounted:
            self.query_one('#manager-output', Static).update(self.transcript)
            self.query_one('#manager-output-scroll', VerticalScroll).scroll_end(animate=False)
            if text and self.awaiting_output:
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
        field = self.query_one('#manager-input', Input)
        self.awaiting_output = True
        field.disabled = True
        self.query_one('#manager-send', Button).disabled = True
        self.channel.reply(Reply('input', field.value))

    def on_input_submitted(self, event):
        self.send_value()

    def on_button_pressed(self, event):
        if event.button.id == 'manager-send':
            self.send_value()
        else:
            self.request_cancel()

    def request_cancel(self):
        if not self.pending and not self.channel.closed:
            self.app.push_screen(QuitDialog('Cancel password-manager login? Enter literal y to confirm; every other response cancels.'),
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

    def check_action(self, action, parameters):
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
            self.closed = True
        self.interaction.app.call_from_thread(self.interaction.dismiss, self)


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
        if channel.screen in self.app.screen_stack:
            channel.screen.query_one('#manager-input', Input).value = ''
            while self.app.screen is not channel.screen:
                self.app.pop_screen()
            self.app.pop_screen()
        channel.screen.transcript = ''
        if channel in self.waiting:
            self.waiting.remove(channel)
        if self.active is channel:
            self.active = None
            if self.waiting:
                self.present(self.waiting.pop(0))
