"""The bundled workflow follows the executing checkout or packaged asset root."""
from pathlib import Path

import pytest

from api_agent.workflows import resources

ROOT = Path(__file__).resolve().parents[2]


def test_real_source_checkout_uses_its_flow_when_package_has_no_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(resources, 'files', lambda package: tmp_path / 'unpackaged_api_agent')
    assert resources.bundled_root().samefile(ROOT)


def candidate(tmp_path, monkeypatch):
    checkout = tmp_path / 'release'
    module = checkout / 'src/api_agent/workflows/resources.py'
    module.parent.mkdir(parents=True)
    module.write_text('# relocated module\n')
    for name in ('src/api_agent/__init__.py', 'flow.md.html', 'launch.py', 'pyproject.toml'):
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('marker\n')
    package = tmp_path / 'installed/api_agent'
    package.mkdir(parents=True)
    (package / 'flow.md.html').write_text('# packaged flow\n')
    monkeypatch.setattr(resources, '__file__', str(module))
    return checkout, package


def test_verified_source_candidate_precedes_installed_assets(tmp_path, monkeypatch):
    checkout, package = candidate(tmp_path, monkeypatch)
    (package / 'flow.md.html').unlink()
    monkeypatch.setattr(resources, 'files', lambda name: package)
    assert resources.bundled_root() == checkout


def test_packaged_flow_wins_even_beneath_a_valid_checkout(tmp_path, monkeypatch):
    checkout, package = candidate(tmp_path, monkeypatch)
    assert (checkout / 'flow.md.html').is_file()
    monkeypatch.setattr(resources, 'files', lambda name: package)
    assert resources.bundled_root() == package


@pytest.mark.parametrize('missing', ('src/api_agent/__init__.py', 'flow.md.html',
                                     'launch.py', 'pyproject.toml', 'src/api_agent/workflows/resources.py'))
def test_missing_checkout_marker_cannot_select_source_flow(tmp_path, monkeypatch, missing):
    checkout, package = candidate(tmp_path, monkeypatch)
    (checkout / missing).unlink()
    (package / 'flow.md.html').unlink()
    calls = []

    def installed(name):
        calls.append(name)
        return package

    monkeypatch.setattr(resources, 'files', installed)
    with pytest.raises(FileNotFoundError, match='Bundled application flow is missing'):
        resources.bundled_root()
    assert calls == ['api_agent']


def test_release_src_shaped_install_uses_packaged_asset(tmp_path, monkeypatch):
    package = tmp_path / 'release/src/api_agent'
    module = package / 'workflows/resources.py'
    module.parent.mkdir(parents=True)
    module.write_text('# installed module\n')
    (package / 'flow.md.html').write_text('# actual packaged flow\n')
    monkeypatch.setattr(resources, '__file__', str(module))
    calls = []

    def installed(name):
        calls.append(name)
        return package

    monkeypatch.setattr(resources, 'files', installed)
    assert resources.bundled_root() == package
    assert calls == ['api_agent']


def test_damaged_wheel_with_valid_parent_checkout_refuses_fallback(tmp_path, monkeypatch):
    checkout, _ = candidate(tmp_path, monkeypatch)
    package = checkout / 'src/api_agent'
    marker = checkout / 'src/api_agent-0.1.0.dist-info'
    marker.mkdir()
    (marker / 'METADATA').write_text('Metadata-Version: 2.4\nName: api-agent\nVersion: 0.1.0\n')
    (marker / 'RECORD').write_text('api_agent/workflows/resources.py,,\napi_agent/flow.md.html,,\n')
    monkeypatch.setattr(resources, 'files', lambda name: package)

    assert (checkout / 'flow.md.html').is_file()
    assert not (package / 'flow.md.html').exists()
    with pytest.raises(FileNotFoundError, match='Bundled application flow is missing'):
        resources.bundled_root()


@pytest.mark.parametrize('metadata,record,installed', [
    ('Name: unrelated\n', 'api_agent/workflows/resources.py,,\n', False),
    ('Name: api-agent\n', 'unrelated/module.py,,\n', False),
    ('Name: api-agent\n', None, True),
    (None, 'api_agent/workflows/resources.py,,\n', True),
])
def test_adjacent_metadata_must_identify_installed_module(tmp_path, monkeypatch, metadata, record, installed):
    checkout, _ = candidate(tmp_path, monkeypatch)
    package = checkout / 'src/api_agent'
    marker = checkout / 'src/api_agent-0.1.0.dist-info'
    marker.mkdir()
    if metadata is not None:
        (marker / 'METADATA').write_text(metadata)
    if record is not None:
        (marker / 'RECORD').write_text(record)
    monkeypatch.setattr(resources, 'files', lambda name: package)

    if installed:
        with pytest.raises(FileNotFoundError, match='Bundled application flow is missing'):
            resources.bundled_root()
    else:
        assert resources.bundled_root() == checkout


def test_missing_packaged_flow_fails_without_other_graph_fallback(tmp_path, monkeypatch):
    _, package = candidate(tmp_path, monkeypatch)
    (tmp_path / 'release/pyproject.toml').unlink()
    (package / 'flow.md.html').unlink()
    monkeypatch.setattr(resources, 'files', lambda name: package)
    with pytest.raises(FileNotFoundError, match='Bundled application flow is missing'):
        resources.bundled_root()
