"""Typed Limits, component Timeouts and declared timeout-expiry routing."""
import asyncio
from pathlib import Path

import pytest

from api_agent.workflows.definition import load_definition
from api_agent.workflows.execution import Workflow
from api_agent.workflows.layout import LayoutError
from api_agent.workflows.native import load_native_bindings

NATIVE = '''import asyncio
from typing import TypedDict


class State(TypedDict):
    next_step: str
    plan_retries: int
    timed_out: str
    timeout_retries: int
    delay: float
    fast_after: int
    trail: list


async def plan_request(state, context):
    wait = 0 if state['timeout_retries'] >= state['fast_after'] else state['delay']
    await asyncio.sleep(wait)
    return {'next_step': 'finish', 'timed_out': '', 'trail': [*state['trail'], 'plan']}


async def plan_keep(state, context):
    wait = 0 if state['timeout_retries'] >= state['fast_after'] else state['delay']
    await asyncio.sleep(wait)
    return {'next_step': 'done', 'trail': [*state['trail'], 'keep']}


async def effectful(state, context):
    context.mark_effect()
    await asyncio.sleep(state['delay'])
    return {}


def raises_timeout(state, context):
    raise TimeoutError('inner')


def loop(state, context):
    return {'plan_retries': state['plan_retries'] + 1, 'next_step': 'plan_request'}


def step_timeout_error(state, context):
    return {'next_step': 'error', 'trail': [*state['trail'], 'error']}


def finish(state, context):
    return {'trail': [*state['trail'], 'finish']}
'''

# Long enough that a loaded host cannot exhaust it before the scope's own work starts.
TIMEOUT = 0.5

LIMITS = f'''version = 1

[fields.TIMEOUT_RETRIES]
type = "int"
default = 1

[fields.PLAN_RETRIES]
type = "int"
default = 2

[fields.RATIO]
type = "float"
default = 1.0

[fields.NO_DEFAULT]
type = "int"

[fields.NEGATIVE]
type = "int"
default = -1

[fields.NODE_TIMEOUT]
type = "float"
default = {TIMEOUT}

[fields.GROUP_TIMEOUT]
type = "float"
default = {TIMEOUT}

[fields.LONG_TIMEOUT]
type = "float"
default = 30.0

[fields.UNSET_TIMEOUT]
type = "float"
'''

NODES = ('plan_request', 'plan_keep', 'effectful', 'raises_timeout', 'loop', 'step_timeout_error', 'finish')
STATE = dict(next_step='', plan_retries=0, timed_out='', timeout_retries=0, delay=5.0, fast_after=1, trail=[])
EXPIRY = '''## Timeouts

| Owner | Source | Key | Expired | Expirations |
| --- | --- | --- | --- | --- |
'''
PLAIN = '''## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
'''
LIMIT_TABLE = '''## Limits

| Key | Source |
| --- | --- |
'''


def write(root, name, content):
    path = root / name
    path.write_text(content, encoding='utf-8')
    return path


def build(root, flow, edges, sections='', components=None):
    write(root, 'native.py', NATIVE)
    write(root, 'limits.toml', LIMITS)
    for name in NODES:
        write(root, f'{name}.md.html', f'''# Node: {name}

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| {name} | deterministic | [Python](native.py) |
''')
    for name, body in (components or {}).items():
        write(root, f'{name}.md.html', f'# Component: {name}\n\n{body}')
    steps = '\n'.join(f'{index}.  [{kind} {name}]({name}.md.html)'
                      for index, (kind, name) in enumerate(flow, 1))
    return write(root, 'flow.md.html', f'''# Workflow

## Graph ID

| ID |
| --- |
| limits_flow |

## State

| Registry | Source |
| --- | --- |
| State | [Python](native.py) |

## Flow

{steps}

## Edges

| From | To | When |
| --- | --- | --- |
{edges}

{sections}''')


def admit(root, entry):
    return load_definition(entry, base_root=root, pack_root=root)


def run(root, entry, update=None, config=None, events=None, journal=None):
    definition = admit(root, entry)
    catalog = load_native_bindings(definition, config=config)
    workflow = Workflow(definition, catalog=catalog, reducers={}, config=config, journal=journal,
                        services={'observe': events.append} if events is not None else None)
    return asyncio.run(workflow.arun({**STATE, **(update or {})}, thread_id='limits')), workflow


RETRY_EDGES = '''| plan_request | plan_request | .timed_out == "plan_request" and .timeout_retries <= TIMEOUT_RETRIES |
| plan_request | step_timeout_error | .timed_out == "plan_request" and .timeout_retries > TIMEOUT_RETRIES |
| plan_request | finish | otherwise |
| step_timeout_error | END | always |
| finish | END | always |'''
RETRY_FLOW = [('node', 'plan_request'), ('node', 'step_timeout_error'), ('node', 'finish')]
RETRY_SECTIONS = (LIMIT_TABLE + '| TIMEOUT_RETRIES | [config](limits.toml) |\n\n' + EXPIRY +
                  '| plan_request | [config](limits.toml) | NODE_TIMEOUT | timed_out | timeout_retries |\n')


def retry_graph(root, sections=RETRY_SECTIONS):
    return build(root, RETRY_FLOW, RETRY_EDGES, sections)


# --- typed Limits admission -------------------------------------------------

def test_root_limit_becomes_a_predicate_constant_resolved_from_configuration(tmp_path):
    entry = build(tmp_path, [('node', 'loop'), ('node', 'plan_request'), ('node', 'finish')],
                  '''| loop | loop | .next_step == "plan_request" and .plan_retries <= PLAN_RETRIES |
| loop | plan_request | otherwise |
| plan_request | finish | always |
| finish | END | always |''',
                  LIMIT_TABLE + '| PLAN_RETRIES | [config](limits.toml) |\n')
    definition = admit(tmp_path, entry)

    assert [(row['Key'], row['Source'], row.get('_owner')) for row in definition.limits] == [
        ('PLAN_RETRIES', tmp_path / 'limits.toml', None)]
    default, workflow = run(tmp_path, entry, {'timeout_retries': 1})
    configured, _ = run(tmp_path, entry, {'timeout_retries': 1}, config={'PLAN_RETRIES': 5})

    assert workflow.limit_values == {'PLAN_RETRIES': 2}
    assert default['plan_retries'] == 3
    assert configured['plan_retries'] == 6



def test_a_zero_limit_disables_the_retry_it_bounds(tmp_path):
    entry = build(tmp_path, [('node', 'loop'), ('node', 'plan_request'), ('node', 'finish')],
                  '''| loop | loop | .next_step == "plan_request" and .plan_retries <= PLAN_RETRIES |
| loop | plan_request | otherwise |
| plan_request | finish | always |
| finish | END | always |''',
                  LIMIT_TABLE + '| PLAN_RETRIES | [config](limits.toml) |\n')
    result, workflow = run(tmp_path, entry, {'timeout_retries': 1}, config={'PLAN_RETRIES': 0})

    assert workflow.limit_values == {'PLAN_RETRIES': 0}
    assert result['plan_retries'] == 1

def test_component_declares_limits_and_prompt_conditions_use_them(tmp_path):
    component = '''## Flow

1.  [node loop](loop.md.html)

## Limits

| Key | Source |
| --- | --- |
| PLAN_RETRIES | [config](limits.toml) |
'''
    entry = build(tmp_path, [('component', 'group'), ('node', 'finish')],
                  '''| loop | loop | .plan_retries <= PLAN_RETRIES |
| loop | END | otherwise |
| group | finish | always |
| finish | END | always |''', components={'group': component})
    definition = admit(tmp_path, entry)

    assert [(row['Key'], row['_owner'].name) for row in definition.limits] == [
        ('PLAN_RETRIES', 'group.md.html')]
    result, workflow = run(tmp_path, entry)
    assert workflow.limit_values == {'PLAN_RETRIES': 2}
    assert result['plan_retries'] == 3


@pytest.mark.parametrize(('table', 'message'), [
    ('| plan_retries | [config](limits.toml) |', 'invalid limit key'),
    ('| PLAN_RETRIES | [config](limits.toml) |\n| PLAN_RETRIES | [config](limits.toml) |', 'duplicate limit key'),
    ('| PLAN_RETRIES | [config](native.py) |', 'limit source must be TOML'),
    ('| PLAN_RETRIES | limits.toml |', 'expected one source link'),
    ('| MISSING | [config](limits.toml) |', 'limit key must name an integer configuration field'),
    ('| RATIO | [config](limits.toml) |', 'limit key must name an integer configuration field'),
    ('| NO_DEFAULT | [config](limits.toml) |', 'limit requires a declared integer default'),
    ('| NEGATIVE | [config](limits.toml) |', 'limit must be an integer from 0 to 99'),
])
def test_limit_admission_rejects_invalid_rows(tmp_path, table, message):
    entry = build(tmp_path, [('node', 'finish')], '| finish | END | always |', LIMIT_TABLE + table + '\n')

    with pytest.raises(LayoutError, match=message):
        admit(tmp_path, entry)


def test_limit_source_cannot_relabel_another_resource_kind(tmp_path):
    entry = retry_graph(tmp_path, '## Resources\n\n| ID | Kind | Source |\n| --- | --- | --- |\n'
                        '| greeting | prompt | [text](limits.toml) |\n\n' + LIMIT_TABLE +
                        '| PLAN_RETRIES | [config](limits.toml) |\n')

    with pytest.raises(LayoutError, match='limit source has conflicting kind'):
        admit(tmp_path, entry)


def test_limit_key_must_be_unique_across_root_and_component(tmp_path):
    component = '## Flow\n\n1.  [node loop](loop.md.html)\n\n' + LIMIT_TABLE + '| PLAN_RETRIES | [config](limits.toml) |\n'
    entry = build(tmp_path, [('component', 'group'), ('node', 'finish')],
                  '| loop | END | always |\n| group | finish | always |\n| finish | END | always |',
                  LIMIT_TABLE + '| PLAN_RETRIES | [config](limits.toml) |\n', components={'group': component})

    with pytest.raises(LayoutError, match='duplicate limit key'):
        admit(tmp_path, entry)


@pytest.mark.parametrize('config', [{'PLAN_RETRIES': -1}, {'PLAN_RETRIES': 100}, {'PLAN_RETRIES': 1.5},
                                    {'PLAN_RETRIES': True}])
def test_configured_limit_value_is_validated_before_native_effects(tmp_path, config):
    entry = build(tmp_path, [('node', 'finish')], '| finish | END | .plan_retries <= PLAN_RETRIES |\n'
                  '| finish | END | otherwise |', LIMIT_TABLE + '| PLAN_RETRIES | [config](limits.toml) |\n')
    marker = tmp_path / 'loaded'
    write(tmp_path, 'native.py', f'from pathlib import Path\nPath({str(marker)!r}).write_text("x")\n' + NATIVE)
    definition = admit(tmp_path, entry)

    with pytest.raises(LayoutError, match='limit must be an integer from 0 to 99'):
        load_native_bindings(definition, config=config)
    assert not marker.exists()


def test_undeclared_constant_in_edge_or_prompt_condition_is_rejected_at_load(tmp_path):
    entry = build(tmp_path, [('node', 'finish')], '| finish | END | .plan_retries <= PLAN_RETRIES |\n'
                  '| finish | END | otherwise |')

    with pytest.raises(LayoutError, match='flow.md.html:\\d+: condition uses undeclared constant PLAN_RETRIES'):
        admit(tmp_path, entry)

    write(tmp_path, 'prompt.txt', 'Hello')
    entry = build(tmp_path, [('node', 'finish')], '| finish | END | always |')
    write(tmp_path, 'finish.md.html', '''# Node: finish

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| finish | deterministic | [Python](native.py) |

## Prompts

| Node | File | When |
| --- | --- | --- |
| finish | [prompt](prompt.txt) | .plan_retries <= PLAN_RETRIES |
''')
    with pytest.raises(LayoutError, match='finish.md.html:\\d+: condition uses undeclared constant PLAN_RETRIES'):
        admit(tmp_path, entry)


def test_prompt_conditions_may_use_declared_limits(tmp_path):
    write(tmp_path, 'prompt.txt', 'Hello')
    entry = build(tmp_path, [('node', 'finish')], '| finish | END | always |',
                  LIMIT_TABLE + '| PLAN_RETRIES | [config](limits.toml) |\n')
    write(tmp_path, 'finish.md.html', '''# Node: finish

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| finish | deterministic | [Python](native.py) |

## Prompts

| Node | File | When |
| --- | --- | --- |
| finish | [prompt](prompt.txt) | .plan_retries <= PLAN_RETRIES |
''')
    result, _ = run(tmp_path, entry)

    assert result['trail'] == ['finish']


def test_limits_change_the_workflow_identity_and_declaration_digest(tmp_path):
    from api_agent.workflows.identity import declaration_identity, workflow_identity
    entry = retry_graph(tmp_path)
    definition = admit(tmp_path, entry)
    _, first = run(tmp_path, entry)
    _, second = run(tmp_path, entry, config={'TIMEOUT_RETRIES': 3})
    def identity(workflow):
        return workflow_identity(definition, timeouts=workflow.timeouts,
                                 component_passes={}, limits=workflow.limit_values)

    assert identity(first) != identity(second)
    before = declaration_identity(definition)
    write(tmp_path, 'limits.toml', LIMITS.replace('default = 1\n\n[fields.PLAN', 'default = 4\n\n[fields.PLAN'))
    assert declaration_identity(admit(tmp_path, entry)) != before
    with pytest.raises(ValueError, match='limit coverage'):
        workflow_identity(definition, timeouts=first.timeouts, component_passes={}, limits={})


# --- typed component Timeouts --------------------------------------------------

def group_graph(root, body, timeouts='', *, root_sections='', edges=None):
    component = f'## Flow\n\n{body}\n\n{timeouts}'
    return build(root, [('component', 'group'), ('node', 'step_timeout_error'), ('node', 'finish')],
                 edges or '''| plan_request | END | always |
| group | group | .timed_out == "group" and .timeout_retries <= TIMEOUT_RETRIES |
| group | step_timeout_error | .timed_out == "group" and .timeout_retries > TIMEOUT_RETRIES |
| group | finish | otherwise |
| step_timeout_error | END | always |
| finish | END | always |''', root_sections or (LIMIT_TABLE + '| TIMEOUT_RETRIES | [config](limits.toml) |\n'),
                 components={'group': component})


GROUP_FLOW = '1.  [node plan_request](plan_request.md.html)'


def test_component_timeouts_are_enforced_for_the_component_and_its_children(tmp_path):
    entry = group_graph(tmp_path, GROUP_FLOW,
                        PLAIN + '| group | [config](limits.toml) | GROUP_TIMEOUT |\n'
                        '| plan_request | [config](limits.toml) | LONG_TIMEOUT |\n')
    definition = admit(tmp_path, entry)
    events = []

    assert {(row['Owner'], row['_owner'].name) for row in definition.timeouts} == {
        ('group', 'group.md.html'), ('plan_request', 'group.md.html')}
    with pytest.raises(TimeoutError):
        run(tmp_path, entry, events=events)
    starts = {event['owner']: event for event in events if event['event'] == 'scope_start'}

    assert starts['group']['timeout_key'] == 'GROUP_TIMEOUT'
    assert starts['plan_request']['declared_timeout'] == 30.0
    assert starts['plan_request']['effective_timeout'] <= TIMEOUT


@pytest.mark.parametrize(('rows', 'message'), [
    ('| finish | [config](limits.toml) | NODE_TIMEOUT |', 'invalid timeout owner or key'),
    ('| GRAPH | [config](limits.toml) | NODE_TIMEOUT |', 'invalid timeout owner or key'),
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT |\n| plan_request | [config](limits.toml) | NODE_TIMEOUT |',
     'invalid timeout owner or key'),
    ('| plan_request | [config](native.py) | NODE_TIMEOUT |', 'timeout source must be TOML'),
    ('| plan_request | [config](limits.toml) | bad-key |', 'invalid timeout owner or key'),
])
def test_component_timeouts_reject_foreign_duplicate_and_malformed_rows(tmp_path, rows, message):
    entry = group_graph(tmp_path, GROUP_FLOW, PLAIN + rows + '\n')

    with pytest.raises(LayoutError, match=message):
        admit(tmp_path, entry)


def test_component_timeout_owner_may_not_be_a_grandchild_or_repeat_a_root_owner(tmp_path):
    inner = '## Flow\n\n1.  [node plan_request](plan_request.md.html)\n'
    outer = ('## Flow\n\n1.  [component inner](inner.md.html)\n\n' + PLAIN +
             '| plan_request | [config](limits.toml) | NODE_TIMEOUT |\n')
    entry = build(tmp_path, [('component', 'group'), ('node', 'finish')],
                  '| plan_request | END | always |\n| inner | END | always |\n'
                  '| group | finish | always |\n| finish | END | always |',
                  components={'group': outer})
    write(tmp_path, 'inner.md.html', '# Component: inner\n\n' + inner)

    with pytest.raises(LayoutError, match='invalid timeout owner or key'):
        admit(tmp_path, entry)

    outer = '## Flow\n\n1.  [node plan_request](plan_request.md.html)\n\n' + PLAIN + \
            '| plan_request | [config](limits.toml) | NODE_TIMEOUT |\n'
    entry = build(tmp_path, [('component', 'group'), ('node', 'finish')],
                  '| plan_request | END | always |\n| group | finish | always |\n| finish | END | always |',
                  PLAIN + '| plan_request | [config](limits.toml) | LONG_TIMEOUT |\n', components={'group': outer})

    with pytest.raises(LayoutError, match='invalid timeout owner or key'):
        admit(tmp_path, entry)


# --- declared expiry routing ------------------------------------------------------

def test_expired_node_routes_by_its_ordinary_edges_and_records_the_expiry(tmp_path):
    entry = retry_graph(tmp_path)
    events = []

    result, workflow = run(tmp_path, entry, events=events)

    assert result['trail'] == ['plan', 'finish']
    assert result['timeout_retries'] == 1
    assert result['timed_out'] == ''
    ends = [event for event in events if event['event'] == 'scope_end' and event['owner'] == 'plan_request']
    assert [(event['outcome'], event.get('reason')) for event in ends] == [
        ('failed', 'declared_timeout_expired'), ('passed', None)]
    assert workflow.timeouts['plan_request'] == TIMEOUT
    assert any(event['event'] == 'edge' and event.get('condition') ==
               '.timed_out == "plan_request" and .timeout_retries <= TIMEOUT_RETRIES' for event in events)


def test_exhausted_expiry_budget_falls_through_to_the_error_node(tmp_path):
    entry = retry_graph(tmp_path)

    result, _ = run(tmp_path, entry, {'fast_after': 99})

    assert result['trail'] == ['error']
    assert result['next_step'] == 'error'
    assert result['timeout_retries'] == 2
    assert result['timed_out'] == 'plan_request'


def test_configured_limit_extends_the_expiry_budget(tmp_path):
    entry = retry_graph(tmp_path)

    result, _ = run(tmp_path, entry, {'fast_after': 99}, config={'TIMEOUT_RETRIES': 3})

    assert (result['timeout_retries'], result['trail']) == (4, ['error'])


def test_completion_does_not_clear_expiry_fields(tmp_path):
    entry = build(tmp_path, [('node', 'plan_keep'), ('node', 'finish')],
                  '''| plan_keep | plan_keep | .timed_out == "plan_keep" and .next_step != "done" and .timeout_retries < TIMEOUT_RETRIES |
| plan_keep | finish | otherwise |
| finish | END | always |''',
                  LIMIT_TABLE + '| TIMEOUT_RETRIES | [config](limits.toml) |\n\n' + EXPIRY +
                  '| plan_keep | [config](limits.toml) | NODE_TIMEOUT | timed_out | timeout_retries |\n')

    result, _ = run(tmp_path, entry, config={'TIMEOUT_RETRIES': 2})

    assert result['trail'] == ['keep', 'finish']
    assert (result['timed_out'], result['timeout_retries']) == ('plan_keep', 1)


def test_expiry_without_columns_still_fails_the_turn(tmp_path):
    entry = retry_graph(tmp_path, RETRY_SECTIONS.replace(
        EXPIRY + '| plan_request | [config](limits.toml) | NODE_TIMEOUT | timed_out | timeout_retries |',
        PLAIN + '| plan_request | [config](limits.toml) | NODE_TIMEOUT |'))

    with pytest.raises(TimeoutError):
        run(tmp_path, entry)


def test_dash_columns_disable_routing_for_that_row(tmp_path):
    entry = retry_graph(tmp_path, RETRY_SECTIONS.replace('timed_out | timeout_retries |', '- | - |'))

    assert not any('Expired' in row for row in admit(tmp_path, entry).timeouts)
    with pytest.raises(TimeoutError):
        run(tmp_path, entry)


def test_expired_node_with_a_dispatched_effect_still_fails_the_turn(tmp_path):
    entry = build(tmp_path, [('node', 'effectful'), ('node', 'finish')],
                  '| effectful | finish | always |\n| finish | END | always |', EXPIRY +
                  '| effectful | [config](limits.toml) | NODE_TIMEOUT | timed_out | timeout_retries |\n')

    with pytest.raises(TimeoutError):
        run(tmp_path, entry)


def test_ancestor_deadline_and_inner_timeouts_are_not_declared_expiries(tmp_path):
    entry = group_graph(tmp_path, GROUP_FLOW,
                        EXPIRY + '| group | [config](limits.toml) | GROUP_TIMEOUT | - | - |\n'
                        '| plan_request | [config](limits.toml) | LONG_TIMEOUT | timed_out | timeout_retries |\n')
    with pytest.raises(TimeoutError):
        run(tmp_path, entry)

    entry = build(tmp_path, [('node', 'raises_timeout'), ('node', 'finish')],
                  '| raises_timeout | finish | always |\n| finish | END | always |', EXPIRY +
                  '| raises_timeout | [config](limits.toml) | LONG_TIMEOUT | timed_out | timeout_retries |\n')
    with pytest.raises(TimeoutError, match='inner'):
        run(tmp_path, entry)

    entry = build(tmp_path, [('node', 'raises_timeout'), ('node', 'finish')],
                  '| raises_timeout | finish | always |\n| finish | END | always |', EXPIRY +
                  '| raises_timeout | [config](limits.toml) | UNSET_TIMEOUT | timed_out | timeout_retries |\n')
    with pytest.raises(TimeoutError, match='inner'):
        run(tmp_path, entry)


def test_a_scope_refused_before_it_starts_is_not_a_declared_expiry(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from api_agent.workflows import execution
    entry = retry_graph(tmp_path)
    original = execution.component_timeout

    @asynccontextmanager
    async def refuse(name, timeout):
        if name == 'Workflow plan_request':
            raise TimeoutError('already expired')
        async with original(name, timeout) as budget:
            yield budget

    monkeypatch.setattr(execution, 'component_timeout', refuse)

    with pytest.raises(TimeoutError, match='already expired'):
        run(tmp_path, entry)


def test_expiry_journal_stays_valid_and_complete(tmp_path):
    from api_agent.workflows.journal import WorkflowJournal, validate_journal
    entry = retry_graph(tmp_path)
    journal = WorkflowJournal(admit(tmp_path, entry))

    run(tmp_path, entry, journal=journal)
    envelope = journal.finalize()

    assert validate_journal(envelope) is envelope
    assert envelope['completeness'] == 'complete'


@pytest.mark.parametrize(('row', 'message'), [
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT | missing | timeout_retries |',
     'expiry field missing must be a string State field'),
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT | timeout_retries | plan_retries |',
     'expiry field timeout_retries must be a string State field'),
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT | timed_out | missing |',
     'expiry field missing must be an integer State field'),
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT | timed_out | next_step |',
     'expiry field next_step must be an integer State field'),
])
def test_expiry_fields_must_be_typed_state_fields(tmp_path, row, message):
    entry = retry_graph(tmp_path, RETRY_SECTIONS.replace(
        '| plan_request | [config](limits.toml) | NODE_TIMEOUT | timed_out | timeout_retries |', row))
    definition = admit(tmp_path, entry)

    with pytest.raises(LayoutError, match=message):
        Workflow(definition, catalog=load_native_bindings(definition), reducers={})


@pytest.mark.parametrize(('row', 'message'), [
    ('| GRAPH | [config](limits.toml) | NODE_TIMEOUT | timed_out | timeout_retries |',
     'expiry routing requires a node or component owner'),
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT | timed_out | - |',
     'expiry columns must both name State fields or both be -'),
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT | - | timeout_retries |',
     'expiry columns must both name State fields or both be -'),
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT | Bad-Name | timeout_retries |',
     'invalid expiry field name'),
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT | timed_out | 9lives |',
     'invalid expiry field name'),
    ('| plan_request | [config](limits.toml) | NODE_TIMEOUT | timed_out | timed_out |',
     'expiry fields must differ'),
])
def test_expiry_columns_are_validated_at_admission(tmp_path, row, message):
    entry = retry_graph(tmp_path, EXPIRY + row + '\n')

    with pytest.raises(LayoutError, match=message):
        admit(tmp_path, entry)


def test_expiry_columns_require_the_complete_header(tmp_path):
    entry = retry_graph(tmp_path, '## Timeouts\n\n| Owner | Source | Key | Expired |\n| --- | --- | --- | --- |\n'
                        '| plan_request | [config](limits.toml) | NODE_TIMEOUT | timed_out |\n')

    with pytest.raises(LayoutError, match='unknown table columns|wrong table column count'):
        admit(tmp_path, entry)


def test_a_missing_expirations_field_value_starts_counting_at_one(tmp_path):
    entry = retry_graph(tmp_path)
    definition = admit(tmp_path, entry)
    workflow = Workflow(definition, catalog=load_native_bindings(definition), reducers={})

    state = {key: value for key, value in STATE.items() if key != 'timeout_retries'}

    assert workflow._expiry_update('plan_request', state) == {'timed_out': 'plan_request', 'timeout_retries': 1}


# --- component expiry -------------------------------------------------------------

GROUP_EXPIRY = EXPIRY + '| group | [config](limits.toml) | GROUP_TIMEOUT | timed_out | timeout_retries |\n'


def test_expired_component_routes_by_its_edges_and_abandons_its_scopes(tmp_path):
    entry = group_graph(tmp_path, GROUP_FLOW, GROUP_EXPIRY)
    events = []

    result, workflow = run(tmp_path, entry, events=events)

    assert result['trail'] == ['plan', 'finish']
    assert result['timeout_retries'] == 1
    ends = [event for event in events if event['event'] == 'scope_end' and event['owner'] == 'group']
    assert [event['outcome'] for event in ends] == ['failed', 'passed']
    assert ends[0]['reason'] == 'declared_timeout_expired'


def test_expired_component_exhaustion_routes_to_the_error_node(tmp_path):
    entry = group_graph(tmp_path, GROUP_FLOW, GROUP_EXPIRY)

    result, _ = run(tmp_path, entry, {'fast_after': 99})

    assert (result['trail'], result['timed_out'], result['timeout_retries']) == (['error'], 'group', 2)


def test_expired_component_without_columns_fails_the_turn(tmp_path):
    entry = group_graph(tmp_path, GROUP_FLOW, PLAIN + '| group | [config](limits.toml) | GROUP_TIMEOUT |\n')

    with pytest.raises(TimeoutError):
        run(tmp_path, entry)


def test_expired_component_with_a_dispatched_descendant_effect_fails_the_turn(tmp_path):
    entry = group_graph(tmp_path, '1.  [node effectful](effectful.md.html)', GROUP_EXPIRY,
                        edges='''| effectful | END | always |
| group | finish | always |
| step_timeout_error | END | always |
| finish | END | always |''')

    with pytest.raises(TimeoutError):
        run(tmp_path, entry)


def test_expired_outer_component_closes_nested_open_components(tmp_path):
    inner = '## Flow\n\n1.  [node plan_request](plan_request.md.html)\n'
    outer = ('## Flow\n\n1.  [component inner](inner.md.html)\n\n' + EXPIRY +
             '| group | [config](limits.toml) | GROUP_TIMEOUT | timed_out | timeout_retries |\n')
    entry = build(tmp_path, [('component', 'group'), ('node', 'step_timeout_error'), ('node', 'finish')],
                  '''| plan_request | END | always |
| inner | END | always |
| group | group | .timed_out == "group" and .timeout_retries <= TIMEOUT_RETRIES |
| group | step_timeout_error | .timed_out == "group" and .timeout_retries > TIMEOUT_RETRIES |
| group | finish | otherwise |
| step_timeout_error | END | always |
| finish | END | always |''', LIMIT_TABLE + '| TIMEOUT_RETRIES | [config](limits.toml) |\n',
                  components={'group': outer})
    write(tmp_path, 'inner.md.html', '# Component: inner\n\n' + inner)
    events = []

    result, _ = run(tmp_path, entry, events=events)

    assert result['trail'] == ['plan', 'finish']
    closed = [(event['owner'], event['outcome']) for event in events
              if event['event'] == 'scope_end' and event['owner'] in ('group', 'inner')]
    assert closed.count(('inner', 'failed')) == 1 and closed.count(('group', 'failed')) == 1


def test_component_expiry_fields_are_validated_against_state(tmp_path):
    entry = group_graph(tmp_path, GROUP_FLOW, GROUP_EXPIRY.replace('timeout_retries', 'missing'))
    definition = admit(tmp_path, entry)

    with pytest.raises(LayoutError, match='expiry field missing must be an integer State field'):
        Workflow(definition, catalog=load_native_bindings(definition), reducers={})


def test_limit_source_must_be_valid_configuration(tmp_path):
    entry = build(tmp_path, [('node', 'finish')], '| finish | END | always |',
                  LIMIT_TABLE + '| PLAN_RETRIES | [config](broken.toml) |\n')
    write(tmp_path, 'broken.toml', 'broken = [\n')

    with pytest.raises(LayoutError, match='broken.toml|flow.md.html:\\d+: invalid limit declaration'):
        admit(tmp_path, entry)


def test_a_component_refused_before_it_starts_is_not_a_declared_expiry(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from api_agent.workflows import execution
    entry = group_graph(tmp_path, GROUP_FLOW, GROUP_EXPIRY)
    original = execution.component_timeout

    @asynccontextmanager
    async def refuse(name, timeout):
        if name == 'Workflow group':
            raise TimeoutError('already expired')
        async with original(name, timeout) as budget:
            yield budget

    monkeypatch.setattr(execution, 'component_timeout', refuse)

    with pytest.raises(TimeoutError, match='already expired'):
        run(tmp_path, entry)


# --- compiled plans ---------------------------------------------------------------

def test_compiled_plans_reject_typed_limits_and_expiry_columns(tmp_path):
    from api_agent.workflows.artifact_compile import ArtifactCompileError, compile_definition
    limited = build(tmp_path, [('node', 'finish')],
                    '| finish | END | .plan_retries <= PLAN_RETRIES |\n| finish | END | otherwise |',
                    LIMIT_TABLE + '| PLAN_RETRIES | [config](limits.toml) |\n')
    with pytest.raises(ArtifactCompileError, match='declared limits and timeout expiry routing'):
        compile_definition(admit(tmp_path, limited), ())

    expiring = build(tmp_path, [('node', 'effectful'), ('node', 'finish')],
                     '| effectful | finish | always |\n| finish | END | always |', EXPIRY +
                     '| effectful | [config](limits.toml) | NODE_TIMEOUT | timed_out | timeout_retries |\n')
    with pytest.raises(ArtifactCompileError, match='declared limits and timeout expiry routing'):
        compile_definition(admit(tmp_path, expiring), ())


# --- viewer -------------------------------------------------------------------------

def view_source(projection, stable_id):
    from html import unescape
    from api_agent.workflows.viewer_render import _projection_source
    document = next(item for item in projection.documents if item.stable_id == stable_id)
    source, links = _projection_source(projection, document)
    return unescape(source.decode()), links


def test_typed_graph_timeout_limits_and_component_timeouts_render_in_their_declaring_diagrams(tmp_path):
    from api_agent.workflows.viewer_projection import project_view
    entry = group_graph(
        tmp_path, GROUP_FLOW,
        LIMIT_TABLE + '| PLAN_RETRIES | [config](limits.toml) |\n\n' + PLAIN +
        '| group | [config](limits.toml) | GROUP_TIMEOUT |\n',
        root_sections=LIMIT_TABLE + '| TIMEOUT_RETRIES | [config](limits.toml) |\n\n' + PLAIN +
        '| GRAPH | [config](limits.toml) | LONG_TIMEOUT |\n')
    projection = project_view(admit(tmp_path, entry))

    assert {(ref.kind, ref.owner_id, dict(ref.attributes)['key'])
            for ref in projection.references if ref.kind in ('limit', 'timeout')} == {
        ('limit', 'group', 'PLAN_RETRIES'), ('limit', None, 'TIMEOUT_RETRIES'),
        ('timeout', 'group', 'GROUP_TIMEOUT'), ('timeout', None, 'LONG_TIMEOUT')}
    root, root_links = view_source(projection, 'limits_flow')
    group, group_links = view_source(projection, 'group')

    assert 'Timeout: LONG_TIMEOUT' in root and 'Timeout: GROUP_TIMEOUT' in root
    assert 'Configuration: TIMEOUT_RETRIES' in root and 'PLAN_RETRIES' not in root
    assert 'Timeout: GROUP_TIMEOUT' in group and 'Timeout: LONG_TIMEOUT' in group
    assert 'Configuration: PLAN_RETRIES' in group and 'TIMEOUT_RETRIES' not in group
    assert any(link.endswith('limits.toml') for link in root_links)
    assert any(link.endswith('limits.toml') for link in group_links)


# --- compiled plans ---------------------------------------------------------------

def test_expired_inner_component_leaves_its_open_ancestor_running(tmp_path):
    inner = ('## Flow\n\n1.  [node plan_request](plan_request.md.html)\n\n' + EXPIRY +
             '| inner | [config](limits.toml) | GROUP_TIMEOUT | timed_out | timeout_retries |\n')
    outer = '## Flow\n\n1.  [component inner](inner.md.html)\n2.  [node finish](finish.md.html)\n'
    entry = build(tmp_path, [('component', 'group')],
                  '''| plan_request | END | always |
| inner | inner | .timed_out == "inner" and .timeout_retries <= TIMEOUT_RETRIES |
| inner | finish | otherwise |
| finish | END | always |
| group | END | always |''', LIMIT_TABLE + '| TIMEOUT_RETRIES | [config](limits.toml) |\n',
                  components={'group': outer, 'inner': inner})
    events = []

    result, _ = run(tmp_path, entry, events=events)

    assert result['trail'] == ['plan', 'finish']
    ends = {(event['owner'], event['outcome']) for event in events if event['event'] == 'scope_end'}
    assert ('inner', 'failed') in ends and ('group', 'passed') in ends
