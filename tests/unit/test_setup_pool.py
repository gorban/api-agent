"""Profile backend ownership and equal-configuration coalescing."""
from types import SimpleNamespace

import pytest
from api_agent.testing.setup_pool import SetupPool


def test_pool_coalesces_identical_effective_config_and_retains_distinct_owners(monkeypatch):
    from api_agent.testing import setup_backend
    made = []
    class Backend:
        def __init__(self, config, root, **services):
            self.config = config
            self.closed = False
            made.append(self)
        def start(self):
            return {'selected': self.config['api']}
        def close(self):
            self.closed = True
    monkeypatch.setattr(setup_backend, 'SetupBackend', Backend)
    profiles = {name: {'config': {'api': api}, 'provenance': {}}
                for name, api in [('one','a'), ('two','a'), ('three','b')]}
    pool = SetupPool(profiles, '/')
    rows = pool.start()
    assert len(made) == 2
    assert rows['one']['setup_backend'] == rows['two']['setup_backend'] != rows['three']['setup_backend']
    rows['one']['config']['api'] = 'changed'
    assert pool.profiles['one']['config']['api'] == profiles['one']['config']['api'] == 'a'
    pool.close()
    assert all(owner.closed for owner in made)


def test_pool_attempts_every_cleanup_and_preserves_first_failure(monkeypatch):
    pool = SetupPool({}, '/')
    failure = RuntimeError('pending')
    seen = []
    def fail():
        seen.append('first')
        raise failure
    pool.owners = [SimpleNamespace(close=fail), SimpleNamespace(close=lambda: seen.append('second'))]
    with pytest.raises(RuntimeError) as caught:
        pool.close()
    assert caught.value is failure
    assert seen == ['first', 'second']


def test_pool_ignores_empty_optional_backend_descriptor(monkeypatch):
    from api_agent.testing import setup_backend
    owner = SimpleNamespace(start=lambda: None, close=lambda: None)
    monkeypatch.setattr(setup_backend, 'SetupBackend', lambda *args, **kwargs: owner)
    pool = SetupPool({'case': {'config': {}, 'provenance': {}}}, '/')
    assert 'setup_backend' not in pool.start()['case']
    pool.close()


def test_pool_preserves_all_inspection_endpoints_and_publication_lifecycle():
    calls = []
    callback = lambda kind: None
    def owner(label):
        return SimpleNamespace(prepare_publication=lambda: calls.append(('publish', label)),
            finish_run=lambda: (f'http://127.0.0.1/{label}',),
            watch_inspection=lambda observed: calls.append(('watch', label, observed)))
    pool = SetupPool({}, '/')
    pool.owners = [owner('first'), owner('second')]
    pool.prepare_publication()
    assert pool.finish_run() == ('http://127.0.0.1/first', 'http://127.0.0.1/second')
    pool.watch_inspection(callback)
    assert calls == [('publish', 'first'), ('publish', 'second'),
                     ('watch', 'first', callback), ('watch', 'second', callback)]


def test_pool_keeps_first_error_when_multiple_owners_need_cleanup():
    failures = [RuntimeError('first pending'), RuntimeError('second pending')]
    calls = []
    def fail(index):
        calls.append(index)
        raise failures[index]
    pool = SetupPool({}, '/')
    pool.owners = [SimpleNamespace(close=lambda: fail(0)), SimpleNamespace(close=lambda: fail(1))]
    with pytest.raises(RuntimeError) as caught:
        pool.close()
    assert caught.value is failures[0]
    assert calls == [0, 1]
