"""A re-run step reads values written after its earlier run through the TURN source."""
import json

from loop_support import edges, limits, run, table, write

NATIVE = '''CALLS = []


def draft(inputs, context):
    CALLS.append(('draft', inputs['feedback']))
    return {'n': len(CALLS) // 2 + 1}


def verify(inputs, context):
    CALLS.append(('verify', inputs['draft_n']))
    accepted = inputs['draft_n'] >= inputs['accept_at']
    return {'accepted': accepted, 'feedback': None if accepted else f"revise draft {inputs['draft_n']}"}


def accept(inputs, context):
    return {'ok': True}


def failed(inputs, context):
    return {'ok': False}
'''

NODES = {
    'draft': ({'feedback': ('TURN', 'feedback', 'null')}, {'n': {'type': 'integer'}}),
    'verify': ({'draft_n': ('draft', 'n', '-'), 'accept_at': ('START', 'accept_at', '-')},
               {'accepted': {'type': 'boolean'}, 'feedback': {'type': ['string', 'null']}}),
    'accept': ({}, {'ok': {'type': 'boolean'}}),
    'failed': ({}, {'ok': {'type': 'boolean'}}),
}


def graph(root):
    write(root, 'native.py', NATIVE)
    write(root, 'config.toml', 'version = 1\n\n[fields.PLAN_PASSES]\ntype = "int"\ndefault = 3\n')
    for name, (inputs, outputs) in NODES.items():
        properties = {key: {'type': ['string', 'null']} if key == 'feedback' else {} for key in inputs}
        write(root, f'{name}_in.json', json.dumps({'type': 'object', 'properties': properties,
                                                   'required': list(inputs)}))
        write(root, f'{name}_out.json', json.dumps({'type': 'object', 'properties': outputs}))
        write(root, f'{name}.md.html', f'# Node: {name}\n\n'
              + table('Native', ('Registry', 'Kind', 'Source'), [(name, 'deterministic', '[source](native.py)')])
              + table('Functions', ('Input', 'Output'), [(f'[in]({name}_in.json)', f'[out]({name}_out.json)')])
              + table('Bindings', ('Input', 'From', 'Field', 'Default'),
                      [(key, *source) for key, source in inputs.items()]))
    steps = '\n'.join(f'{index}.  [node {name}]({name}.md.html)' for index, name in enumerate(NODES, 1))
    return write(root, 'flow.md.html', f'# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| loops |\n\n'
                 f'## Flow\n\n{steps}\n\n' + limits('PLAN_PASSES') + edges(
                     ('verify', 'accept', 'verify.accepted'),
                     ('verify', 'draft', 'not verify.accepted and draft.attempt < PLAN_PASSES'),
                     ('verify', 'failed', 'otherwise'), ('accept', 'END', 'always')))


def test_a_retried_step_reads_the_verifiers_latest_feedback(tmp_path):
    values, calls, _ = run(graph(tmp_path), {'accept_at': 3})

    assert calls == [('draft', None), ('verify', 1), ('draft', 'revise draft 1'), ('verify', 2),
                     ('draft', 'revise draft 2'), ('verify', 3)]
    assert values['accept'] == {'ok': True}


def test_a_turn_without_a_retry_reads_the_default(tmp_path):
    values, calls, _ = run(graph(tmp_path), {'accept_at': 1})

    assert calls == [('draft', None), ('verify', 1)]
    assert values['verify'] == {'accepted': True, 'feedback': None}
