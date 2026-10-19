"""Bundled one-node chat flow, compiled from its selected root declaration."""
from importlib.resources import files
from pathlib import Path

from api_agent.providers.llm import GenerationOptions
from .definition import load_definition, load_native_catalog
from .execution import NodeFunction, Workflow


def build(base_root=None, *, llm, selection, generation=None, request_id=None, config=None, observe=None):
    """Bind an owned async provider callable to the bundled selected-model graph."""
    if base_root is not None:
        root = Path(base_root).resolve()
    else:
        source_root = Path(__file__).resolve().parents[3]
        source_module = source_root / 'src/api_agent/workflows/application.py'
        root = (source_root if source_module.resolve() == Path(__file__).resolve()
                else Path(str(files('api_agent'))).resolve())
    entry = root / 'flow.md.html'
    if not entry.is_file():
        raise FileNotFoundError(f'Selected application flow is missing: {entry}')
    if generation is not None and (not isinstance(generation, GenerationOptions)
                                   or generation.temperature is not None):
        raise ValueError('generation must be accepted chat GenerationOptions')
    definition = load_definition(entry, base_root=root, pack_root=root)
    source = next(step.source for step in definition.layout.steps if step.id == 'renderer')
    catalog = load_native_catalog(definition, source)
    return Workflow(definition, {'render': NodeFunction(catalog['render'], 'llm')},
                    config=config, services={'llm': llm, 'selection': selection,
                                             'generation': generation, 'request_id': request_id,
                                             'observe': observe})
