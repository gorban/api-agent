import io
import json
import hashlib
from uuid import uuid4
import sys
from types import SimpleNamespace
import os

import pytest

from api_agent.testing.storage import RunStorage, StorageError


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.fail = False

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
        if self.fail or Key in self.objects:
            raise RuntimeError('secret provider error')
        assert IfNoneMatch == '*'
        self.objects[Key] = Body

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
        return {'Contents': [{'Key': key} for key in self.objects if key.startswith(Prefix)]}

    def get_object(self, *, Bucket, Key):
        return {'Body': io.BytesIO(self.objects[Key])}


def staged(tmp_path, run_id, finished_at, status='completed'):
    directory = tmp_path / run_id
    directory.mkdir()
    (directory / 'a-result.json').write_text('{}')
    manifest = {'schema': 1, 'repo': 'example', 'run_id': run_id, 'mode': 'integration',
                'started_at': '2026-01-01T00:00:00Z', 'finished_at': finished_at,
                'status': status, 'files': {'a-result.json': hashlib.sha256(b'{}').hexdigest()}}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    return directory, manifest


def test_persistence_history_and_secondary_copy(tmp_path):
    s3 = FakeS3()
    storage = RunStorage('bucket', client=s3, secondary_directory=tmp_path / 'copy')
    newer = staged(tmp_path, str(uuid4()), '2026-01-02T00:00:00Z', 'interrupted')
    older = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z', 'failed')
    storage.persist(*newer)
    storage.persist(*older)
    assert [row['run_id'] for row in storage.history('example')] == [older[1]['run_id'], newer[1]['run_id']]
    assert storage.latest('example')['run_id'] == newer[1]['run_id']
    assert storage.latest('other') is None
    assert (tmp_path / 'copy/example' / newer[1]['run_id'] / 'manifest.json').exists()
    assert storage.persist(*newer).endswith('/manifest.json')


def test_failure_does_not_commit_manifest(tmp_path):
    s3 = FakeS3()
    s3.fail = True
    storage = RunStorage('bucket', client=s3)
    source = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    with pytest.raises(StorageError) as caught:
        storage.persist(*source)
    assert 'secret provider error' not in str(caught.value)
    assert storage.latest('example') is None


def test_rejects_unsafe_or_changed_artifacts(tmp_path):
    s3 = FakeS3()
    with pytest.raises(ValueError):
        RunStorage('', client=s3)
    with pytest.raises(ValueError):
        RunStorage('bucket', client=s3, prefix='../bad')
    storage = RunStorage('bucket', client=s3)
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    with pytest.raises(ValueError):
        storage.persist(source, {**manifest, 'repo': '../bad'})
    with pytest.raises(ValueError):
        storage.persist(source, {**manifest, 'files': {'../escape': 'digest'}})
    (source / 'a-result.json').write_text('changed')
    with pytest.raises(StorageError):
        storage.persist(source, manifest)
    (source / 'a-result.json').write_text('{}')
    (source / 'manifest.json').write_text('{}')
    with pytest.raises(StorageError):
        storage.persist(source, manifest)
    assert storage.latest('example') is None


def test_rejects_symlinked_artifact(tmp_path):
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    artifact = source / 'a-result.json'
    artifact.unlink()
    artifact.symlink_to(tmp_path / 'outside')
    with pytest.raises(ValueError):
        RunStorage('bucket', client=FakeS3()).persist(source, manifest)


def test_history_ignores_invalid_entries_and_bounds_failures(tmp_path):
    s3 = FakeS3()
    storage = RunStorage('bucket', client=s3)
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    storage.persist(source, manifest)
    key = storage._key('example', str(uuid4()), 'manifest.json')
    s3.objects[key] = json.dumps({**manifest, 'run_id': 'bad'}).encode()
    assert storage.latest('example')['run_id'] == manifest['run_id']
    s3.objects[key] = json.dumps({**manifest, 'run_id': str(uuid4())}).encode()
    assert len(storage.history('example')) == 1
    s3.objects[key] = b'x' * 1_000_001
    assert storage.latest('example')['run_id'] == manifest['run_id']


def test_paginated_history_and_repeated_token(tmp_path):
    class PagedS3(FakeS3):
        def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
            if 'ContinuationToken' not in kwargs:
                return {'IsTruncated': True, 'NextContinuationToken': 'next'}
            return {'IsTruncated': True, 'NextContinuationToken': 'next'}
    with pytest.raises(StorageError):
        RunStorage('bucket', client=PagedS3()).history('example')


def test_lazy_boto3_adapter_and_page_bound(monkeypatch):
    fake = FakeS3()
    calls = []
    def client(service, **kwargs):
        calls.append((service, kwargs))
        return fake
    class Config:
        def __init__(self, **kwargs):
            self.values = kwargs
    monkeypatch.setitem(sys.modules, 'boto3', SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, 'botocore', SimpleNamespace(config=SimpleNamespace(Config=Config)))
    monkeypatch.setitem(sys.modules, 'botocore.config', SimpleNamespace(Config=Config))
    storage = RunStorage('bucket', endpoint_url='http://localhost:4566', region_name='us-east-1')
    assert storage.latest('example') is None
    assert calls[0][0] == 's3'
    assert calls[0][1]['endpoint_url'] == 'http://localhost:4566'
    assert calls[0][1]['region_name'] == 'us-east-1'
    assert calls[0][1]['config'].values == {'connect_timeout': 5, 'read_timeout': 15,
                                           'retries': {'mode': 'standard', 'total_max_attempts': 3}}

    class EndlessS3(FakeS3):
        def __init__(self):
            super().__init__()
            self.number = 0
        def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
            self.number += 1
            return {'IsTruncated': True, 'NextContinuationToken': str(self.number)}
    with pytest.raises(StorageError):
        RunStorage('bucket', client=EndlessS3()).history('example')


def test_retry_partial_upload_and_reject_mismatched_existing(tmp_path):
    class FailManifestOnce(FakeS3):
        def __init__(self):
            super().__init__()
            self.fail_manifest = True
        def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
            if Key.endswith('/manifest.json') and self.fail_manifest:
                self.fail_manifest = False
                raise RuntimeError('provider secret')
            super().put_object(Bucket=Bucket, Key=Key, Body=Body, IfNoneMatch=IfNoneMatch)
    s3 = FailManifestOnce()
    storage = RunStorage('bucket', client=s3, secondary_directory=tmp_path / 'copies')
    source = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    with pytest.raises(StorageError):
        storage.persist(*source)
    assert storage.latest('example') is None
    assert not (tmp_path / 'copies').exists()
    storage.persist(*source)
    assert storage.latest('example')['run_id'] == source[1]['run_id']
    key = storage._key('example', source[1]['run_id'], 'a-result.json')
    s3.objects[key] = b'wrong'
    with pytest.raises(StorageError):
        storage.persist(*source)


def test_secondary_manifest_only_after_primary_and_retry(tmp_path):
    s3 = FakeS3()
    storage = RunStorage('bucket', client=s3, secondary_directory=tmp_path / 'copies')
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    target = tmp_path / 'copies/example' / manifest['run_id']
    target.mkdir(parents=True)
    (target / 'a-result.json').write_text('wrong')
    with pytest.raises(StorageError):
        storage.persist(source, manifest)
    assert not (target / 'manifest.json').exists()
    (target / 'a-result.json').write_text('{}')
    storage.persist(source, manifest)
    assert (target / 'manifest.json').exists()


def test_history_parses_instant_and_validates_repo_and_files(tmp_path):
    s3 = FakeS3()
    storage = RunStorage('bucket', client=s3)
    first = staged(tmp_path, str(uuid4()), '2026-01-01T01:00:00Z')
    second = staged(tmp_path, str(uuid4()), '2026-01-01T10:00:00+10:00')
    storage.persist(*first)
    storage.persist(*second)
    assert storage.latest('example')['run_id'] == first[1]['run_id']
    with pytest.raises(ValueError):
        storage.history('..')
    with pytest.raises(ValueError):
        storage.persist(first[0], {**first[1], 'repo': '..'})
    key = storage._key('example', str(uuid4()), 'manifest.json')
    s3.objects[key] = json.dumps({**first[1], 'run_id': key.split('/')[-2], 'files': {'../escape': 'x'}}).encode()
    assert len(storage.history('example')) == 2


@pytest.mark.parametrize('which', ['repo', 'run'])
def test_secondary_symlink_descendant_cannot_escape(tmp_path, which):
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    outside = tmp_path / 'outside'
    outside.mkdir()
    base = tmp_path / 'copies'
    base.mkdir()
    if which == 'repo':
        (base / 'example').symlink_to(outside, target_is_directory=True)
    else:
        repo = base / 'example'
        repo.mkdir()
        (repo / manifest['run_id']).symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError):
        RunStorage('bucket', client=FakeS3(), secondary_directory=base).persist(source, manifest)
    assert list(outside.iterdir()) == []


def test_source_mutated_after_primary_commit_never_marks_secondary_complete(tmp_path):
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    class MutatingS3(FakeS3):
        def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
            super().put_object(Bucket=Bucket, Key=Key, Body=Body, IfNoneMatch=IfNoneMatch)
            if Key.endswith('/manifest.json'):
                (source / 'a-result.json').write_text('mutated')
    storage = RunStorage('bucket', client=MutatingS3(), secondary_directory=tmp_path / 'copies')
    with pytest.raises(StorageError):
        storage.persist(source, manifest)
    assert storage.latest('example')['run_id'] == manifest['run_id']
    assert not (tmp_path / 'copies/example' / manifest['run_id'] / 'manifest.json').exists()


def test_secondary_repo_swap_during_mkdir_never_writes_outside(tmp_path, monkeypatch):
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    outside = tmp_path / 'outside'
    outside.mkdir()
    base = tmp_path / 'copies'
    base.mkdir()
    original_mkdir = os.mkdir
    def swapping_mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == 'example' and kwargs.get('dir_fd') is not None:
            (base / 'example').rename(base / 'moved')
            (base / 'example').symlink_to(outside, target_is_directory=True)
        return result
    monkeypatch.setattr(os, 'mkdir', swapping_mkdir)
    with pytest.raises(StorageError):
        RunStorage('bucket', client=FakeS3(), secondary_directory=base).persist(source, manifest)
    assert list(outside.iterdir()) == []


def test_secondary_existing_fifo_is_rejected_without_blocking(tmp_path):
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    base = tmp_path / 'copies'
    target = base / 'example' / manifest['run_id']
    target.mkdir(parents=True)
    os.mkfifo(target / 'a-result.json')
    storage = RunStorage('bucket', client=FakeS3(), secondary_directory=base)
    with pytest.raises(StorageError):
        storage.persist(source, manifest)
    assert not (target / 'manifest.json').exists()


@pytest.mark.parametrize('name', ['manifest.json', 'a-result.json'])
@pytest.mark.parametrize('kind', ['fifo', 'directory'])
def test_nonregular_sources_fail_promptly_before_commit(tmp_path, name, kind):
    import subprocess
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    path = source / name
    path.unlink()
    if kind == 'fifo':
        os.mkfifo(path)
    else:
        path.mkdir()
    script = '''
import json, sys
from api_agent.testing.storage import RunStorage, StorageError
class Client:
    def put_object(self, **kwargs):
        pass
try:
    RunStorage._read_source(__import__('pathlib').Path(sys.argv[1]) / sys.argv[3])
except StorageError:
    pass
else:
    raise AssertionError('Nonregular source read was accepted')
try:
    RunStorage('bucket', client=Client()).persist(sys.argv[1], json.loads(sys.argv[2]))
except StorageError as error:
    assert str(error) == 'Could not persist Allure run'
else:
    raise AssertionError('Nonregular source was accepted')
'''
    result = subprocess.run([sys.executable, '-c', script, str(source), json.dumps(manifest), name],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_source_fifo_swapped_after_primary_commit_fails_promptly(tmp_path):
    import subprocess
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    secondary = tmp_path / 'copies'
    script = '''
import json, os, sys
from pathlib import Path
from api_agent.testing.storage import RunStorage, StorageError
source = Path(sys.argv[1])
class Client:
    def put_object(self, **kwargs):
        if kwargs['Key'].endswith('/manifest.json'):
            path = source / 'a-result.json'
            path.unlink()
            os.mkfifo(path)
try:
    RunStorage('bucket', client=Client(), secondary_directory=sys.argv[3]).persist(
        source, json.loads(sys.argv[2]))
except StorageError as error:
    assert str(error) == 'Could not persist Allure run'
else:
    raise AssertionError('Nonregular secondary source was accepted')
'''
    result = subprocess.run([sys.executable, '-c', script, str(source), json.dumps(manifest), str(secondary)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not (secondary / 'example' / manifest['run_id'] / 'manifest.json').exists()


def test_source_symlink_swapped_after_validation_is_rejected(tmp_path, monkeypatch):
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    outside = tmp_path / 'outside'
    outside.write_bytes(b'{}')
    original = RunStorage._read_source
    def swap(path, **kwargs):
        if path == 'a-result.json':
            artifact = source / path
            artifact.unlink()
            artifact.symlink_to(outside)
        return original(path, **kwargs)
    monkeypatch.setattr(RunStorage, '_read_source', staticmethod(swap))
    s3 = FakeS3()
    with pytest.raises(StorageError):
        RunStorage('bucket', client=s3).persist(source, manifest)
    assert s3.objects == {}


def test_source_directory_is_rejected_before_read(tmp_path):
    with pytest.raises(StorageError, match='Could not persist Allure run'):
        RunStorage._read_source(tmp_path)


def test_source_root_swapped_before_open_is_rejected(tmp_path, monkeypatch):
    import shutil
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    outside = tmp_path / 'outside'
    shutil.copytree(source, outside)
    original_open = os.open
    def swap(path, flags, *args, **kwargs):
        if path == source:
            source.rename(tmp_path / 'original')
            source.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', swap)
    s3 = FakeS3()
    with pytest.raises(StorageError):
        RunStorage('bucket', client=s3).persist(source, manifest)
    assert s3.objects == {}


def test_pinned_source_root_survives_swap_through_secondary_copy(tmp_path, monkeypatch):
    import shutil
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    outside = tmp_path / 'outside'
    shutil.copytree(source, outside)
    (outside / 'a-result.json').write_bytes(b'foreign staging bytes')
    storage = RunStorage('bucket', client=FakeS3(), secondary_directory=tmp_path / 'copies')
    original_client = storage._client
    swapped = False
    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(tmp_path / 'original')
            source.symlink_to(outside, target_is_directory=True)
        return original_client()
    monkeypatch.setattr(storage, '_client', swap)
    storage.persist(source, manifest)
    key = storage._key('example', manifest['run_id'], 'a-result.json')
    assert storage.client.objects[key] == b'{}'
    target = tmp_path / 'copies/example' / manifest['run_id']
    assert (target / 'a-result.json').read_bytes() == b'{}'
    assert json.loads((target / 'manifest.json').read_bytes()) == manifest


@pytest.mark.parametrize('failure', ['none', 'manifest', 'artifact', 'provider'])
def test_source_descriptors_are_closed_on_success_and_failure(tmp_path, monkeypatch, failure):
    source, manifest = staged(tmp_path, str(uuid4()), '2026-01-01T00:00:00Z')
    if failure == 'manifest':
        (source / 'manifest.json').write_bytes(b'{}')
    elif failure == 'artifact':
        (source / 'a-result.json').unlink()
        (source / 'a-result.json').mkdir()
    s3 = FakeS3()
    s3.fail = failure == 'provider'
    storage = RunStorage('bucket', client=s3)
    opened = set()
    original_open, original_close = os.open, os.close
    def track_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor
    def track_close(descriptor):
        original_close(descriptor)
        opened.remove(descriptor)
    monkeypatch.setattr(os, 'open', track_open)
    monkeypatch.setattr(os, 'close', track_close)
    if failure == 'none':
        storage.persist(source, manifest)
    else:
        with pytest.raises(StorageError):
            storage.persist(source, manifest)
    assert not opened
