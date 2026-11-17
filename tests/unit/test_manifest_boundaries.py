"""Manifest limits apply consistently at publication and history boundaries."""

import json
from hashlib import sha256
from uuid import uuid4

import pytest

from api_agent.testing.allure_source import MAX_MANIFEST, RunSource, ViewerError
from api_agent.testing.storage import RunStorage, StorageError
from test_recording_storage import FakeS3, staged
from test_historical_cache import fixture as history_fixture


def test_storage_rejects_too_many_manifest_entries_before_publish(
    tmp_path, monkeypatch
):
    from api_agent.testing import manifest_budget

    storage = RunStorage("bucket", client=FakeS3())
    path, manifest = staged(tmp_path, str(uuid4()), "2026-01-01T00:00:00Z")
    monkeypatch.setattr(manifest_budget, "MAX_MANIFEST_FILES", 0)
    with pytest.raises(ValueError, match="budget"):
        storage.persist(path, manifest)
    assert not storage._client().objects


def test_storage_rejects_oversized_actual_manifest_bytes(tmp_path):
    storage = RunStorage("bucket", client=FakeS3())
    path, manifest = staged(tmp_path, str(uuid4()), "2026-01-01T00:00:00Z")
    with (path / "manifest.json").open("ab") as stream:
        stream.write(b" " * MAX_MANIFEST)
    with pytest.raises(StorageError):
        storage.persist(path, manifest)
    assert not storage._client().objects


def test_history_rejects_oversized_remote_manifest_bytes(tmp_path):
    storage = RunStorage("bucket", client=FakeS3())
    path, manifest = staged(tmp_path, str(uuid4()), "2026-01-01T00:00:00Z")
    _, payload = history_fixture()
    accounting = json.loads(payload)
    accounting["run_id"] = manifest["run_id"]
    payload = json.dumps(accounting).encode()
    (path / "accounting.json").write_bytes(payload)
    manifest["files"]["accounting.json"] = sha256(payload).hexdigest()
    (path / "manifest.json").write_text(json.dumps(manifest))
    storage.persist(path, manifest)
    assert storage.history("example", require_accounting=True) == [manifest]
    key = storage._key("example", manifest["run_id"], "manifest.json")
    storage._client().objects[key] += b" " * MAX_MANIFEST
    assert storage.history("example") == []
    with pytest.raises(StorageError):
        storage.history("example", require_accounting=True)


@pytest.mark.parametrize("local", [True, False])
def test_load_rejects_oversized_raw_manifest_even_if_json_equivalent(tmp_path, local):
    storage = RunStorage("bucket", client=FakeS3())
    directory, manifest = staged(tmp_path, str(uuid4()), "2026-01-01T00:00:00Z")
    result = b'{"uuid":"a","status":"passed"}'
    (directory / "a-result.json").write_bytes(result)
    manifest["files"]["a-result.json"] = sha256(result).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest))
    storage.persist(directory, manifest)
    assert (
        RunSource("example", storage=storage).load(manifest)["a-result.json"] == result
    )
    if local:
        target = tmp_path / "example" / manifest["run_id"]
        target.parent.mkdir()
        directory.rename(target)
        with (target / "manifest.json").open("ab") as stream:
            stream.write(b" " * MAX_MANIFEST)
        source = RunSource("example", tmp_path)
    else:
        key = storage._key("example", manifest["run_id"], "manifest.json")
        storage._client().objects[key] += b" " * MAX_MANIFEST
        source = RunSource("example", storage=storage)
    with pytest.raises(ViewerError, match="manifest exceeds"):
        source.load(manifest)


def test_history_cache_does_not_migrate_oversized_manifest_row(tmp_path):
    from api_agent.telemetry.history import HistoryCache

    path = tmp_path / "history.sqlite3"
    cache = HistoryCache(path)
    valid, valid_payload = history_fixture()
    valid_encoded = json.dumps(valid, sort_keys=True, separators=(",", ":"))
    cache.connection.execute(
        "INSERT INTO history VALUES (?,?,?,?,?)",
        (
            "origin",
            valid["run_id"],
            sha256(valid_encoded.encode()).hexdigest(),
            valid_encoded,
            valid_payload.decode(),
        ),
    )
    manifest, payload = history_fixture()
    encoded = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + " " * MAX_MANIFEST
    )
    digest = sha256(encoded.encode()).hexdigest()
    cache.connection.execute(
        "INSERT INTO history VALUES (?,?,?,?,?)",
        ("origin", manifest["run_id"], digest, encoded, payload.decode()),
    )
    cache.connection.commit()
    cache.connection.close()
    cache._finalizer.detach()
    restored = HistoryCache(path)
    assert [row[0]["run_id"] for row in restored.records("origin")] == [valid["run_id"]]
    assert restored.connection.execute(
        "SELECT count(*) FROM history_finish"
    ).fetchone() == (1,)
    restored.connection.close()
    restored._finalizer.detach()


def test_nonbudget_finalization_error_propagates_to_runner(tmp_path, monkeypatch):
    from api_agent.testing import recorded
    from api_agent.testing.integration import IntegrationRunResult
    from api_agent.testing.contracts import TestResult
    from test_recorded_integration import fixture, Storage

    context, test, config = fixture(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic finalization failure")

    monkeypatch.setattr(recorded.RunRecorder, "finalize", fail)

    def runner(context, tests, **hooks):
        result = IntegrationRunResult(0, (TestResult(test, "passed"),), "", False)
        hooks["before_run"]()
        hooks["on_result"](result.tests[0])
        with pytest.raises(RuntimeError, match="synthetic finalization failure"):
            hooks["after_run"](result)
        return result

    outcome = recorded.run_recorded_integration(
        context, [test], config, accepted=True, storage=Storage(), runner=runner
    )
    assert outcome.execution.tests[0].status == "passed"
    assert not outcome.persisted
