"""Installed base case identity is canonical only with matching package receipts."""
import base64
from hashlib import sha256
from importlib.metadata import PathDistribution
from types import SimpleNamespace

import pytest

from api_agent.testing import collection


@pytest.fixture
def installed(tmp_path, monkeypatch):
    site = tmp_path / 'site-packages'
    package = site / 'api_agent'
    source = package / 'testing/collection.py'
    source.parent.mkdir(parents=True)
    source.write_text('# Installed source.\n')
    flow = package / 'flow.md.html'
    flow.write_text('# Workflow v2\n')
    metadata = site / 'api_agent-0.1.0.dist-info'
    metadata.mkdir()
    (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: api-agent\nVersion: 0.1.0\n')
    rows = []
    for path in (source, flow):
        body = path.read_bytes()
        digest = base64.urlsafe_b64encode(sha256(body).digest()).decode().rstrip('=')
        rows.append(f'{path.relative_to(site)},sha256={digest},{len(body)}\n')
    (metadata / 'RECORD').write_text(''.join(rows))
    monkeypatch.setattr(collection, '__file__', str(source))
    monkeypatch.setattr(collection.metadata, 'distribution', lambda name: PathDistribution(metadata))
    return SimpleNamespace(package=package, source=source, metadata=metadata)


def test_receipted_installed_base_uses_catalog_repository_identity(installed, tmp_path):
    context = SimpleNamespace(base_root=installed.package, owner_root=installed.package)
    assert collection.repository_label(context, installed.package) == 'api-agent'
    external = tmp_path / 'relocated-pack'
    external.mkdir()
    context.owner_root = external
    assert collection.repository_label(context, external) == 'relocated-pack'
    assert collection.repository_label(context, installed.package) == 'api-agent'


@pytest.mark.parametrize('change', ['source-hash', 'flow-hash', 'missing-record', 'unhashed', 'algorithm', 'missing-row'])
def test_unverified_install_never_gains_canonical_identity(installed, change):
    record = installed.metadata / 'RECORD'
    if change == 'source-hash':
        installed.source.write_text('# Modified_ source.\n')
    elif change == 'flow-hash':
        (installed.package / 'flow.md.html').write_text('# Workflow v3\n')
    elif change == 'missing-record':
        record.unlink()
    elif change == 'unhashed':
        text = record.read_text().splitlines()
        text[0] = 'api_agent/testing/collection.py,,'
        record.write_text('\n'.join(text) + '\n')
    elif change == 'algorithm':
        record.write_text(record.read_text().replace('sha256=', 'sha512=', 1))
    else:
        record.write_text(record.read_text().splitlines()[0] + '\n')
    context = SimpleNamespace(base_root=installed.package, owner_root=installed.package)
    assert collection.repository_label(context, installed.package) == 'api_agent'


def test_source_directory_names_remain_physical_identity(installed, tmp_path):
    source = tmp_path / 'source-checkout'
    source.mkdir()
    context = SimpleNamespace(base_root=source, owner_root=source)
    assert collection.repository_label(context, source) == 'source-checkout'


@pytest.mark.parametrize('change', ['other-owner', 'symlink', 'missing-source', 'size', 'duplicate', 'no-distribution', 'too-large'])
def test_invalid_installed_ownership_does_not_alias_case_identity(installed, tmp_path, monkeypatch, change):
    if change == 'other-owner':
        metadata = tmp_path / 'other/api_agent-0.1.0.dist-info'
        metadata.mkdir(parents=True)
        monkeypatch.setattr(collection.metadata, 'distribution', lambda name: PathDistribution(metadata))
    elif change == 'symlink':
        copy = installed.source.with_name('copy.py')
        installed.source.rename(copy)
        installed.source.symlink_to(copy)
    elif change == 'missing-source':
        installed.source.unlink()
    elif change == 'size':
        installed.source.write_text('# Additional content')
    elif change == 'duplicate':
        record = installed.metadata / 'RECORD'
        record.write_text(record.read_text() + record.read_text().splitlines()[0] + '\n')
    elif change == 'too-large':
        with installed.source.open('wb') as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
    else:
        def missing(name):
            raise collection.metadata.PackageNotFoundError(name)
        monkeypatch.setattr(collection.metadata, 'distribution', missing)
    assert not collection._installed_base_identity(installed.package)
