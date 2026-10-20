"""Inference-free selected workflow validation and topology display."""
import argparse
from pathlib import Path

from .execution import diagram_layout
from .layout import LayoutError
from .definition import _read_bounded, load_definition
from .stateful import StatefulDefinition, diagram_stateful, load_stateful_definition
from .resources import bundled_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate', 'diagram'))
    parser.add_argument('--entry', type=Path,
                        help='Validate this raw declaration without applying a resource-pack manifest.')
    parser.add_argument('--entry-root', choices=('base', 'pack'))
    parser.add_argument('--base-root', type=Path)
    parser.add_argument('--pack-root', type=Path)
    parser.add_argument('--demo', choices=('contrived',))
    parser.add_argument('--resource-pack-dir', type=Path,
                        help='Select the manifest workflow, including ordered replacements.')
    args = parser.parse_args(argv)
    if args.entry is not None and args.demo is not None:
        parser.error('select at most one of --entry or --demo')
    if args.resource_pack_dir is not None and any((args.entry, args.demo, args.entry_root, args.pack_root)):
        parser.error('--resource-pack-dir cannot be combined with raw workflow selectors')
    if args.entry_root is not None and args.entry is None:
        parser.error('--entry-root requires --entry')
    try:
        if args.resource_pack_dir is not None:
            from api_agent.runtime.resource_pack import load_launch_context, load_selected_definition
            base = args.base_root.resolve() if args.base_root else bundled_root()
            context = load_launch_context(base, args.resource_pack_dir.resolve())
            definition = load_selected_definition(context)
        else:
            base = (args.base_root.resolve() if args.base_root else
                    args.entry.resolve().parent if args.entry is not None else bundled_root())
            if args.demo:
                path = base / 'demo' / args.demo / 'flow.md.html'
                selected_pack_root = base / 'demo'
                selected_entry_root = 'pack'
            elif args.entry is not None:
                path = args.entry.resolve()
                selected_pack_root = path.parent
                selected_entry_root = args.entry_root or 'base'
            else:
                path = base / 'flow.md.html'
                selected_pack_root = path.parent
                selected_entry_root = 'base'
            selected_roots = dict(base_root=base, pack_root=args.pack_root or selected_pack_root,
                                  entry_root=selected_entry_root)
            owner = selected_roots[selected_entry_root + '_root']
            if _read_bounded(path, owner).split(b'\n', 1)[0] == b'# Workflow v3':
                definition = load_stateful_definition(path, **selected_roots)
            else:
                definition = load_definition(path, **selected_roots)
    except (LayoutError, ValueError, FileNotFoundError) as error:
        parser.exit(2, f'{error}\n')
    print((diagram_stateful(definition) if isinstance(definition, StatefulDefinition)
           else diagram_layout(definition.layout)) if args.command == 'diagram'
          else f'Valid workflow: {definition.graph_id} ({definition.definition_id})',
          end='\n' if args.command == 'validate' else '')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
