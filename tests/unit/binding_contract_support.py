"""Build a mutable contract-compiler fixture using the release declaration."""
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).parent / 'fixtures/binding_contracts'


def contract_graph(root):
    root.mkdir(parents=True, exist_ok=True)
    for name in ('schemas',):
        shutil.copytree(ROOT / 'demo/contrived' / name, root / name, dirs_exist_ok=True)
    shutil.copytree(ROOT / 'demo/cfg', root / 'cfg', dirs_exist_ok=True)
    shutil.copy2(ROOT / 'demo/contrived/functions.py', root / 'functions.py')
    (root / 'prompts').mkdir(exist_ok=True)
    shutil.copy2(TEMPLATE / 'produce.md', root / 'prompts/produce.md')
    for path in TEMPLATE.iterdir():
        if path.name == 'produce.md':
            continue
        shutil.copy2(path, root / ('flow.md.html' if path.name == 'contract.md.html' else path.name))
    return root / 'flow.md.html'


def mutable_layout(layout):
    def mutable(value):
        if isinstance(value, Mapping):
            return {key: mutable(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [mutable(item) for item in value]
        return value
    return replace(layout, tables=mutable(layout.tables))


def compiler_fixture(layout, registry=None, conditions=None, **kwargs):
    """Exercise compiler guards with explicitly synthetic mutable definitions.

    Production accepts only admitted definitions. These low-level tests alter
    layout rows and use test-module functions deliberately, so their fixture
    must own that capability rather than exposing it through the public engine.
    """
    from types import SimpleNamespace
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.execution import Workflow, NodeFunction
    from api_agent.workflows.model import declares_source
    if hasattr(layout, 'definition_id'):
        return Workflow(layout, registry, conditions, **kwargs)
    original = (load_definition(layout.path, base_root=layout.path.parent, pack_root=layout.path.parent)
                if layout.path.name.endswith('.md.html') else SimpleNamespace(
                    graph_id='compiler_fixture', definition_id='test-only', entry=layout.path, entry_root='base',
                    contents={}, resources=(), replacements=(), selected_declarations={}, declaration_fragments={},
                    tool_usages=(), imports=(), declaration_entry=None,
                    roots={'base': layout.path.parent, 'pack': layout.path.parent}))
    rows = []
    for row in layout.tables['Functions']:
        function = (registry or {}).get(row['Registry'])
        rows.append({**row, 'Kind': function.kind if isinstance(function, NodeFunction) else row.get('Kind', 'deterministic')})
    selected = replace(layout, tables={**layout.tables, 'Functions': rows})
    def native_rejection(function, step):
        return None if declares_source(function, step.source) else 'registered function does not match its declared source link'
    definition = SimpleNamespace(**vars(original), version=1, origin='source',
        verify_sources=lambda: None, native_rejection=native_rejection)
    definition.layout = selected
    definition.contents = dict(definition.contents)
    for row in selected.tables["Timeouts"]:
        definition.contents[row["Source"]] = row["Source"].read_bytes()
    return Workflow(definition, registry, conditions, **kwargs)
