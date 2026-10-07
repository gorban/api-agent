from hashlib import sha256
import pytest
from api_agent.runtime.ownership import StackScope


def test_canonical_root_identity(tmp_path):
    scope=StackScope.from_roots(tmp_path / 'base' / '..',tmp_path / 'pack')
    assert scope.id == sha256(f'{tmp_path.resolve()}\n{(tmp_path / "pack").resolve()}'.encode()).hexdigest()[:16]
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
