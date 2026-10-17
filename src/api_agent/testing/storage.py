"""S3 primary persistence with immutable run keys and optional local copies."""
from pathlib import Path
import json
import hashlib
import os
import stat
from urllib.parse import urlsplit
from contextlib import ExitStack
from datetime import datetime, timezone
from uuid import UUID
from uuid import uuid4


class StorageError(RuntimeError):
    """Bounded error that never includes provider response text."""


class RunStorage:
    def __init__(self, bucket, *, client=None, prefix='allure', secondary_directory=None,
                 endpoint_url=None, region_name=None, localstack_port=4566, managed_local=False):
        if not bucket or not isinstance(bucket, str):
            raise ValueError('An S3 bucket is required')
        if type(localstack_port) is not int or not 1 <= localstack_port <= 65535:
            raise ValueError('LocalStack port must be an integer between 1 and 65535')
        if type(managed_local) is not bool:
            raise ValueError('Managed local storage selection must be a boolean')
        if managed_local:
            from api_agent.runtime.compose import _endpoint, _local_s3_endpoint
            parsed, port = _endpoint(f'http://localhost:{localstack_port}' if endpoint_url is None else endpoint_url)
            if not _local_s3_endpoint(parsed, port, localstack_port):
                raise ValueError('Managed local storage requires its configured loopback endpoint')
            # Managed listeners and ownership verification bind IPv4 explicitly.
            # Never resolve localhost to a different, unowned IPv6 service.
            endpoint_url = f'http://127.0.0.1:{localstack_port}'
        self.localstack_port = localstack_port
        self.bucket = bucket
        self.client = client
        self.prefix = prefix.strip('/')
        if not self.prefix or any(part in {'', '.', '..'} for part in self.prefix.split('/')):
            raise ValueError('Invalid storage prefix')
        self.secondary_directory = Path(secondary_directory) if secondary_directory else None
        self.endpoint_url = endpoint_url
        self.region_name = region_name

    def _client(self):
        if self.client is None:
            import boto3
            from botocore.config import Config
            endpoint = urlsplit(self.endpoint_url or '')
            local = (endpoint.scheme == 'http' and endpoint.hostname in {'localhost', '127.0.0.1'}
                     and endpoint.port == self.localstack_port and not endpoint.path.rstrip('/')
                     and not endpoint.query and not endpoint.fragment and not endpoint.username
                     and not endpoint.password)
            credentials = {'aws_access_key_id': 'test', 'aws_secret_access_key': 'test'} if local else {}
            self.client = boto3.client('s3', endpoint_url=self.endpoint_url,
                                       region_name=self.region_name,
                                       **credentials,
                                       config=Config(connect_timeout=5, read_timeout=15,
                                                     retries={'mode': 'standard', 'total_max_attempts': 3}))
        return self.client

    def _key(self, repo, run_id, filename):
        return f'{self.prefix}/{repo}/{run_id}/{filename}'

    def _put_once(self, key, data):
        try:
            self._client().put_object(Bucket=self.bucket, Key=key, Body=data, IfNoneMatch='*')
        except Exception:
            # A retry is safe only when the object already has precisely these bytes.
            try:
                body = self._client().get_object(Bucket=self.bucket, Key=key)['Body']
                try:
                    existing = body.read(len(data) + 1)
                finally:
                    body.close()
            except Exception:
                raise StorageError('Could not persist Allure run') from None
            if existing != data:
                raise StorageError('Could not persist Allure run') from None

    @staticmethod
    def _open_directory(parent_fd, name):
        try:
            os.mkdir(name, dir_fd=parent_fd)
        except FileExistsError:
            pass
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)

    @staticmethod
    def _copy_once(directory_fd, name, data):
        temporary = f'.allure-{uuid4()}'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            try:
                os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                        follow_symlinks=False)
            except FileExistsError:
                existing_fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                                      dir_fd=directory_fd)
                with os.fdopen(existing_fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise StorageError('Could not persist Allure run') from None
                    if stream.read(len(data) + 1) != data:
                        raise StorageError('Could not persist Allure run') from None
        finally:
            os.unlink(temporary, dir_fd=directory_fd)

    @staticmethod
    def _read_source(path, *, directory_fd=None, max_bytes=None):
        """Reject links and nonregular staging entries before any blocking read."""
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StorageError('Could not persist Allure run')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                return stream.read() if max_bytes is None else stream.read(max_bytes + 1)
        finally:
            os.close(descriptor)

    def persist(self, directory, manifest):
        """Upload all content, then manifest last as the durable commit marker."""
        source = Path(directory)
        repo, run_id = manifest['repo'], manifest['run_id']
        if (not isinstance(repo, str) or not repo or repo in {'.', '..'} or any(c in repo for c in '/\\\0')
                or not isinstance(run_id, str) or str(UUID(run_id)) != run_id):
            raise ValueError('Invalid run identity')
        names = manifest['files']
        if not isinstance(names, dict) or any(
                not isinstance(name, str) or not name or name in {'.', '..', 'manifest.json'}
                or any(c in name for c in '/\\\0') for name in names):
            raise ValueError('Invalid artifact name')
        if source.is_symlink() or any((source / name).is_symlink() for name in [*names, 'manifest.json']):
            raise ValueError('Symlinked artifacts are forbidden')
        try:
            with ExitStack() as sources:
                source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                sources.callback(os.close, source_fd)
                client = self._client()
                manifest_body = self._read_source('manifest.json', directory_fd=source_fd)
                if json.loads(manifest_body) != manifest:
                    raise ValueError('Manifest does not match finalized metadata')
                for name in sorted(names):
                    body = self._read_source(name, directory_fd=source_fd)
                    if hashlib.sha256(body).hexdigest() != names[name]:
                        raise ValueError('Artifact digest mismatch')
                    self._put_once(self._key(repo, run_id, name), body)
                self._put_once(self._key(repo, run_id, 'manifest.json'), manifest_body)
                if self.secondary_directory:
                    base = self.secondary_directory.resolve()
                    base.mkdir(parents=True, exist_ok=True)
                    with ExitStack() as stack:
                        base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        stack.callback(os.close, base_fd)
                        repo_fd = self._open_directory(base_fd, repo)
                        stack.callback(os.close, repo_fd)
                        run_fd = self._open_directory(repo_fd, run_id)
                        stack.callback(os.close, run_fd)
                        for name in sorted(names):
                            body = self._read_source(name, directory_fd=source_fd)
                            if hashlib.sha256(body).hexdigest() != names[name]:
                                raise StorageError('Could not persist Allure run')
                            self._copy_once(run_fd, name, body)
                        self._copy_once(run_fd, 'manifest.json', manifest_body)
        except Exception:
            raise StorageError('Could not persist Allure run') from None
        return self._key(repo, run_id, 'manifest.json')

    def history(self, repo, *, max_pages=100, budget=None, require_accounting=False):
        """Return finalized manifests ordered by finish time and run ID."""
        if not isinstance(repo, str) or not repo or repo in {'.', '..'} or any(c in repo for c in '/\\\0'):
            raise ValueError('Invalid repository')
        prefix = f'{self.prefix}/{repo}/'
        rows = []
        try:
            from api_agent.telemetry.history import MAX_BYTES
            client = self._client()
            token = None
            seen_tokens = set()
            pages = 0
            while True:
                if budget is not None and budget.remaining == 0:
                    raise TimeoutError()
                pages += 1
                if max_pages is not None and pages > max_pages:
                    raise ValueError('History page limit exceeded')
                kwargs = {'Bucket': self.bucket, 'Prefix': prefix}
                if token:
                    kwargs['ContinuationToken'] = token
                page = client.list_objects_v2(**kwargs)
                for entry in page.get('Contents', ()):
                    if budget is not None and budget.remaining == 0:
                        raise TimeoutError()
                    key = entry['Key']
                    if not key.startswith(prefix) or not key.endswith('/manifest.json'):
                        continue
                    try:
                        body = client.get_object(Bucket=self.bucket, Key=key)['Body']
                        try:
                            data = body.read(MAX_BYTES + 1)
                        finally:
                            body.close()
                        if len(data) > MAX_BYTES:
                            raise ValueError('Oversized history manifest')
                        manifest = json.loads(data)
                        valid_id = str(UUID(manifest.get('run_id'))) == manifest.get('run_id')
                        finished = datetime.fromisoformat(manifest.get('finished_at').replace('Z', '+00:00'))
                        started = datetime.fromisoformat(manifest.get('started_at').replace('Z', '+00:00'))
                        valid_time = finished.tzinfo is not None and started.tzinfo is not None and finished >= started
                        files = manifest.get('files')
                        valid_files = isinstance(files, dict) and all(
                            isinstance(name, str) and name and name not in {'.', '..', 'manifest.json'} and
                            not any(c in name for c in '/\\\0') and isinstance(digest, str) and
                            len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)
                            for name, digest in files.items())
                    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                        if require_accounting:
                            raise StorageError('Could not load Allure history') from None
                        continue
                    if (manifest.get('schema') == 1 and manifest.get('repo') == repo and valid_id and valid_time and
                            manifest.get('mode') in {'integration', 'e2e'} and
                            key == self._key(repo, manifest['run_id'], 'manifest.json') and
                            manifest.get('status') in {'completed', 'interrupted', 'failed'} and
                            valid_files):
                        if require_accounting and 'accounting.json' not in files:
                            raise StorageError('Could not load Allure history')
                        rows.append((finished.astimezone(timezone.utc), manifest))
                    elif require_accounting:
                        raise StorageError('Could not load Allure history')
                if not page.get('IsTruncated'):
                    break
                token = page['NextContinuationToken']
                if token in seen_tokens:
                    raise ValueError('Repeated history page')
                seen_tokens.add(token)
        except Exception:
            raise StorageError('Could not load Allure history') from None
        return [manifest for _, manifest in sorted(rows, key=lambda row: (row[0], row[1]['run_id']))]

    def latest(self, repo):
        rows = self.history(repo)
        return rows[-1] if rows else None

    def accounting_history(self, repo, *, budget=None):
        """Reconcile every finalized manifest, including late uploads, without media."""
        from api_agent.telemetry.history import MAX_BYTES
        for manifest in self.history(repo, budget=budget, require_accounting=True,
                                     max_pages=None if budget is not None else 100):
            if budget is not None and budget.remaining == 0:
                raise TimeoutError()
            if 'accounting.json' not in manifest['files']:
                raise StorageError('Could not load Allure history')
            try:
                body = self._client().get_object(Bucket=self.bucket, Key=self._key(repo, manifest['run_id'], 'accounting.json'))['Body']
                try:
                    payload = body.read(MAX_BYTES + 1)
                finally:
                    body.close()
                if len(payload) > MAX_BYTES or hashlib.sha256(payload).hexdigest() != manifest['files']['accounting.json']:
                    raise StorageError('Could not load Allure history')
                yield manifest, payload
            except Exception:
                raise StorageError('Could not load Allure history') from None
