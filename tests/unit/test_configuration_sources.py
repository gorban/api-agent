"""Source receipts describe what existing readers actually consumed."""
from pathlib import Path
import threading

import pytest

from api_agent.config.source_reads import (capture_reads, capturing, record_file,
    record_link, record_optional, record_probe)
from api_agent.config.session import ConfigSession
from api_agent.config import schema_from_dict
from api_agent.workflows.definition import _read_bounded
from test_tool_package_declarations import package_receipt
from test_tool_selection import selection_fixture
from test_tool_config_session import session_for


def test_observation_is_inert_outside_its_context(tmp_path):
    assert not capturing()
    record_file(tmp_path / 'x', tmp_path, b'a')
    record_link(tmp_path / 'x', tmp_path / 'y')
    record_optional(tmp_path / 'x', False)
    record_probe('x', lambda: False, True)


def test_nested_and_threaded_capture_have_independent_receipts(tmp_path):
    path = tmp_path / 'x'
    path.write_bytes(b'a')
    with capture_reads() as outer:
        assert capturing()
        record_file(path, tmp_path, b'a')
        record_file(path, tmp_path, b'a')
        with capture_reads() as inner:
            record_optional(tmp_path / 'missing', False)
        child = []
        thread = threading.Thread(target=lambda: child.append(capturing()))
        thread.start()
        thread.join()
    assert child == [False]
    assert outer.snapshot().current()
    assert inner.snapshot().current()
    assert len(outer.snapshot().files) == 1
    assert inner.snapshot().files == ()
    assert not capturing()


@pytest.mark.parametrize('kind', ['file', 'link', 'optional', 'probe'])
def test_receipt_rejects_a_source_changed_during_collection(tmp_path, kind):
    with capture_reads():
        with pytest.raises(ValueError, match='changed during resolution'):
            if kind == 'file':
                record_file(tmp_path / 'x', tmp_path, b'a')
                record_file(tmp_path / 'x', tmp_path, b'b')
            elif kind == 'link':
                record_link(tmp_path / 'x', tmp_path / 'a')
                record_link(tmp_path / 'x', tmp_path / 'b')
            elif kind == 'optional':
                record_optional(tmp_path / 'x', False)
                record_optional(tmp_path / 'x', True)
            else:
                record_probe('x', lambda: 1, 1)
                record_probe('x', lambda: 2, 2)
    assert not capturing()


@pytest.mark.parametrize('kind', ['file', 'link', 'optional', 'probe', 'missing'])
def test_delivery_check_detects_changed_source_without_timestamps(tmp_path, kind):
    path = tmp_path / 'x'
    path.write_bytes(b'a')
    alias = tmp_path / 'alias'
    alias.symlink_to(path)
    values = [1]
    with capture_reads() as reads:
        _read_bounded(path, tmp_path)
        record_link(alias, path)
        record_link(alias, path)
        record_optional(tmp_path / 'optional', False)
        record_optional(tmp_path / 'optional', False)
        record_probe('probe', lambda: tuple(values), (1,))
        record_probe('probe', lambda: tuple(values), (1,))
    snapshot = reads.snapshot()
    assert snapshot.current()
    if kind == 'file':
        path.write_bytes(b'b')
    elif kind == 'link':
        alias.unlink()
        alias.symlink_to(tmp_path / 'other')
    elif kind == 'optional':
        (tmp_path / 'optional').touch()
    elif kind == 'probe':
        values.append(2)
    else:
        path.unlink()
    assert not snapshot.current()


def test_provider_contribution_and_absent_contribution_are_observed(tmp_path):
    provider = tmp_path / 'provider'
    provider.mkdir()
    (provider / 'factory.py').write_text('raise AssertionError("native provider imported")\n')
    schema = schema_from_dict({'version': 1, 'fields': {
        'LLM_PROVIDER': {'type': 'str', 'default': str(provider / 'factory.py') + ':create'}}})
    from types import SimpleNamespace
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path, owner_root=tmp_path,
        dotenv_path=tmp_path / '.env', external_pack=False, anchors={})
    session = ConfigSession(context, schema, environ={})
    missing = session.review_snapshot()
    assert missing.sources_current()
    (provider / 'cfg').mkdir()
    config = provider / 'cfg/config.toml'
    config.write_text('version=1\n[fields.COUNT]\ntype="int"\ndefault=3\n')
    assert not missing.sources_current()
    first = session.review_snapshot()
    assert first.review.values['COUNT'] == 3
    config.write_text(config.read_text().replace('default=3', 'default=9'))
    assert not first.sources_current()
    assert session.review_snapshot().review.values['COUNT'] == 9


def test_cached_workflow_alias_remains_a_delivery_dependency(selection_fixture):
    context, _ = selection_fixture
    node = context.base_root / 'node.md.html'
    first = node.with_name('first.md.html')
    second = node.with_name('second.md.html')
    first.write_bytes(node.read_bytes())
    second.write_bytes(node.read_bytes() + b'\n')
    node.unlink()
    node.symlink_to(first)
    session = session_for(selection_fixture)
    old = session.review_snapshot()
    assert old.sources_current()
    node.unlink()
    node.symlink_to(second)
    assert not old.sources_current()
    changed = session.review_snapshot()
    assert changed.sources_current()
    assert changed.plan.definitions[0].definition_id != old.plan.definitions[0].definition_id


@pytest.mark.parametrize('kind', ['name', 'receipt', 'owners'])
def test_installed_declaration_receipt_observes_ownership_inputs(package_receipt, monkeypatch, kind):
    capture, source, distribution = package_receipt
    from api_agent.tooling import declarations
    with capture_reads() as reads:
        capture.read(capture.package_path('package:sample-tools:sample_tools/tool.toml'))
    assert reads.snapshot().current()
    if kind == 'name':
        distribution.metadata['Name'] = 'changed'
    elif kind == 'receipt':
        distribution.read_text = lambda name: 'changed receipt'
    else:
        monkeypatch.setattr(declarations.metadata, 'distributions', lambda **kwargs: [])
    assert not reads.snapshot().current()


def test_failed_read_is_observed_when_an_optional_graph_becomes_readable(tmp_path):
    missing = tmp_path / 'missing.md.html'
    with capture_reads() as reads:
        with pytest.raises(ValueError, match='unavailable resource'):
            _read_bounded(missing, tmp_path)
    snapshot = reads.snapshot()
    assert snapshot.current()
    missing.write_text('# newly available graph')
    assert not snapshot.current()


def test_cache_bypass_still_receipts_executable_workflow_aliases(selection_fixture, monkeypatch):
    context, _ = selection_fixture
    node = context.base_root / 'node.md.html'
    old_target = node.with_name('old.md.html')
    new_target = node.with_name('new.md.html')
    old_target.write_bytes(node.read_bytes())
    new_target.write_bytes(node.read_bytes() + b'\n')
    node.unlink()
    node.symlink_to(old_target)
    monkeypatch.setattr('api_agent.tooling.discovery_cache.MAX_WATCHED_LINKS', 1)
    session = session_for(selection_fixture)
    snapshot = session.review_snapshot()
    assert snapshot.sources_current()
    node.unlink()
    node.symlink_to(new_target)
    assert not snapshot.sources_current()


def test_failed_resolution_is_observed_without_changing_normal_errors(tmp_path):
    from api_agent.config.source_reads import resolve_path
    missing = tmp_path / 'missing'
    with pytest.raises(OSError):
        resolve_path(missing, strict=True)
    with capture_reads() as reads:
        with pytest.raises(OSError):
            resolve_path(missing, strict=True)
    assert reads.snapshot().current()
    missing.touch()
    assert not reads.snapshot().current()
