"""Read-only omnibus checks for a complete indexed or CI-supplied snapshot."""
import argparse
from pathlib import Path
import sys


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-git', action='store_true', help='Check a complete trusted CI snapshot')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--frontend-source', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.no_git:
        if args.root is None:
            parser.error('--no-git requires --root')
        root = args.root
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.pre_commit_dispatcher import run
        return run()
    try:
        check(root, args.frontend_source)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Workflow view checks failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
