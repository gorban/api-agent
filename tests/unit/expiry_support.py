"""Bindings-grammar graphs whose `work` node can outlive its declared timeout, scripted per run."""
import asyncio

from loop_support import CONFIG as LOOP_CONFIG, INPUTS, OUTPUTS, build, edges, graph, limits, table

CONFIG = LOOP_CONFIG + '''
[fields.NODE_TIMEOUT]
type = "float"
default = 0.1

[fields.GROUP_TIMEOUT]
type = "float"
default = 0.1

[fields.GRAPH_TIMEOUT]
type = "float"
default = 60.0

[fields.NODE_TIMEOUT_PASSES]
type = "int"
default = 2
'''

# `work` follows its SCRIPT entry for each run: 'ok' completes, 'hang' outlives any timeout, 'effect'
# marks a write before hanging, 'port' hangs inside a nested tool-style budget, 'native' raises
# TimeoutError itself. `prepare` and `review` always complete.
NATIVE = '''import asyncio

from api_agent.providers.progress import component_timeout

CALLS = []
SCRIPT = {}


async def work(inputs, context):
    CALLS.append('work')
    step = SCRIPT['work'].pop(0) if SCRIPT.get('work') else 'ok'
    if step == 'effect':
        context.mark_effect()
    if step in ('hang', 'effect'):
        await asyncio.sleep(30)
    if step == 'port':
        async with component_timeout('Tool execution', 0.01):
            await asyncio.sleep(30)
    if step == 'native':
        raise TimeoutError('raised by native code')
    return {'n': CALLS.count('work')}


def prepare(inputs, context):
    CALLS.append('prepare')
    return {'ok': True}


def review(inputs, context):
    CALLS.append('review')
    return {'ok': True}


def report(inputs, context):
    CALLS.append('report')
    return {'n': inputs['n']}


def after(inputs, context):
    CALLS.append('after')
    return {'ok': True}


def recover(inputs, context):
    CALLS.append('recover')
    return {'ok': False}


def finish(inputs, context):
    CALLS.append('finish')
    return {'ok': True}


def timed_out(inputs, context):
    CALLS.append('timed_out')
    return {'ok': False}
'''

OUTPUTS.update({'work': {'n': {'type': 'integer'}}, 'prepare': {'ok': {'type': 'boolean'}},
                'review': {'ok': {'type': 'boolean'}}, 'report': {'n': {'type': 'integer'}},
                'after': {'ok': {'type': 'boolean'}}, 'recover': {'ok': {'type': 'boolean'}},
                'timed_out': {'ok': {'type': 'boolean'}}})
INPUTS['report'] = {'n': ('work', 'n')}

FLOW = [('node', 'work'), ('node', 'finish'), ('node', 'timed_out')]
ROUTE = (('work', 'timed_out', 'work.expired'), ('work', 'finish', 'otherwise'))
REPLAY = (('work', 'work', 'work.expired and work.attempt < NODE_TIMEOUT_PASSES'),
          ('work', 'timed_out', 'work.expired and work.attempt >= NODE_TIMEOUT_PASSES'),
          ('work', 'finish', 'otherwise'))
ENDS = (('finish', 'END', 'always'), ('timed_out', 'END', 'always'))


def timeouts(*rows):
    return table('Timeouts', ('Owner', 'Source', 'Key'), [(owner, '[config](config.toml)', key) for owner, key in rows])


def expiring(root, *rows, flow=FLOW, owners=(('work', 'NODE_TIMEOUT'),), ends=ENDS, extra='', **kwargs):
    """A graph declaring ``owners`` as Timeouts rows, with ``rows`` as the Edges and every flow end routed to END."""
    sections = (limits('NODE_TIMEOUT_PASSES') + timeouts(*owners) + edges(*rows, *ends) + extra)
    return graph(root, flow, sections=sections, native=NATIVE, config=CONFIG, **kwargs)


def play(entry, script=None, config=None, update=None, **services):
    """Run one turn; returns ``(values, calls, workflow)`` and raises whatever fails the turn."""
    workflow, native, _ = build(entry, config, **services)
    for name, steps in (script or {}).items():
        native['SCRIPT'][name] = list(steps)
    values = asyncio.run(workflow.arun({'accept_at': 1} if update is None else update))
    return values, native['CALLS'], workflow


def calls_after(entry, script=None, config=None, **services):
    """The calls made before the turn ended, whether it passed or failed."""
    workflow, native, _ = build(entry, config, **services)
    native['SCRIPT'].update({name: list(steps) for name, steps in (script or {}).items()})
    try:
        asyncio.run(workflow.arun({'accept_at': 1}))
    except BaseException as error:
        return native['CALLS'], error
    return native['CALLS'], None

