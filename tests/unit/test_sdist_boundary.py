"""The source archive admits explicit public paths, even with local files present."""

from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


def test_sdist_excludes_untracked_private_and_cache_sentinels(tmp_path):
    root = Path(__file__).resolve().parents[2]
    staged = tmp_path / 'source'
    staged.mkdir()
    shutil.copyfile(root / 'pyproject.toml', staged / 'pyproject.toml')
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
    sentinels = (
        '.synthetic-private/secret.txt',
        'src/api_agent/.synthetic-cache/secret.txt',
        'tests/.synthetic-private/secret.txt',
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
    assert not any(any(part.startswith('.') for part in name.split('/')) for name in members)
    assert not any(name.endswith('secret.txt') or name in {'unlisted.txt', 'uv.lock'} for name in members)
