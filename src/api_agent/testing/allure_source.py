"""Verified immutable inputs for operation-owned Allure viewers."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID

from .storage import RunStorage

MAX_MANIFEST = 2 * 1024 * 1024
MAX_ARTIFACT = 16 * 1024 * 1024
MAX_RUN = 128 * 1024 * 1024


class ViewerError(RuntimeError):
    """Actionable viewer failure, independent of test publication."""


def component(value):
    if not isinstance(value, str) or not value or value in {'.', '..'} or any(c in value for c in '/\\\0'):
        raise ViewerError('Invalid Allure artifact identity')
    return value


def _check_budget(budget):
    if budget is not None and budget.remaining == 0:
        raise ViewerError('Allure history retrieval timed out')


def validate_manifest(manifest, *, repo=None, preview=False):
    """Require explicit run identity and flat, digest-bound artifact names."""
    try:
        if (not isinstance(manifest, dict) or type(manifest.get('schema')) is not int
                or manifest['schema'] != 1):
            raise ValueError()
        component(manifest['repo'])
        if repo is not None and repo != manifest['repo']:
            raise ValueError()
        if str(UUID(manifest['run_id'])) != manifest['run_id']:
            raise ValueError()
        start = datetime.fromisoformat(manifest['started_at'].replace('Z', '+00:00'))
        if start.tzinfo is None or manifest['mode'] not in {'integration', 'e2e'}:
            raise ValueError()
        if preview:
            if type(manifest.get('preview_version')) is not int or manifest['preview_version'] < 1:
                raise ValueError()
        else:
            end = datetime.fromisoformat(manifest['finished_at'].replace('Z', '+00:00'))
            if end.tzinfo is None or end < start or manifest['status'] not in {'completed', 'failed', 'interrupted'}:
                raise ValueError()
        files = manifest['files']
        if not isinstance(files, dict) or not files or len(files) > 4096:
            raise ValueError()
        for name, digest in files.items():
            component(name)
            if name == 'manifest.json' or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None:
                raise ValueError()
        if not any(name.endswith('-result.json') for name in files):
            raise ValueError()
        encoded = json.dumps(manifest, sort_keys=True, allow_nan=False).encode()
        if len(encoded) > MAX_MANIFEST:
            raise ValueError()
        return hashlib.sha256(encoded).hexdigest()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ViewerError('Invalid finalized Allure manifest or preview binding') from None


def verified_files(manifest, read):
    files = {}
    size = 0
    for name, digest in manifest['files'].items():
        body = read(name)
        if type(body) is not bytes or len(body) > MAX_ARTIFACT or hashlib.sha256(body).hexdigest() != digest:
            raise ViewerError('Allure artifact digest mismatch or size limit exceeded')
        size += len(body)
        if size > MAX_RUN:
            raise ViewerError('Allure run exceeds viewer staging limit')
        files[name] = body
    # Every attachment, including nested steps, must resolve within this binding.
    def attachments(value):
        if isinstance(value, dict):
            for attachment in value.get('attachments', []):
                if not isinstance(attachment, dict) or attachment.get('source') not in files:
                    raise ViewerError('Allure result references a missing attachment')
            for key, child in value.items():
                if key != 'attachments':
                    attachments(child)
        elif isinstance(value, list):
            for child in value:
                attachments(child)
    try:
        for name, body in files.items():
            if name.endswith('-result.json'):
                row = json.loads(body)
                if (not isinstance(row, dict) or not isinstance(row.get('uuid'), str)
                        or name != f'{row["uuid"]}-result.json'
                        or row.get('status') not in {'passed', 'failed', 'broken', 'skipped', 'unknown'}):
                    raise ViewerError('Invalid Allure result identity')
                attachments(row)
    except (ValueError, TypeError, RecursionError):
        raise ViewerError('Invalid Allure result data') from None
    return files


@dataclass(frozen=True)
class PreviewSnapshot:
    """Producer-owned bytes; viewers copy and validate before any generation."""
    files: dict
    manifest: dict
    result_files: dict
    preview: bool = True


class RunSource:
    def __init__(self, repo, local_root=None, storage=None):
        self.repo = component(repo)
        self.local_root = Path(local_root) if local_root else None
        self.storage = storage

    def _local(self, run_id):
        directory = self.local_root / self.repo / run_id
        if self.local_root.is_symlink() or directory.parent.is_symlink() or directory.is_symlink():
            raise ViewerError('Symlinked Allure history is forbidden')
        return directory

    def history(self, *, budget=None):
        """Discover committed manifests, without a time/lookback filter."""
        rows = {}
        try:
            _check_budget(budget)
            if self.local_root is not None:
                directory = self.local_root / self.repo
                if directory.exists():
                    if directory.is_symlink() or self.local_root.is_symlink():
                        raise ViewerError('Symlinked Allure history is forbidden')
                    for child in directory.iterdir():
                        _check_budget(budget)
                        if child.is_symlink():
                            raise ViewerError('Symlinked Allure history is forbidden')
                        if not child.is_dir() or not (child / 'manifest.json').exists():
                            continue
                        body = RunStorage._read_source(self._local(child.name) / 'manifest.json', max_bytes=MAX_MANIFEST)
                        if len(body) > MAX_MANIFEST:
                            raise ViewerError('Allure manifest exceeds viewer limit')
                        manifest = json.loads(body)
                        validate_manifest(manifest, repo=self.repo)
                        if child.name != manifest['run_id']:
                            raise ViewerError('Allure history directory identity mismatch')
                        rows[manifest['run_id']] = manifest
            if self.storage is not None:
                for manifest in self.storage.history(self.repo, **({'budget': budget} if budget is not None else {})):
                    _check_budget(budget)
                    digest = validate_manifest(manifest, repo=self.repo)
                    previous = rows.get(manifest['run_id'])
                    if previous is not None and validate_manifest(previous) != digest:
                        raise ViewerError('Conflicting immutable Allure run manifests')
                    rows[manifest['run_id']] = manifest
            return sorted(rows.values(), key=lambda row: (
                datetime.fromisoformat(row['finished_at'].replace('Z', '+00:00')).astimezone(timezone.utc), row['run_id']), reverse=True)
        except ViewerError:
            raise
        except Exception:
            raise ViewerError('Could not discover finalized Allure history') from None

    def load(self, manifest, *, budget=None):
        """Fetch this exact committed manifest and verify every artifact byte."""
        digest = validate_manifest(manifest, repo=self.repo)
        try:
            _check_budget(budget)
            directory = self._local(manifest['run_id']) if self.local_root is not None else None
            if directory is not None and (directory / 'manifest.json').exists():
                committed = json.loads(RunStorage._read_source(directory / 'manifest.json', max_bytes=MAX_MANIFEST))
                if validate_manifest(committed, repo=self.repo) != digest:
                    raise ViewerError('Finalized Allure manifest binding changed')
                def local_read(name):
                    _check_budget(budget)
                    return RunStorage._read_source(directory / name, max_bytes=MAX_ARTIFACT)
                return verified_files(manifest, local_read)
            if self.storage is None:
                raise ViewerError('Exact finalized Allure run is unavailable locally or in S3')
            def read(name, limit=MAX_ARTIFACT):
                _check_budget(budget)
                stream = self.storage._client().get_object(Bucket=self.storage.bucket,
                    Key=self.storage._key(self.repo, manifest['run_id'], name))['Body']
                try:
                    return stream.read(limit + 1)
                finally:
                    stream.close()
            committed = json.loads(read('manifest.json', MAX_MANIFEST))
            if validate_manifest(committed, repo=self.repo) != digest:
                raise ViewerError('Finalized Allure manifest binding changed')
            return verified_files(manifest, read)
        except ViewerError:
            raise
        except Exception:
            raise ViewerError('Could not fetch the exact Allure run; check reporting storage') from None
