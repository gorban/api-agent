"""Manifest publication remains bounded and readable across recording layers."""

from uuid import uuid4

import pytest

from api_agent.testing import allure_preview
from api_agent.testing.payloads import PayloadResolver, PayloadStore, encoded
from test_payloads import recorder, result


def test_recorded_large_manifest_resolves_digest_bound_payload(tmp_path):
    run = recorder(tmp_path)
    store = PayloadStore(run, "example::test")
    value = {"value": "recorded evidence"}
    ref = store.put_wire(str(uuid4()), "response", value)
    run.record_artifact("payload-index.json", encoded(store.index()))
    for number in range(4100):
        run.record_artifact(f"part-{number:05d}.json", b"{}")
    run.record_test(result())
    manifest = run.finalize()

    with PayloadResolver.local(tmp_path, manifest) as resolver:
        assert resolver.index("payload-index.json") == store.index()
        assert resolver(ref) == value

    tampered = encoded({"value": "recorded evidencf"})
    assert len(tampered) == ref["length"]
    (tmp_path / ref["source"]).write_bytes(tampered)
    with PayloadResolver.local(tmp_path, manifest) as resolver:
        resolver.index("payload-index.json")
        with pytest.raises(ValueError, match="digest"):
            resolver(ref)


@pytest.mark.parametrize("limit", ["count", "bytes"])
def test_recorder_rejects_oversized_manifest_without_committing(
    tmp_path, monkeypatch, limit
):
    from api_agent.testing import manifest_budget

    run = recorder(tmp_path)
    run.record_test(result())
    if limit == "count":
        monkeypatch.setattr(manifest_budget, "MAX_MANIFEST_FILES", 0)
    else:
        monkeypatch.setattr(manifest_budget, "MAX_MANIFEST_BYTES", 1)

    with pytest.raises(manifest_budget.ManifestBudgetError):
        run.finalize()
    assert not (tmp_path / "manifest.json").exists()
    assert list(tmp_path.glob("*-result.json"))


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("status, expected_exit", [("passed", 0), ("failed", 1)])
def test_budget_failure_retains_staging_and_test_outcome(
    tmp_path, monkeypatch, capsys, local, status, expected_exit
):
    from api_agent.testing import manifest_budget, recorded
    from test_recorded_integration import fixture, fake_runner, Storage

    context, test, config = fixture(tmp_path)
    if not local:
        config["ALLURE_RESULTS_DIR"] = None
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(manifest_budget, "MAX_MANIFEST_FILES", 0)
    outcome = recorded.run_recorded_integration(
        context,
        [test],
        config,
        accepted=True,
        storage=Storage(),
        runner=fake_runner(status),
    )
    assert outcome.exit_code == expected_exit
    assert outcome.execution.tests[0].status == status
    assert outcome.persisted is False
    assert outcome.manifest is None
    assert outcome.directory.is_dir()
    assert list(outcome.directory.glob("*-result.json"))
    assert not (outcome.directory / "manifest.json").exists()
    diagnostic = capsys.readouterr().err
    assert "Allure publication failed:" in diagnostic
    assert "No valid manifest was committed" in diagnostic
    assert str(outcome.directory) in diagnostic


def test_preview_rejects_oversized_manifest_without_affecting_recording(
    tmp_path, monkeypatch
):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    import json

    from api_agent.testing.recording import RunIdentity

    identity = RunIdentity(
        str(uuid4()), "example", "integration", datetime.now(timezone.utc)
    )
    owner = SimpleNamespace(directory=tmp_path, identity=identity)
    monkeypatch.setattr(
        allure_preview.RunStorage, "_read_source", lambda *args, **kwargs: b"{}"
    )
    mapping = {str(i): f"{i:05d}-{'x' * 230}.json" for i in range(9000)}
    assert len(mapping) < 16384
    projected = {name: "0" * 64 for name in mapping.values()}
    assert len(json.dumps({"files": projected}).encode()) > 2 * 1024 * 1024
    received = []

    allure_preview.emit_snapshot(
        owner, {"small": next(iter(mapping.values()))}, received.append
    )
    assert len(received) == 1
    received.clear()
    allure_preview.emit_snapshot(owner, mapping, received.append)
    assert received == []
