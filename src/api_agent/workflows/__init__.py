"""Validated Markdown workflows; providers remain independently owned."""
from .model import LayoutError
__all__ = ['LayoutError', 'load_layout', 'Workflow', 'WorkflowError', 'NodeFunction', 'GuardResult', 'NodeContext']


def __getattr__(name):
    # The Markdown parser and the execution engine are each loaded only when one
    # of their symbols is used, so a compiled-plan process never imports the parser.
    if name == 'load_layout':
        from . import layout
        return layout.load_layout
    if name in {'Workflow', 'WorkflowError', 'NodeFunction', 'GuardResult', 'NodeContext'}:
        from . import execution
        return getattr(execution, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
