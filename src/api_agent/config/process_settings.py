"""Declared settings read where configuration has not been resolved."""
from functools import lru_cache
import os

from .declarations import load_shared_schema
from .resolve import convert_value


@lru_cache(maxsize=1)
def _declarations():
    return load_shared_schema().fields


def process_setting(name, environ=None):
    """Return the process environment's value for a declared setting, else its default.

    Callers run before or outside configuration resolution, so .env files and
    options do not apply.
    """
    declaration = _declarations()[name]
    raw = (os.environ if environ is None else environ).get(name, '')
    if not raw:
        return declaration.default
    try:
        return convert_value(declaration, raw, external=True, anchors={})
    except ValueError as error:
        raise ValueError(f'{name}: {error}') from None
