"""Redact native input payloads before Textual writes files or devtools logs."""
from functools import wraps
from copy import copy
import inspect
import sys

from textual import Logger, events
from textual._context import active_app
from textual._xterm_parser import XTermParser
from textual.widgets import Input


def diagnostic_value(value):
    # Key names also encode typed characters; hiding character alone is insufficient.
    if isinstance(value, (events.Key, events.Paste)):
        return type(value).__name__ + '(<private input>)'
    if isinstance(value, (Input.Changed, Input.Submitted, Input.Blurred)) and value.input.password:
        return type(value).__name__ + '(<private input>)'
    return value


def discard_parser_debug(self, *objects):
    """Raw parser diagnostics bypass Logger and cannot safely record input."""
    return None


def install_private_input_logging():
    """Protect all logger instances, including native event logging before dispatch.

    Raw keyboard/paste payloads are always private so queued or bubbled events
    remain safe after a secret screen closes. Other diagnostics stay available.
    """
    # Also affects an existing driver's parser: each feed/token resolves this
    # method dynamically. Do not disable general TEXTUAL_DEBUG diagnostics.
    XTermParser.debug_log = discard_parser_debug
    original = Logger.__call__
    if getattr(original, '_private_input_filter', False):
        return

    @wraps(original)
    def safe_log(self, *objects, **values):
        caller = sys._getframe(1)
        logger = copy(self)

        def forward(group, verbosity, ignored_caller, *objects, **values):
            sink = self._log or (self.app or active_app.get())._log
            return sink(group, verbosity, inspect.getframeinfo(caller), *objects, **values)

        # A per-call clone avoids mutating a shared logger or application sink.
        # Textual still owns file/debug routing; only devtools metadata is fixed.
        logger._log = forward
        return original(logger, *(diagnostic_value(value) for value in objects),
                        **{key: diagnostic_value(value) for key, value in values.items()})

    safe_log._private_input_filter = True
    Logger.__call__ = safe_log
