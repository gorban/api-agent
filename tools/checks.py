"""Read-only omnibus checks for a complete indexed or CI-supplied snapshot."""
import argparse
import hashlib
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


def check(root: Path, frontend_source: Path | None = None) -> None:
    root = root.resolve()
    if not (root / 'tools/workflow_views.py').is_file():
        raise ValueError('Workflow view checker is missing from snapshot')
    sys.path.insert(0, str(root / 'src'))
    sys.path.insert(0, str(root))
    if frontend_source is not None and (root / 'tools/frontend-assets.json').is_file():
        from tools.frontend_assets import prepare_snapshot
        prepare_snapshot(root, frontend_source.resolve())
    from api_agent.workflows.viewer_build import check_views
    check_views(root)
    if frontend_source is not None:
        from api_agent.workflows.viewer_build import check_staged_workflow_matches_worktree
        check_staged_workflow_matches_worktree(root, frontend_source.resolve())


def check_head(root: Path) -> bool:
    """Compare checked workflow files with HEAD when Git metadata exists."""
    root = root.resolve()
    executable = shutil.which('git')
    if executable is None:
        return False
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run([executable, 'rev-parse', '--show-toplevel'], cwd=root,
                            env=env, capture_output=True, timeout=10)
    if result.returncode:
        if any((parent / '.git').exists() for parent in (root, *root.parents)):
            raise ValueError('Git checkout metadata is present but cannot be read')
        return False
    top = Path(os.fsdecode(result.stdout).strip()).resolve()
    if root != top:
        raise ValueError('HEAD checks must run at the Git checkout root')
    from api_agent.workflows.viewer_build import _checked_view_inventory
    inventory = _checked_view_inventory(root)
    result = subprocess.run([executable, 'rev-parse', '--show-object-format'], cwd=root, env=env,
                            capture_output=True, timeout=10, check=True)
    algorithm = result.stdout.decode('ascii').strip()
    if algorithm not in ('sha1', 'sha256'):
        raise ValueError('Unsupported Git object format')
    result = subprocess.run([executable, 'ls-tree', '-r', '-z', '--full-tree', 'HEAD'], cwd=root,
                            env=env, capture_output=True, timeout=30, check=True)
    tree = {}
    for record in result.stdout.split(b'\0'):
        if record:
            metadata, encoded = record.split(b'\t', 1)
            mode, kind, identity = metadata.decode('ascii').split()
            tree[os.fsdecode(encoded)] = (mode, kind, identity)
    relevant = []
    for path, data in inventory.items():
        entry = tree.get(path)
        # Offline third-party viewer assets are installed from the locked npm
        # manifest and need not themselves be tracked in the Git tree.
        if (entry is None and '/.private_resources/viewer/' in '/' + path
                and Path(path).name in ('marked.js', 'dompurify.js', 'heading-id.js')):
            continue
        digest = hashlib.new(algorithm, b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if entry is None or entry[0] not in ('100644', '100755') or entry[1:] != ('blob', digest):
            relevant.append(path)
    from api_agent.workflows.document_format import canonical_executable_bytes
    from api_agent.workflows.viewer_build import _DISCOVERY_EXCLUDED, _MAX_VIEW_FILE, _owned_orphan

    def committed_blob(identity):
        size = subprocess.run([executable, 'cat-file', '-s', identity], cwd=root, env=env,
                              capture_output=True, timeout=10, check=True).stdout.strip()
        if not size.isdigit():
            raise ValueError('Invalid committed workflow blob size')
        if int(size) > _MAX_VIEW_FILE:
            return None
        return subprocess.run([executable, 'cat-file', 'blob', identity], cwd=root, env=env,
                              capture_output=True, timeout=10, check=True).stdout

    for path, (mode, kind, identity) in tree.items():
        if path in inventory:
            continue
        if '/.private_resources/diagrams/' in '/' + path:
            if kind == 'blob' and mode in ('100644', '100755'):
                data = committed_blob(identity)
                if data is None:
                    continue
                with tempfile.TemporaryDirectory(prefix='workflow-head-check-') as temporary:
                    candidate = Path(temporary) / Path(path).name
                    if Path(path).parent.name == 'contracts':
                        candidate = candidate.parent / 'contracts' / candidate.name
                        candidate.parent.mkdir()
                    candidate.write_bytes(data)
                    try:
                        owned = _owned_orphan(candidate)
                    except (AttributeError, TypeError):
                        owned = False
                    if owned:
                        relevant.append(path)
        elif (Path(path).name == 'flow.md.html'
              and all(not part.startswith('.') and part not in _DISCOVERY_EXCLUDED
                      for part in Path(path).parts[:-1])):
            if mode not in ('100644', '100755') or kind != 'blob':
                raise ValueError('Invalid committed workflow document')
            data = committed_blob(identity)
            if data is None:
                raise ValueError('Committed workflow document exceeds size limit')
            heading = canonical_executable_bytes(data, path=root / path).split(b'\n', 1)[0]
            if heading in (b'# Workflow v2', b'# Workflow v3', b'# LangGraph flow'):
                relevant.append(path)
    relevant.sort()
    if relevant:
        raise ValueError('workflow files differ from HEAD: ' + ', '.join(relevant[:10]))
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--no-git', action='store_true', help='Check current files without Git')
    modes.add_argument('--head', action='store_true', help='Check current workflow files against HEAD when available')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--frontend-source', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.no_git or args.head:
        if args.root is None:
            parser.error('--no-git and --head require --root')
        root = args.root
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.pre_commit_dispatcher import run
        return run()
    try:
        check(root, args.frontend_source)
        if args.head and not check_head(root):
            print('Git metadata unavailable; checked current files without a HEAD comparison.')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Workflow view checks failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
