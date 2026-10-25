"""Bounded session-local reuse of inert definitions, never effective settings."""
from collections import OrderedDict
from pathlib import Path
import os
import re
from threading import RLock
from urllib.parse import unquote, urlsplit

from api_agent.runtime.resource_pack import load_selected_definition
from api_agent.workflows.definition import _read_bounded

MAX_WATCHED_LINKS = 4096


class DefinitionCache:
    """Revalidate bytes and lexical link targets before reusing a parsed graph.

    Discovery still rereads catalogs, implementation declarations and settings.
    Errors are never cached. Execution uses its existing admission checks.
    """
    def __init__(self, *, max_bytes=64 * 1024 * 1024, max_entries=32):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def fork(self):
        """Give a read-only UI resolver independent eviction and locking state."""
        with self._lock:
            other = DefinitionCache(max_bytes=self.max_bytes, max_entries=self.max_entries)
            other._entries = self._entries.copy()
            other._bytes = self._bytes
            return other

    def __call__(self, context):
        base = getattr(context, 'workflow_base_root', None) or context.base_root
        entry = context.workflow_entry or base / 'flow.md.html'
        owned = getattr(context, 'workflow_library_files', None)
        key = (base, context.pack_root, entry, context.workflow_root,
               tuple(context.workflow_replacements), None if owned is None else frozenset(owned))
        with self._lock:
            cached = self._entries.pop(key, None)
            if cached is not None:
                definition, links, size = cached
                self._bytes -= size
                if self._current(definition, links):
                    self._record_links(links)
                    self._entries[key] = cached
                    self._bytes += size
                    return definition
            definition = load_selected_definition(context)
            try:
                links = self._links(definition, entry)
            except (OSError, ValueError, RuntimeError):
                # Optional presentation links can be unresolvable. Caching must
                # never make a valid executable declaration fail admission.
                return definition
            size = sum(len(body) for body in definition.contents.values())
            self._record_links(links)
            if size <= self.max_bytes and self.max_entries > 0:
                while self._entries and (self._bytes + size > self.max_bytes
                                         or len(self._entries) >= self.max_entries):
                    _, (_, _, removed) = self._entries.popitem(last=False)
                    self._bytes -= removed
                self._entries[key] = (definition, links, size)
                self._bytes += size
            return definition

    @staticmethod
    def _record_links(links):
        from api_agent.config.source_reads import record_link
        for path, target in links:
            record_link(path, target)

    @staticmethod
    def _current(definition, links):
        try:
            if any(path.resolve() != target for path, target in links):
                return False
            for path, body in definition.contents.items():
                if not isinstance(path, Path):
                    continue
                owner = max((root for root in definition.roots.values() if path.is_relative_to(root)),
                            key=lambda root: len(root.parts))
                if _read_bounded(path, owner) != body:
                    return False
        except (OSError, ValueError, RuntimeError):
            return False
        return True

    @staticmethod
    def _links(definition, entry):
        # Contents use canonical paths. Also watch the original lexical links:
        # retargeting an in-root symlink must not leave the old graph cached.
        paths = {entry}
        paths.update(definition.roots[item.root_id] / item.entry for item in definition.replacements)
        for path, body in definition.contents.items():
            if not isinstance(path, Path) or path.suffixes[-2:] != ['.md', '.html']:
                continue
            text = body.decode('utf-8')
            for value in re.findall(r'\[[^\]\n]+\]\(([^()\n]+)\)', text):
                value = value.replace(r'\<resource_pack\>', '<resource_pack>')
                parsed = urlsplit(value)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                name = unquote(parsed.path)
                target = (definition.roots['pack'] / name.removeprefix('<resource_pack>/')
                          if name.startswith('<resource_pack>/') else path.parent / name)
                paths.add(Path(os.path.normpath(target)))
                if len(paths) > MAX_WATCHED_LINKS:
                    raise ValueError('Definition link-watch budget exceeded')
        return tuple((path, path.resolve()) for path in sorted(paths))
