"""Validated Markdown workflows; providers remain independently owned."""
from .layout import LayoutError, load_layout
__all__ = ['LayoutError', 'load_layout', 'Workflow', 'WorkflowError', 'NodeFunction', 'GuardResult', 'NodeContext']


def __getattr__(name):
    # Parsing and discovery do not need the execution engine. Preserve the
    # public imports while loading it only when an execution symbol is used.
    if name in {'Workflow', 'WorkflowError', 'NodeFunction', 'GuardResult', 'NodeContext'}:
        from . import execution
        return getattr(execution, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
