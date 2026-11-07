"""Suite auth cache borrows only the admitted launch snapshot lifetime."""
import pytest

from api_agent.providers.contracts import ContractError
from api_agent.testing import provider_setup


def test_admitted_context_directory_is_private(tmp_path, monkeypatch):
    directory = tmp_path / 'suite'
    directory.mkdir(mode=0o700)
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(directory / 'launch.json'))
    calls = []
    def read():
        calls.append('admitted')
        return {}
    monkeypatch.setattr(provider_setup, '_read_snapshot', read)
    assert provider_setup.integration_context_directory() == directory
    assert calls == ['admitted']


def test_operation_directory_is_shared_by_different_wave_snapshots(tmp_path, monkeypatch):
    operation = tmp_path / 'operation'
    operation.mkdir(mode=0o700)
    monkeypatch.setattr(provider_setup, '_read_snapshot',
                        lambda: {'suite_directory': str(operation)})
    for wave in ('first', 'confirmation'):
        snapshot = tmp_path / wave / 'launch.json'
        monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(snapshot))
        assert provider_setup.integration_context_directory() == operation


def test_operation_directory_round_trips_in_protected_snapshot(tmp_path, monkeypatch):
    from types import SimpleNamespace
    operation = tmp_path / 'operation'
    operation.mkdir(mode=0o700)
    path = provider_setup.write_launch_snapshot(tmp_path, SimpleNamespace(owner_root=tmp_path),
                                              {}, suite_directory=operation)
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    assert provider_setup._read_snapshot()['suite_directory'] == str(operation)
    assert provider_setup.integration_context_directory() == operation


@pytest.mark.parametrize('value', [None, 1, 'relative'])
def test_snapshot_rejects_invalid_operation_directory(tmp_path, monkeypatch, value):
    import json
    from types import SimpleNamespace
    path = provider_setup.write_launch_snapshot(tmp_path, SimpleNamespace(owner_root=tmp_path), {})
    data = json.loads(path.read_text())
    data['suite_directory'] = value
    path.write_text(json.dumps(data))
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(path))
    with pytest.raises(ContractError):
        provider_setup._read_snapshot()


@pytest.mark.parametrize('unsafe', ['mode', 'symlink', 'filename'])
def test_context_directory_rejects_unowned_layout(tmp_path, monkeypatch, unsafe):
    directory = tmp_path / 'suite'
    directory.mkdir(mode=0o700)
    snapshot = directory / 'launch.json'
    if unsafe == 'mode':
        directory.chmod(0o755)
    elif unsafe == 'symlink':
        link = tmp_path / 'linked'
        link.symlink_to(directory, target_is_directory=True)
        snapshot = link / 'launch.json'
    else:
        snapshot = directory / 'other.json'
    monkeypatch.setenv('API_AGENT_INTEGRATION_CONTEXT', str(snapshot))
    monkeypatch.setattr(provider_setup, '_read_snapshot', lambda: {})
    with pytest.raises(ContractError, match='private'):
        provider_setup.integration_context_directory()


def test_context_directory_preserves_admission_failure(monkeypatch):
    def rejected():
        raise ContractError('not admitted')
    monkeypatch.setattr(provider_setup, '_read_snapshot', rejected)
    with pytest.raises(ContractError, match='not admitted'):
        provider_setup.integration_context_directory()
