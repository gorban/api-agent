"""Index checks prepare only staged-contract verified npm originals."""
import json
from pathlib import Path
import shutil

import pytest

from tools.frontend_assets import prepare_snapshot


@pytest.fixture
def inputs(tmp_path):
    source = Path(__file__).resolve().parents[2]
    snapshot = tmp_path / 'index'
    installed = tmp_path / 'working'
    snapshot.mkdir()
    installed.mkdir()
    for relative in ('tools/frontend-assets.json', 'tools/frontend_assets.py', 'package.json', 'package-lock.json'):
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    manifest = json.loads((snapshot / 'tools/frontend-assets.json').read_text())
    for entry in manifest['assets']:
        for relative in ('package.json', entry['source']):
            path = Path('node_modules') / entry['package'] / relative
            target = installed / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / path, target)
    return snapshot, installed, manifest


def test_snapshot_ignores_dirty_working_contracts_and_generated_assets(inputs):
    snapshot, installed, manifest = inputs
    for relative in ('tools/frontend-assets.json', 'package.json', 'package-lock.json',
                     '.private_resources/viewer/marked.js'):
        target = installed / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('untrusted working bytes')
    prepare_snapshot(snapshot, installed)
    for entry in manifest['assets']:
        original = installed / 'node_modules' / entry['package'] / entry['source']
        assert (snapshot / entry['target']).read_bytes() == original.read_bytes()
    prepare_snapshot(snapshot, installed)


@pytest.mark.parametrize('change', ['missing', 'bytes', 'identity', 'symlink', 'fifo'])
def test_unprepared_or_changed_originals_cannot_enter_snapshot(inputs, change):
    snapshot, installed, manifest = inputs
    target = installed / 'node_modules/marked/lib/marked.umd.js'
    if change == 'identity':
        target = installed / 'node_modules/marked/package.json'
        target.write_text('{"name":"marked","version":"0.0.0"}')
    else:
        target.unlink()
        if change == 'bytes':
            target.write_text('unknown original')
        elif change == 'symlink':
            target.symlink_to(installed / 'node_modules/dompurify/package.json')
        elif change == 'fifo':
            import os
            os.mkfifo(target)
    with pytest.raises(RuntimeError, match='npm ci'):
        prepare_snapshot(snapshot, installed)
    assert not (snapshot / manifest['assets'][0]['target']).exists()


@pytest.mark.parametrize('field,value', [
    ('source', '../../package.json'), ('target', '../outside.js'),
    ('sha256', 'invalid'), ('size', 0), ('package', '../marked'), ('version', 'next'),
])
def test_staged_manifest_bounds_are_authoritative(inputs, field, value):
    snapshot, installed, manifest = inputs
    manifest['assets'][0][field] = value
    (snapshot / 'tools/frontend-assets.json').write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match='npm ci'):
        prepare_snapshot(snapshot, installed)
    assert not (snapshot.parent / 'outside.js').exists()


def test_index_existing_unknown_bytes_are_preserved(inputs):
    snapshot, installed, manifest = inputs
    target = snapshot / manifest['assets'][0]['target']
    target.parent.mkdir(parents=True)
    target.write_text('unknown staged bytes')
    with pytest.raises(RuntimeError, match='npm ci'):
        prepare_snapshot(snapshot, installed)
    assert target.read_text() == 'unknown staged bytes'


def test_build_validation_uses_prepared_bytes_without_node_or_npm(inputs, monkeypatch):
    import runpy

    snapshot, installed, manifest = inputs
    prepare_snapshot(snapshot, installed)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'hatch_build.py'))
    monkeypatch.setenv('PATH', '')
    namespace['_validate_frontend_assets'](snapshot)
    for entry in manifest['assets']:
        target = snapshot / entry['target']
        original = target.read_bytes()
        target.write_bytes(b'changed prepared output')
        with pytest.raises(RuntimeError, match='run npm ci'):
            namespace['_validate_frontend_assets'](snapshot)
        target.write_bytes(original)
    target.unlink()
    with pytest.raises(RuntimeError, match='run npm ci'):
        namespace['_validate_frontend_assets'](snapshot)


def declare_nested_owner(snapshot):
    entry = snapshot / 'nested/flow.md.html'
    entry.parent.mkdir()
    entry.write_text('# Workflow v2\n')
    context = {'version': 1, 'entries': {'nested/flow.md.html': {
        'base_root': 'nested', 'pack_root': 'nested', 'entry_root': 'base'}}}
    target = snapshot / 'cfg/workflow-view-roots.json'
    target.parent.mkdir()
    target.write_text(json.dumps(context))
    return target, context


def test_snapshot_hydrates_only_explicit_staged_owners_from_official_originals(inputs):
    snapshot, installed, manifest = inputs
    declare_nested_owner(snapshot)
    working = installed / 'cfg/workflow-view-roots.json'
    working.parent.mkdir()
    working.write_text('{"version":1,"entries":{"outside/flow.md.html":{}}}')

    prepare_snapshot(snapshot, installed)

    viewer_entries = [entry for entry in manifest['assets']
                      if entry['target'].startswith('.private_resources/viewer/')]
    assert len(viewer_entries) == 7
    for entry in viewer_entries:
        assert (snapshot / 'nested' / entry['target']).read_bytes() == (
            installed / 'node_modules' / entry['package'] / entry['source']).read_bytes()
    assert not (snapshot / 'outside').exists()


@pytest.mark.parametrize('change', [
    'duplicate', 'version', 'unknown', 'escape', 'missing', 'symlink',
    'outside-entry', 'redundant', 'entry-link', 'fifo',
])
def test_invalid_staged_owner_contexts_reject_before_copy(inputs, change):
    import os

    snapshot, installed, manifest = inputs
    target, context = declare_nested_owner(snapshot)
    row = context['entries']['nested/flow.md.html']
    if change == 'duplicate':
        target.write_text('{"version":1,"version":1,"entries":{}}')
    elif change == 'version':
        context['version'] = True
    elif change == 'unknown':
        row['unknown'] = '.'
    elif change == 'escape':
        row['base_root'] = '../outside'
    elif change == 'missing':
        (snapshot / 'nested/flow.md.html').unlink()
    elif change == 'symlink':
        (snapshot / 'alias').symlink_to(snapshot / 'nested', target_is_directory=True)
        row['pack_root'] = 'alias'
    elif change == 'outside-entry':
        (snapshot / 'other').mkdir()
        row['base_root'] = 'other'
    elif change == 'redundant':
        row.update(base_root='.', pack_root='.')
    elif change == 'entry-link':
        entry = snapshot / 'nested/flow.md.html'
        entry.unlink()
        entry.symlink_to(target)
    elif change == 'fifo':
        target.unlink()
        os.mkfifo(target)
    if change not in ('duplicate', 'fifo'):
        target.write_text(json.dumps(context))

    with pytest.raises(RuntimeError, match='npm ci'):
        prepare_snapshot(snapshot, installed)
    assert not (snapshot / manifest['assets'][0]['target']).exists()


def test_nested_unknown_output_is_preserved_before_root_copy(inputs):
    snapshot, installed, manifest = inputs
    declare_nested_owner(snapshot)
    target = snapshot / 'nested/.private_resources/viewer/marked.js'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'owned local edit')

    with pytest.raises(RuntimeError, match='npm ci'):
        prepare_snapshot(snapshot, installed)

    assert target.read_bytes() == b'owned local edit'
    assert not (snapshot / manifest['assets'][0]['target']).exists()


@pytest.mark.parametrize('owner', ['.hidden', 'node_modules', 'build', 'foreign'])
def test_dependency_hidden_and_foreign_owners_are_not_hydration_targets(inputs, owner):
    snapshot, installed, manifest = inputs
    target, context = declare_nested_owner(snapshot)
    location = snapshot / owner
    location.mkdir()
    if owner == 'foreign':
        (location / '.git').write_text('gitdir: unrelated\n')
    context['entries']['nested/flow.md.html']['pack_root'] = owner
    target.write_text(json.dumps(context))

    with pytest.raises(RuntimeError, match='npm ci'):
        prepare_snapshot(snapshot, installed)

    assert not (snapshot / manifest['assets'][0]['target']).exists()


@pytest.mark.parametrize('owner', ['nested?owner', 'nested#owner'])
def test_owner_context_rejects_url_query_and_fragment_names(inputs, owner):
    snapshot, installed, _ = inputs
    target, context = declare_nested_owner(snapshot)
    (snapshot / owner).mkdir()
    context['entries']['nested/flow.md.html']['pack_root'] = owner
    target.write_text(json.dumps(context))

    with pytest.raises(RuntimeError, match='npm ci'):
        prepare_snapshot(snapshot, installed)
