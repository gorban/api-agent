"""TURN resolves against the turn's recorded completed runs, the same record Session merges use."""
import asyncio
import json

from api_agent.workflows import NodeFunction, Workflow
from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import _latest_turn_value

from loop_support import edges, limits, table, write

CONFIG = 'version = 1\n\n[fields.PLAN_PASSES]\ntype = "int"\ndefault = 2\n'

# Each node returns the next entry of its script (the last one repeats) and records the call.
NATIVE = '''CALLS = []
SCRIPT = {}


def _run(name):
    CALLS.append(name)
    outputs = SCRIPT[name]
    return dict(outputs[min(CALLS.count(name), len(outputs)) - 1])


def seed(inputs, context):
    return _run('seed')


def x(inputs, context):
    return _run('x')


def y(inputs, context):
    return _run('y')


def w(inputs, context):
    return {**_run('w'), 'saw': inputs['prior']}


def gate(inputs, context):
    return _run('gate')


def judge(inputs, context):
    return _run('judge')


def reader(inputs, context):
    CALLS.append('reader')
    return {'seen': inputs['seen']}
'''

ANY = {}
NODES = {
    'seed': ({}, {'v': {'type': 'integer'}}),
    'x': ({}, {'v': {'type': 'integer'}}),
    'y': ({}, {'v': {'type': 'integer'}}),
    'w': ({'prior': ('TURN', 'v', '"none"')}, {'v': {'type': 'integer'}, 'saw': ANY}),
    'gate': ({}, {'accepted': {'type': 'boolean'}, 'feedback': {'type': 'object'}}),
    'judge': ({}, {'ok': {'type': 'boolean'}}),
    'reader': ({'seen': ('TURN', 'v', '"none"')}, {'seen': ANY}),
}


def node(root, name):
    inputs, outputs = NODES[name]
    write(root, f'{name}_in.json', json.dumps({'type': 'object', 'properties': {key: ANY for key in inputs},
                                               'required': list(inputs)}))
    write(root, f'{name}_out.json', json.dumps({'type': 'object', 'properties': outputs}))
    write(root, f'{name}.md.html', f'# Node: {name}\n\n'
          + table('Native', ('Registry', 'Kind', 'Source'), [(name, 'deterministic', '[source](native.py)')])
          + table('Functions', ('Input', 'Output'), [(f'[in]({name}_in.json)', f'[out]({name}_out.json)')])
          + table('Bindings', ('Input', 'From', 'Field', 'Default'),
                  [(key, *source) for key, source in inputs.items()]))


def component(root, name, children, row):
    flow = '\n'.join(f'{index}.  [node {child}]({child}.md.html)' for index, child in enumerate(children, 1))
    write(root, f'{name}.md.html', f'# Component: {name}\n\n## Flow\n\n{flow}\n\n'
          + table('Components', ('Max passes', 'Guard'), [row]))


def graph(root, flow, *, components=None, sections=''):
    write(root, 'native.py', NATIVE)
    write(root, 'config.toml', CONFIG)
    steps = []
    for kind, name in flow:
        if kind == 'component':
            children, row = components[name]
            for child in children:
                node(root, child)
            component(root, name, children, row)
        else:
            node(root, name)
        steps.append(f'{len(steps) + 1}.  [{kind} {name}]({name}.md.html)')
    return write(root, 'flow.md.html', '# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| turn_runs |\n\n'
                 '## Flow\n\n' + '\n'.join(steps) + '\n\n' + sections)


def start(entry, script):
    definition = load_definition(entry, base_root=entry.parent, pack_root=entry.parent)
    native = load_native_catalog(definition, entry.parent / 'native.py')
    native['SCRIPT'].update(script)
    registry = {name: NodeFunction(native[name]) for name in NODES
                if any(row['ID'] == name for row in definition.layout.tables['Functions'])}
    workflow = Workflow(definition, registry)
    return workflow, native['CALLS']


def run(workflow, thread=None):
    return asyncio.run(workflow.arun({}, thread_id=thread))


REPEAT_X = limits('PLAN_PASSES') + edges(('x', 'x', 'x.attempt < PLAN_PASSES'), ('x', 'reader', 'otherwise'))
REENTER = limits('PLAN_PASSES') + edges(('judge', 'group', 'group.attempt < PLAN_PASSES'),
                                        ('judge', 'END', 'otherwise'))
SESSION = table('Session', ('Field', 'Source', 'Merge'), [
    ('last_v', 'y.v', 'replace'), ('last_v', 'x.v', 'replace'), ('seen_v', 'reader.seen', 'replace')])


def test_a_rerun_that_omits_a_field_does_not_hide_the_nodes_own_earlier_write(tmp_path):
    entry = graph(tmp_path, [('node', 'y'), ('node', 'x'), ('node', 'reader')], sections=REPEAT_X)
    workflow, calls = start(entry, {'y': [{'v': 2}], 'x': [{'v': 1}, {}]})

    values = run(workflow)

    assert calls == ['y', 'x', 'x', 'reader']
    assert values['x'] == {} and values['reader']['seen'] == 1


def test_reentering_a_component_keeps_its_earlier_values_visible_to_turn(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), ('node', 'judge')], sections=REENTER,
                  components={'group': (['w'], ('1', '-'))})
    workflow, calls = start(entry, {'w': [{'v': 1}, {'v': 2}], 'judge': [{'ok': False}]})

    values = run(workflow)

    assert calls == ['w', 'judge', 'w', 'judge']
    assert values['w'] == {'v': 2, 'saw': 1}


def test_a_value_written_only_in_a_rejected_pass_is_not_counted(tmp_path):
    entry = graph(tmp_path, [('node', 'seed'), ('component', 'group'), ('node', 'reader')],
                  components={'group': (['w', 'gate'], ('2', 'gate'))})
    workflow, calls = start(entry, {
        'seed': [{'v': 3}], 'w': [{'v': 7}, {}],
        'gate': [{'accepted': False, 'feedback': {'reason': 'again'}}, {'accepted': True, 'feedback': {}}]})

    values = run(workflow)

    assert calls == ['seed', 'w', 'gate', 'w', 'gate', 'reader']
    assert values['w']['saw'] == 3 and values['reader']['seen'] == 3


def test_a_rejected_pass_with_no_earlier_writer_leaves_the_default(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), ('node', 'reader')],
                  components={'group': (['w', 'gate'], ('2', 'gate'))})
    workflow, _ = start(entry, {
        'w': [{'v': 7}, {}],
        'gate': [{'accepted': False, 'feedback': {'reason': 'again'}}, {'accepted': True, 'feedback': {}}]})

    values = run(workflow)

    assert values['w']['saw'] == 'none' and values['reader']['seen'] == 'none'


def test_turn_and_session_agree_on_the_latest_completed_write(tmp_path):
    entry = graph(tmp_path, [('node', 'y'), ('node', 'x'), ('node', 'reader')], sections=REPEAT_X + SESSION)
    workflow, _ = start(entry, {'y': [{'v': 2}], 'x': [{'v': 1}, {}]})

    values = run(workflow, 'thread')
    committed = asyncio.run(workflow.session('thread'))

    assert values['reader']['seen'] == 1
    assert committed == {'last_v': 1, 'seen_v': 1}


def test_turn_and_session_agree_across_component_reentry(tmp_path):
    rows = table('Session', ('Field', 'Source', 'Merge'), [('last_v', 'w.v', 'replace')])
    entry = graph(tmp_path, [('component', 'group'), ('node', 'judge')], sections=REENTER + rows,
                  components={'group': (['w'], ('1', '-'))})
    workflow, _ = start(entry, {'w': [{'v': 1}, {}], 'judge': [{'ok': False}]})

    values = run(workflow, 'thread')

    assert values['w'] == {'saw': 1}
    assert asyncio.run(workflow.session('thread')) == {'last_v': 1}


def test_latest_turn_value_follows_completion_order_not_flow_order():
    runs = [('second', {'x': 2}), ('first', {'x': 1}), ('third', {'y': 3})]

    assert _latest_turn_value(runs, 'x') == (True, 1)
    assert _latest_turn_value(runs[:1], 'x') == (True, 2)
    assert _latest_turn_value(runs, 'y') == (True, 3)


def test_latest_turn_value_counts_every_recorded_run_and_reports_absence():
    runs = [('first', {'x': 1}), ('first', {})]

    assert _latest_turn_value(runs, 'x') == (True, 1)
    assert _latest_turn_value(runs, 'z') == (False, None)
    assert _latest_turn_value([], 'x') == (False, None)
