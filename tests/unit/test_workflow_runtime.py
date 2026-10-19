"""Runtime adversarial cases execute only local deterministic functions."""
import asyncio
from dataclasses import replace
from pathlib import Path
import runpy

import pytest

from api_agent.workflows import GuardResult, LayoutError, NodeFunction, Workflow, WorkflowError, load_layout
from api_agent.workflows.__main__ import main
from api_agent.workflows.contrived import guard, validate_format
from api_agent.workflows.execution import NodeContext

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runtime_layout():
    layout = load_layout(ROOT / 'demo/contrived/layout.md', base_root=ROOT, pack_root=ROOT / 'demo')
    return replace(layout, steps=tuple(replace(step, source=Path(__file__).resolve()) if step.kind == 'node' else step for step in layout.steps))


def local_format(inputs, context):
    return validate_format(inputs, context)


def local_guard(inputs, context):
    return guard(inputs, context)


def produce(inputs, context):
    return dict(original_input=inputs['user_input'], route='skip', ready=True, final_output=inputs['user_input'])


def workflow(layout, producer=produce, **kwargs):
    return Workflow(layout, {'produce': NodeFunction(producer), 'validate_format': NodeFunction(local_format), 'guard': NodeFunction(local_guard)},
                    kwargs.pop('conditions', {'skip_second': lambda values: True}), **kwargs)


@pytest.mark.parametrize('entry', [None, NodeFunction(None), NodeFunction(produce, 'unknown')])
def test_registry_contract_rejects_invalid_entry(runtime_layout, entry):
    with pytest.raises(LayoutError, match='registered node function'):
        Workflow(runtime_layout, {'produce': entry})


@pytest.mark.parametrize('function', [guard, len])
def test_registry_source_must_match_declaration(runtime_layout, function):
    with pytest.raises(LayoutError, match='source link'):
        workflow(runtime_layout, function)



@pytest.mark.parametrize('alias_kind', ['hardlink', 'symlink'])
def test_registry_accepts_distinct_paths_to_same_source(runtime_layout, tmp_path, alias_kind):
    source = tmp_path / 'node.py'
    alias = tmp_path / 'node-alias.py'
    source.write_text('def node(inputs, context):\n    return {}\n')
    if alias_kind == 'hardlink':
        import os
        os.link(source, alias)
    else:
        alias.symlink_to(source)
    namespace = {}
    exec(compile(source.read_text(), str(source), 'exec'), namespace)
    layout = replace(runtime_layout, steps=tuple(replace(step, source=alias)
        if step.kind == 'node' else step for step in runtime_layout.steps))
    registry = {row['Registry']: NodeFunction(namespace['node']) for row in layout.tables['Functions']}

    compiled = Workflow(layout, registry, {'skip_second': lambda values: True})

    assert source != alias and source.samefile(alias)
    assert compiled.registry == registry


@pytest.mark.parametrize('unavailable', ['missing', 'inaccessible'])
def test_registry_rejects_unavailable_declared_source(runtime_layout, tmp_path, monkeypatch, unavailable):
    source = tmp_path / 'unavailable.py'
    if unavailable == 'inaccessible':
        source.write_text('')
        samefile = Path.samefile
        def denied(path, other):
            if other == source:
                raise PermissionError('synthetic inaccessible source')
            return samefile(path, other)
        monkeypatch.setattr(Path, 'samefile', denied)
    layout = replace(runtime_layout, steps=tuple(replace(step, source=source)
        if step.kind == 'node' else step for step in runtime_layout.steps))

    with pytest.raises(LayoutError, match='source link'):
        workflow(layout)

def test_unknown_condition_rejected_before_execution(runtime_layout):
    with pytest.raises(LayoutError, match='registered condition'):
        workflow(runtime_layout, conditions={})


@pytest.mark.parametrize('value', [{}, {'accepted': 1, 'feedback': {}}, {'accepted': True, 'feedback': []}, []])
def test_guard_has_strict_contract(value):
    with pytest.raises(WorkflowError, match='Guard must return'):
        GuardResult.from_output(value)


def test_top_level_context_has_single_pass():
    context = NodeContext('node', (), (), {})
    assert (context.attempt, context.max_passes) == (1, 1)


def test_missing_start_binding_fails_before_function(runtime_layout):
    with pytest.raises(WorkflowError, match='Missing required binding'):
        asyncio.run(workflow(runtime_layout).arun({}))


@pytest.mark.parametrize('value', [{'value': float('inf')}, {'value': object()}, {'value': (1, 2)}, {1: 'nonstring key'}])
def test_non_json_output_is_rejected(value):
    with pytest.raises(WorkflowError, match='non-JSON output'):
        Workflow._validate({'type': 'object'}, value, 'example', 'output')


@pytest.mark.parametrize('edges,conditions', [
    ([{'From': 'first', 'To': 'second', 'When': 'choose'}], {'choose': lambda values: False}),
    ([{'From': 'first', 'To': 'second', 'When': 'choose'}, {'From': 'first', 'To': 'END', 'When': 'other'}],
     {'choose': lambda values: True, 'other': lambda values: True}),
])
def test_ambiguous_or_unmatched_route_fails(runtime_layout, edges, conditions):
    runtime_layout.tables['Edges'] = edges
    with pytest.raises(WorkflowError, match='exactly one'):
        asyncio.run(workflow(runtime_layout, conditions=conditions).arun({'user_input': 'x'}))


def test_guard_must_execute_in_current_pass(runtime_layout):
    runtime_layout.tables['Edges'].append({'From': 'first_format', 'To': 'END', 'When': 'always'})
    with pytest.raises(WorkflowError, match='current pass'):
        asyncio.run(workflow(runtime_layout).arun({'user_input': 'x'}))


def test_retry_cannot_bind_stale_skipped_output(runtime_layout):
    calls = []
    def retry_producer(inputs, context):
        calls.append(inputs)
        return dict(original_input=inputs['user_input'], route='skip', ready=context.attempt > 1, final_output='x')
    runtime_layout.tables['Edges'].extend([{'From': 'first_draft', 'To': 'first_guard', 'When': 'second_pass', 'line': 1}, {'From': 'first_draft', 'To': 'first_format', 'When': 'always', 'line': 1}])
    binding = next(row for row in runtime_layout.tables['Bindings'] if row['Node'] == 'first_guard')
    binding['From'] = 'first_format'
    binding['Field'] = '*'
    # The first-pass guard receives the format output; retain a permissive contract.
    runtime_layout.tables['Functions'][2]['Input schema'] = {'type': 'object'}
    def reject(inputs, context):
        return {'accepted': False, 'feedback': {'reason': 'again'}}
    compiled = Workflow(runtime_layout, {'produce': NodeFunction(retry_producer), 'validate_format': NodeFunction(local_format), 'guard': NodeFunction(reject)},
                        {'skip_second': lambda values: True, 'second_pass': lambda values: values['first_draft']['ready']})
    with pytest.raises(WorkflowError, match='Missing required binding for first_guard.draft'):
        asyncio.run(compiled.arun({'user_input': 'x'}))
    assert calls[1]['previous']['ready'] is False
    assert calls[1]['feedback'] == {'reason': 'again'}


def timeout_row(tmp_path, text, owner='GRAPH', key='LIMIT'):
    path = tmp_path / 'timeouts.toml'
    path.write_text(text)
    return {'Owner': owner, 'Source': path, 'Key': key, 'line': 1}


@pytest.mark.parametrize('declaration,key,config,expected', [
    ('invalid', 'LIMIT', {}, 'Expected'),
    ('version=1\n[fields.LIMIT]\ntype="str"', 'LIMIT', {}, 'numeric'),
    ('version=1\n[fields.LIMIT]\ntype="float"', 'MISSING', {}, 'numeric'),
    ('version=1\n[fields.LIMIT]\ntype="float"', 'LIMIT', {'LIMIT': 0}, 'positive'),
    ('version=1\n[fields.LIMIT]\ntype="float"', 'LIMIT', {'LIMIT': True}, 'positive'),
])
def test_invalid_timeout_declaration(runtime_layout, tmp_path, declaration, key, config, expected):
    runtime_layout.tables['Timeouts'] = [timeout_row(tmp_path, declaration, key=key)]
    with pytest.raises(LayoutError, match=expected):
        workflow(runtime_layout, config=config)


def test_optional_timeout_and_config_override(runtime_layout, tmp_path):
    runtime_layout.tables['Timeouts'] = [timeout_row(tmp_path, 'version=1\n[fields.LIMIT]\ntype="float"')]
    assert workflow(runtime_layout).timeouts == {'GRAPH': None}
    assert workflow(runtime_layout, config={'LIMIT': 2.0}).timeouts == {'GRAPH': 2.0}


def test_parent_deadline_is_retained_across_retries(runtime_layout, tmp_path):
    runtime_layout.tables['Timeouts'] = [timeout_row(tmp_path, 'version=1\n[fields.LIMIT]\ntype="float"\ndefault=0.05', owner='first')]
    attempts, events = [], []
    async def slow(inputs, context):
        attempts.append(context.attempt)
        await asyncio.sleep(0.03)
        return dict(original_input=inputs['user_input'], route='skip', ready=False, final_output='x')
    with pytest.raises(TimeoutError):
        asyncio.run(workflow(runtime_layout, slow, services={'observe': events.append}).arun({'user_input': 'x'}))
    assert attempts == [1, 2]
    ends = [event for event in events if event['event'] == 'scope_end']
    assert ends[-1]['outcome'] == 'failed'
    component = next(event for event in ends if event['owner'] == 'first')
    assert component['timeout_key'] == 'LIMIT' and component['declared_timeout'] == 0.05
    assert component['outcome'] == 'failed'


def test_conditional_prompt_and_compiled_topology(runtime_layout):
    runtime_layout.tables['Prompts'][0]['When'] = 'choose'
    prompts = []
    def capture(inputs, context):
        prompts.append(context.prompts)
        return produce(inputs, context)
    compiled = workflow(runtime_layout, capture, conditions={'skip_second': lambda values: True, 'choose': lambda values: False})
    asyncio.run(compiled.arun({'user_input': 'x'}))
    assert prompts == [()]
    diagram = compiled.diagram()
    assert 'first_draft --> first_format' in diagram
    assert 'first --> first_guard' not in diagram
    assert 'subgraph first' in diagram and 'first_END -. reject, at most 2 passes .-> first_START' in diagram


@pytest.mark.parametrize('command', ['validate', 'diagram'])
def test_inference_free_demo_cli(command, capsys):
    assert main([command, '--demo', 'contrived']) == 0
    assert ('Valid layout:' if command == 'validate' else 'flowchart TD') in capsys.readouterr().out


def test_cli_explicit_roots_and_invalid_path(tmp_path, capsys):
    path = ROOT / 'demo/contrived/layout.md'
    assert main(['validate', '--layout', str(path), '--base-root', str(ROOT), '--pack-root', str(path.parent)]) == 0
    with pytest.raises(SystemExit) as caught:
        main(['validate', '--layout', str(tmp_path / 'absent.md')])
    assert caught.value.code == 2
    assert 'absent.md:1' in capsys.readouterr().err


@pytest.mark.parametrize('args', [['validate'], ['validate', '--demo', 'contrived', '--layout', 'x']])
def test_cli_requires_exactly_one_layout_choice(args):
    with pytest.raises(SystemExit) as caught:
        main(args)
    assert caught.value.code == 2


def test_module_cli_entrypoint(monkeypatch, capsys):
    monkeypatch.delitem(__import__('sys').modules, 'api_agent.workflows.__main__')
    monkeypatch.setattr('sys.argv', ['api_agent.workflows', 'validate', '--demo', 'contrived'])
    with pytest.raises(SystemExit) as caught:
        runpy.run_module('api_agent.workflows', run_name='__main__')
    assert caught.value.code == 0
    assert 'Valid layout:' in capsys.readouterr().out


def test_scope_lifecycle_preserves_nested_identity_and_success(runtime_layout):
    events = []
    asyncio.run(workflow(runtime_layout, services={'observe': events.append}).arun({'user_input': 'x'}))
    starts = {event['scope_id']: event for event in events if event['event'] == 'scope_start'}
    ends = [event for event in events if event['event'] == 'scope_end']
    assert len(starts) == len(ends) == 5
    for event in ends:
        start = starts[event['scope_id']]
        assert event['outcome'] == 'passed'
        assert event['parent_id'] == start['parent_id']
        assert event['stopped_ms'] >= event['started_ms'] and event['elapsed'] >= 0
    graph = next(event for event in starts.values() if event['owner'] == 'GRAPH')
    component = next(event for event in starts.values() if event['owner'] == 'first')
    node = next(event for event in starts.values() if event['owner'] == 'first_draft')
    assert graph['parent_id'] is None
    assert component['parent_id'] == graph['scope_id']
    assert node['parent_id'] == component['scope_id']
    attempt = next(event for event in events if event['event'] == 'attempt')
    guarded = next(event for event in events if event['event'] == 'guard')
    completed = next(event for event in events if event['event'] == 'node')
    assert attempt['scope_id'] == guarded['scope_id'] == component['scope_id']
    assert completed['scope_id'] == node['scope_id']


def test_failed_scope_preserves_original_exception_when_observer_fails(runtime_layout):
    ends = []
    def observe(event):
        if event['event'] == 'scope_end':
            ends.append(event)
            raise RuntimeError('observer failure')
    def broken(inputs, context):
        raise ValueError('original failure')
    with pytest.raises(ValueError, match='original failure'):
        asyncio.run(workflow(runtime_layout, broken, services={'observe': observe}).arun({'user_input': 'x'}))
    assert len(ends) == 3 and all(event['outcome'] == 'failed' for event in ends)


def test_cancelled_scope_reports_cancellation(runtime_layout):
    events = []
    started = asyncio.Event()
    async def cancelled(inputs, context):
        started.set()
        await asyncio.Event().wait()
    async def cancel_run():
        task = asyncio.create_task(workflow(runtime_layout, cancelled, services={'observe': events.append}).arun({'user_input': 'x'}))
        await started.wait()
        task.cancel()
        await task
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_run())
    ends = [event for event in events if event['event'] == 'scope_end']
    assert len(ends) == 3 and all(event['outcome'] == 'cancelled' for event in ends)


def test_successful_scope_observer_failure_propagates(runtime_layout):
    def observe(event):
        if event['event'] == 'scope_end':
            raise ValueError('observer failure')
    with pytest.raises(ValueError, match='observer failure'):
        asyncio.run(workflow(runtime_layout, services={'observe': observe}).arun({'user_input': 'x'}))


def test_nested_components_reset_independent_passes_and_restore_parent_feedback(runtime_layout):
    from api_agent.workflows.layout import Step
    seen = []
    def nested_produce(inputs, context):
        seen.append((context.ancestry, inputs['previous'], inputs['feedback']))
        return dict(original_input=inputs['user_input'], route='skip', ready=context.attempt == 2, final_output='x')
    def outer_guard(inputs, context):
        return {'accepted': context.attempt == 2, 'feedback': {'reason': 'outer correction'}}
    runtime_layout = replace(runtime_layout, steps=(
        Step('outer', 'component', None, runtime_layout.path, 1),
        *(replace(step, parent='outer') if step.id == 'first' else step for step in runtime_layout.steps[:4]),
        Step('outer_guard', 'node', 'outer', Path(__file__).resolve(), 1),
        *runtime_layout.steps[4:],
    ))
    runtime_layout.tables['Components'].append({'ID': 'outer', 'Max passes': '2', 'Guard': 'outer_guard'})
    runtime_layout.tables['Functions'].append({**runtime_layout.tables['Functions'][2], 'ID': 'outer_guard', 'Registry': 'outer_guard'})
    runtime_layout.tables['Bindings'].append({'Node': 'outer_guard', 'Input': 'draft', 'From': 'first_draft', 'Field': '*', 'Default': '-'})
    runtime_layout.tables['Edges'] = [{'From': 'outer', 'To': 'END', 'When': 'always'}]
    compiled = Workflow(runtime_layout, {'produce': NodeFunction(nested_produce), 'validate_format': NodeFunction(local_format),
                                        'guard': NodeFunction(local_guard), 'outer_guard': NodeFunction(outer_guard)})
    result = asyncio.run(compiled.arun({'user_input': 'x'}))
    assert [ancestry for ancestry, _, _ in seen] == [
        (('outer', 1, 2), ('first', 1, 2)), (('outer', 1, 2), ('first', 2, 2)),
        (('outer', 2, 2), ('first', 1, 2)), (('outer', 2, 2), ('first', 2, 2)),
    ]
    assert seen[0][1:] == seen[2][1:] == (None, None)
    assert seen[1][1]['ready'] is False and seen[3][1]['ready'] is False
    assert result['outer_guard']['accepted'] is True


def test_registered_conditions_require_boolean_results(runtime_layout):
    compiled = workflow(runtime_layout, conditions={'skip_second': lambda values: 'yes'})
    with pytest.raises(WorkflowError, match='must return a boolean'):
        asyncio.run(compiled.arun({'user_input': 'x'}))


def test_condition_mutation_cannot_change_execution_state(runtime_layout):
    def mutate(values):
        values['first_draft']['original_input'] = 'changed'
        return True
    result = asyncio.run(workflow(runtime_layout, conditions={'skip_second': mutate}).arun({'user_input': 'x'}))
    assert result['first_draft']['original_input'] == 'x'


def test_whole_empty_source_object_is_bound_without_default(runtime_layout):
    inputs_seen = []
    def capture(inputs, context):
        inputs_seen.append(inputs)
        return {'original_input': 'x', 'route': 'skip', 'ready': True, 'final_output': 'x'}
    runtime_layout.tables['Bindings'] = [row for row in runtime_layout.tables['Bindings'] if row['Node'] != 'first_draft']
    runtime_layout.tables['Bindings'].append({'Node': 'first_draft', 'Input': 'empty', 'From': 'START', 'Field': '*', 'Default': '-'})
    runtime_layout.tables['Functions'][0]['Input schema'] = {'type': 'object', 'properties': {'empty': {'type': 'object'}}, 'required': ['empty']}
    # End after the producer; this test isolates whole-value binding.
    runtime_layout.tables['Edges'].append({'From': 'first_draft', 'To': 'END', 'When': 'always'})
    with pytest.raises(WorkflowError, match='current pass'):
        asyncio.run(workflow(runtime_layout, capture).arun({}))
    assert inputs_seen == [{'empty': {}}]


def test_guard_exhaustion_reports_failed_component_lifecycle(runtime_layout):
    events = []
    def rejected(inputs, context):
        return dict(original_input=inputs['user_input'], route='skip', ready=False, final_output='x')
    compiled = workflow(runtime_layout, rejected, services={'observe': events.append})

    with pytest.raises(WorkflowError, match='Component first exhausted 2 passes'):
        asyncio.run(compiled.arun({'user_input': 'x'}))

    guards = [event for event in events if event['event'] == 'guard']
    assert len(guards) == 2 and all(event['accepted'] is False for event in guards)
    component = next(event for event in events if event['event'] == 'scope_end' and event['owner'] == 'first')
    assert all(event['scope_id'] == component['scope_id'] for event in guards)
    assert component['outcome'] == 'failed'
    graph = next(event for event in events if event['event'] == 'scope_end' and event['owner'] == 'GRAPH')
    assert graph['outcome'] == 'failed'


def test_async_node_cannot_accept_output_after_swallowing_timeout(runtime_layout, tmp_path):
    events = []
    runtime_layout.tables['Timeouts'] = [timeout_row(tmp_path, 'version=1\n[fields.LIMIT]\ntype="float"\ndefault=0.01', owner='first_draft')]
    async def ignore_timeout(inputs, context):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return produce(inputs, context)
    compiled = workflow(runtime_layout, ignore_timeout, services={'observe': events.append})

    with pytest.raises(TimeoutError, match='deadline expired'):
        asyncio.run(compiled.arun({'user_input': 'x'}))

    assert not any(event['event'] == 'node' for event in events)
    node_end = next(event for event in events if event['event'] == 'scope_end' and event['owner'] == 'first_draft')
    assert node_end['outcome'] == 'failed'


def test_scope_cannot_report_success_after_body_swallows_timeout(runtime_layout, tmp_path):
    events = []
    runtime_layout.tables['Timeouts'] = [timeout_row(tmp_path, 'version=1\n[fields.LIMIT]\ntype="float"\ndefault=0.01', owner='first')]
    compiled = workflow(runtime_layout, services={'observe': events.append})
    async def swallowed():
        async with compiled._scope('first'):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass

    with pytest.raises(TimeoutError, match='deadline expired'):
        asyncio.run(swallowed())

    assert events[-1]['event'] == 'scope_end' and events[-1]['outcome'] == 'failed'


def test_sync_node_offload_preserves_context_and_drains_before_timeout(runtime_layout, tmp_path, monkeypatch):
    from contextvars import ContextVar
    import threading
    from api_agent.runtime import tasks

    context_value = ContextVar('workflow_test_value', default='missing')
    started, finished, release = (threading.Event() for _ in range(3))
    captured, observations, delivered, drained = [], [], [], []
    runtime_layout.tables['Timeouts'] = [timeout_row(tmp_path, 'version=1\n[fields.LIMIT]\ntype="float"\ndefault=0.02', owner='first_draft')]
    original_settle = tasks.settle
    loop = dispatched = draining = None

    async def observe_drain(task):
        draining.set()
        result = await original_settle(task)
        drained.append(task.done())
        return result

    monkeypatch.setattr(tasks, 'settle', observe_drain)

    def blocking(inputs, context):
        captured.append(context_value.get())
        started.set()
        loop.call_soon_threadsafe(dispatched.set)
        assert release.wait(1), 'observer did not release the owned thread'
        finished.set()
        return produce(inputs, context)

    async def run():
        nonlocal loop, dispatched, draining
        loop = asyncio.get_running_loop()
        dispatched, draining = asyncio.Event(), asyncio.Event()
        context_value.set('preserved')
        # Construction failures must not leave an observer awaiting dispatch.
        compiled = workflow(runtime_layout, blocking)

        async def observe_responsiveness():
            try:
                await asyncio.wait_for(dispatched.wait(), 1)
                observations.append(finished.is_set())
                await asyncio.wait_for(draining.wait(), 1)
            finally:
                release.set()

        observer = asyncio.create_task(observe_responsiveness())
        try:
            await compiled.arun({'user_input': 'x'})
        except TimeoutError:
            delivered.append(finished.is_set())
            raise
        finally:
            await observer

    with pytest.raises(TimeoutError):
        asyncio.run(run())

    assert started.is_set() and draining.is_set()
    assert observations == [False]
    assert captured == ['preserved']
    assert delivered == [True]
    assert drained == [True]
    assert finished.is_set()
