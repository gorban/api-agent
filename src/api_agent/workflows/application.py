"""Bundled chat component, compiled from its selected root declaration."""
from pathlib import Path

from api_agent.providers.llm import GenerationOptions
from .definition import load_definition, load_native_catalog
from .execution import NodeFunction, Workflow
from .resources import bundled_root


def build(base_root=None, *, llm, selection, generation=None, request_id=None, config=None, observe=None):
    """Bind an owned async provider callable to the bundled selected-model graph."""
    if base_root is not None:
        root = Path(base_root).resolve()
    else:
        root = bundled_root()
    entry = root / 'flow.md.html'
    if not entry.is_file():
        raise FileNotFoundError(f'Selected application flow is missing: {entry}')
    if generation is not None and (not isinstance(generation, GenerationOptions)
                                   or generation.temperature is not None):
        raise ValueError('generation must be accepted chat GenerationOptions')
    definition = load_definition(entry, base_root=root, pack_root=root)
    source = next(step.source for step in definition.layout.steps if step.id == 'llm_call')
    catalog = load_native_catalog(definition, source)
    return Workflow(definition, {'llm_call': NodeFunction(catalog['llm_call'], 'llm')},
                    config=config, services={'llm': llm, 'selection': selection,
                                             'generation': generation, 'request_id': request_id,
                                             'observe': observe})
