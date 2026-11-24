"""The shared LocalStack's entrypoint keeps rejected conditional writes from emptying objects.

The stand-in store mirrors localstack-persist 4.14.0: opening an object for writing
truncates its file in place, and LocalStack opens before checking preconditions.
"""
import json
import os
from pathlib import Path
import sys
from types import ModuleType

import pytest

COMPOSE = Path(__file__).resolve().parents[2] / 'infra/compose/compose.json'
START_HOOK = '/etc/localstack/init/start.d/20-api-agent-staged-writes.py'


class PreconditionFailed(Exception):
    pass


def localstack_entrypoint():
    return json.loads(COMPOSE.read_text())['services']['localstack']['entrypoint']


def persisted_storage_module():
    class PersistedS3ObjectStore:
        def __init__(self):
            self._open_files = set()

        def open(self, path, mode='r'):
            return PersistedS3StoredObject(self, self.open_file(path, mode))

        def open_file(self, path, mode):
            file = open(path, mode + 'b')
            self._open_files.add(file)
            return file

        def close_file(self, file):
            file.close()
            self._open_files.discard(file)

    class PersistedS3StoredObject:
        def __init__(self, store, file):
            self._store, self._file, self.closed = store, file, False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self._store.close_file(self._file)
            self.closed = True

        def write(self, data):
            self._file.truncate()
            self._file.write(data)

    storage = ModuleType('localstack_persist.s3.storage')
    storage.PersistedS3ObjectStore = PersistedS3ObjectStore
    storage.PersistedS3StoredObject = PersistedS3StoredObject
    return storage


@pytest.fixture
def staged_store(monkeypatch):
    storage = persisted_storage_module()
    package, s3 = ModuleType('localstack_persist'), ModuleType('localstack_persist.s3')
    package.s3, s3.storage = s3, storage
    for name, module in (('localstack_persist', package), ('localstack_persist.s3', s3),
                         ('localstack_persist.s3.storage', storage)):
        monkeypatch.setitem(sys.modules, name, module)
    exec(localstack_entrypoint()[3], {})
    return storage.PersistedS3ObjectStore()


def conditional_put(store, path, body, *, precondition_holds):
    with store.open(path, 'w') as stored:
        if not precondition_holds:
            raise PreconditionFailed()
        stored.write(body)


def test_a_rejected_conditional_write_keeps_the_stored_object(staged_store, tmp_path):
    path = tmp_path / 'meta.json@null'
    conditional_put(staged_store, path, b'original', precondition_holds=True)

    with pytest.raises(PreconditionFailed):
        conditional_put(staged_store, path, b'rejected', precondition_holds=False)

    assert path.read_bytes() == b'original'
    assert os.listdir(tmp_path) == [path.name]


def test_an_accepted_write_replaces_the_object_only_when_it_completes(staged_store, tmp_path):
    path = tmp_path / 'meta.json@null'
    conditional_put(staged_store, path, b'original', precondition_holds=True)

    with staged_store.open(path, 'w') as stored:
        stored.write(b'updated')
        assert path.read_bytes() == b'original'

    assert path.read_bytes() == b'updated'
    assert os.listdir(tmp_path) == [path.name]
    assert not staged_store._open_files


def test_an_object_at_the_longest_persisted_file_name_can_be_written(staged_store, tmp_path):
    path = tmp_path / ('k' * 235 + '@null')

    conditional_put(staged_store, path, b'payload', precondition_holds=True)

    assert path.read_bytes() == b'payload'
    assert os.listdir(tmp_path) == [path.name]


def test_closing_twice_commits_once_and_reads_are_not_staged(staged_store, tmp_path):
    path = tmp_path / 'object@null'
    stored = staged_store.open(path, 'w')
    stored.write(b'payload')
    stored.close()
    stored.close()

    with staged_store.open(path, 'r') as reader:
        assert reader._file.read() == b'payload'
    assert os.listdir(tmp_path) == [path.name]


def test_an_already_closed_object_ignores_a_later_failure(staged_store, tmp_path):
    path = tmp_path / 'object@null'
    stored = staged_store.open(path, 'w')
    stored.write(b'payload')
    stored.close()

    stored.__exit__(PreconditionFailed, PreconditionFailed(), None)

    assert path.read_bytes() == b'payload'


def test_the_entrypoint_installs_the_start_hook_then_runs_localstack(monkeypatch, tmp_path):
    executable, flag, installer, patch = localstack_entrypoint()
    hook = tmp_path / Path(START_HOOK).relative_to('/')
    executed = []
    monkeypatch.setattr(os, 'execvp', lambda program, arguments: executed.append((program, arguments)))
    monkeypatch.setattr(sys, 'argv', ['-c', patch])

    exec(installer.replace(START_HOOK, str(hook)), {})

    assert (executable, flag) == ('python3', '-c')
    assert START_HOOK in installer
    assert hook.read_text() == patch
    assert executed == [('docker-entrypoint.sh', ['docker-entrypoint.sh'])]
