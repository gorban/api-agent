"""Build small Bindings-grammar graphs whose nodes are scripted by a shared call log."""
import asyncio
import json
from pathlib import Path

from api_agent.workflows import NodeFunction, Workflow
from api_agent.workflows.definition import load_definition, load_native_catalog

CONFIG = '''version = 1

[fields.PLAN_PASSES]
type = "int"
default = 2

[fields.REPLAN_PASSES]
type = "int"
default = 2

[fields.NO_PASSES]
type = "int"
default = 0

[fields.NEGATIVE]
type = "int"
default = -1

[fields.HUGE]
type = "int"
default = 100

[fields.NO_DEFAULT]
type = "int"

[fields.RATIO]
type = "float"
default = 1.0
'''

# Every node appends its name to CALLS, so a test reads exactly which steps ran
# and how often. `draft` reports its own run number; `verify` accepts once the
# latest draft reached the run number bound from START.
NATIVE = '''CALLS = []


def draft(inputs, context):
    CALLS.append('draft')
    return {'n': CALLS.count('draft')}


def verify(inputs, context):
    CALLS.append('verify')
    return {'accepted': inputs['draft_n'] >= inputs['accept_at']}


def accept(inputs, context):
    CALLS.append('accept')
    return {'ok': True}


def failed(inputs, context):
    CALLS.append('failed')
    return {'ok': False}


def plan(inputs, context):
    CALLS.append('plan')
    return {'n': CALLS.count('plan')}


def analyze(inputs, context):
    CALLS.append('analyze')
    return {'plan_n': inputs['plan_n']}


def check(inputs, context):
    CALLS.append('check')
    return {'route': 'done' if inputs['plan_n'] >= inputs['accept_at'] else 'replan'}


def finish(inputs, context):
    CALLS.append('finish')
    return {'ok': True}
'''

OUTPUTS = {
    'draft': {'n': {'type': 'integer'}},
    'verify': {'accepted': {'type': 'boolean'}},
    'accept': {'ok': {'type': 'boolean'}},
    'failed': {'ok': {'type': 'boolean'}},
    'plan': {'n': {'type': 'integer'}},
    'analyze': {'plan_n': {'type': 'integer'}},
    'check': {'route': {'type': 'string'}},
    'finish': {'ok': {'type': 'boolean'}},
}
INPUTS = {
    'verify': {'draft_n': ('draft', 'n'), 'accept_at': ('START', 'accept_at')},
    'analyze': {'plan_n': ('plan', 'n')},
    'check': {'plan_n': ('analyze', 'plan_n'), 'accept_at': ('START', 'accept_at')},
}


def write(root, name, text):
    path = Path(root) / name
    path.write_text(text, encoding='utf-8')
    return path


def table(title, header, rows):
    if not rows:
        return ''
    lines = [f'## {title}', '', '| ' + ' | '.join(header) + ' |', '| ' + ' | '.join('---' for _ in header) + ' |']
    lines.extend('| ' + ' | '.join(row) + ' |' for row in rows)
    return '\n'.join(lines) + '\n\n'


def limits(*pairs, source='config.toml'):
    return table('Limits', ('Key', 'Source'), [(key, f'[config]({source})') for key in pairs])


def edges(*rows):
    return table('Edges', ('From', 'To', 'When'), rows)


def node(root, name, *, outputs=None, extra=''):
    """Write one node declaration with its Functions and Bindings rows."""
    write(root, f'{name}_in.json', json.dumps({
        'type': 'object', 'properties': {key: {} for key in INPUTS.get(name, {})},
        'required': list(INPUTS.get(name, {}))}))
    write(root, f'{name}_out.json', json.dumps({
        'type': 'object', 'properties': OUTPUTS[name] if outputs is None else outputs}))
    bindings = [(name, key, source, field, '-') for key, (source, field) in INPUTS.get(name, {}).items()]
    return write(root, f'{name}.md.html', f'# Node: {name}\n\n'
                 + table('Native', ('Registry', 'Kind', 'Source'), [(name, 'deterministic', '[source](native.py)')])
                 + table('Functions', ('ID', 'Registry', 'Input', 'Output'),
                         [(name, name, f'[in]({name}_in.json)', f'[out]({name}_out.json)')])
                 + table('Bindings', ('Node', 'Input', 'From', 'Field', 'Default'), bindings) + extra)


def component(root, name, children, extra=''):
    flow = '\n'.join(f'{index}.  [node {child}]({child}.md.html)' for index, child in enumerate(children, 1))
    unguarded = '' if '## Components' in extra else table('Components', ('ID', 'Max passes', 'Guard'), [(name, '1', '-')])
    return write(root, f'{name}.md.html', f'# Component: {name}\n\n## Flow\n\n{flow}\n\n{unguarded}' + extra)


def graph(root, flow, *, sections='', nodes=None, components=None, native=NATIVE, config=CONFIG):
    """Write a root declaration over ``flow`` (a list of ('node'|'component', id))."""
    root = Path(root)
    write(root, 'native.py', native)
    write(root, 'config.toml', config)
    for name in nodes if nodes is not None else [i for kind, i in flow if kind == 'node']:
        node(root, name)
    for name, (children, extra) in (components or {}).items():
        for child in children:
            node(root, child)
        component(root, name, children, extra)
    steps = '\n'.join(f'{index}.  [{kind} {name}]({name}.md.html)' for index, (kind, name) in enumerate(flow, 1))
    return write(root, 'flow.md.html', f'# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| loops |\n\n'
                 f'## Flow\n\n{steps}\n\n{sections}')


def load(entry):
    return load_definition(entry, base_root=entry.parent, pack_root=entry.parent)


def build(entry, config=None, **services):
    """Load ``entry`` and return (workflow, native namespace, definition)."""
    definition = load(entry)
    native = load_native_catalog(definition, entry.parent / 'native.py')
    registry = {name: NodeFunction(native[name]) for name in OUTPUTS if name in native
                and any(row['ID'] == name for row in definition.layout.tables['Functions'])}
    return Workflow(definition, registry, config=config, services=services), native, definition


def run(entry, update=None, config=None, **services):
    workflow, native, definition = build(entry, config, **services)
    values = asyncio.run(workflow.arun({'accept_at': 1} if update is None else update))
    return values, native['CALLS'], workflow
