from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest

from api_agent.telemetry.history import HistoryCache, MAX_BYTES, cache_path
from api_agent.testing.cost_history import estimator_for
from api_agent.providers.rates import bundled_rate_card


def fixture():
    run_id = str(uuid4())
    artifact = dict(version=1, estimator_version=1, run_id=run_id, rate_card=None, quota=dict(before=None, after=None, pct_point_delta=None, quota_unavailable_reason='missing_snapshot', payment_policy=None, payment_month=None), tests=[])
    payload = json.dumps(artifact).encode()
    manifest = dict(schema=1, repo='repo', mode='integration', started_at='2026-10-09T00:00:00Z', run_id=run_id, status='completed', files={'accounting.json': sha256(payload).hexdigest()}, finished_at='2026-10-10T00:00:00Z')
    return manifest, payload


@pytest.mark.parametrize('version', [1, 2])
def test_estimator_versions_rebuild_without_rewriting_evidence(tmp_path, version):
    manifest, payload = fixture()
    artifact = json.loads(payload)
    artifact['estimator_version'] = version
    payload = json.dumps(artifact).encode()
    manifest['files']['accounting.json'] = sha256(payload).hexdigest()
    cache = HistoryCache(tmp_path / 'cost-history.sqlite3')
    cache.ingest('review', manifest, payload)
    cache.close()
    cache = HistoryCache(tmp_path / 'cost-history.sqlite3')
    assert cache.records('review') == [(manifest, artifact)]
    cache.close()


@pytest.mark.parametrize('version', [True, 1.0, '2', 0, 3])
def test_estimator_version_is_a_supported_integer(version):
    from api_agent.telemetry.accounting import validate_accounting
    from api_agent.providers.contracts import ContractError
    _, payload = fixture()
    artifact = json.loads(payload)
    artifact['estimator_version'] = version
    with pytest.raises(ContractError, match='Invalid suite accounting'):
        validate_accounting(artifact)


def test_new_accounting_identifies_arithmetic_estimator():
    from api_agent.telemetry.accounting import build_accounting
    manifest, _ = fixture()
    usage = SimpleNamespace(inference_calls={}, report=lambda: {})
    artifact = build_accounting(SimpleNamespace(run_id=manifest['run_id']), [], {}, usage, [])
    assert artifact['estimator_version'] == 2


def test_transaction_verified_idempotent_and_origin(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    assert cache_path() == tmp_path / 'api-agent' / 'cost-history.sqlite3'
    cache = HistoryCache()
    manifest, payload = fixture()
    cache.ingest('one', manifest, payload)
    cache.ingest('one', manifest, payload)
    assert len(cache.records('one')) == 1
    assert cache.records('two') == []
    cache.ingest('two', manifest, payload)
    changed = dict(manifest, finished_at='2026-10-10T01:00:00Z')
    with pytest.raises(ValueError, match='Conflicting'): cache.ingest('one', changed, payload)
    assert len(cache.records('one')) == 1
    cache.close()


@pytest.mark.parametrize('change', ['origin', 'bytes', 'oversized', 'uuid', 'status', 'schema', 'digest', 'run'])
def test_reject_history(tmp_path, change):
    cache = HistoryCache(tmp_path / 'cache')
    manifest, payload = fixture()
    origin = 'one'
    if change == 'origin': origin = None
    elif change == 'bytes': payload = 'text'
    elif change == 'oversized': payload = b' ' * (MAX_BYTES + 1)
    elif change == 'uuid': manifest['run_id'] = 'bad'
    elif change == 'status': manifest['status'] = 'running'
    elif change == 'schema': manifest['schema'] = 2
    elif change == 'digest': payload += b' '
    else:
        artifact = json.loads(payload); artifact['run_id'] = str(uuid4()); payload = json.dumps(artifact).encode(); manifest['files']['accounting.json'] = sha256(payload).hexdigest()
    with pytest.raises(ValueError): cache.ingest(origin, manifest, payload)
    assert cache.records('one') == []
    cache.close()


def test_adapter_degrades_without_cache_or_auth(tmp_path, monkeypatch):
    context = SimpleNamespace(owner_root=tmp_path, base_root=tmp_path)
    test = SimpleNamespace(id='t', root=None, node_id='test.py::t')
    class Bad:
        def records(self, origin): raise OSError('private response')
    snapshot = estimator_for(context, [test], {}, cache=Bad(), rate_card=bundled_rate_card())
    assert snapshot.predictions['t']['estimated_cost_usd'] is None
    assert snapshot.synchronize() is snapshot


def test_adapter_sync_reconciles_late_upload_and_ingest(tmp_path):
    from api_agent.testing.cost_history import HistoricalEstimator
    cache = HistoryCache(tmp_path / 'cache')
    manifest, payload = fixture()
    class Storage:
        available = False
        def accounting_history(self, repo, **kwargs):
            return [(manifest, payload)] if self.available else []
    storage = Storage()
    snap = HistoricalEstimator([], {}, cache, 'origin', 'repo', bundled_rate_card(), storage)
    assert snap.synchronize() is not snap
    assert cache.records('origin') == []
    storage.available = True
    changed = snap.synchronize()
    assert len(cache.records('origin')) == 1
    assert changed.records
    changed.synchronize()
    assert len(cache.records('origin')) == 1
    (tmp_path / 'accounting.json').write_bytes(payload)
    changed.ingest(tmp_path, manifest)
    changed.ingest(tmp_path / 'absent', manifest)
    class BadStorage:
        def accounting_history(self, repo, **kwargs): raise OSError('private')
    changed.storage = BadStorage()
    assert changed.synchronize().sync_unavailable
    changed.cache = None
    assert changed.synchronize() is changed
    changed.ingest(tmp_path, manifest)
    cache.close()


def test_adapter_card_cache_and_initialization(tmp_path, monkeypatch):
    context = SimpleNamespace(owner_root=tmp_path, base_root=tmp_path)
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    snapshot = estimator_for(context, [], {'ALLURE_S3_BUCKET': 'bucket'})
    assert snapshot.synchronize is not None
    assert snapshot.storage.bucket == 'bucket'
    snapshot.cache.close()


def test_adapter_policy_qualified(tmp_path, monkeypatch):
    from api_agent.testing.cost_history import HistoricalEstimator
    from api_agent.telemetry.accounting import compatibility
    from test_historical_estimation import records, compat
    now = __import__('datetime').datetime(2026, 10, 10, tzinfo=__import__('datetime').timezone.utc)
    history = records()
    settings = dict(compat(), TEST_COST_HISTORY_DAYS=30, _COST_QUOTA_WINDOW_ID='window')
    actual = compatibility(settings)
    for _, artifact in history:
        artifact['tests'][0]['compatibility'] = actual
        artifact['tests'][0]['calls'][0]['quote']['accounting']['compatibility'] = actual
    class Cache:
        def records(self, origin): return history
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda test, **kwargs: 'hash')
    test = SimpleNamespace(id='t', node_id='test.py::test[one]', root=None)
    snapshot = HistoricalEstimator([test], settings, Cache(), 'origin', 'repo', bundled_rate_card(), now=now)
    assert snapshot.predictions['t']['basis'] == 'historical_quota'
    no_account = dict(settings); no_account.pop('_COST_QUOTA_WINDOW_ID')
    unknown_account = HistoricalEstimator([test], no_account, Cache(), 'origin', 'repo', bundled_rate_card(), now=now)
    assert unknown_account.predictions['t']['basis'] == 'historical_list_price'
    assert unknown_account.config.get('_COST_QUOTA_WINDOW_ID') is None
    snapshot = HistoricalEstimator([test], dict(settings, TEST_COST_HISTORY_DAYS=1), Cache(), 'origin', 'repo', bundled_rate_card(), now=now)
    assert snapshot.predictions['t']['basis'] == 'unavailable'
    history[0][1]['quota']['payment_policy'] = dict(history[0][1]['quota']['payment_policy'], usd=200)
    snapshot = HistoricalEstimator([test], settings, Cache(), 'origin', 'repo', bundled_rate_card(), now=now)
    assert snapshot.predictions['t']['basis'] == 'historical_list_price'


def test_oversized_manifest_and_user_default_cache(tmp_path, monkeypatch):
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    monkeypatch.setattr('api_agent.telemetry.history.Path.home', lambda: tmp_path)
    assert cache_path() == tmp_path / '.cache/api-agent/cost-history.sqlite3'
    cache = HistoryCache(tmp_path / 'cache')
    manifest, payload = fixture()
    manifest['padding'] = ' ' * MAX_BYTES
    with pytest.raises(ValueError, match='Oversized manifest'):
        cache.ingest('origin', manifest, payload)
    cache.close()


def test_adapter_unqualified_and_unknown_prediction(tmp_path, monkeypatch):
    from api_agent.testing.cost_history import HistoricalEstimator
    from api_agent.telemetry.accounting import compatibility
    from test_historical_estimation import records, compat
    from datetime import datetime, timezone
    history = records()
    settings = dict(compat(), TEST_COST_HISTORY_DAYS=30, _COST_QUOTA_WINDOW_ID='window'); actual = compatibility(settings)
    for _, artifact in history:
        artifact['tests'][0]['compatibility'] = actual
        artifact['tests'][0]['calls'][0]['quote']['accounting']['compatibility'] = actual
    class Cache:
        def records(self, origin): return history
        def ingest(self, *args): pytest.fail('Oversized data ingested')
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda test, **kwargs: 'hash')
    tests = [SimpleNamespace(id='t', node_id='test.py::test[one]'), SimpleNamespace(id='other', node_id='test.py::other')]
    snapshot = HistoricalEstimator(tests, settings, Cache(), 'origin', 'repo', bundled_rate_card(), now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert snapshot.predictions['other']['estimated_cost_usd'] is None
    history.pop()
    snapshot = HistoricalEstimator(tests, settings, Cache(), 'origin', 'repo', bundled_rate_card(), now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert snapshot.predictions['t']['basis'] == 'historical_list_price'
    (tmp_path / 'accounting.json').write_bytes(b' ' * (MAX_BYTES + 1))
    snapshot.ingest(tmp_path, {})


def test_partial_sync_deadline_cache_error(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from api_agent.testing.cost_history import HistoricalEstimator
    cache = HistoryCache(tmp_path / 'cache')
    manifest, payload = fixture()
    @contextmanager
    def deadline(*args):
        yield SimpleNamespace(remaining=0)
    monkeypatch.setattr('api_agent.testing.cost_history.report_timeout_sync', deadline)
    class Storage:
        def accounting_history(self, *args, **kwargs): return [(manifest, payload)]
    snapshot = HistoricalEstimator([], {}, cache, 'origin', 'repo', bundled_rate_card(), Storage())
    replacement = snapshot.synchronize()
    assert replacement.sync_unavailable
    assert replacement.cache.records('origin') == []
    monkeypatch.setattr(cache, 'records', lambda *args: (_ for _ in ()).throw(OSError()))
    assert snapshot.synchronize() is snapshot
    assert snapshot.sync_unavailable
    cache.close()


@pytest.mark.parametrize('platform,expected', [('darwin', 'Library/Caches'), ('win32', 'AppData/Local')])
def test_platform_cache_directory(tmp_path, monkeypatch, platform, expected):
    monkeypatch.setattr('api_agent.telemetry.history.sys.platform', platform)
    monkeypatch.setattr('api_agent.telemetry.history.Path.home', lambda: tmp_path)
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    assert cache_path() == tmp_path / expected / 'api-agent/cost-history.sqlite3'
    if platform == 'win32':
        monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
        assert cache_path() == tmp_path / 'local/api-agent/cost-history.sqlite3'


def test_concurrent_handles_cannot_accept_conflicting_manifests(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    first, second = HistoryCache(tmp_path / 'cache'), HistoryCache(tmp_path / 'cache')
    manifest, payload = fixture()
    other = dict(manifest, finished_at='2026-10-10T01:00:00Z')
    barrier = Barrier(2)
    def ingest(cache, value):
        barrier.wait()
        try:
            cache.ingest('origin', value, payload)
            return True
        except (ValueError, __import__('sqlite3').OperationalError):
            return False
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda args: ingest(*args), [(first, manifest), (second, other)]))
    assert sum(results) == 1
    assert len(first.records('origin')) == 1
    first.close(); second.close()


@pytest.mark.parametrize('field,value', [('repo', '../repo'), ('mode', 'unit'), ('finished_at', 'bad'), ('finished_at', '2026-01-01T00:00:00Z'), ('started_at', '2026-10-09T00:00:00'), ('files', {'../secret': 'a' * 64}), ('schema', True)])
def test_manifest_semantics_independently(tmp_path, field, value):
    cache = HistoryCache(tmp_path / 'cache')
    manifest, payload = fixture()
    manifest[field] = value
    with pytest.raises(ValueError): cache.ingest('origin', manifest, payload)
    assert cache.records('origin') == []
    cache.close()


def test_corrupt_cached_rows_are_isolated(tmp_path):
    cache = HistoryCache(tmp_path / 'cache')
    manifest, payload = fixture()
    cache.ingest('origin', manifest, payload)
    original = cache.connection.execute('SELECT * FROM history').fetchone()
    for accounting in ('invalid', json.dumps(dict(json.loads(payload), run_id=str(uuid4())))):
        data = dict(manifest, files={'accounting.json': sha256(accounting.encode()).hexdigest()})
        encoded = json.dumps(data)
        cache.connection.execute('UPDATE history SET digest=?,manifest=?,accounting=?', (sha256(encoded.encode()).hexdigest(), encoded, accounting))
        assert cache.records('origin') == []
    cache.connection.execute('UPDATE history SET digest=?,manifest=?,accounting=?', (original[2], original[3], payload.decode() + ' '))
    assert cache.records('origin') == []
    cache.connection.execute('UPDATE history SET digest=?,manifest=?,accounting=?', (original[2], original[3], original[4]))
    assert len(cache.records('origin')) == 1
    cache.close()


def test_cached_manifest_hash_is_immutable(tmp_path):
    cache = HistoryCache(tmp_path / 'cache')
    manifest, payload = fixture()
    cache.ingest('origin', manifest, payload)
    cache.connection.execute('UPDATE history SET manifest=?', (json.dumps(dict(manifest, finished_at='2026-10-10T01:00:00Z')),))
    assert cache.records('origin') == []
    cache.close()


def test_cached_primary_identity_and_deep_json_are_isolated(tmp_path):
    cache = HistoryCache(tmp_path / 'cache')
    manifest, payload = fixture()
    cache.ingest('origin', manifest, payload)
    cache.connection.execute('UPDATE history SET run_id=?', (str(uuid4()),))
    assert cache.records('origin') == []
    # A correctly hashed nested object must not poison unrelated valid rows.
    cache.connection.execute('DELETE FROM history')
    cache.connection.commit()
    cache.ingest('origin', manifest, payload)
    nested = '[' * 20000 + '0' + ']' * 20000
    other = dict(manifest, run_id=str(uuid4()), files={'accounting.json': sha256(nested.encode()).hexdigest()})
    encoded = json.dumps(other, sort_keys=True, separators=(',', ':'))
    cache.connection.execute('INSERT INTO history VALUES (?,?,?,?,?)', ('origin', other['run_id'], sha256(encoded.encode()).hexdigest(), encoded, nested))
    assert len(cache.records('origin')) == 1
    cache.close()


def test_api_key_history_never_scales_with_same_compatibility(tmp_path, monkeypatch):
    from api_agent.testing.cost_history import HistoricalEstimator
    from api_agent.telemetry.accounting import compatibility
    from test_historical_estimation import records, compat, row
    from datetime import datetime, timezone
    history = records()
    settings = dict(compat(), TEST_COST_HISTORY_DAYS=30, _COST_QUOTA_WINDOW_ID='window')
    actual = compatibility(settings)
    for _, artifact in history:
        artifact['tests'][0]['compatibility'] = actual
        artifact['tests'][0]['calls'][0]['quote']['accounting']['compatibility'] = actual
    api = row()
    api['node_id'] = 'test.py::api'
    api['compatibility'] = actual
    api['calls'][0]['quote']['accounting'].update(compatibility=actual, billing_mode='api_key')
    history.append((dict(history[-1][0], finished_at='2026-10-04T01:00:00Z'), dict(history[-1][1], tests=[api])))
    class Cache:
        def records(self, origin): return history
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda test, **kwargs: 'hash')
    snapshot = HistoricalEstimator([SimpleNamespace(id='t', node_id=api['node_id'])], settings, Cache(), 'origin', 'repo', bundled_rate_card(), now=datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert snapshot.predictions['t']['basis'] == 'historical_list_price'
    assert snapshot.predictions['t']['estimated_cost_usd'] == pytest.approx(.0001)


def binding_settings():
    from test_historical_estimation import compat
    return dict(compat(), _COST_PACK_IDENTITY='b' * 64, TEST_COST_HISTORY_DAYS=30,
                UNATTENDED_USER_INDEX=0, LLM_CREDENTIAL_ROLE='primary', UNATTENDED_AUTH={'password_manager': {'manager': 'example-manager', 'cli_path': '/tool'}, 'roles': {'primary': [{'vault': 'example-vault', 'user': 'example-user'}]}})


def binding_run(day=1, billing='subscription'):
    from test_historical_estimation import records
    from test_historical_pricing import _evidence
    from api_agent.telemetry.accounting import compatibility
    settings = binding_settings()
    manifest, _ = fixture()
    template_manifest, artifact = deepcopy(records()[day - 1])
    manifest.update(template_manifest)
    artifact.update(version=1, estimator_version=1, run_id=manifest['run_id'])
    artifact['quota']['payment_month'] = None
    test = artifact['tests'][0]
    test.update(source_fingerprint='c' * 64, compatibility=compatibility(settings), attempts=2, original_estimate=None,
                list_cost_usd=.0001, raw_quota_cost_usd=None, prediction=None, stabilized_estimate=None, observed_call_count=1)
    call = test['calls'][0]
    call['quote']['accounting'].update(version=1, requested_model='gpt-4.1-nano', catalog_model='gpt-4.1-nano', serving_revision=None,
                                      rate_card=bundled_rate_card(), billing_mode=billing, compatibility=compatibility(settings))
    call['quote'].update(list_cost_usd=.0001, payment=None)
    test['calls'] = [dict(_evidence(), **{})]
    test['calls'][0].update(call, test_id='t')
    for snapshot in (artifact['quota']['before'], artifact['quota']['after']): snapshot['pct_used'] = 1
    payload = json.dumps(artifact).encode()
    manifest['files']['accounting.json'] = sha256(payload).hexdigest()
    return settings, manifest, payload


def test_local_finalized_binding_enables_next_picker_never_current(tmp_path, monkeypatch):
    from api_agent.testing.cost_history import HistoricalEstimator, allocation_scope
    from datetime import datetime, timezone
    cache = HistoryCache(tmp_path / 'cache')
    settings, manifest, payload = binding_run()
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda test, **kwargs: 'c' * 64)
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    initial = HistoricalEstimator([test], settings, cache, 'origin', 'repo', bundled_rate_card(), now=now)
    for day in (1, 2, 3):
        _, manifest, payload = binding_run(day)
        (tmp_path / 'accounting.json').write_bytes(payload)
        initial.ingest(tmp_path, manifest)
    assert initial.predictions['t']['estimated_cost_usd'] is None
    assert initial.records == []
    assert cache.bound_window('origin', allocation_scope(settings), now) == 'window'
    next_picker = HistoricalEstimator([test], settings, cache, 'origin', 'repo', bundled_rate_card(), now=now)
    assert next_picker.predictions['t']['basis'] == 'historical_quota'
    raw = cache.connection.execute('SELECT * FROM allocation_bindings').fetchone()
    assert 'example-user' not in repr(raw)
    changed = deepcopy(settings); changed['UNATTENDED_AUTH']['roles']['primary'][0]['user'] = 'changed-entry'
    assert HistoricalEstimator([test], changed, cache, 'origin', 'repo', bundled_rate_card(), now=now).predictions['t']['basis'] == 'historical_list_price'
    assert cache.bound_window('origin', allocation_scope(settings), datetime(2026, 11, 1, tzinfo=timezone.utc)) is None
    cache.close()


def test_bindings_remote_api_key_corruption_and_absence(tmp_path):
    from api_agent.testing.cost_history import allocation_scope
    from datetime import datetime, timezone
    cache = HistoryCache(tmp_path / 'cache')
    settings, manifest, payload = binding_run(billing='api_key')
    scope = allocation_scope(settings)
    cache.ingest('origin', manifest, payload)  # Remote reconciliation cannot bind.
    assert cache.bound_window('origin', scope, datetime.now(timezone.utc)) is None
    cache.bind('origin', scope, manifest['run_id'])
    assert cache.bound_window('origin', scope, datetime.now(timezone.utc)) is None
    cache.bind('origin', None, manifest['run_id'])
    cache.bind('origin', scope, str(uuid4()))
    settings, manifest, payload = binding_run()
    cache.ingest('origin', manifest, payload)
    cache.bind('origin', scope, manifest['run_id'])
    cache.connection.execute('UPDATE history SET accounting=? WHERE run_id=?', ('corrupt', manifest['run_id']))
    assert cache.bound_window('origin', scope, datetime.now(timezone.utc)) is None
    cache.close()


@pytest.mark.parametrize('mutate', [lambda c: c.update(UNATTENDED_USER_INDEX=None), lambda c: c.update(UNATTENDED_USER_INDEX=3),
    lambda c: c.update(UNATTENDED_AUTH={}), lambda c: c['UNATTENDED_AUTH'].update(password_manager={}),
    lambda c: c['UNATTENDED_AUTH'].update(roles={'role': 'bad'}), lambda c: c['UNATTENDED_AUTH']['roles']['primary'][0].update(password='secret'),
    lambda c: c.update(_COST_PACK_IDENTITY=None), lambda c: c.update(_COST_REQUEST_SETTINGS=None),
    lambda c: c.update(_COST_REQUEST_SETTINGS={'secret': 'unhashed'})])
def test_allocation_scope_rejects_unvalidated_descriptors(mutate):
    from api_agent.testing.cost_history import allocation_scope
    settings = binding_settings(); mutate(settings)
    assert allocation_scope(settings) is None


def test_remote_sync_never_creates_local_account_binding(tmp_path):
    from api_agent.testing.cost_history import HistoricalEstimator, allocation_scope
    from datetime import datetime, timezone
    cache = HistoryCache(tmp_path / 'cache')
    settings, manifest, payload = binding_run()
    class Storage:
        def accounting_history(self, *args, **kwargs): return [(manifest, payload)]
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    snap = HistoricalEstimator([], settings, cache, 'origin', 'repo', bundled_rate_card(), Storage(), now)
    snap.synchronize()
    assert len(cache.records('origin')) == 1
    assert cache.bound_window('origin', allocation_scope(settings), now) is None
    cache.close()


def test_allocation_scope_matches_only_effective_llm_role():
    from api_agent.testing.cost_history import allocation_scope
    settings = binding_settings()
    settings['UNATTENDED_AUTH']['roles']['secondary'] = [{'vault': 'other-vault', 'user': 'other-user'}]
    primary = allocation_scope(settings)
    settings['LLM_CREDENTIAL_ROLE'] = 'secondary'
    assert allocation_scope(settings) != primary
    settings['LLM_CREDENTIAL_ROLE'] = 'missing'
    assert allocation_scope(settings) is None
    settings.pop('LLM_CREDENTIAL_ROLE')
    assert allocation_scope(settings) is None
    settings['LLM_CREDENTIAL_ROLE'] = 'primary'
    settings['UNATTENDED_AUTH']['roles']['secondary'][0]['user'] = 'changed-unselected-entry'
    assert allocation_scope(settings) == primary


def test_binding_pointer_digest_and_manifest_digest_are_verified(tmp_path):
    from api_agent.testing.cost_history import allocation_scope
    from api_agent.telemetry.history import binding_digest
    from datetime import datetime, timezone
    cache = HistoryCache(tmp_path / 'cache')
    settings, first, payload = binding_run()
    cache.ingest('origin', first, payload)
    scope = allocation_scope(settings)
    cache.bind('origin', scope, first['run_id'])
    original_digest = cache.connection.execute('SELECT digest FROM allocation_bindings').fetchone()[0]
    cache.connection.execute('UPDATE allocation_bindings SET digest=?', ('bad',))
    assert cache.bound_window('origin', scope, datetime(2026, 10, 10, tzinfo=timezone.utc)) is None
    cache.connection.execute('UPDATE allocation_bindings SET digest=?', (original_digest,))
    cache.connection.commit()
    _, remote, payload = binding_run(2)
    cache.ingest('origin', remote, payload)
    cache.connection.execute('UPDATE allocation_bindings SET run_id=?', (remote['run_id'],))
    assert cache.bound_window('origin', scope, datetime(2026, 10, 10, tzinfo=timezone.utc)) is None
    wrong = 'a' * 64
    cache.connection.execute('UPDATE allocation_bindings SET manifest_digest=?,digest=?', (wrong, binding_digest('origin', scope, remote['run_id'], wrong)))
    assert cache.bound_window('origin', scope, datetime(2026, 10, 10, tzinfo=timezone.utc)) is None
    cache.close()


@pytest.mark.parametrize('change', ['zero', 'incomplete', 'unobserved', 'unknown'])
def test_local_binding_requires_complete_explicit_subscription_call(tmp_path, change):
    from api_agent.testing.cost_history import allocation_scope
    cache = HistoryCache(tmp_path / 'cache')
    settings, manifest, payload = binding_run()
    artifact = json.loads(payload); row = artifact['tests'][0]
    if change == 'zero': row.update(calls=[], observed_call_count=0)
    elif change == 'incomplete': row['calls_complete'] = False
    elif change == 'unobserved': row['observed_call_count'] = 2
    else: row['calls'][0]['quote']['accounting']['billing_mode'] = 'unknown'
    payload = json.dumps(artifact).encode(); manifest['files']['accounting.json'] = sha256(payload).hexdigest()
    cache.ingest('origin', manifest, payload)
    cache.bind('origin', allocation_scope(settings), manifest['run_id'])
    assert cache.connection.execute('SELECT * FROM allocation_bindings').fetchall() == []
    cache.close()


def test_disposable_legacy_bindings_are_invalidated(tmp_path):
    cache = HistoryCache(tmp_path / 'cache')
    cache.connection.execute('DROP TABLE allocation_bindings')
    cache.connection.execute('CREATE TABLE allocation_bindings (origin TEXT,scopehash TEXT,run_id TEXT,PRIMARY KEY(origin,scopehash))')
    cache.connection.execute('INSERT INTO allocation_bindings VALUES (?,?,?)', ('origin', 'a' * 64, str(uuid4())))
    cache.connection.commit(); cache.close()
    rebuilt = HistoryCache(tmp_path / 'cache')
    assert rebuilt.connection.execute('SELECT * FROM allocation_bindings').fetchall() == []
    assert 'digest' in {row[1] for row in rebuilt.connection.execute('PRAGMA table_info(allocation_bindings)')}
    rebuilt.close()


def test_recording_adapter_never_opens_user_cache(tmp_path, monkeypatch, isolated_recording_history):
    monkeypatch.setattr('api_agent.telemetry.history.cache_path',
                        lambda: pytest.fail('Unit fixtures must not open the user cache'))
    context = SimpleNamespace(owner_root=tmp_path, base_root=tmp_path)
    snapshot = estimator_for(context, [], {})
    assert snapshot.cache is not None
    filename = snapshot.cache.connection.execute('PRAGMA database_list').fetchone()[2]
    assert filename == str(isolated_recording_history)
    snapshot.cache.close()


def test_repository_root_resolution_is_shared_by_picker_refresh(tmp_path):
    from hashlib import sha256
    from api_agent.testing.collection import TestIdentity
    from api_agent.testing.cost_history import HistoricalEstimator
    from api_agent.telemetry.accounting import source_fingerprint
    from test_historical_estimation import records, compat
    from datetime import datetime, timezone
    source = tmp_path / 'test.py'; source.write_text('def test_one(): pass')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    test = TestIdentity('t', tmp_path.name, 'test.py', 'test.py::test[one]')
    expected = sha256(source.read_bytes()).hexdigest()
    histories = records()
    settings = dict(compat(), TEST_COST_HISTORY_DAYS=30)
    from api_agent.telemetry.accounting import compatibility
    for _, artifact in histories:
        artifact['tests'][0]['source_fingerprint'] = expected
        artifact['tests'][0]['compatibility'] = compatibility(settings)
        artifact['tests'][0]['calls'][0]['quote']['accounting']['compatibility'] = compatibility(settings)
    class Cache:
        def records(self, origin): return histories
    snapshot = HistoricalEstimator([test], settings, Cache(), 'origin', 'repo', bundled_rate_card(),
                                   now=datetime(2026,10,10,tzinfo=timezone.utc), context=context)
    assert snapshot.fingerprints['t'] == expected
    assert source_fingerprint(test, context=context) == expected
    assert snapshot.predictions['t']['basis'] == 'historical_list_price'
    assert snapshot.predictions['t']['sample_count'] == 3
    snapshot.storage = SimpleNamespace(accounting_history=lambda *args, **kwargs: ())
    refreshed = snapshot.synchronize()
    assert refreshed.context is context
    assert refreshed.fingerprints == snapshot.fingerprints
    assert refreshed.predictions == snapshot.predictions


def test_sqlite_retains_reset_evidence_but_picker_and_rebuild_exclude_it(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from api_agent.testing.cost_history import HistoricalEstimator, allocation_scope
    settings, manifest, payload = binding_run()
    artifact = json.loads(payload)
    artifact['quota'].update(pct_point_delta=None, quota_unavailable_reason='changed_window')
    artifact['quota']['after']['reset_at'] = '2026-12-01T00:00:00Z'
    payload = json.dumps(artifact).encode()
    manifest['files']['accounting.json'] = sha256(payload).hexdigest()
    monkeypatch.setattr('api_agent.telemetry.accounting.source_fingerprint', lambda *a, **kw: 'c' * 64)
    test = SimpleNamespace(id='t', node_id='test.py::test[one]')
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    for rebuild in (False, True):
        if rebuild:
            (tmp_path / 'reset-history.sqlite3').unlink()
        cache = HistoryCache(tmp_path / 'reset-history.sqlite3')
        cache.ingest('origin', manifest, payload)
        assert cache.records('origin') == [(manifest, artifact)]
        cache.bind('origin', allocation_scope(settings), manifest['run_id'])
        assert cache.bound_window('origin', allocation_scope(settings), now) is None
        snapshot = HistoricalEstimator([test], settings, cache, 'origin', 'repo', bundled_rate_card(), now=now)
        assert snapshot.records == []
        assert snapshot.predictions['t']['estimated_cost_usd'] is None
        assert snapshot.predictions['t']['sample_count'] == 0
        assert snapshot.synchronize().predictions['t']['estimated_cost_usd'] is None
        cache.close()
