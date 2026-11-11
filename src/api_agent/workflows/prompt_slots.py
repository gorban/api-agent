"""Prompt slots: ``{{name}}`` placeholders whose text the selected resource pack supplies.

A pack declares ``[prompt_slots]`` in its ``pack.toml``, mapping each slot name to a
pack-relative Markdown or text file. Placeholder syntax is shared by admission, the
engine and compiled-plan decoding, which import nothing from the declaration parser.
"""
from pathlib import PurePosixPath
import re

from .model import LayoutError, _ID

_SLOT = re.compile(r'(\\)?\{\{([a-z][a-z0-9_]{0,63})\}\}')
_SLOT_BYTES = re.compile(_SLOT.pattern.encode())
_SUFFIXES = ('.md', '.txt')


def used_slots(data):
    """Slot names a prompt's bytes use, in first-use order; ``\\{{name}}`` is a literal."""
    return list(dict.fromkeys(match.group(2).decode() for match in _SLOT_BYTES.finditer(data)
                              if not match.group(1)))


def slot_text(data):
    """The text a slot file supplies: UTF-8 without its single final line break."""
    return data.decode('utf-8').removesuffix('\n')


def fill_slots(text, slots):
    """Replace each unescaped placeholder once; supplied text is never rescanned."""
    return _SLOT.sub(lambda match: match.group(0)[1:] if match.group(1) else slots[match.group(2)], text)


def check_prompt_file(where, resolved, root, *, text_only):
    """A prompt file, even through a symlink, stays inside the root declaring it.

    Pack-supplied slot files and typed-grammar prompts must also be Markdown or text.
    """
    if text_only and resolved.suffix not in _SUFFIXES:
        raise LayoutError(f'{where}: prompt file must be a Markdown or text file')
    if root is None or not resolved.is_relative_to(root):
        raise LayoutError(f'{where}: prompt file must stay inside its resource root')


def slot_paths(document, where):
    """The validated ``slot -> relative path`` table of a parsed pack declaration."""
    table = document.get('prompt_slots', {})
    if type(table) is not dict:
        raise LayoutError(f'{where}: prompt_slots must be a table')
    for name, path in table.items():
        if (not _ID.fullmatch(name) or type(path) is not str or not path or path.startswith('/')
                or '\\' in path or any(part in ('', '.', '..') for part in path.split('/'))
                or PurePosixPath(path).suffix not in _SUFFIXES):
            raise LayoutError(f'{where}: invalid prompt slot {name}')
    return table
