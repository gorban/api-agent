"""Admitted ordered-pack source paths for controlled pytest children only."""
from pathlib import Path
import os


def composition_roots(context):
    return tuple(dict.fromkeys(Path(root).resolve() for root in
        (context.base_root, *(layer.owner_root for layer in context.pack_layers))))


def composition_pythonpath(context, inherited=''):
    roots = composition_roots(context)
    paths = tuple(dict.fromkeys((str(roots[0] / 'src'),
        *(str(path) for root in roots for path in (root, root / 'src')))))
    return os.pathsep.join((*paths, inherited))
