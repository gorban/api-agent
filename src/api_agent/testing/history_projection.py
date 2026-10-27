"""Validated history query reuse limited to one estimate projection."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from threading import RLock


_CANCEL = ContextVar('history_projection_cancel', default=None)


class ProjectionCancelled(RuntimeError):
    """An obsolete read must stop rather than become an unavailable estimate."""


@contextmanager
def cancellable_projection(cancel):
    token = _CANCEL.set(cancel)
    try:
        check_projection()
        yield
    finally:
        _CANCEL.reset(token)


def check_projection():
    cancel = _CANCEL.get()
    if cancel is not None and cancel.is_set():
        raise ProjectionCancelled()


def encode_history(records):
    check_projection()
    encoded = json.dumps(records, separators=(',', ':')).encode('utf-8')
    check_projection()
    return encoded


def decode_history(encoded):
    # JSON arrays represent the API's outer manifest/artifact tuple pairs.
    check_projection()
    records = [(manifest, artifact) for manifest, artifact in json.loads(encoded)]
    check_projection()
    return records


class HistoryProjection:
    """Share validation work, never mutable rows or cross-build query results."""
    def __init__(self, cache):
        self.cache = cache
        self.now = None
        self._queries = None
        self._lock = RLock()

    def __getattr__(self, name):
        return getattr(self.cache, name)

    @contextmanager
    def read(self):
        with self._lock:
            if self._queries is not None:
                yield self
                return
            self._queries = {}
            self.now = datetime.now(timezone.utc)
            try:
                yield self
            finally:
                self._queries = None
                self.now = None

    def query(self, origin, since, until, run_id):
        options = dict(since=since, until=until, run_id=run_id)
        if _CANCEL.get() is not None:
            options['_check'] = check_projection
        return self.cache.records(origin, **options)

    def records(self, origin=None, *, since=None, until=None, run_id=None):
        with self._lock:
            check_projection()
            if self._queries is None:
                return decode_history(encode_history(self.query(origin, since, until, run_id)))
            key = origin, since, until, run_id
            if key not in self._queries:
                self._queries[key] = encode_history(self.query(origin, since, until, run_id))
            return decode_history(self._queries[key])

    def ingest(self, *args, **kwargs):
        with self._lock:
            result = self.cache.ingest(*args, **kwargs)
            if self._queries is not None:
                self._queries.clear()
            return result
