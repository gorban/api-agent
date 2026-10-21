"""The source archive admits explicit public paths, even with local files present."""

from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import json


_VIEWER_FILES = (
    'viewer.js', 'viewer.css', 'marked.js', 'dompurify.js',
    'marked-LICENSE.txt', 'dompurify-LICENSE.txt', 'PlantUML-NOTICE.txt',
    'VIEWER-ASSET-NOTICE.txt', 'heading-id.js', 'heading-id-LICENSE.txt',
    'github-slugger-LICENSE.txt', 'svg-links.js',
)
_DIAGRAM_FILES = ('flow.svg', 'flow.md', 'workflows/components/agent.svg',
                  'workflows/components/agent.md',
                  'workflows/components/agent/llm_call.svg',
                  'workflows/components/agent/llm_call.md')


def test_sdist_excludes_untracked_private_and_cache_sentinels(tmp_path):
    root = Path(__file__).resolve().parents[2]
    staged = tmp_path / 'source'
    staged.mkdir()
    shutil.copyfile(root / 'pyproject.toml', staged / 'pyproject.toml')
    shutil.copyfile(root / 'hatch_build.py', staged / 'hatch_build.py')
    for relative in ('package.json', 'package-lock.json', '.npmrc'):
        shutil.copyfile(root / relative, staged / relative)
    for entry in json.loads((root / 'tools/frontend-assets.json').read_text())['assets']:
        path = staged / entry['target']
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / entry['target'], path)
    (staged / 'LICENSE').write_text('Synthetic license for archive selection test.\n')
    (staged / 'README.md').write_text('Synthetic package.\n')
    (staged / 'flow.md.html').write_text('# Workflow v2\n')
    (staged / 'src/api_agent').mkdir(parents=True)
    (staged / 'src/api_agent/__init__.py').write_text('"""Synthetic package."""\n')
    (staged / 'demo').mkdir()
    (staged / 'demo/pack.toml').write_text('version=1\n')
    (staged / 'workflows').mkdir()
    (staged / 'workflows/component.md.html').write_text('# Component v2\n')
    (staged / 'tests').mkdir()
    (staged / 'tests/test_public.py').write_text('def test_public(): pass\n')
    admitted_hidden = {'.npmrc',
        *('.private_resources/viewer/' + name for name in _VIEWER_FILES),
        *('.private_resources/diagrams/' + name for name in _DIAGRAM_FILES),
    }
    for relative in admitted_hidden:
        path = staged / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, path)
    admitted_tools = {'tools/checks.py', 'tools/pre_commit_dispatcher.py',
                      'tools/workflow_views.py', 'tools/frontend-assets.json',
                      'tools/prepare_frontend_assets.mjs', 'tools/frontend_assets.py'}
    for relative in admitted_tools:
        path = staged / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, path)
    sentinels = (
        '.synthetic-private/secret.txt',
        'src/api_agent/.synthetic-cache/secret.txt',
        'tests/.synthetic-private/secret.txt',
        '.private_resources/viewer/secret.txt',
        '.private_resources/diagrams/secret.txt',
        '.private_resources/runtime/secret.txt',
        'tools/synthetic-review.txt',
        'tools/runtime/debug.log',
        'unlisted.txt',
        'uv.lock',
    )
    for relative in sentinels:
        path = staged / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic sentinel\n')
    output = tmp_path / 'dist'

    subprocess.run([sys.executable, '-I', '-m', 'build', '--no-isolation', '--sdist',
                    '--outdir', str(output), str(staged)], check=True, capture_output=True, text=True)

    archive, = output.glob('*.tar.gz')
    with tarfile.open(archive) as source:
        members = {name.split('/', 1)[1] for name in source.getnames() if '/' in name}
    assert {'src/api_agent/__init__.py', 'demo/pack.toml', 'flow.md.html',
            'workflows/component.md.html', 'tests/test_public.py'} <= members
    hidden = {name for name in members if any(part.startswith('.') for part in name.split('/'))}
    assert hidden == admitted_hidden
    assert {name for name in members if name.startswith('tools/')} == admitted_tools
    assert not any(name.endswith('secret.txt') or name in {'unlisted.txt', 'uv.lock'} for name in members)
