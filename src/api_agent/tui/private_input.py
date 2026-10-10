"""Redact native input payloads before Textual writes files or devtools logs."""
from functools import wraps

from textual import Logger, events
from textual.widgets import Input


def diagnostic_value(value):
    # Key names also encode typed characters; hiding character alone is insufficient.
    if isinstance(value, (events.Key, events.Paste)):
        return type(value).__name__ + '(<private input>)'
    if isinstance(value, (Input.Changed, Input.Submitted, Input.Blurred)) and value.input.password:
        return type(value).__name__ + '(<private input>)'
    return value


def install_private_input_logging():
    """Protect all logger instances, including native event logging before dispatch.

    Raw keyboard/paste payloads are always private so queued or bubbled events
    remain safe after a secret screen closes. Other diagnostics stay available.
    """
    original = Logger.__call__
    if getattr(original, '_private_input_filter', False):
        return

    @wraps(original)
    def safe_log(self, *objects, **values):
        return original(self, *(diagnostic_value(value) for value in objects),
                        **{key: diagnostic_value(value) for key, value in values.items()})

    safe_log._private_input_filter = True
    Logger.__call__ = safe_log
