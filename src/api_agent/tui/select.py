"""Select confirmation, including reselecting the already-current value."""
from textual import on
from textual.message import Message
from textual.widgets import Select
from textual.widgets._select import SelectOverlay


class AdvancingSelect(Select):
    """Distinguish an explicit picker choice from initialization or cancellation."""

    class Submitted(Message):
        def __init__(self, select):
            super().__init__()
            self.select = select

    @on(SelectOverlay.UpdateSelection)
    def _update_selection(self, event):
        # Textual's Changed message omits confirmation of an unchanged value.
        super()._update_selection(event)
        self.post_message(self.Submitted(self))
