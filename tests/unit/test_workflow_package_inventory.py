"""Distributions preserve finite owner artifacts without importing local files."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def git(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments])


@pytest.fixture
def packaged_owner(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    for relative in ('tools/frontend_assets.py', 'tools/frontend-assets.json',
                     'package.json', 'package-lock.json'):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    manifest = json.loads((root / 'tools/frontend-assets.json').read_text())
    for entry in manifest['assets']:
        target = root / entry['target']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / entry['target'], target)
        if entry['target'].startswith('.private_resources/viewer/'):
            target = root / 'tests/integration/fixture' / entry['target']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / entry['target'], target)
    for name in ('viewer.js', 'viewer.css', 'svg-links.js', 'PlantUML-NOTICE.txt',
                 'VIEWER-ASSET-NOTICE.txt'):
        for owner in ('', 'tests/integration/fixture/'):
            target = root / (owner + '.private_resources/viewer/' + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / '.private_resources/viewer' / name, target)
    for relative, body in {
        'tests/integration/fixture/flow.md.html': '# Workflow v2\n',
        'tests/integration/fixture/nodes/step.md.html': '# Node v2: step\n',
        'tests/integration/fixture/cfg/workflow-view.toml': 'version=1\n',
        'tests/integration/fixture/.private_resources/diagrams/flow.svg': '<svg/>',
        'tests/integration/fixture/.private_resources/viewer/viewer.js': '// first party',
    }.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    context = root / 'cfg/workflow-view-roots.json'
    context.parent.mkdir()
    context.write_text(json.dumps({'version': 1, 'entries': {
        'tests/integration/fixture/flow.md.html': {'base_root': 'tests/integration/fixture', 'pack_root': 'tests/integration/fixture',
                               'entry_root': 'base'}}}))
    (root / '.gitignore').write_text('**/.private_resources/viewer/*.js\n')
    git(root, 'init', '-q')
    git(root, 'add', '.')
    git(root, 'add', '-f', 'tests/integration/fixture/.private_resources/viewer/viewer.js',
        'tests/integration/fixture/.private_resources/viewer/svg-links.js',
        '.private_resources/viewer/viewer.js', '.private_resources/viewer/svg-links.js')
    git(root, '-c', 'user.name=Test Author', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'Create independent package fixture')
    namespace = runpy.run_path(str(ROOT / 'hatch_build.py'))
    return root, namespace


def test_git_inventory_is_exact_and_survives_relocated_no_git_rebuild(packaged_owner):
    root, build = packaged_owner
    unknown = root / 'tests/integration/fixture/private-sentinel.txt'
    unknown.write_text('untracked human file')
    diagram = root / 'tests/integration/fixture/.private_resources/diagrams/unknown.svg'
    diagram.write_text('untracked graphic')

    inventory = build['_package_inventory'](root, True)

    paths = {row['path'] for row in inventory['files']}
    assert {'tests/integration/fixture/flow.md.html', 'tests/integration/fixture/nodes/step.md.html',
            'tests/integration/fixture/cfg/workflow-view.toml',
            'tests/integration/fixture/.private_resources/diagrams/flow.svg'} <= paths
    assert sum('/.private_resources/viewer/' in '/' + path for path in paths) == 24
    assert 'tests/integration/fixture/private-sentinel.txt' not in paths
    assert 'tests/integration/fixture/.private_resources/diagrams/unknown.svg' not in paths
    (root / 'workflow-package-files.json').write_text(json.dumps(inventory))
    shutil.rmtree(root / '.git')
    assert build['_package_inventory'](root, False) == inventory


@pytest.mark.parametrize('change', ['bytes', 'mode', 'duplicate', 'unknown-viewer',
                                    'escape', 'missing-original', 'symlink', 'missing-sidecar'])
def test_no_git_inventory_rejects_changed_or_unbounded_payload(packaged_owner, change):
    root, build = packaged_owner
    inventory = build['_package_inventory'](root, True)
    sidecar = root / 'workflow-package-files.json'
    target = root / 'tests/integration/fixture/flow.md.html'
    if change == 'bytes':
        target.write_text('changed source')
    elif change == 'mode':
        target.chmod(0o755)
    elif change == 'duplicate':
        inventory['files'].append(inventory['files'][0])
    elif change == 'unknown-viewer':
        inventory['files'][0]['path'] = 'tests/integration/fixture/.private_resources/viewer/secret.txt'
    elif change == 'escape':
        inventory['files'][0]['path'] = '../outside'
    elif change == 'missing-original':
        inventory['files'] = [row for row in inventory['files']
                              if row['path'] != 'tests/integration/fixture/.private_resources/viewer/marked.js']
    elif change == 'symlink':
        target.unlink()
        target.symlink_to(root / 'package.json')
    sidecar.write_text(json.dumps(inventory))
    if change == 'missing-sidecar':
        sidecar.unlink()

    with pytest.raises((RuntimeError, ValueError, OSError)):
        build['_package_inventory'](root, False)


@pytest.mark.parametrize('owner', [
    'tests/integration/fixture', 'demo/nested', 'tests/integration/fixture[one]',
    'tests/integration/fixture*one', 'tests/integration/fixtureé',
    'tests/integration/fixture"one',
])
def test_owner_artifacts_and_originals_ship_in_sdist_and_rebuilt_wheel(packaged_owner, tmp_path, owner):
    import sys
    import tarfile
    import tomllib
    import zipfile

    root, _ = packaged_owner
    if owner != 'tests/integration/fixture':
        (root / owner).parent.mkdir(parents=True, exist_ok=True)
        (root / 'tests/integration/fixture').rename(root / owner)
        context = {'version': 1, 'entries': {owner + '/flow.md.html': {
            'base_root': '.', 'pack_root': owner, 'entry_root': 'pack'}}}
        (root / 'cfg/workflow-view-roots.json').write_text(json.dumps(context))
    configuration = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    explicit = configuration['tool']['hatch']['build']['targets']['sdist']['force-include']
    for relative in (*explicit, 'pyproject.toml', 'hatch_build.py'):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    for relative, text in {
        'LICENSE': 'Independent package fixture license.\n',
        'README.md': 'Independent package fixture.\n',
        'flow.md.html': '# Workflow v2\n',
        'src/api_agent/__init__.py': '"""Independent package fixture."""\n',
        'demo/placeholder.txt': 'Public example fixture.\n',
        'workflows/placeholder.txt': 'Public example fixture.\n',
        'infra/compose/placeholder.txt': 'Public example fixture.\n',
        'tests/integration/fixtureo/public.txt': 'Tracked literal sibling.\n',
        'tests/integration/fixtureo/flow.md.html': '# Workflow v2\n',
        'tests/integration/fixtureXone/public.txt': 'Tracked literal sibling.\n',
        'tests/integration/fixtureXone/flow.md.html': '# Workflow v2\n',
    }.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git(root, 'add', '.')
    git(root, 'add', '-f', owner + '/.private_resources/viewer/viewer.js',
        owner + '/.private_resources/viewer/svg-links.js')
    git(root, '-c', 'user.name=Test Author', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'Prepare independent source archive fixture')
    (root / owner / 'private-sentinel.txt').write_text('untracked human bytes')
    if owner == 'demo/nested':
        (root / 'demo/ancestor-private.txt').write_text('untracked ancestor bytes')
    output = tmp_path / 'dist'

    subprocess.run([sys.executable, '-I', '-m', 'build', '--no-isolation', '--sdist',
                    '--outdir', str(output), str(root)], check=True, capture_output=True)
    archive, = output.glob('*.tar.gz')
    extracted = tmp_path / 'extracted'
    extracted.mkdir()
    with tarfile.open(archive) as source:
        members = {name.split('/', 1)[1] for name in source.getnames() if '/' in name}
        assert 'workflow-package-files.json' in members
        assert owner + '/private-sentinel.txt' not in members
        if owner == 'demo/nested':
            assert 'demo/ancestor-private.txt' not in members
        assert 'demo/placeholder.txt' in members
        assert 'tests/integration/fixtureo/public.txt' in members
        assert 'tests/integration/fixtureXone/public.txt' in members
        source.extractall(extracted, filter='data')
    rebuilt, = extracted.iterdir()
    subprocess.run([sys.executable, '-I', '-m', 'build', '--no-isolation', '--wheel',
                    '--outdir', str(output), str(rebuilt)], check=True, capture_output=True)
    wheel, = output.glob('*.whl')
    with zipfile.ZipFile(wheel) as installed:
        assert 'api_agent/' + owner + '/private-sentinel.txt' not in installed.namelist()
        assert 'api_agent/demo/ancestor-private.txt' not in installed.namelist()
        assert installed.read('api_agent/demo/placeholder.txt') == b'Public example fixture.\n'
        for suffix in ('flow.md.html', 'cfg/workflow-view.toml',
                       '.private_resources/diagrams/flow.svg',
                       '.private_resources/viewer/marked.js',
                       '.private_resources/viewer/heading-id-LICENSE.txt'):
            relative = owner + '/' + suffix
            assert installed.read('api_agent/' + relative) == (root / relative).read_bytes()
        destination = tmp_path / 'installed'
        installed.extractall(destination)
    from types import SimpleNamespace
    from api_agent.workflows.definition import LayoutError
    from api_agent.workflows.packaged_provenance import installed_proof

    owned = destination / 'api_agent' / owner
    entry = owned / 'flow.md.html'
    selected = SimpleNamespace(roots={'base': owned, 'pack': owned},
                               contents={entry: entry.read_bytes()}, resources=())
    manifest = installed_proof(selected)
    assert manifest['owners'][0]['commit'] == git(root, 'rev-parse', 'HEAD').decode().strip()
    assert manifest['owners'][0]['source_prefix'] == owner
    receipt = owned / 'workflow-source-proof.json'
    original = receipt.read_bytes()
    receipt.unlink()
    with pytest.raises(LayoutError, match='packaged source proof'):
        installed_proof(selected)
    receipt.write_bytes(original)
    selected.contents[entry] += b'changed selected bytes'
    with pytest.raises(LayoutError, match='bytes do not match receipt'):
        installed_proof(selected)


def test_explicit_owner_replaces_broad_directory_includes(packaged_owner):
    from types import SimpleNamespace

    root, build = packaged_owner
    config = SimpleNamespace(exclude_spec=None, force_include={
        str(root / 'tests/integration/fixture'): 'api_agent/tests/integration/fixture'})

    build['_limit_owner_traversal'](config, root, ['.', 'tests/integration/fixture'])

    assert not config.force_include
    assert config.exclude_spec.match_file('tests/integration/fixture/private-sentinel.txt')
    assert not config.exclude_spec.match_file('tests/unit/test_public.py')


def test_ancestor_include_keeps_configured_destination_without_broad_traversal(packaged_owner):
    from types import SimpleNamespace

    root, build = packaged_owner
    ancestor = root / 'tests'
    config = SimpleNamespace(exclude_spec=None, force_include={str(ancestor): 'api_agent/examples'})
    directories = {'tests': 'api_agent/examples'}

    build['_limit_owner_traversal'](config, root, ['.', 'tests/integration/fixture'], directories)

    assert not config.force_include
    assert config.exclude_spec.match_file('tests/untracked-sibling.txt')
    assert build['_wheel_destination']('tests/integration/fixture/flow.md.html', directories) == (
        'api_agent/examples/integration/fixture/flow.md.html')
    assert build['_wheel_destination']('tests/sibling.txt', directories) == 'api_agent/examples/sibling.txt'


@pytest.mark.parametrize('name', ['fixture[one]', 'fixture*one', 'fixture?one',
                                 'fixture!#one', 'fixture ', 'fixture]one['])
def test_owner_exclusions_are_literal_and_keep_exact_sibling_boundaries(packaged_owner, name):
    from types import SimpleNamespace
    from pathspec import GitIgnoreSpec

    root, build = packaged_owner
    prefix = 'tests/integration/' + name
    config = SimpleNamespace(exclude_spec=GitIgnoreSpec.from_lines(['*.log']), force_include={})

    build['_limit_owner_traversal'](config, root, ['.', prefix])

    assert config.exclude_spec.match_file(prefix + '/private-sentinel.txt')
    assert config.exclude_spec.match_file(prefix)
    assert not config.exclude_spec.match_file(prefix + '-sibling/public.txt')
    assert not config.exclude_spec.match_file('tests/integration/fixtureo/public.txt')
    assert config.exclude_spec.match_file('unrelated/debug.log')


@pytest.mark.parametrize('change', ['hash', 'version', 'owner', 'shape'])
def test_owner_receipts_cannot_rebase_an_invalid_parent_manifest(packaged_owner, change):
    root, build = packaged_owner
    proof = build['_proof'](root)
    if change == 'hash':
        proof['manifest']['sha256'] = '0' * 64
    elif change == 'version':
        proof['version'] = True
    elif change == 'owner':
        proof['manifest']['owners'][0]['owner'] = 'pack'
    else:
        proof['extra'] = 'unknown'

    with pytest.raises(RuntimeError, match='Invalid packaged source'):
        build['_owner_proofs'](proof, ['.', 'tests/integration/fixture'])
