"""Packaging batches immutable Git reads without weakening source admission."""
import subprocess

import pytest

import hatch_build as build


@pytest.fixture
def workflow_source(tmp_path, monkeypatch):
    import os
    for name in tuple(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    for name, value in {'GIT_AUTHOR_NAME': 'Test Author',
                        'GIT_COMMITTER_NAME': 'Test Author',
                        'GIT_AUTHOR_EMAIL': 'test@example.invalid',
                        'GIT_COMMITTER_EMAIL': 'test@example.invalid',
                        'GIT_CONFIG_GLOBAL': os.devnull,
                        'GIT_CONFIG_SYSTEM': os.devnull}.items():
        monkeypatch.setenv(name, value)
    directory = tmp_path / 'workflows' / 'nested'
    directory.mkdir(parents=True)
    for index in range(12):
        (directory / f'part{index}.md.html').write_text(f'# Step {index}\n')
    for arguments in [('init', '-q'), ('add', '.'),
                      ('-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=' + os.devnull,
                       'commit', '-qm', 'Synthetic workflow source')]:
        subprocess.run(['git', '-C', str(tmp_path), *arguments], check=True,
                       capture_output=True, timeout=10)
    return tmp_path


def test_proof_reads_shared_trees_once_and_batches_committed_bytes(workflow_source, monkeypatch):
    calls = []
    original = build._git

    def observed(root, *arguments, **kwargs):
        calls.append(arguments)
        return original(root, *arguments, **kwargs)

    monkeypatch.setattr(build, '_git', observed)
    proof = build._proof(workflow_source)

    assert len(proof['manifest']['documents']) == 12
    assert len(calls) == 10  # Fixed discovery, three shared trees, one blob batch.
    assert calls.count(('cat-file', '--batch')) == 1
    assert len([row for row in calls if row[:2] == ('cat-file', 'tree')]) == 3
    assert calls[-1] == ('rev-parse', 'HEAD')


@pytest.mark.parametrize('change', ['working_bytes', 'staged_bytes'])
def test_proof_rejects_changed_source_bytes(workflow_source, change):
    path = workflow_source / 'workflows/nested/part0.md.html'
    original = path.read_bytes()
    path.write_text('Changed source\n')
    if change == 'staged_bytes':
        subprocess.run(['git', '-C', str(workflow_source), 'add', '.'],
                       check=True, capture_output=True, timeout=10)
        path.write_bytes(original)

    with pytest.raises(RuntimeError, match='differs from committed source'):
        build._proof(workflow_source)


@pytest.mark.parametrize('change', ['header', 'size', 'truncated', 'separator', 'surplus'])
def test_proof_rejects_invalid_blob_batch(workflow_source, monkeypatch, change):
    original = build._git

    def changed(root, *arguments, **kwargs):
        body = original(root, *arguments, **kwargs)
        if arguments != ('cat-file', '--batch'):
            return body
        header, _, content = body.partition(b'\n')
        if change == 'header':
            return b'unknown missing\n'
        if change == 'size':
            return header.rsplit(b' ', 1)[0] + b' invalid\n' + content
        if change == 'truncated':
            return header + b'\n'
        if change == 'separator':
            length = int(header.rsplit(b' ', 1)[1])
            return header + b'\n' + content[:length] + b'X' + content[length + 1:]
        return body + b'extra'

    monkeypatch.setattr(build, '_git', changed)
    with pytest.raises(RuntimeError, match='Git blob response'):
        build._proof(workflow_source)


def test_proof_rechecks_live_head_after_cached_object_reads(workflow_source, monkeypatch):
    original = build._git
    observations = []

    def moved(root, *arguments, **kwargs):
        body = original(root, *arguments, **kwargs)
        if arguments == ('rev-parse', 'HEAD'):
            observations.append(body)
            if len(observations) == 2:
                return b'0' * 40 + b'\n'
        return body

    monkeypatch.setattr(build, '_git', moved)
    with pytest.raises(RuntimeError, match='changed during build'):
        build._proof(workflow_source)
