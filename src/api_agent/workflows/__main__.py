"""Inference-free declaration validation and topology display."""
import argparse
from importlib.resources import files
from pathlib import Path

from .execution import diagram_layout
from .layout import LayoutError, load_layout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'diagram'))
    parser.add_argument('--layout', type=Path)
    parser.add_argument('--base-root', type=Path)
    parser.add_argument('--pack-root', type=Path)
    parser.add_argument('--demo', choices=('contrived',))
    args = parser.parse_args(argv)
    if (args.layout is None) == (args.demo is None):
        parser.error('select exactly one of --layout or --demo')
    package_root = Path(str(files('api_agent')))
    if args.demo:
        source_root = Path(__file__).resolve().parents[3]
        base = source_root if (source_root / 'demo' / args.demo / 'layout.md').is_file() else package_root
        path = base / 'demo' / args.demo / 'layout.md'
    else:
        path = args.layout.resolve()
        base = path.parent
    try:
        layout = load_layout(path, base_root=args.base_root or base, pack_root=args.pack_root or path.parent)
    except LayoutError as error:
        parser.exit(2, f'{error}\n')
    print(diagram_layout(layout) if args.command == 'diagram' else f'Valid layout: {layout.path}', end='\n' if args.command == 'validate' else '')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
