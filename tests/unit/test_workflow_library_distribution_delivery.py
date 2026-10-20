"""Real filesystem distribution discovery controls; no package import."""
from importlib.metadata import PathDistribution
from pathlib import Path
from unittest.mock import patch
import pytest
from api_agent.runtime.resource_pack import _workflow_library, ResourcePackError


def installed(root):
    (root / 'bin').mkdir(parents=True)
    (root / 'bin/example').write_text('#!/usr/bin/env python\n')
    site = root / 'lib/python3.13/site-packages'
    metadata = site / 'example_library-0.1.0.dist-info'
    metadata.mkdir(parents=True)
    (metadata / 'METADATA').write_text('Metadata-Version: 2.1\nName: example-library\nVersion: 0.1.0\n')
    resource = site / 'example_library/workflows'
    resource.mkdir(parents=True)
    (resource / 'graph.json').write_text('{}')
    (metadata / 'RECORD').write_text('example_library/workflows/graph.json,,\nexample_library-0.1.0.dist-info/METADATA,,\nexample_library-0.1.0.dist-info/RECORD,,\n../../../bin/example,,\n')
    return metadata


def resolve(tmp_path, distributions):
    pack = tmp_path / 'pack'
    pack.mkdir(exist_ok=True)
    with patch('api_agent.runtime.resource_pack.metadata.distributions', return_value=distributions):
        return _workflow_library(pack, {'source': '../absent', 'distribution': 'example-library',
                                      'resource': 'example_library/workflows'})


def test_physical_alias_and_console_script_are_admitted(tmp_path):
    metadata = installed(tmp_path / 'venv')
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path / 'venv', target_is_directory=True)
    other = alias / metadata.relative_to(tmp_path / 'venv')
    root, files = resolve(tmp_path, [PathDistribution(metadata), PathDistribution(other)])
    assert root == metadata.parent / 'example_library/workflows'
    assert files == frozenset({'graph.json'})


def test_distinct_installs_are_rejected(tmp_path):
    first = installed(tmp_path / 'one')
    second = installed(tmp_path / 'two')
    with pytest.raises(ResourcePackError, match='exactly one'):
        resolve(tmp_path, [PathDistribution(first), PathDistribution(second)])


def test_unverifiable_alias_is_rejected(tmp_path):
    metadata = installed(tmp_path / 'venv')
    (metadata / 'RECORD').unlink()
    with pytest.raises(ResourcePackError, match='exactly one'):
        resolve(tmp_path, [PathDistribution(metadata), PathDistribution(metadata)])


@pytest.mark.parametrize('inventory', [
    'example_library/workflows/../graph.json,,\n',
    '/example_library/workflows/graph.json,,\n',
])
def test_alias_inventory_conflict_rejects(tmp_path, inventory):
    metadata = installed(tmp_path / 'venv')
    first = PathDistribution(metadata)
    class Conflicting(PathDistribution):
        def read_text(self, filename):
            text = super().read_text(filename)
            return text + inventory if filename == 'RECORD' else text
    with pytest.raises(ResourcePackError, match='exactly one'):
        resolve(tmp_path, [first, Conflicting(metadata)])


def test_missing_physical_alias_record_rejects(tmp_path):
    metadata = installed(tmp_path / 'venv')
    class Missing(PathDistribution):
        def locate_file(self, item):
            path = super().locate_file(item)
            return path / 'missing' if str(item).endswith('/RECORD') else path
    with pytest.raises(ResourcePackError, match='exactly one'):
        resolve(tmp_path, [Missing(metadata), Missing(metadata)])


def test_independently_authored_wheel_inventory(tmp_path):
    import zipfile
    wheel = tmp_path / 'example_library-0.1.0-py3-none-any.whl'
    entries = {
        'example_library/workflows/graph.json': '{}',
        'example_library-0.1.0.dist-info/METADATA': 'Metadata-Version: 2.1\nName: example-library\nVersion: 0.1.0\n',
        'example_library-0.1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nGenerator: synthetic-control\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        'example_library-0.1.0.dist-info/entry_points.txt': '[console_scripts]\nexample = example_library:main\n',
    }
    entries['example_library-0.1.0.dist-info/RECORD'] = ''.join(f'{name},,\n' for name in entries) + 'example_library-0.1.0.dist-info/RECORD,,\n'
    with zipfile.ZipFile(wheel, 'w') as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    site = tmp_path / 'site'
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    root, files = resolve(tmp_path, [PathDistribution(site / 'example_library-0.1.0.dist-info')])
    assert root == site / 'example_library/workflows'
    assert files == frozenset({'graph.json'})


def test_record_cannot_claim_another_metadata_provider_identity(tmp_path):
    other = installed(tmp_path / 'venv')
    shared_record = (other / 'RECORD').read_text()
    impostors = []
    for name in ('first-0.1.0.dist-info', 'second-0.1.0.dist-info'):
        provider = other.parent / name
        provider.mkdir()
        (provider / 'METADATA').write_text((other / 'METADATA').read_text())
        (provider / 'RECORD').write_text(shared_record)
        impostors.append(PathDistribution(provider))
    with pytest.raises(ResourcePackError, match='exactly one'):
        resolve(tmp_path, impostors)


def test_distinct_metadata_roots_with_hardlinked_inventory_reject(tmp_path):
    import os
    other = installed(tmp_path / 'venv')
    provider = other.parent / 'second-0.1.0.dist-info'
    provider.mkdir()
    os.link(other / 'METADATA', provider / 'METADATA')
    os.link(other / 'RECORD', provider / 'RECORD')
    with pytest.raises(ResourcePackError, match='exactly one'):
        resolve(tmp_path, [PathDistribution(other), PathDistribution(provider)])


def test_repeated_provider_cannot_substitute_claimed_metadata_files(tmp_path):
    other = installed(tmp_path / 'venv')
    provider = other.parent / 'impostor-0.1.0.dist-info'
    provider.mkdir()
    (provider / 'METADATA').write_text((other / 'METADATA').read_text())
    (provider / 'RECORD').write_text((other / 'RECORD').read_text())
    with pytest.raises(ResourcePackError, match='exactly one'):
        resolve(tmp_path, [PathDistribution(provider), PathDistribution(provider)])
