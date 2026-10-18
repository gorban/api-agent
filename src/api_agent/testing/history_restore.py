"""Explicit, bounded restoration of finalized secondary Allure runs to S3."""
import json
import os
from pathlib import Path
from contextlib import ExitStack
from botocore.exceptions import BotoCoreError, ClientError

from api_agent.providers.progress import report_timeout_sync

from .allure_source import MAX_ARTIFACT, MAX_MANIFEST, ViewerError, component, validate_manifest, verified_files
from .storage import RunStorage, StorageError
from .suite_lock import SuiteBusyError, suite_lock


class HistoryRestoreError(RuntimeError):
    """A safe restore failure without storage response or artifact content."""


class HistoryRestoreCancelled(HistoryRestoreError):
    """The caller cancelled before another immutable object write."""


def _active(cancelled, budget):
    if cancelled():
        raise HistoryRestoreCancelled('Allure history restore was cancelled.')
    if budget.remaining == 0:
        raise HistoryRestoreError('Allure history restore timed out.')


def _check_committed_destination(storage, repo, run_id, expected, active):
    """Reject an existing different manifest before writing any artifact key."""
    active()
    client = storage._client()
    active()
    try:
        result = client.get_object(Bucket=storage.bucket,
            Key=storage._key(repo, run_id, 'manifest.json'))
    except KeyError:
        return
    except ClientError as error:
        if error.response.get('Error', {}).get('Code') in {'NoSuchKey', '404'}:
            return
        raise HistoryRestoreError('Could not inspect the destination Allure manifest.') from None
    stream = result['Body']
    try:
        active()
        existing = stream.read(MAX_MANIFEST + 1)
    finally:
        stream.close()
    if existing != expected:
        raise HistoryRestoreError('Destination Allure run has different immutable manifest bytes.')


def restore_missing_history(repo, secondary_directory, storage, *, progress=lambda done, total: None,
                            cancelled=lambda: False, max_runs=1000, timeout=120):
    """Restore verified local runs, publishing each manifest only after its artifacts.

    A failed or cancelled run may leave uncommitted artifact keys. Retrying checks
    their exact bytes through RunStorage's conditional-write contract.
    """
    component(repo)
    if not isinstance(storage, RunStorage) or not secondary_directory:
        raise HistoryRestoreError('Select secondary history and an S3 destination before restoring.')
    if type(max_runs) is not int or not 1 <= max_runs <= 10000:
        raise ValueError('Invalid restore run limit')
    base = Path(secondary_directory)
    try:
        with suite_lock(), report_timeout_sync('Allure history restore', timeout) as budget:
            with ExitStack() as stack:
                base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, base_fd)
                try:
                    repo_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base_fd)
                except FileNotFoundError:
                    progress(0, 0)
                    return 0
                stack.callback(os.close, repo_fd)
                children = []
                with os.scandir(repo_fd) as entries:
                    for index, child in enumerate(entries, 1):
                        _active(cancelled, budget)
                        if index > max_runs * 4:
                            raise HistoryRestoreError('Secondary history exceeds the restore entry limit.')
                        if child.is_symlink():
                            raise HistoryRestoreError('Symlinked secondary history is forbidden.')
                        if child.is_dir(follow_symlinks=False):
                            children.append(child.name)
                            if len(children) > max_runs:
                                raise HistoryRestoreError('Secondary history exceeds the restore run limit.')
                total = len(children)
                progress(0, total)
                restored = 0
                for done, run_id in enumerate(sorted(children), 1):
                    _active(cancelled, budget)
                    with ExitStack() as run_stack:
                        run_fd = os.open(run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=repo_fd)
                        run_stack.callback(os.close, run_fd)
                        try:
                            body = RunStorage._read_source('manifest.json', directory_fd=run_fd, max_bytes=MAX_MANIFEST)
                        except FileNotFoundError:
                            # The producer has not finalized this run yet.
                            progress(done, total)
                            continue
                        if len(body) > MAX_MANIFEST:
                            raise HistoryRestoreError('Secondary history manifest exceeds the size limit.')
                        manifest = json.loads(body)
                        validate_manifest(manifest, repo=repo)
                        if run_id != manifest['run_id']:
                            raise HistoryRestoreError('Secondary history run identity differs from its directory.')
                        def read_verified(name):
                            _active(cancelled, budget)
                            return RunStorage._read_source(name, directory_fd=run_fd, max_bytes=MAX_ARTIFACT)
                        files = verified_files(manifest, read_verified)
                        active = lambda: _active(cancelled, budget)
                        _check_committed_destination(storage, repo, manifest['run_id'], body, active)
                        for name in sorted(files):
                            active()
                            storage._put_once(storage._key(repo, manifest['run_id'], name), files[name])
                        active()
                        storage._put_once(storage._key(repo, manifest['run_id'], 'manifest.json'), body)
                        restored += 1
                    progress(done, total)
                return restored
    except (HistoryRestoreError, SuiteBusyError):
        raise
    except (ViewerError, StorageError, BotoCoreError, OSError, ValueError, TypeError, KeyError):
        raise HistoryRestoreError('Could not restore finalized Allure history; check secondary files and S3 storage.') from None
