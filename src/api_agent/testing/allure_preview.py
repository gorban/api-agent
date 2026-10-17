"""Optional immutable viewer copies, independent of finalized publication."""
from copy import deepcopy
import hashlib
import json

from api_agent.providers.progress import report_timeout_sync
from .storage import RunStorage


MAX_FILES = 4096
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024


def emit_snapshot(recorder, result_files, callback, *, manifest=None, version=1):
    """Copy only recorded bytes; viewer failure must not alter test outcomes.

    Previews have no publication manifest on disk. Their hashes identify a
    frozen copy; final snapshots retain the authoritative manifest unchanged.
    Callbacks must transfer ownership synchronously before recording resumes.
    """
    if callback is None:
        return
    try:
        with report_timeout_sync('Allure viewer snapshot', 5):
            names = set(result_files.values())
            if manifest is None:
                for name in tuple(names):
                    row = json.loads(RunStorage._read_source(
                        recorder.directory / name, max_bytes=MAX_FILE_BYTES))
                    names.update(attachment['source'] for attachment in row['attachments'])
            else:
                names = set(manifest['files'])
            if len(names) > MAX_FILES:
                raise ValueError('Viewer snapshot has too many artifacts')
            files = {}
            size = 0
            for name in sorted(names):
                if (not isinstance(name, str) or not name or name in {'.', '..'}
                        or any(character in name for character in '/\\\0')):
                    raise ValueError('Invalid viewer artifact name')
                body = RunStorage._read_source(recorder.directory / name, max_bytes=MAX_FILE_BYTES)
                size += len(body)
                if len(body) > MAX_FILE_BYTES or size > MAX_TOTAL_BYTES:
                    raise ValueError('Viewer snapshot exceeds its size limit')
                files[name] = body
            if manifest is None:
                identity = recorder.identity
                manifest = {'schema': 1, 'repo': identity.repo, 'run_id': identity.run_id,
                            'mode': identity.mode, 'started_at': identity.started_at.isoformat(),
                            'preview_version': version,
                            'files': {name: hashlib.sha256(body).hexdigest()
                                      for name, body in files.items()}}
                preview = True
            else:
                preview = False
                if any(hashlib.sha256(files[name]).hexdigest() != digest
                       for name, digest in manifest['files'].items()):
                    raise ValueError('Viewer snapshot integrity mismatch')
            from .allure_viewer import PreviewSnapshot
            callback(PreviewSnapshot(files, deepcopy(manifest), dict(result_files), preview))
    except Exception:
        # Evidence capture/publication remains authoritative, including when a
        # viewer cannot retain a bounded copy or its UI has already closed.
        return
