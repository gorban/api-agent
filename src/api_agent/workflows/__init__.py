"""Validated Markdown workflows; providers remain independently owned."""
from .model import LayoutError
__all__ = ['LayoutError', 'load_layout', 'Workflow', 'WorkflowError', 'NodeFunction', 'GuardResult', 'NodeContext', 'RecoverableNodeFailure',
           'SessionSnapshot', 'SessionConflict', 'WorkflowSessionStore']


def __getattr__(name):
    # The Markdown parser and the execution engine are each loaded only when one
    # of their symbols is used, so a compiled-plan process never imports the parser.
    if name == 'load_layout':
        from . import layout
        return layout.load_layout
    if name in {'Workflow', 'WorkflowError', 'NodeFunction', 'GuardResult', 'NodeContext', 'RecoverableNodeFailure'}:
        from . import execution
        return getattr(execution, name)
    if name in {'SessionSnapshot', 'SessionConflict', 'WorkflowSessionStore'}:
        from . import session_store
        return getattr(session_store, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
