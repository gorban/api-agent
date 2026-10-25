"""Pack tooling discovery rejects unsafe catalogs before executable selection."""
import pytest

from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context


def pack_at(root, tooling):
    (root.parent / 'base').mkdir(exist_ok=True)
    root.mkdir()
    (root / 'config.toml').write_text('version=1\n')
    (root / 'pack.toml').write_text('version=1\nid="synthetic"\nconfig="config.toml"\n' + tooling)
    return root


@pytest.mark.parametrize('tooling', [
    'tooling="catalog.toml"\n',
    '[tooling]\nversion=1\n',
    '[tooling]\nversion=true\ncatalog="catalog.toml"\n',
    '[tooling]\nversion=2\ncatalog="catalog.toml"\n',
    '[tooling]\nversion=1\ncatalog=4\n',
    '[tooling]\nversion=1\ncatalog=""\n',
    '[tooling]\nversion=1\ncatalog="/catalog.toml"\n',
    '[tooling]\nversion=1\ncatalog="../catalog.toml"\n',
    "[tooling]\nversion=1\ncatalog='folder\\catalog.toml'\n",
    '[tooling]\nversion=1\ncatalog="catalog.json"\n',
    '[tooling]\nversion=1\ncatalog="catalog.toml"\nextra=true\n',
])
def test_invalid_tooling_manifest_cannot_admit_existing_catalog(tmp_path, tooling):
    pack = pack_at(tmp_path / 'pack', tooling)
    (pack / 'catalog.toml').write_text('version=1\n')
    with pytest.raises(ResourcePackError, match='tooling requires version 1'):
        load_launch_context(tmp_path / 'base', pack)


@pytest.mark.parametrize('kind', ['missing', 'directory', 'file-symlink', 'parent-symlink', 'fifo', 'oversize'])
def test_catalog_must_be_bounded_regular_file_beneath_pack(tmp_path, kind):
    relative = 'alias/catalog.toml' if kind == 'parent-symlink' else 'catalog.toml'
    pack = pack_at(tmp_path / 'pack', '[tooling]\nversion=1\ncatalog="' + relative + '"\n')
    catalog = pack / relative
    if kind == 'directory':
        catalog.mkdir()
    elif kind in {'file-symlink', 'parent-symlink'}:
        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'catalog.toml').write_text('version=1\n')
        if kind == 'file-symlink':
            catalog.symlink_to(outside / 'catalog.toml')
        else:
            (pack / 'alias').symlink_to(outside, target_is_directory=True)
    elif kind == 'fifo':
        import os
        os.mkfifo(catalog)
    elif kind == 'oversize':
        with catalog.open('wb') as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(ResourcePackError, match='Tool catalog must be a regular file inside its pack'):
        load_launch_context(tmp_path / 'base', pack)
