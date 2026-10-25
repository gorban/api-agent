"""Optional read receipts for isolated configuration display resolution."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_ACTIVE = ContextVar('configuration_source_reads', default=None)


def capturing():
    return _ACTIVE.get() is not None


def resolve_path(path, *, strict=False):
    from pathlib import Path
    path = Path(path).absolute()
    try:
        target = path.resolve(strict=strict)
    except (OSError, RuntimeError):
        if capturing():
            def resolved():
                try:
                    return path.resolve(strict=strict)
                except (OSError, RuntimeError):
                    return None
            record_probe(('unresolved', path, strict), resolved, None)
        raise
    record_link(path, target)
    return target


@dataclass(frozen=True)
class SourceSnapshot:
    files: tuple
    links: tuple
    optional: tuple
    probes: tuple

    def current(self):
        from api_agent.workflows.definition import _read_bounded
        try:
            return (all(path.resolve() == target for path, target in self.links)
                    and all(path.exists() == present for path, present in self.optional)
                    and all(_read_bounded(path, root) == body for path, root, body in self.files)
                    and all(read() == expected for read, expected in self.probes))
        except (OSError, ValueError, RuntimeError):
            return False


class SourceReads:
    def __init__(self):
        self.files, self.links, self.optional, self.probes = {}, {}, {}, {}

    def snapshot(self):
        return SourceSnapshot(tuple((path, root, body) for path, (root, body) in self.files.items()),
                              tuple(self.links.items()), tuple(self.optional.items()),
                              tuple(self.probes.values()))


@contextmanager
def capture_reads():
    reads = SourceReads()
    token = _ACTIVE.set(reads)
    try:
        yield reads
    finally:
        _ACTIVE.reset(token)


def record_file(path, root, body):
    reads = _ACTIVE.get()
    if reads is not None:
        previous = reads.files.get(path)
        if previous is not None and previous[1] != body:
            raise ValueError('Configuration source changed during resolution')
        reads.files[path] = root, body


def record_link(path, target):
    reads = _ACTIVE.get()
    if reads is not None:
        previous = reads.links.get(path)
        if previous is not None and previous != target:
            raise ValueError('Configuration link changed during resolution')
        reads.links[path] = target


def record_optional(path, present):
    reads = _ACTIVE.get()
    if reads is not None:
        previous = reads.optional.get(path)
        if previous is not None and previous != present:
            raise ValueError('Configuration availability changed during resolution')
        reads.optional[path] = present


def record_probe(key, read, expected):
    reads = _ACTIVE.get()
    if reads is not None:
        previous = reads.probes.get(key)
        if previous is not None and previous[1] != expected:
            raise ValueError('Configuration inventory changed during resolution')
        reads.probes[key] = read, expected


def record_unreadable(path, root):
    if capturing():
        def readable():
            from api_agent.workflows.definition import _read_bounded
            try:
                _read_bounded(path, root)
                return True
            except (OSError, ValueError):
                return False
        record_probe(('unreadable', path, root), readable, False)
