from hashlib import sha256
import pytest
from api_agent.runtime.ownership import StackScope


def test_canonical_root_identity(tmp_path):
    scope=StackScope.from_roots(tmp_path / 'base' / '..',tmp_path / 'pack')
    assert sha256(f'{tmp_path.resolve()}\n{(tmp_path / "pack").resolve()}'.encode()).hexdigest()[:16] in scope.legacy_ids
    assert scope.project == 'sa-' + scope.id
    assert scope == StackScope.from_roots(tmp_path,tmp_path / 'pack')
    assert scope != StackScope.from_roots(tmp_path,tmp_path / 'other')


def test_labels_require_all_ownership_evidence():
    scope=StackScope('test')
    labels={**scope.labels('langfuse'),'com.docker.compose.project':scope.project,'com.docker.compose.service':'api'}
    assert scope.owns(labels)
    assert not scope.owns(None)
    for key in labels:
        broken={**labels,key:'wrong'}
        if key == 'com.docker.compose.service': broken[key]=' '
        assert not scope.owns(broken)
        assert not scope.owns({k:v for k,v in labels.items() if k != key})
    for profile in ('otel','localstack'):
        assert scope.labels(profile)['api-agent.profile']==profile
    with pytest.raises(ValueError,match='Unsupported'):
        scope.labels('unknown')


def test_physical_aliases_keep_scope_without_resolve_equality(tmp_path, monkeypatch):
    from pathlib import Path
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir(); pack.mkdir()
    alias_base, alias_pack = tmp_path / 'alias-base', tmp_path / 'alias-pack'
    alias_base.symlink_to(base, target_is_directory=True)
    alias_pack.symlink_to(pack, target_is_directory=True)
    original = Path.resolve
    monkeypatch.setattr(Path, 'resolve', lambda path: path.absolute() if path in {alias_base, alias_pack} else original(path))
    assert base.resolve() != alias_base.resolve() and base.samefile(alias_base)
    assert pack.resolve() != alias_pack.resolve() and pack.samefile(alias_pack)
    scope = StackScope.from_roots(base, pack)
    assert scope == StackScope.from_roots(alias_base, alias_pack)
    other = tmp_path / 'worktree'; other.mkdir()
    assert scope != StackScope.from_roots(base, other)
    legacy = scope.legacy_ids[0]
    assert scope.is_legacy({**scope.labels('otel'), 'api-agent.scope': legacy,
                            'com.docker.compose.project': 'sa-' + legacy})
    assert not scope.is_legacy(None)
    assert not scope.is_legacy(scope.labels('otel'))
