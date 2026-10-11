import io
from hashlib import sha256
import pytest

from api_agent.testing.storage import RunStorage
from api_agent.telemetry.history import MAX_BYTES


def test_accounting_only_verified_and_late_upload(monkeypatch):
    class Client:
        objects = {}
        requested = []
        def get_object(self, *, Bucket, Key):
            self.requested.append(Key)
            return {'Body': io.BytesIO(self.objects[Key])}
    client = Client()
    storage = RunStorage('bucket', client=client)
    good = {'run_id': 'one', 'files': {'accounting.json': sha256(b'{}').hexdigest(), 'video.mp4': 'digest'}}
    missing = {'run_id': 'two', 'files': {}}
    oversized = {'run_id': 'three', 'files': {'accounting.json': sha256(b' ' * (MAX_BYTES + 1)).hexdigest()}}
    corrupt = {'run_id': 'four', 'files': {'accounting.json': sha256(b'good').hexdigest()}}
    monkeypatch.setattr(storage, 'history', lambda repo, **kwargs: [good, missing, oversized, corrupt])
    assert list(storage.accounting_history('repo')) == []
    client.objects['allure/repo/one/accounting.json'] = b'{}'
    client.objects['allure/repo/three/accounting.json'] = b' ' * (MAX_BYTES + 1)
    client.objects['allure/repo/four/accounting.json'] = b'bad'
    assert list(storage.accounting_history('repo')) == [(good, b'{}')]
    assert list(storage.accounting_history('repo')) == [(good, b'{}')]
    assert all(key.endswith('/accounting.json') for key in client.requested)


def test_history_deadline_checks(monkeypatch):
    from types import SimpleNamespace
    from api_agent.testing.storage import StorageError
    class Client:
        def list_objects_v2(self, **kwargs):
            return {'Contents': [{'Key': 'allure/repo/run/manifest.json'}]}
    storage = RunStorage('bucket', client=Client())
    with pytest.raises(StorageError): storage.history('repo', budget=SimpleNamespace(remaining=0))
    class Budget:
        values = iter([1, 0])
        @property
        def remaining(self): return next(self.values)
    with pytest.raises(StorageError): storage.history('repo', budget=Budget())
    monkeypatch.setattr(storage, 'history', lambda *args, **kwargs: [{}])
    with pytest.raises(TimeoutError): list(storage.accounting_history('repo', budget=SimpleNamespace(remaining=0)))
