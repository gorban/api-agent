"""Bindings-grammar graphs of scripted nodes: each run returns its inputs updated with the next entry
of its node's script (the last entry repeats).

``CALLS`` lists every run in order, so a test reads which steps ran and how often.
"""
import asyncio
import json

from api_agent.workflows import NodeFunction, Workflow
from api_agent.workflows.definition import load_definition, load_native_catalog

from loop_support import table, write

CONFIG = '''version = 1

[fields.PLAN_PASSES]
type = "int"
default = 2

[fields.NODE_TIMEOUT]
type = "float"
default = 30.0
'''

NATIVE_HEAD = '''CALLS = []
SCRIPT = {}


def _run(name, inputs):
    CALLS.append(name)
    outputs = SCRIPT.get(name)
    if outputs is None:
        return dict(inputs)
    return {**inputs, **outputs[min(CALLS.count(name), len(outputs)) - 1]}
'''

ANY = {}


def native_source(names):
    return NATIVE_HEAD + ''.join(
        f'\n\ndef {name}(inputs, context):\n    return _run({name!r}, inputs)\n' for name in names)


def node(root, name, inputs, outputs, *, extra=''):
    """Write one node: ``inputs`` maps an input to ``(source, field, default[, schema])``; ``outputs`` is a properties map."""
    write(root, f'{name}_in.json', json.dumps({
        'type': 'object', 'properties': {key: source[3] if len(source) > 3 else ANY for key, source in inputs.items()},
        'required': list(inputs)}))
    write(root, f'{name}_out.json', json.dumps({'type': 'object', 'properties': outputs}))
    write(root, f'{name}.md.html', f'# Node: {name}\n\n'
          + table('Native', ('Registry', 'Kind', 'Source'), [(name, 'deterministic', '[source](native.py)')])
          + table('Functions', ('Input', 'Output'), [(f'[in]({name}_in.json)', f'[out]({name}_out.json)')])
          + table('Bindings', ('Input', 'From', 'Field', 'Default'),
                  [(key, *source[:3]) for key, source in inputs.items()]) + extra)


def flow_lines(steps):
    """Flow list items for ``steps``: node IDs, or ``(kind, id)`` pairs for components."""
    pairs = [('node', step) if isinstance(step, str) else step for step in steps]
    return '\n'.join(f'{index}.  [{kind} {name}]({name}.md.html)' for index, (kind, name) in enumerate(pairs, 1))


def component(root, name, children, row=('1', '-'), extra=''):
    flow = flow_lines(children)
    write(root, f'{name}.md.html', f'# Component: {name}\n\n## Flow\n\n{flow}\n\n'
          + table('Components', ('Max passes', 'Guard'), [row]) + extra)


def graph(root, flow, nodes, *, components=None, sections='', config=CONFIG):
    """Write a root over ``flow`` (node IDs, or ``(kind, id)`` pairs). ``components`` maps every component
    at any depth to ``(children, Components row, extra document text)``; ``nodes`` maps every node to
    ``(inputs, outputs)``."""
    write(root, 'native.py', native_source(nodes))
    write(root, 'config.toml', config)
    for name, (children, row, extra) in (components or {}).items():
        component(root, name, children, row, extra)
    for name, (inputs, outputs) in nodes.items():
        node(root, name, inputs, outputs)
    return write(root, 'flow.md.html', '# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| scripted |\n\n'
                 '## Flow\n\n' + flow_lines(flow) + '\n\n' + sections)


def load(entry):
    return load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def start(entry, script=None, *, config=None, journal=None, **services):
    """The workflow, its call log and its definition, with ``script`` installed in the native module."""
    definition = load(entry)
    native = load_native_catalog(definition, entry.parent / 'native.py')
    native['SCRIPT'].update({name: list(steps) for name, steps in (script or {}).items()})
    registry = {row['Registry']: NodeFunction(native[row['Registry']]) for row in definition.layout.tables['Functions']}
    return (Workflow(definition, registry, config=config, services=services, journal=journal),
            native['CALLS'], definition)


def run(workflow, update=None, thread=None):
    return asyncio.run(workflow.arun({} if update is None else update, thread_id=thread))
