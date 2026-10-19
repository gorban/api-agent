"""Inference-free selected workflow validation and topology display."""
import argparse
from importlib.resources import files
from pathlib import Path

from .execution import diagram_layout
from .layout import LayoutError
from .definition import load_definition


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'diagram'))
    parser.add_argument('--entry', type=Path)
    parser.add_argument('--entry-root', choices=('base', 'pack'))
    parser.add_argument('--base-root', type=Path)
    parser.add_argument('--pack-root', type=Path)
    parser.add_argument('--demo', choices=('contrived',))
    args = parser.parse_args(argv)
    if args.entry is not None and args.demo is not None:
        parser.error('select at most one of --entry or --demo')
    if args.entry_root is not None and args.entry is None:
        parser.error('--entry-root requires --entry')
    package_root = Path(str(files('api_agent')))
    source_root = Path(__file__).resolve().parents[3]
    source_module = source_root / 'src/api_agent/workflows/__main__.py'
    base = source_root if source_module.resolve() == Path(__file__).resolve() else package_root
    if args.demo:
        path = base / 'demo' / args.demo / 'flow.md.html'
        selected_pack_root = base / 'demo'
        selected_entry_root = 'pack'
    elif args.entry is not None:
        path = args.entry.resolve()
        base = args.base_root.resolve() if args.base_root else path.parent
        selected_pack_root = path.parent
        selected_entry_root = args.entry_root or 'base'
    else:
        path = base / 'flow.md.html'
        selected_pack_root = path.parent
        selected_entry_root = 'base'
    try:
        definition = load_definition(path, base_root=args.base_root or base,
                                     pack_root=args.pack_root or selected_pack_root,
                                     entry_root=selected_entry_root)
    except LayoutError as error:
        parser.exit(2, f'{error}\n')
    print(diagram_layout(definition.layout) if args.command == 'diagram'
          else f'Valid workflow: {definition.graph_id} ({definition.definition_id})',
          end='\n' if args.command == 'validate' else '')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
