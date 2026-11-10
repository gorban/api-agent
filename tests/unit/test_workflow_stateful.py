"""Synthetic stateful workflow contract checks."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages
import pytest

from api_agent.workflows.definition import Replacement
from api_agent.workflows.execution import (Workflow)
from api_agent.workflows.native import (load_native_bindings)
from api_agent.workflows.definition import (load_definition)
from api_agent.workflows.layout import LayoutError
from api_agent.runtime.resource_pack import (ResourcePackError, load_launch_context,
                                             load_selected_definition, load_workflow)
from api_agent.workflows.__main__ import main as workflow_cli
from api_agent.workflows.journal import WorkflowJournal, validate_journal


def _write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    return path


@pytest.fixture
def fixture_graph(tmp_path):
    root = tmp_path
    _write(root, 'native.py', '''from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import AIMessage

class State(TypedDict):
    messages: Annotated[list, add_messages]
    pending: bool
    visits: int

def intake(state, context):
    return {'messages': [AIMessage(id='answer', content='new')], 'pending': False,
            'visits': state['visits'] + 1}

def finish(state, context):
    return {'pending': True}

def choose(state, context):
    return 'finish' if not state['pending'] else 'END'
''')
    _write(root, 'intake.md.html', '''# Node: intake

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| intake | deterministic | [Python](native.py) |
''')
    _write(root, 'finish.md.html', '''# Node: finish

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| finish | deterministic | [Python](native.py) |
''')
    entry = _write(root, 'flow.md.html', '''# Workflow

## Graph ID

| ID |
| --- |
| synthetic_flow |

## State

| Registry | Source |
| --- | --- |
| State | [Python](native.py) |

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)

## Edges

| From | To | Router | Source |
| --- | --- | --- | --- |
| intake | finish | choose | [Python](native.py) |
| intake | END | choose | [Python](native.py) |
| finish | END | - | - |
''')
    return root, entry


def test_admitted_state_continues_at_entry_with_message_id_replacement(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    catalog = load_native_bindings(definition)
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})

    first = asyncio.run(workflow.arun({'messages': [HumanMessage(id='human', content='hello')], 'pending': False,
                                      'visits': 0},
                                      thread_id='conversation'))
    second = asyncio.run(workflow.arun({'messages': [AIMessage(id='answer', content='old')]},
                                       thread_id='conversation'))

    assert [message.content for message in first['messages']] == ['hello', 'new']
    assert [message.content for message in second['messages']] == ['hello', 'new']
    assert second['pending'] is True
    assert (first['visits'], second['visits']) == (1, 2)


def test_stateful_cycle_keeps_node_history_but_a_new_turn_starts_fresh(fixture_graph):
    import json
    from api_agent.llm_providers.gpt.inference import _result, validate_text_request
    from api_agent.providers.llm import LLMResult, Message, TextContent
    root, entry = fixture_graph
    native = root / 'native.py'
    native.write_text(native.read_text().replace('def intake(state, context):', '''async def intake(state, context):
    from api_agent.providers.llm import LLMRequest, CompletionRequest, Message, TextContent
    from uuid import uuid4
    request = LLMRequest('openai/gpt-6.1-terra', CompletionRequest((
        Message('system', (TextContent('Keep prior context'),)),
        Message('user', (TextContent('Visit ' + str(state['visits'])),)))), str(uuid4()))
    await context.services['complete'](request)
''').replace("return 'finish' if not state['pending'] else 'END'",
             "return 'intake' if state['visits'] < 2 else 'END'"))
    entry.write_text(entry.read_text().replace('2.  [node finish](finish.md.html)\n', '').replace(
        '| intake | finish | choose |', '| intake | intake | choose |').replace(
        '| finish | END | - | - |\n', ''))
    definition = load_definition(entry, base_root=root, pack_root=root)
    requests = []
    native = [
        {'type': 'reasoning', 'id': 'rs-1', 'status': 'completed', 'summary': [],
         'encrypted_content': 'opaque-retry-state', 'format': 'synthetic'},
        {'type': 'message', 'id': 'msg-1', 'status': 'completed', 'role': 'assistant',
         'content': [{'type': 'output_text', 'text': 'Prior response', 'annotations': []}]},
    ]
    assistant = _result({'status': 'completed', 'output': native}, True,
                        'credential-sentinel', openrouter=True).payload
    async def complete(value):
        requests.append(value)
        return LLMResult(assistant)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
        reducers={'messages':add_messages}, services={'complete':complete})

    async def turns():
        for _ in range(2):
            final = await workflow.arun({'messages':[], 'pending':False, 'visits':0}, thread_id='same-conversation')
            assert final['visits'] == 2
    asyncio.run(turns())

    assert len(requests) == 4
    assert requests[1].payload.messages[:2] == requests[0].payload.messages
    assert requests[1].payload.messages[2] is assistant
    assert json.loads(assistant.continuation)['state'] == native
    _, wire = validate_text_request(base_url='https://openrouter.ai/api/v1',
        access_token='credential-sentinel', subscription=False, api_mode='responses', request=requests[1])
    assert wire['input'][2:4] == native
    assert requests[1].payload.messages[3].content[0].text == 'Visit 1'
    assert len(requests[2].payload.messages) == 2
    assert requests[2].payload.messages == requests[0].payload.messages
    assert requests[3].payload.messages[:2] == requests[2].payload.messages


def test_source_change_is_rejected_before_catalog_execution(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    (root / 'native.py').write_text((root / 'native.py').read_text() + '\n# changed\n')

    with pytest.raises(LayoutError, match='resource changed'):
        load_native_bindings(definition)


def test_nested_component_uses_declared_subgraph(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)
''')
    original = entry.read_text()
    original = original.replace('1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
                                '1.  [component group](group.md.html)')
    original = original.replace('| finish | END | - | - |',
                                '| finish | END | - | - |\n| group | END | - | - |')
    entry.write_text(original)
    definition = load_definition(entry, base_root=root, pack_root=root)
    catalog = load_native_bindings(definition)
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})

    result = asyncio.run(workflow.arun({'messages': [HumanMessage(content='hello')],
                                        'pending': False, 'visits': 0}, thread_id='nested'))

    assert result['visits'] == 1
    assert result['pending'] is True


def test_declared_helper_is_captured_and_checked(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'helper.py', 'def next_count(value):\n    return value + 1\n')
    source = root / 'native.py'
    source.write_text(source.read_text().replace("state['visits'] + 1", "next_count(state['visits'])"))
    entry.write_text(entry.read_text() + '''\n## Native Sources

| Source |
| --- |
| [Python](helper.py) |
''')
    definition = load_definition(entry, base_root=root, pack_root=root)
    catalog = load_native_bindings(definition)
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})

    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                        thread_id='helpers'))
    (root / 'helper.py').write_text('def next_count(value):\n    return value + 2\n')

    assert result['visits'] == 1
    with pytest.raises(LayoutError, match='resource changed'):
        load_native_bindings(definition)


def test_resource_replacement_is_selected_before_original_read(fixture_graph):
    root, entry = fixture_graph
    pack = root / 'pack'
    _write(pack, 'selected.txt', 'selected response')
    entry.write_text(entry.read_text() + '''\n## Resources

| ID | Kind | Source |
| --- | --- | --- |
| greeting | prompt | [text](missing.txt) |
''')
    source = root / 'native.py'
    source.write_text(source.read_text().replace("content='new'",
                        "content=context.resources['greeting'].content_bytes.decode()"))
    definition = load_definition(entry, base_root=root, pack_root=pack,
                                           replacements=(Replacement('greeting', Path('selected.txt'), 'pack'),))
    catalog = load_native_bindings(definition)
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})

    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                        thread_id='replacement'))

    assert result['messages'][0].content == 'selected response'
    assert definition.declared_resources['greeting'].logical_path == 'pack/selected.txt'


def test_declared_package_dependency_uses_admitted_bytes_and_rejects_stale_cache(fixture_graph):
    root, entry = fixture_graph
    package = 'synthetic_state_dep'
    _write(root, f'src/{package}/__init__.py', '')
    dependency = _write(root, f'src/{package}/helper.py',
                        'def next_count(value):\n    return value + 1\n')
    source = root / 'native.py'
    source.write_text(f'from {package}.helper import next_count\n' +
                      source.read_text().replace("state['visits'] + 1", "next_count(state['visits'])"))
    entry.write_text(entry.read_text() + f'''\n## Native Dependencies

| Module | Source |
| --- | --- |
| {package} | [Python](src/{package}/__init__.py) |
| {package}.helper | [Python](src/{package}/helper.py) |
''')
    definition = load_definition(entry, base_root=root, pack_root=root)
    try:
        catalog = load_native_bindings(definition)
        workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})
        result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                            thread_id='dependency'))
        dependency.write_text('def next_count(value):\n    return value + 2\n')
        revised = load_definition(entry, base_root=root, pack_root=root)

        assert result['visits'] == 1
        with pytest.raises(LayoutError, match='restart required'):
            load_native_bindings(revised)
    finally:
        sys.modules.pop(package + '.helper', None)
        sys.modules.pop(package, None)


def _lazy_dependency_graph(fixture_graph, package, *, preload=False, gated=False):
    root, entry = fixture_graph
    _write(root, f'src/{package}/__init__.py', '')
    helper = _write(root, f'src/{package}/helper.py',
                    'def next_count(value):\n    return value + 1\n')
    source = root / 'native.py'
    original = source.read_text()
    preface = f'import {package}\n' if preload else ''
    gate = "context.services['entered'].set()\n    await context.services['release'].wait()\n    " if gated else ''
    original = original.replace('def intake(state, context):', 'async def intake(state, context):' if gated else 'def intake(state, context):')
    original = original.replace("    return {'messages': [AIMessage(id='answer'", f"    {gate}from {package}.helper import next_count\n    return {{'messages': [AIMessage(id='answer'")
    original = original.replace("state['visits'] + 1", "next_count(state['visits'])")
    source.write_text(preface + original)
    entry.write_text(entry.read_text() + f'''\n## Native Dependencies

| Module | Source |
| --- | --- |
| {package} | [Python](src/{package}/__init__.py) |
| {package}.helper | [Python](src/{package}/helper.py) |
''')
    return root, entry, helper


@pytest.mark.parametrize('preload', [False, True])
def test_lazy_native_import_uses_admitted_bytes_without_ambient_path(fixture_graph, preload):
    package = 'synthetic_lazy_preloaded' if preload else 'synthetic_lazy_only'
    root, entry, helper = _lazy_dependency_graph(fixture_graph, package, preload=preload)
    assert str(root) not in sys.path and str(root / 'src') not in sys.path
    definition = load_definition(entry, base_root=root, pack_root=root)
    catalog = load_native_bindings(definition)
    assert package + '.helper' not in sys.modules
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                        thread_id=package))
    assert result['visits'] == 1
    assert sys.modules[package + '.helper'].__file__ == str(helper)


def test_lazy_native_import_does_not_read_disk_after_turn_verification(fixture_graph):
    package = 'synthetic_lazy_midturn'
    root, entry, helper = _lazy_dependency_graph(fixture_graph, package, preload=True, gated=True)
    definition = load_definition(entry, base_root=root, pack_root=root)
    catalog = load_native_bindings(definition)
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages},
                                services={'entered': asyncio.Event(), 'release': asyncio.Event()})

    async def run():
        task = asyncio.create_task(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                                 thread_id=package))
        await asyncio.wait_for(workflow.services['entered'].wait(), 1)
        helper.write_text('def next_count(value):\n    return value + 99\n')
        workflow.services['release'].set()
        return await asyncio.wait_for(task, 2)

    assert asyncio.run(run())['visits'] == 1


def test_unloaded_dependency_name_is_reserved_for_first_definition(fixture_graph, tmp_path):
    package = 'synthetic_lazy_reserved'
    root, entry, helper = _lazy_dependency_graph(fixture_graph, package)
    first = load_definition(entry, base_root=root, pack_root=root)
    load_native_bindings(first)
    assert package not in sys.modules and package + '.helper' not in sys.modules
    helper.write_text('def next_count(value):\n    return value + 2\n')
    second = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='restart required'):
        load_native_bindings(second)


def test_failed_catalog_releases_new_dependency_reservations(fixture_graph):
    package = 'synthetic_lazy_failed_catalog'
    root, entry, helper = _lazy_dependency_graph(fixture_graph, package, preload=True)
    source = root / 'native.py'
    source.write_text(source.read_text() + '\nraise RuntimeError("catalog failure")\n')
    failed = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(RuntimeError, match='catalog failure'):
        load_native_bindings(failed)
    assert package not in sys.modules
    source.write_text(source.read_text().replace('\nraise RuntimeError("catalog failure")\n', '\n'))
    helper.write_text('def next_count(value):\n    return value + 3\n')
    recovered = load_definition(entry, base_root=root, pack_root=root)
    load_native_bindings(recovered)


def test_lazy_import_relocated_fresh_process(fixture_graph, tmp_path):
    import subprocess
    package = 'synthetic_lazy_relocated'
    root, entry, _helper = _lazy_dependency_graph(fixture_graph, package)
    command = '''import asyncio, sys
from pathlib import Path
from langgraph.graph.message import add_messages
from api_agent.workflows.definition import load_definition
from api_agent.workflows.native import load_native_bindings
from api_agent.workflows.execution import Workflow
root = Path(sys.argv[1]); entry = Path(sys.argv[2])
assert str(root) not in sys.path and str(root / 'src') not in sys.path
definition = load_definition(entry, base_root=root, pack_root=root)
catalog = load_native_bindings(definition)
workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})
result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='relocated'))
assert result['visits'] == 1
'''
    completed = subprocess.run([sys.executable, '-c', command, str(root), str(entry)],
                               cwd=tmp_path.parent, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr


def test_unreserved_loader_refuses_finding_or_executing_module(fixture_graph):
    from api_agent.workflows.native import (_AdmittedDependencyLoader)
    package = 'synthetic_lazy_unreserved'
    root, entry, _helper = _lazy_dependency_graph(fixture_graph, package)
    definition = load_definition(entry, base_root=root, pack_root=root)
    loader = _AdmittedDependencyLoader(definition)
    assert loader.find_spec(package) is None
    with pytest.raises(LayoutError, match='reservation changed'):
        loader.exec_module(SimpleNamespace(__name__=package))


def test_reused_catalog_loader_survives_later_catalog_failure(fixture_graph, monkeypatch):
    package = 'synthetic_lazy_reused'
    root, entry, _helper = _lazy_dependency_graph(fixture_graph, package)
    source = root / 'native.py'
    source.write_text('import os\nif os.environ.get("SYNTHETIC_FAIL_RELOAD"):\n'
                      '    raise RuntimeError("second load")\n' + source.read_text())
    definition = load_definition(entry, base_root=root, pack_root=root)
    catalog = load_native_bindings(definition)
    monkeypatch.setenv('SYNTHETIC_FAIL_RELOAD', '1')
    with pytest.raises(RuntimeError, match='second load'):
        load_native_bindings(definition)
    monkeypatch.delenv('SYNTHETIC_FAIL_RELOAD')
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})
    assert asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                     thread_id=package))['visits'] == 1


def test_failed_catalog_cleans_loaded_dependency_even_if_cache_entry_replaced(fixture_graph):
    package = 'synthetic_lazy_cache_replaced'
    root, entry, _helper = _lazy_dependency_graph(fixture_graph, package, preload=True)
    source = root / 'native.py'
    source.write_text(source.read_text() + f'''\nimport sys
sys.modules[{package!r}] = object()
raise RuntimeError("catalog failure")
''')
    definition = load_definition(entry, base_root=root, pack_root=root)
    try:
        with pytest.raises(RuntimeError, match='catalog failure'):
            load_native_bindings(definition)
        assert package not in sys.modules or type(sys.modules[package]) is object
    finally:
        sys.modules.pop(package, None)


def test_lazy_import_proceeds_while_another_catalog_executes_native_code(fixture_graph, tmp_path, monkeypatch):
    import builtins
    import importlib
    from threading import Event, Thread

    first_package = 'synthetic_lazy_concurrent_first'
    first_root, first_entry, _ = _lazy_dependency_graph(fixture_graph, first_package)
    first = load_definition(first_entry, base_root=first_root, pack_root=first_root)
    load_native_bindings(first)
    assert first_package not in sys.modules

    second_root = tmp_path / 'other_graph'
    second_root.mkdir()
    for path in first_root.iterdir():
        if path.is_file():
            (second_root / path.name).write_bytes(path.read_bytes())
    second_entry = second_root / 'flow.md.html'
    # This independent catalog blocks inside admitted native execution. A lazy
    # import owned by the first catalog must not wait on the registry lock.
    second_entry.write_text(second_entry.read_text().split('\n## Native Dependencies')[0])
    second_source = second_root / 'native.py'
    second_source.write_text('import builtins\nbuiltins._loader_entered.set()\n'
                             'builtins._loader_release.wait(2)\n' +
                             second_source.read_text().replace(
                                 f'from {first_package}.helper import next_count',
                                 "next_count = lambda value: value + 1"))
    second = load_definition(second_entry, base_root=second_root, pack_root=second_root)
    entered, release = Event(), Event()
    monkeypatch.setattr(builtins, '_loader_entered', entered, raising=False)
    monkeypatch.setattr(builtins, '_loader_release', release, raising=False)
    errors = []

    def load_second():
        try:
            load_native_bindings(second)
        except BaseException as error:
            errors.append(error)

    worker = Thread(target=load_second)
    worker.start()
    try:
        assert entered.wait(1)
        imported = importlib.import_module(first_package + '.helper')
        assert imported.next_count(0) == 1
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and not errors


@pytest.mark.parametrize('target_kind', ['parent', 'child'])
@pytest.mark.parametrize('rollback_first', [False, True])
def test_failed_catalog_does_not_resurrect_inflight_import(fixture_graph, monkeypatch,
                                                            target_kind, rollback_first):
    import builtins
    import importlib
    from threading import Event, Thread
    from api_agent.workflows import native as stateful

    package = f'synthetic_failed_overlap_{target_kind}_{rollback_first}'
    root, entry, helper = _lazy_dependency_graph(fixture_graph, package)
    source = root / 'native.py'
    original = source.read_text()
    source.write_text('import builtins\nbuiltins._catalog_entered.set()\n'
                      'assert builtins._catalog_release.wait(2)\n'
                      'raise RuntimeError("synthetic catalog failure")\n' + original)
    target = helper.parent / '__init__.py' if target_kind == 'parent' else helper
    target.write_text('import builtins\nbuiltins._import_entered.set()\n'
                      'assert builtins._import_release.wait(2)\n' + target.read_text())
    failed = load_definition(entry, base_root=root, pack_root=root)
    catalog_entered, catalog_release, import_entered, import_release = (Event() for _ in range(4))
    for name, event in (('_catalog_entered', catalog_entered), ('_catalog_release', catalog_release),
                        ('_import_entered', import_entered), ('_import_release', import_release)):
        monkeypatch.setattr(builtins, name, event, raising=False)
    catalog_errors, import_errors, imported = [], [], []

    def load_failed():
        try:
            load_native_bindings(failed)
        except BaseException as error:
            catalog_errors.append(error)

    def import_inflight():
        try:
            imported.append(importlib.import_module(package if target_kind == 'parent'
                                                    else package + '.helper'))
        except BaseException as error:
            import_errors.append(error)

    catalog_thread = Thread(target=load_failed)
    import_thread = Thread(target=import_inflight)
    catalog_thread.start()
    try:
        assert catalog_entered.wait(1)
        import_thread.start()
        assert import_entered.wait(1)
        if rollback_first:
            catalog_release.set()
            catalog_thread.join(1)
            assert not catalog_thread.is_alive()
            assert package not in stateful._RESERVED_DEPENDENCIES
            import_release.set()
        else:
            import_release.set()
            import_thread.join(1)
            assert not import_thread.is_alive() and imported
            catalog_release.set()
    finally:
        catalog_release.set()
        import_release.set()
        catalog_thread.join(2)
        if import_thread.ident is not None:
            import_thread.join(2)
    assert not catalog_thread.is_alive() and not import_thread.is_alive()
    assert len(catalog_errors) == 1 and isinstance(catalog_errors[0], RuntimeError)
    assert bool(import_errors) == rollback_first
    if rollback_first:
        assert isinstance(import_errors[0], LayoutError) and not imported
    assert package not in sys.modules and package + '.helper' not in sys.modules
    assert package not in stateful._LOADED_DEPENDENCIES
    assert package + '.helper' not in stateful._LOADED_DEPENDENCIES
    assert package not in stateful._RESERVED_DEPENDENCIES
    assert package + '.helper' not in stateful._RESERVED_DEPENDENCIES
    source.write_text(original)
    target.write_text('' if target_kind == 'parent' else
                      'def next_count(value):\n    return value + 1\n')
    corrected = load_definition(entry, base_root=root, pack_root=root)
    load_native_bindings(corrected)


def test_interrupted_catalog_releases_declared_lazy_names(fixture_graph):
    from api_agent.workflows import native as stateful
    package = 'synthetic_lazy_interrupted'
    root, entry, _ = _lazy_dependency_graph(fixture_graph, package)
    source = root / 'native.py'
    original = source.read_text()
    source.write_text('raise KeyboardInterrupt()\n' + original)
    interrupted = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(KeyboardInterrupt):
        load_native_bindings(interrupted)
    assert package not in stateful._RESERVED_DEPENDENCIES
    assert interrupted.definition_id not in stateful._ADMITTED_LOADERS
    source.write_text(original)
    corrected = load_definition(entry, base_root=root, pack_root=root)
    load_native_bindings(corrected)


def test_selected_pack_loader_dispatches_v3_with_same_identity(fixture_graph):
    root, entry = fixture_graph
    context = SimpleNamespace(workflow_entry=entry, base_root=root, pack_root=root,
                              workflow_root='base', workflow_replacements=())
    definition = load_selected_definition(context)
    catalog = load_native_bindings(definition)

    workflow = load_workflow(context, catalog, definition=definition,
                             reducers={'messages': add_messages})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                        thread_id='selected'))

    assert result['visits'] == 1
    assert workflow.definition.definition_id == definition.definition_id


def test_parallel_conversations_keep_request_and_services_isolated(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text('import asyncio\n' + source.read_text().replace(
        'def intake(state, context):', 'async def intake(state, context):\n    await asyncio.sleep(0.02)')
        .replace("content='new'", "content=context.request + context.services['suffix']"))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})

    async def execute():
        return await asyncio.gather(*(
            workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id=identifier,
                          request=identifier, services={'suffix': suffix})
            for identifier, suffix in (('alpha', 'A'), ('beta', 'B'))))

    first, second = asyncio.run(execute())

    assert first['messages'][0].content == 'alphaA'
    assert second['messages'][0].content == 'betaB'


def test_overlapping_turns_on_one_conversation_are_rejected(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text('import asyncio\n' + source.read_text().replace(
        'def intake(state, context):', 'async def intake(state, context):\n    await asyncio.sleep(0.04)'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})

    async def execute():
        first = asyncio.create_task(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                                   thread_id='same'))
        await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError, match='active turn'):
            await workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='same')
        return await first

    result = asyncio.run(execute())

    assert result['visits'] == 1


def test_raw_cli_validates_and_diagrams_v3_from_foreign_cwd(fixture_graph, tmp_path, monkeypatch, capsys):
    root, entry = fixture_graph
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    monkeypatch.chdir(foreign)

    assert workflow_cli(['validate', '--entry', str(entry), '--base-root', str(root)]) == 0
    assert 'synthetic_flow' in capsys.readouterr().out
    assert workflow_cli(['diagram', '--entry', str(entry), '--base-root', str(root)]) == 0
    diagram = capsys.readouterr().out

    assert 'START --> intake' in diagram
    assert 'intake -->|choose| finish' in diagram


def test_native_v2_stream_events_preserve_node_identity(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})

    async def collect():
        return [event async for event in workflow.astream_events(
            {'messages': [], 'pending': False, 'visits': 0}, thread_id='stream')]

    events = asyncio.run(collect())

    assert any(event['event'] == 'on_chain_start' and event['name'] == 'intake' for event in events)
    assert any(event['event'] == 'on_chain_end' and event['name'] == 'finish' for event in events)


def test_stream_close_closes_graph_scope_and_releases_conversation(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    observed = []

    async def close_early():
        stream = workflow.astream_events({'messages': [], 'pending': False, 'visits': 0},
                                         thread_id='closed', services={'observe': observed.append})
        await anext(stream)
        await stream.aclose()
        return await workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                   thread_id='closed')

    result = asyncio.run(close_early())

    assert result['visits'] == 1
    assert any(event['event'] == 'scope_end' and event['owner'] == 'GRAPH' for event in observed)


def test_v3_journal_records_complete_declared_route_without_state(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    journal = WorkflowJournal(definition)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, journal=journal)

    asyncio.run(workflow.arun({'messages': [HumanMessage(content='secret input')],
                               'pending': False, 'visits': 0}, thread_id='recorded'))
    envelope = journal.finalize()

    assert validate_journal(envelope) is envelope
    assert envelope['completeness'] == 'complete'
    assert 'secret input' not in str(envelope)


def test_nested_component_journal_has_scope_owned_route_chain(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)
''')
    entry.write_text(entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)').replace(
        '| finish | END | - | - |', '| finish | END | - | - |\n| group | END | - | - |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    journal = WorkflowJournal(definition)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, journal=journal)

    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='nested-journal'))
    envelope = journal.finalize()

    assert envelope['completeness'] == 'complete'
    assert any(event.get('owner') == 'group' and event.get('source') == 'START'
               for event in envelope['events'])


@pytest.mark.parametrize('observe', [False, True])
def test_reporting_marker_verifies_before_native_exec_and_publishes_turn(fixture_graph, monkeypatch, observe):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    calls = []
    from api_agent.workflows import provenance, evidence
    monkeypatch.setattr(provenance, 'verified_documents',
                        lambda admitted, packaged_proof=None: calls.append(admitted.definition_id) or None)
    monkeypatch.setattr(evidence, 'publish', calls.append)
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    catalog = load_native_bindings(definition)
    rows = []
    from api_agent.workflows.observations import WorkflowObservationFactory
    from api_agent.telemetry.privacy import EmissionBoundary
    services = {'observation_factory': WorkflowObservationFactory(
        {}, boundary=EmissionBoundary(), sink=rows.append)} if observe else {}
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages},
                                services=services)

    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                               thread_id='reported'))

    assert calls[:2] == [definition.definition_id, definition.definition_id]
    assert calls[2]['completeness'] == 'complete'
    if observe:
        assert rows
        assert [row['event_id'] for row in rows] == [row['event_id'] for row in calls[2]['events']]


def test_reporting_records_unstaged_workflow_identity_that_matches_later_commit(fixture_graph, monkeypatch):
    import subprocess
    from api_agent.workflows import evidence
    from api_agent.workflows.provenance import verified_documents

    root, entry = fixture_graph
    def git(*args):
        subprocess.run(('git', '-C', str(root), *args), check=True, capture_output=True)
    git('init', '-q')
    git('config', 'user.name', 'Example')
    git('config', 'user.email', 'example@example.invalid')
    git('add', '.')
    git('commit', '-qm', 'initial workflow')
    entry.write_text(entry.read_text().replace('synthetic_flow', 'changed_flow'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    records = []
    monkeypatch.setattr(evidence, 'publish', records.append)
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='reported'))
    recorded = records[-1]
    assert recorded['projection']['definition_id'] == definition.definition_id
    assert recorded['source_manifest']['owners'][0]['state'] == 'working_tree'
    assert recorded['source_manifest']['owners'][0]['commit'] is None

    git('add', '.')
    git('commit', '-qm', 'update workflow')
    committed = load_definition(entry, base_root=root, pack_root=root)
    assert committed.definition_id == recorded['projection']['definition_id']
    assert verified_documents(committed)['documents'] == recorded['source_manifest']['documents']


def test_declared_recovery_loop_revisits_node_then_ends(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text(source.read_text().replace(
        "return 'finish' if not state['pending'] else 'END'",
        "return 'intake' if state['visits'] < 2 else 'finish'"))
    entry.write_text(entry.read_text().replace(
        '| intake | finish | choose | [Python](native.py) |',
        '| intake | intake | choose | [Python](native.py) |\n'
        '| intake | finish | choose | [Python](native.py) |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    journal = WorkflowJournal(definition)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, journal=journal)

    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                        thread_id='loop'))
    envelope = journal.finalize()

    assert result['visits'] == 2
    assert envelope['completeness'] == 'complete'
    assert any(event.get('source') == 'intake' and event.get('target') == 'intake'
               for event in envelope['events'])


def test_closing_stream_drains_started_offload_before_releasing_thread(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text('import time\nfrom pathlib import Path\n' + source.read_text().replace(
        'def intake(state, context):', '''def intake(state, context):
    folder = Path(context.request)
    (folder / 'started').write_text('started')
    time.sleep(0.2)
    (folder / 'done').write_text('done')'''))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    folder = root / 'probe'
    folder.mkdir()

    async def execute():
        stream = workflow.astream_events({'messages': [], 'pending': False, 'visits': 0},
                                         thread_id='drain', request=str(folder))
        async for event in stream:
            if event['event'] == 'on_chain_start' and event['name'] == 'intake':
                break
        while not (folder / 'started').exists():
            await asyncio.sleep(0.005)
        await stream.aclose()
        assert (folder / 'done').exists()
        return await workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                   thread_id='drain', request=str(folder))

    result = asyncio.run(execute())

    assert result['visits'] == 1


def test_admitted_nodes_routes_and_journal_projection_cannot_be_mutated(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    original_id = definition.definition_id

    with pytest.raises(TypeError):
        definition.nodes[0]['Registry'] = 'finish'
    with pytest.raises(TypeError):
        definition.edges[0]['To'] = 'END'
    with pytest.raises(TypeError):
        definition.layout.tables['Edges'][0]['When'] = 'always'

    assert definition.definition_id == original_id
    assert definition.nodes[0]['Registry'] == 'intake'


def test_admission_captures_each_resource_once(fixture_graph, monkeypatch):
    root, entry = fixture_graph
    from api_agent.workflows import definition as stateful
    original_read = stateful._read_bounded
    reads = []
    def count_read(path, owner):
        reads.append(path)
        return original_read(path, owner)
    monkeypatch.setattr(stateful, '_read_bounded', count_read)

    definition = load_definition(entry, base_root=root, pack_root=root)

    assert len(reads) == len(set(reads)) == len(definition.contents)
    assert reads.count(root / 'native.py') == 1


def test_nested_component_uses_shared_budget_and_real_parent_scope(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)
''')
    _write(root, 'limits.toml', '''version = 1
[fields.GRAPH_LIMIT]
type = "float"
default = 1.0
[fields.GROUP_LIMIT]
type = "float"
default = 0.5
[fields.NODE_LIMIT]
type = "float"
default = 0.25
''')
    entry.write_text(entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)').replace(
        '| finish | END | - | - |', '| finish | END | - | - |\n| group | END | - | - |')
        + '''\n## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| GRAPH | [config](limits.toml) | GRAPH_LIMIT |
| group | [config](limits.toml) | GROUP_LIMIT |
| intake | [config](limits.toml) | NODE_LIMIT |
''')
    definition = load_definition(entry, base_root=root, pack_root=root)
    events = []
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, services={'observe': events.append})

    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='timed'))
    starts = {event['owner']: event for event in events if event['event'] == 'scope_start'}

    assert starts['group']['parent_id'] == starts['GRAPH']['scope_id']
    assert starts['intake']['parent_id'] == starts['group']['scope_id']
    assert starts['intake']['declared_timeout'] == 0.25
    assert starts['intake']['effective_timeout'] <= 0.25
    assert starts['group']['timeout_key'] == 'GROUP_LIMIT'


def test_component_deadline_drains_child_offload_and_closes_scopes(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)
''')
    _write(root, 'limit.toml', '''version = 1
[fields.GROUP_LIMIT]
type = "float"
default = 0.02
''')
    source = root / 'native.py'
    source.write_text('import time\nfrom pathlib import Path\n' + source.read_text().replace(
        'def intake(state, context):', '''def intake(state, context):
    time.sleep(0.08)
    Path(context.request).write_text('finished')'''))
    entry.write_text(entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)').replace(
        '| finish | END | - | - |', '| finish | END | - | - |\n| group | END | - | - |')
        + '''\n## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| group | [config](limit.toml) | GROUP_LIMIT |
''')
    definition = load_definition(entry, base_root=root, pack_root=root)
    events = []
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, services={'observe': events.append})
    probe = root / 'finished.txt'

    with pytest.raises(TimeoutError):
        asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                   thread_id='timed-drain', request=str(probe)))

    assert probe.read_text() == 'finished'
    assert any(event['event'] == 'scope_end' and event['owner'] == 'group' for event in events)
    assert any(event['event'] == 'scope_end' and event['owner'] == 'GRAPH' for event in events)


def test_component_startup_error_does_not_leave_waiting_turn(fixture_graph, monkeypatch):
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)
''')
    entry.write_text(entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)').replace(
        '| finish | END | - | - |', '| finish | END | - | - |\n| group | END | - | - |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    from api_agent.workflows import native as stateful
    from api_agent.workflows import execution
    original = execution.component_timeout
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def refuse_group(name, timeout):
        if name == 'Workflow group':
            raise TimeoutError('parent budget exhausted')
        async with original(name, timeout) as budget:
            yield budget

    monkeypatch.setattr(execution, 'component_timeout', refuse_group)

    async def invoke():
        with pytest.raises(TimeoutError, match='parent budget exhausted'):
            await asyncio.wait_for(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                                 thread_id='startup'), timeout=1)

    asyncio.run(invoke())


@pytest.mark.parametrize(('old', 'new', 'message'), [
    ('# Workflow', '# Workflow v9', 'expected Workflow heading'),
    ('## Edges', '## Unsupported', 'missing required section'),
    ('| synthetic_flow |', '| invalid-id |', 'invalid graph or state identity'),
    ('| State | [Python](native.py) |', '| State | native.py |', 'expected one source link'),
    ('| State | [Python](native.py) |', '| State | [Python](https://example.invalid/x.py) |', 'invalid source link'),
    ('| State | [Python](native.py) |', '| State | [Python](../outside.py) |', 'outside declared roots'),
    ('| State | [Python](native.py) |', '| State | [Python](intake.md.html) |', 'native source must be Python'),
    ('1.  [node intake]', '2.  [node intake]', 'sequential linked steps'),
    ('2.  [node finish](finish.md.html)', '2.  [node intake](finish.md.html)', 'duplicate step ID'),
    ('2.  [node finish](finish.md.html)', '2.  [node finish](finish.txt)', 'declaration must end'),
    ('2.  [node finish](finish.md.html)', '2.  [node finish](intake.md.html)', 'identity mismatch'),
    ('| finish | END | - | - |', '| finish | lost | - | - |', 'invalid edge declaration'),
    ('| finish | END | - | - |', '| finish | END | - | [Python](native.py) |', 'static edge source'),
    ('| finish | END | - | - |', '', 'each node needs'),
    ('| intake | END | choose | [Python](native.py) |', '| intake | finish | choose | [Python](native.py) |', 'duplicate or mixed route'),
])
def test_stateful_admission_rejects_invalid_declarations(fixture_graph, old, new, message):
    root, entry = fixture_graph
    text = entry.read_text()
    assert old in text
    entry.write_text(text.replace(old, new))

    with pytest.raises(LayoutError, match=message):
        load_definition(entry, base_root=root, pack_root=root)


def test_stateful_admission_rejects_entry_outside_roots_and_invalid_root(fixture_graph, tmp_path):
    root, entry = fixture_graph
    with pytest.raises(LayoutError, match='outside declared roots'):
        load_definition(entry, base_root=tmp_path / 'a', pack_root=tmp_path / 'b')
    with pytest.raises(LayoutError, match='invalid declared entry root'):
        load_definition(entry, base_root=root, pack_root=root, entry_root='other')


def test_stateful_admission_rejects_duplicate_and_unknown_replacements(fixture_graph):
    root, entry = fixture_graph
    replacement = Replacement('intake', Path('intake.md.html'), 'base')
    with pytest.raises(LayoutError, match='invalid replacements'):
        load_definition(entry, base_root=root, pack_root=root,
                                 replacements=(replacement, replacement))
    with pytest.raises(LayoutError, match='unknown replacement target'):
        load_definition(entry, base_root=root, pack_root=root,
                                 replacements=(Replacement('missing', Path('intake.md.html'), 'base'),))
    with pytest.raises(LayoutError, match='invalid replacement'):
        load_definition(entry, base_root=root, pack_root=root,
                                 replacements=(Replacement('intake', Path('../outside.md.html'), 'base'),))


@pytest.mark.parametrize(('section', 'message'), [
    ('''## Resources

| ID | Kind | Source |
| --- | --- | --- |
| bad-id | prompt | [text](native.py) |
''', 'invalid resource declaration'),
    ('''## Resources

| ID | Kind | Source |
| --- | --- | --- |
| greeting | prompt | [text](native.py) |
''', 'conflicting kinds'),
    ('''## Native Sources

| Source |
| --- |
| [Python](native.py) |
| [Python](native.py) |
''', 'duplicate native helper'),
    ('''## Native Dependencies

| Module | Source |
| --- | --- |
| bad-name | [Python](native.py) |
''', 'invalid native dependency module'),
    ('''## Native Dependencies

| Module | Source |
| --- | --- |
| unrelated | [Python](native.py) |
''', 'does not match module'),
    ('''## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| unknown | [config](limit.toml) | LIMIT |
''', 'invalid timeout owner or key'),
    ('''## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| GRAPH | [config](native.py) | LIMIT |
''', 'timeout source must be TOML'),
])
def test_stateful_admission_rejects_invalid_optional_tables(fixture_graph, section, message):
    root, entry = fixture_graph
    entry.write_text(entry.read_text() + '\n' + section)

    with pytest.raises(LayoutError, match=message):
        load_definition(entry, base_root=root, pack_root=root)


@pytest.mark.parametrize(('replacement', 'message'), [
    (Replacement('greeting', Path('../outside.txt'), 'pack'), 'invalid resource replacement'),
    (Replacement('greeting', Path('native.py'), 'base'), 'conflicting kinds'),
])
def test_stateful_resource_replacement_admission_rejects_invalid_targets(
        fixture_graph, replacement, message):
    root, entry = fixture_graph
    entry.write_text(entry.read_text() + '''\n## Resources

| ID | Kind | Source |
| --- | --- | --- |
| greeting | prompt | [text](missing.txt) |
''')
    with pytest.raises(LayoutError, match=message):
        load_definition(entry, base_root=root, pack_root=root,
                                 replacements=(replacement,))


def test_stateful_component_and_node_sections_must_be_exact(fixture_graph):
    root, entry = fixture_graph
    child = root / 'intake.md.html'
    child.write_text(child.read_text().replace('## Native', '## Invalid'))
    with pytest.raises(LayoutError, match='node requires only Native'):
        load_definition(entry, base_root=root, pack_root=root)
    child.write_text(child.read_text().replace('## Invalid', '## Native'))
    child.write_text(child.read_text().replace('| intake | deterministic |', '| intake | network |'))
    with pytest.raises(LayoutError, match='invalid native binding'):
        load_definition(entry, base_root=root, pack_root=root)


def test_stateful_component_requires_only_flow(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Extra

something
''')
    entry.write_text(entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)'))
    with pytest.raises(LayoutError, match='component requires only Flow'):
        load_definition(entry, base_root=root, pack_root=root)


def _grouped(root, entry, group_edges, root_edges='| group | END | - | - |\n'):
    _write(root, 'parts/group.md.html', f'''# Component: group

## Flow

1.  [node intake](../intake.md.html)
2.  [node finish](../finish.md.html)

## Edges

| From | To | Router | Source |
| --- | --- | --- | --- |
{group_edges}
## Notes

The group owns the routes between its children.
''')
    text = entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](parts/group.md.html)')
    entry.write_text(text[:text.index('| --- | --- | --- | --- |\n') + 26] + root_edges)


_GROUP_EDGES = ('| intake | finish | choose | [Python](../native.py) |\n'
                '| intake | END | choose | [Python](../native.py) |\n'
                '| finish | END | - | - |\n')


def test_component_declares_routes_for_its_direct_children(fixture_graph):
    from api_agent.workflows.viewer_projection import project_view
    root, entry = fixture_graph
    _grouped(root, entry, _GROUP_EDGES)
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages})

    result = asyncio.run(workflow.arun({'messages': [HumanMessage(id='human', content='hello')],
                                        'pending': False, 'visits': 0}, thread_id='grouped'))

    assert (result['visits'], result['pending']) == (1, True)
    assert {(row['From'], row['To']) for row in definition.edges} == {
        ('group', 'END'), ('intake', 'finish'), ('intake', 'END'), ('finish', 'END')}
    assert all(set(row) == {'From', 'To', 'Router', 'Source', 'line'} for row in definition.edges)
    routers = [item for item in project_view(definition).references if item.kind == 'router_source']
    assert [(item.owner_id, item.owner_path.rsplit('/', 1)[-1]) for item in routers] == [('intake', 'group.md.html')]


@pytest.mark.parametrize(('group_edges', 'root_edges'), [
    (_GROUP_EDGES, '| group | END | - | - |\n| finish | END | - | - |\n'),
    (_GROUP_EDGES + '| group | END | - | - |\n', ''),
])
def test_component_routes_have_one_declaring_owner(fixture_graph, group_edges, root_edges):
    root, entry = fixture_graph
    _grouped(root, entry, group_edges, root_edges)
    with pytest.raises(LayoutError, match='invalid edge declaration'):
        load_definition(entry, base_root=root, pack_root=root)


_WHEN_GROUP = ('| From | To | When |\n| --- | --- | --- |\n'
               '| START | finish | .pending |\n'
               '| START | intake | otherwise |\n'
               '| intake | finish | .visits == 1 |\n'
               '| intake | END | otherwise |\n'
               '| finish | END | always |\n')


def _conditional(root, entry, group_edges=_WHEN_GROUP, root_edges='| group | END | always |\n'):
    _grouped(root, entry, '')
    group = root / 'parts/group.md.html'
    text = group.read_text()
    start = text.index('| From | To | Router | Source |')
    group.write_text(text[:start] + group_edges + text[text.index('\n## Notes'):])
    text = entry.read_text()
    entry.write_text(text[:text.index('| From | To | Router | Source |')]
                     + '| From | To | When |\n| --- | --- | --- |\n' + root_edges)


def test_typed_edges_route_on_state_conditions_and_conditional_component_entry(fixture_graph):
    from api_agent.workflows.viewer_projection import project_view
    root, entry = fixture_graph
    _conditional(root, entry)
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages})

    first = asyncio.run(workflow.arun({'messages': [HumanMessage(id='human', content='hello')],
                                       'pending': False, 'visits': 0}, thread_id='conditional'))
    second = asyncio.run(workflow.arun({'messages': []}, thread_id='conditional'))

    assert (first['visits'], first['pending']) == (1, True)
    assert (second['visits'], second['pending']) == (1, True)
    edges = {(edge.owner, edge.source, edge.target): (edge.condition, edge.implicit)
             for edge in project_view(definition).edges}
    assert edges[('group', 'START', 'finish')] == ('.pending', False)
    assert edges[('group', 'START', 'intake')] == ('otherwise', False)
    assert edges[('group', 'intake', 'finish')] == ('.visits == 1', False)
    assert edges[('GRAPH', 'group', 'END')] == ('always', False)


@pytest.mark.parametrize(('group_edges', 'root_edges', 'message'), [
    (_WHEN_GROUP.replace('| intake | END | otherwise |', '| intake | END | .visits == 2 |'), None,
     'conditional edges require one otherwise fallback'),
    (_WHEN_GROUP.replace('| finish | END | always |', '| finish | END | always |\n| finish | intake | otherwise |'),
     None, 'conditional edges require one otherwise fallback'),
    (_WHEN_GROUP.replace('.visits == 1', '.visits ==='), None, 'invalid condition syntax'),
    (_WHEN_GROUP.replace('| START | finish |', '| START | END |'), None, 'invalid edge declaration'),
    (_WHEN_GROUP, '| START | group | always |\n| group | END | always |\n', 'invalid edge declaration'),
])
def test_typed_condition_sets_are_validated_at_admission(fixture_graph, group_edges, root_edges, message):
    root, entry = fixture_graph
    _conditional(root, entry, group_edges, *(() if root_edges is None else (root_edges,)))
    with pytest.raises(LayoutError, match=message):
        load_definition(entry, base_root=root, pack_root=root)


def test_typed_conditions_may_share_a_target_with_distinct_reasons(fixture_graph):
    root, entry = fixture_graph
    _conditional(root, entry, _WHEN_GROUP.replace(
        '| intake | END | otherwise |', '| intake | END | .visits == 3 |\n| intake | intake | otherwise |').replace(
        '| intake | finish | .visits == 1 |', '| intake | finish | .visits == 1 |\n| intake | END | .visits == 2 |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    assert [row['When'] for row in definition.edges if row['From'] == 'intake' and row['To'] == 'END'] == [
        '.visits == 2', '.visits == 3']
    workflow = Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='shared'))
    assert (result['visits'], result['pending']) == (1, True)


_PROMPTED_INTAKE = '''# Node: intake

## Native

| Registry | Kind | Source |
| --- | --- | --- |
| intake | deterministic | [Python](native.py) |

## Prompts

| Node | File | When |
| --- | --- | --- |
| intake | @intake_prompt | always |
| intake | [prompt](prompts/extra.md) | .pending |

## Prompt: intake_prompt

```text
Hello {{greeting}} and {{greeting}}.

```
'''
_SLOTS = '''
## Prompt Slots

| Slot | Registry | Source |
| --- | --- | --- |
| greeting | greeting | [Python](native.py) |
'''


def _prompted(root, entry, intake=_PROMPTED_INTAKE, slots=_SLOTS):
    native = root / 'native.py'
    native.write_text(native.read_text().replace(
        "def intake(state, context):\n",
        "SEEN = []\nCALLS = []\n\ndef greeting(state, context):\n"
        "    CALLS.append(context.node_id)\n    return 'visitor' + str(state['visits'])\n\n"
        "def intake(state, context):\n    SEEN.append((context.prompts, dict(context.prompt_texts)))\n"))
    _write(root, 'prompts/extra.md', 'Also {{greeting}}.\n')
    _write(root, 'intake.md.html', intake)
    entry.write_text(entry.read_text() + slots)


def test_typed_nodes_receive_selected_prompts_with_declared_slots(fixture_graph):
    root, entry = fixture_graph
    _prompted(root, entry)
    definition = load_definition(entry, base_root=root, pack_root=root)
    catalog = load_native_bindings(definition)
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})
    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='prompted'))
    asyncio.run(workflow.arun({'messages': []}, thread_id='prompted'))
    namespace = catalog.namespaces[(root / 'native.py').resolve()]
    assert namespace['SEEN'] == [
        (('Hello visitor0 and visitor0.\n',), {'intake_prompt': 'Hello visitor0 and visitor0.\n'}),
        (('Hello visitor1 and visitor1.\n', 'Also visitor1.\n'),
         {'intake_prompt': 'Hello visitor1 and visitor1.\n', 'extra': 'Also visitor1.\n'}),
    ]
    assert namespace['CALLS'] == ['intake', 'intake']
    assert any(item.kind == 'prompt' and item.logical_path.endswith('prompts/extra.md') for item in definition.resources)
    from api_agent.workflows.artifact_compile import ArtifactCompileError, compile_definition
    with pytest.raises(ArtifactCompileError, match='typed-node prompts'):
        compile_definition(definition, [])


@pytest.mark.parametrize(('change', 'message'), [
    (lambda intake, slots: (intake, ''), 'undeclared slot'),
    (lambda intake, slots: (intake.replace('| intake | @intake_prompt |', '| intake | @missing |'), slots),
     'unknown inline prompt'),
    (lambda intake, slots: (intake.replace('| intake | @intake_prompt | always |\n', ''), slots),
     'unreferenced inline prompt'),
    (lambda intake, slots: (intake.replace('```text', '```'), slots), 'invalid inline prompt'),
    (lambda intake, slots: (intake.replace('(prompts/extra.md)', '(native.py)'), slots), 'Markdown or text'),
    (lambda intake, slots: (intake.replace('| intake | @intake_prompt |', '| finish | @intake_prompt |'), slots),
     'invalid prompt declaration'),
    (lambda intake, slots: (intake.replace('| .pending |', '| otherwise |'), slots), 'invalid prompt declaration'),
    (lambda intake, slots: (intake.replace('| .pending |', '| .pending === |'), slots), 'invalid condition syntax'),
    (lambda intake, slots: (intake.replace('(prompts/extra.md) | .pending', '(prompts/extra.md) | .pending |\n| intake | [again](prompts/extra.md) | always'), slots),
     'duplicate node prompt'),
    (lambda intake, slots: (intake, slots + '| greeting | greeting | [Python](native.py) |\n'), 'duplicate prompt slot'),
])
def test_typed_prompt_declarations_are_validated_at_admission(fixture_graph, change, message):
    root, entry = fixture_graph
    intake, slots = change(_PROMPTED_INTAKE, _SLOTS)
    _prompted(root, entry, intake, slots)
    with pytest.raises(LayoutError, match=message):
        load_definition(entry, base_root=root, pack_root=root)


def test_typed_prompt_file_keeps_one_resource_kind(fixture_graph):
    root, entry = fixture_graph
    _prompted(root, entry)
    entry.write_text(entry.read_text().replace('\n## Prompt Slots', '''
## Resources

| ID | Kind | Source |
| --- | --- | --- |
| extra | data | [text](prompts/extra.md) |

## Prompt Slots'''))
    with pytest.raises(LayoutError, match='conflicting resource kind'):
        load_definition(entry, base_root=root, pack_root=root)


def test_typed_prompt_slots_must_return_text(fixture_graph):
    from api_agent.workflows.execution import WorkflowError
    root, entry = fixture_graph
    _prompted(root, entry)
    native = root / 'native.py'
    native.write_text(native.read_text().replace("return 'visitor' + str(state['visits'])", "return state['visits']"))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages})
    with pytest.raises(WorkflowError, match='greeting must return text'):
        asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='numeric'))


def test_typed_conditions_must_select_exactly_one_route(fixture_graph):
    from api_agent.workflows.execution import WorkflowError
    root, entry = fixture_graph
    _conditional(root, entry, _WHEN_GROUP.replace('| START | finish | .pending |', '| START | finish | .visits == 1 |')
                 .replace('| intake | END | otherwise |', '| intake | END | not .pending |\n| intake | intake | otherwise |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages})
    with pytest.raises(WorkflowError, match='exactly one matching condition'):
        asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='ambiguous'))


def test_routed_edge_set_has_one_router_source(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'other.py', (root / 'native.py').read_text())
    entry.write_text(entry.read_text().replace('| intake | END | choose | [Python](native.py) |',
                                               '| intake | END | choose | [Python](other.py) |'))
    with pytest.raises(LayoutError, match='duplicate or mixed route source'):
        load_definition(entry, base_root=root, pack_root=root)


def test_state_condition_fields_include_nested_objects():
    from api_agent.workflows.predicates import parse_predicate, state_predicate_fields
    fields = state_predicate_fields({'scope': {'type': 'object', 'properties': {'kind': {'type': 'string'}}},
                                     'count': {'type': 'integer'}})
    predicate = parse_predicate('.scope.kind == "read" and .count > 1', fields)
    assert predicate.evaluate({'state': {'scope': {'kind': 'read'}, 'count': 2}}) is True


def test_typed_conditions_read_only_declared_scalar_state(fixture_graph):
    root, entry = fixture_graph
    _conditional(root, entry, _WHEN_GROUP.replace('.visits == 1', '.missing == 1'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='declared'):
        Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages})


@pytest.mark.parametrize(('schema', 'settings', 'message'), [
    ('broken = [', {}, 'invalid timeout declaration'),
    ('version = 1\n[fields.LIMIT]\ntype = "str"\ndefault = "soon"\n', {}, 'timeout key must be numeric'),
    ('version = 1\n[fields.OTHER]\ntype = "float"\ndefault = 1.0\n', {}, 'timeout key must be numeric'),
    ('version = 1\n[fields.LIMIT]\ntype = "float"\ndefault = 1.0\n', {'LIMIT': 0}, 'finite positive'),
    ('version = 1\n[fields.LIMIT]\ntype = "float"\ndefault = 1.0\n', {'LIMIT': float('inf')}, 'finite positive'),
    ('version = 1\n[fields.LIMIT]\ntype = "float"\ndefault = 1.0\n', {'LIMIT': True}, 'finite positive'),
])
def test_stateful_timeout_config_rejects_invalid_values(fixture_graph, schema, settings, message):
    root, entry = fixture_graph
    _write(root, 'limit.toml', schema)
    entry.write_text(entry.read_text() + '''\n## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| GRAPH | [config](limit.toml) | LIMIT |
''')
    if message != 'finite positive':
        with pytest.raises(LayoutError, match=message):
            load_definition(entry, base_root=root, pack_root=root)
    else:
        definition = load_definition(entry, base_root=root, pack_root=root)
        with pytest.raises(LayoutError, match=message):
            load_native_bindings(definition, config=settings)


def test_stateful_schema_and_bindings_are_explicit(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    catalog = load_native_bindings(definition)
    with pytest.raises(LayoutError, match='catalog does not match'):
        Workflow(definition, catalog=None, reducers={'messages': add_messages})
    with pytest.raises(LayoutError, match='unregistered state reducer'):
        Workflow(definition, catalog=catalog, reducers={})
    with pytest.raises(LayoutError, match='undeclared state field'):
        Workflow(definition, catalog=catalog,
                         reducers={'messages': add_messages, 'missing': lambda a, b: b})
    source = root / 'native.py'
    source.write_text(source.read_text().replace('class State(TypedDict):', 'def State():\n    return {}\n\nclass Unused(TypedDict):'))
    revised = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='must name a TypedDict'):
        Workflow(revised, catalog=load_native_bindings(revised), reducers={})


@pytest.mark.parametrize(('before', 'after', 'message'), [
    ("return {'pending': True}", "return {'unknown': True}", 'partial state update'),
    ("return {'pending': True}", "return None", 'partial state update'),
    ("return 'finish' if not state['pending'] else 'END'", "return 'missing'", 'undeclared target'),
])
def test_stateful_runtime_rejects_bad_native_returns(fixture_graph, before, after, message):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text(source.read_text().replace(before, after))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    with pytest.raises(ValueError, match=message):
        asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='invalid'))


@pytest.mark.parametrize('call', ['arun', 'astream_events'])
def test_stateful_runtime_requires_explicit_thread_and_update(fixture_graph, call):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    async def invoke():
        operation = getattr(workflow, call)([], thread_id='')
        if call == 'arun':
            await operation
        else:
            await anext(operation)
    with pytest.raises(ValueError, match='thread_id and partial state update'):
        asyncio.run(invoke())


def test_stateful_native_bindings_must_be_present_and_callable(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    original = source.read_text()
    source.write_text(original.replace('def intake(state, context):', 'def renamed(state, context):'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='missing admitted native binding'):
        Workflow(definition, catalog=load_native_bindings(definition),
                         reducers={'messages': add_messages})
    source.write_text(original.replace('def intake(state, context):',
                                       'class Binding:\n    pass\n\nintake = Binding()\n\ndef unused(state, context):'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='node registry must be callable'):
        Workflow(definition, catalog=load_native_bindings(definition),
                         reducers={'messages': add_messages})
    source.write_text(original.replace('def choose(state, context):',
                                       'class Binding:\n    pass\n\nchoose = Binding()\n\ndef unused(state, context):'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='router registry must be callable'):
        Workflow(definition, catalog=load_native_bindings(definition),
                         reducers={'messages': add_messages})


def test_stateful_sync_native_can_return_awaitable(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text(source.read_text().replace('def finish(state, context):\n    return {\'pending\': True}',
        '''async def delayed():
    return {'pending': True}

def finish(state, context):
    return delayed()'''))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})

    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                       thread_id='awaitable'))

    assert result['pending'] is True


def test_stateful_declared_local_import_must_be_captured_before_execution(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'src/omitted/__init__.py', '')
    _write(root, 'src/omitted/helper.py', 'def count(value):\n    return value + 1\n')
    source = root / 'native.py'
    source.write_text('from omitted.helper import count\n' + source.read_text())
    definition = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='must be declared'):
        load_native_bindings(definition)


def test_stateful_admitted_resource_bytes_not_mutable(fixture_graph):
    root, entry = fixture_graph
    entry.write_text(entry.read_text() + '''\n## Resources

| ID | Kind | Source |
| --- | --- | --- |
| greeting | prompt | [text](greeting.txt) |
''')
    _write(root, 'greeting.txt', 'immutable')
    definition = load_definition(entry, base_root=root, pack_root=root)
    assert definition.declared_resources['greeting'].content_bytes == b'immutable'
    with pytest.raises(TypeError):
        definition.contents[root / 'greeting.txt'] = b'changed'
    with pytest.raises(TypeError):
        definition.declared_resources['greeting'] = None


def test_stateful_source_link_accepts_explicit_pack_root(fixture_graph):
    root, entry = fixture_graph
    pack = root / 'pack'
    _write(pack, 'greeting.txt', 'selected')
    entry.write_text(entry.read_text() + '''\n## Resources

| ID | Kind | Source |
| --- | --- | --- |
| greeting | prompt | [text](<resource_pack>/greeting.txt) |
''')
    definition = load_definition(entry, base_root=root, pack_root=pack)
    assert definition.declared_resources['greeting'].logical_path == 'pack/greeting.txt'


@pytest.mark.parametrize(('old', 'new', 'message'), [
    ('[Python](native.py)', '[Python](%FF.py)', 'invalid encoded source link'),
    ('[Python](native.py)', '[Python](/native.py)', 'invalid source link'),
    ('[node intake](intake.md.html)', '[node intake](/intake.md.html)', 'invalid declaration link'),
    ('1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
     '   ', 'Flow requires steps'),
])
def test_stateful_declaration_link_and_flow_edges(fixture_graph, old, new, message):
    root, entry = fixture_graph
    entry.write_text(entry.read_text().replace(old, new))
    with pytest.raises(LayoutError, match=message):
        load_definition(entry, base_root=root, pack_root=root)


def test_stateful_replaced_node_selected_before_original_read(fixture_graph):
    root, entry = fixture_graph
    pack = root / 'pack'
    _write(pack, 'replacement.md.html', (root / 'intake.md.html').read_text().replace('(native.py)', '(../native.py)'))
    (root / 'intake.md.html').unlink()
    definition = load_definition(entry, base_root=root, pack_root=pack,
        replacements=(Replacement('intake', Path('replacement.md.html'), 'pack'),))
    assert pack / 'replacement.md.html' in definition.contents
    assert root / 'intake.md.html' not in definition.contents


def test_stateful_resource_id_cannot_alias_a_step(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'prompt.txt', 'greeting')
    entry.write_text(entry.read_text() + '''\n## Resources

| ID | Kind | Source |
| --- | --- | --- |
| intake | prompt | [text](prompt.txt) |
''')
    with pytest.raises(LayoutError, match='resource ID conflicts'):
        load_definition(entry, base_root=root, pack_root=root)


def test_stateful_packaged_proof_verified_before_native_execution(fixture_graph, monkeypatch):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    from api_agent.workflows import provenance, evidence
    calls = []
    monkeypatch.setattr(provenance, 'verified_documents',
                        lambda admitted, packaged_proof=None: calls.append((admitted.definition_id, packaged_proof)) or {})
    monkeypatch.setattr(evidence, 'publish', lambda envelope: None)
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    proof = object()
    catalog = load_native_bindings(definition, packaged_proof=proof)
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages},
                                services={'packaged_proof': proof})
    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='packaged'))
    assert calls == [(definition.definition_id, proof), (definition.definition_id, proof)]


def test_stateful_entry_must_be_utf8(fixture_graph):
    root, entry = fixture_graph
    entry.write_bytes(b'\xff')
    with pytest.raises(LayoutError, match='invalid UTF-8'):
        load_definition(entry, base_root=root, pack_root=root)


def test_stateful_rejects_component_nesting_over_limit(fixture_graph):
    root, entry = fixture_graph
    for index in range(66):
        next_step = (f'1.  [component group{index + 1}](group{index + 1}.md.html)'
                     if index < 65 else '1.  [node intake](intake.md.html)')
        _write(root, f'group{index}.md.html',
               f'# Component: group{index}\n\n## Flow\n\n{next_step}\n')
    entry.write_text(entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group0](group0.md.html)'))
    with pytest.raises(LayoutError, match='nesting exceeds 64'):
        load_definition(entry, base_root=root, pack_root=root)


def test_stateful_snapshot_has_total_size_limit(fixture_graph, monkeypatch):
    root, entry = fixture_graph
    from api_agent.workflows import native as stateful
    original = stateful._read_bounded
    fake_blob = b'x' * (8 * 1024 * 1024)
    for index in range(8):
        _write(root, f'piece{index}.txt', 'placeholder')
    entry.write_text(entry.read_text() + '''\n## Resources

| ID | Kind | Source |
| --- | --- | --- |
''' + ''.join(f'| piece{index} | data | [text](piece{index}.txt) |\n' for index in range(8)))
    def oversized_total(path, owner):
        if path.name.startswith('piece'):
            return fake_blob
        return original(path, owner)
    monkeypatch.setattr(stateful, '_read_bounded', oversized_total)
    with pytest.raises(LayoutError, match='snapshot exceeds 64 MiB'):
        load_definition(entry, base_root=root, pack_root=root)


def test_stateful_static_edge_between_nodes_and_routed_component_end(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)
''')
    source = root / 'native.py'
    source.write_text(source.read_text().replace(
        "return 'finish' if not state['pending'] else 'END'", "return 'END'"))
    entry.write_text(entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)')
        .replace('| finish | END | - | - |', '| finish | END | - | - |\n| group | END | - | - |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                       thread_id='routed-end'))
    assert result['visits'] == 1
    assert result['pending'] is False

    entry.write_text(entry.read_text().replace(
        '| intake | finish | choose | [Python](native.py) |\n| intake | END | choose | [Python](native.py) |',
        '| intake | finish | - | - |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                       thread_id='static-next'))
    assert result['pending'] is True


def test_stateful_failed_nested_node_closes_owned_component(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)
''')
    source = root / 'native.py'
    source.write_text(source.read_text().replace('def intake(state, context):',
                                                  'def intake(state, context):\n    raise RuntimeError("failed")'))
    entry.write_text(entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)')
        .replace('| finish | END | - | - |', '| finish | END | - | - |\n| group | END | - | - |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    events = []
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, services={'observe': events.append})
    with pytest.raises(RuntimeError, match='failed'):
        asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                   thread_id='nested-failed'))
    assert any(event['event'] == 'scope_end' and event['owner'] == 'group' for event in events)
    assert any(event['event'] == 'scope_end' and event['owner'] == 'GRAPH' for event in events)


def test_stateful_relative_package_dependency_uses_selected_source(fixture_graph):
    root, entry = fixture_graph
    package = 'stateful_relative_package'
    _write(root, f'src/{package}/__init__.py', '')
    _write(root, f'src/{package}/helper.py', 'def next_count(value):\n    return value + 1\n')
    _write(root, f'src/{package}/state.py',
           f'from .helper import next_count\n' +
           (root / 'native.py').read_text().replace("state['visits'] + 1", "next_count(state['visits'])"))
    for path in (entry, root / 'intake.md.html', root / 'finish.md.html'):
        path.write_text(path.read_text().replace('(native.py)', f'(src/{package}/state.py)'))
    entry.write_text(entry.read_text() + f'''\n## Native Dependencies

| Module | Source |
| --- | --- |
| {package} | [Python](src/{package}/__init__.py) |
| {package}.helper | [Python](src/{package}/helper.py) |
''')
    definition = load_definition(entry, base_root=root, pack_root=root)
    try:
        catalog = load_native_bindings(definition)
        workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})
        result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                           thread_id='relative'))
        assert result['visits'] == 1
    finally:
        sys.modules.pop(package + '.helper', None)
        sys.modules.pop(package, None)


def test_stateful_invalid_relative_import_rejected_before_native_exec(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text('from ..unavailable import helper\n' + source.read_text())
    definition = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='relative native import leaves package'):
        load_native_bindings(definition)


def test_stateful_graph_cancellation_closes_scope_and_releases_thread(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text('import asyncio\n' + source.read_text().replace(
        'def intake(state, context):', 'async def intake(state, context):\n    await asyncio.sleep(0.2)'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    events = []
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, services={'observe': events.append})

    async def cancel_then_retry():
        task = asyncio.create_task(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                                  thread_id='cancel'))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                   thread_id='cancel')

    result = asyncio.run(cancel_then_retry())
    assert result['visits'] == 1
    assert any(event['event'] == 'scope_end' and event['owner'] == 'GRAPH'
               and event['outcome'] == 'cancelled' for event in events)


def test_stateful_component_completion_is_idempotent(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    async def complete():
        token = workflow._turn_state.set({'visits': {}, 'open_components': {}})
        try:
            await workflow._complete_component('absent')
        finally:
            workflow._turn_state.reset(token)
    asyncio.run(complete())


def test_stateful_nested_declaration_cannot_leave_selected_roots(fixture_graph):
    root, entry = fixture_graph
    outside = root.parent / 'outside.md.html'
    outside.write_text((root / 'intake.md.html').read_text())
    entry.write_text(entry.read_text().replace('(intake.md.html)', '(../outside.md.html)'))
    with pytest.raises(LayoutError, match='source outside roots'):
        load_definition(entry, base_root=root, pack_root=root)


def test_stateful_relative_import_without_package_is_explicitly_rejected(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text('from . import helper\n' + source.read_text())
    _write(root, 'helper.py', 'def helper():\n    return 1\n')
    definition = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='local native import must be declared'):
        load_native_bindings(definition)


def test_stateful_scope_end_survives_component_cleanup_failure(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)
''')
    entry.write_text(entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)')
        .replace('| finish | END | - | - |', '| finish | END | - | - |\n| group | END | - | - |'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    events = []
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, services={'observe': events.append})
    original = workflow._complete_component
    calls = 0

    async def fail_scope_cleanup(parent, outcome='passed', *, state=None):
        nonlocal calls
        if parent is None:
            return
        calls += 1
        if calls == 1:
            return  # Simulate a component that remains open after its route.
        try:
            await original(parent, outcome='failed')
        except asyncio.CancelledError:
            pass
        raise RuntimeError('cleanup failed')

    workflow._complete_component = fail_scope_cleanup
    with pytest.raises(RuntimeError, match='cleanup failed'):
        asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                   thread_id='cleanup-failed'))
    assert any(event['event'] == 'scope_end' and event['owner'] == 'GRAPH' for event in events)


def test_stateful_stream_drains_queued_events_after_fast_producer(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})

    class FastGraph:
        async def astream_events(self, update, config, version):
            assert version == 'v2'
            for index in range(3):
                yield {'event': 'synthetic', 'index': index}

    workflow.graph = FastGraph()
    async def collect():
        return [event async for event in workflow.astream_events({}, thread_id='fast')]
    events = asyncio.run(collect())
    assert [event['index'] for event in events] == [0, 1, 2]


def test_stateful_dependency_finder_declines_unlisted_modules(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    from api_agent.workflows.native import (_AdmittedDependencyLoader)
    finder = _AdmittedDependencyLoader(definition)
    assert finder.find_spec('unlisted_dependency') is None


def test_stateful_stream_propagates_langchain_runnable_callbacks(fixture_graph):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text('from langchain_core.runnables import RunnableLambda\n' + source.read_text().replace(
        'def intake(state, context):', '''async def intake(state, context):
    await RunnableLambda(lambda value: value, name='synthetic_leaf').ainvoke('safe')'''))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})

    async def collect():
        return [event async for event in workflow.astream_events(
            {'messages': [], 'pending': False, 'visits': 0}, thread_id='runnable')]

    events = asyncio.run(collect())
    assert any(event['event'] == 'on_chain_start' and event['name'] == 'synthetic_leaf'
               for event in events)
    assert any(event['event'] == 'on_chain_end' and event['name'] == 'synthetic_leaf'
               for event in events)


def _library_pack(root, library, *, source='../library', distribution='synthetic-workflow',
                  resource='synthetic_workflows'):
    pack = root / 'pack'
    pack.mkdir(exist_ok=True)
    _write(pack, 'config.toml', 'version=1\n')
    _write(pack, 'selected.txt', 'selected')
    _write(pack, 'pack.toml', f'''version=1
id="synthetic-pack"
config="config.toml"
[workflow]
version = 1
root="base"
entry="flow.md.html"
replacements=[{{target_id="greeting",entry="selected.txt",root_id="pack"}}]
[workflow.library]
source="{source}"
distribution="{distribution}"
resource="{resource}"
''')
    entry = library / 'flow.md.html'
    entry.write_text(entry.read_text() + '''\n## Resources

| ID | Kind | Source |
| --- | --- | --- |
| greeting | prompt | [text](missing.txt) |
''')
    native = library / 'native.py'
    native.write_text(native.read_text().replace("content='new'",
                                           "content=context.resources['greeting'].content_bytes.decode()"))
    return pack


def test_source_sibling_workflow_library_keeps_runtime_base_and_pack_replacement(fixture_graph):
    root, _entry = fixture_graph
    import shutil
    library = root / 'library'
    library.mkdir()
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, library / name)
    pack = _library_pack(root, library)
    runtime = root / 'runtime'
    runtime.mkdir()
    context = load_launch_context(runtime, pack)
    definition = load_selected_definition(context)
    catalog = load_native_bindings(definition)
    workflow = load_workflow(context, catalog, definition=definition,
                             reducers={'messages': add_messages})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                       thread_id='sibling'))
    assert context.base_root == runtime
    assert context.workflow_base_root == library
    assert definition.roots == {'base': library, 'pack': pack}
    assert definition.declared_resources['greeting'].content_bytes == b'selected'
    assert result['messages'][0].content == 'selected'


def test_installed_workflow_library_requires_distribution_owned_selection(fixture_graph, monkeypatch):
    root, _entry = fixture_graph
    import shutil
    installed_root = root / 'installed'
    library = installed_root / 'synthetic_workflows'
    library.mkdir(parents=True)
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, library / name)
    pack = _library_pack(root, library, source='../missing-library')
    runtime = root / 'runtime'
    runtime.mkdir()

    class Distribution:
        metadata = {'Name': 'synthetic-workflow'}
        files = [Path('synthetic_workflows') / name for name in
                 ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html')]
        def locate_file(self, name):
            return installed_root / name

    from api_agent.runtime import resource_pack
    monkeypatch.setattr(resource_pack.metadata, 'distributions',
                        lambda **kwargs: iter([Distribution()]))
    context = load_launch_context(runtime, pack)
    definition = load_selected_definition(context)
    catalog = load_native_bindings(definition)
    workflow = load_workflow(context, catalog, definition=definition,
                             reducers={'messages': add_messages})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                       thread_id='installed'))
    assert context.workflow_library_files is not None
    assert result['messages'][0].content == 'selected'

    Distribution.files.remove(Path('synthetic_workflows/native.py'))
    context = load_launch_context(runtime, pack)
    with pytest.raises(ResourcePackError, match='not owned'):
        load_selected_definition(context)


def test_installed_workflow_library_accepts_alias_above_distribution_root(fixture_graph, monkeypatch):
    root, _entry = fixture_graph
    import shutil
    real_site = root / 'real-installed'
    library = real_site / 'synthetic_workflows'
    library.mkdir(parents=True)
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, library / name)
    site_alias = root / 'site-alias'
    site_alias.symlink_to(real_site, target_is_directory=True)
    pack = _library_pack(root, library, source='../missing-library')
    runtime = root / 'runtime'
    runtime.mkdir()

    class Distribution:
        metadata = {'Name': 'synthetic-workflow'}
        files = [Path('synthetic_workflows') / name for name in
                 ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html')]

        def locate_file(self, name):
            return site_alias / name

    from api_agent.runtime import resource_pack
    monkeypatch.setattr(resource_pack.metadata, 'distributions',
                        lambda **kwargs: iter([Distribution()]))
    context = load_launch_context(runtime, pack)
    assert context.workflow_base_root == library
    definition = load_selected_definition(context)
    catalog = load_native_bindings(definition)
    workflow = load_workflow(context, catalog, definition=definition,
                             reducers={'messages': add_messages})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                       thread_id='aliased-site'))
    assert result['messages'][0].content == 'selected'


@pytest.mark.parametrize(('source', 'resource', 'message'), [
    ('/absolute', 'synthetic_workflows', 'sibling directory'),
    ('../../escape', 'synthetic_workflows', 'sibling directory'),
    ('../missing', '../escape', 'normalized relative'),
    ('../missing', '/absolute', 'normalized relative'),
])
def test_workflow_library_rejects_invalid_selection(fixture_graph, source, resource, message):
    root, _entry = fixture_graph
    library = root / 'library'
    library.mkdir()
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        _write(library, name, (root / name).read_text())
    pack = _library_pack(root, library, source=source, resource=resource)
    runtime = root / 'runtime'
    runtime.mkdir()
    with pytest.raises(ResourcePackError, match=message):
        load_launch_context(runtime, pack)


def test_timeout_admission_rejects_bad_schema_and_selected_override_before_native_effects(fixture_graph):
    root, entry = fixture_graph
    marker = root / 'native-loaded.txt'
    source = root / 'native.py'
    source.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("loaded")\n'
                      + source.read_text())
    _write(root, 'limit.toml', 'broken = [\n')
    entry.write_text(entry.read_text() + '''\n## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| GRAPH | [config](limit.toml) | LIMIT |
''')
    with pytest.raises(LayoutError, match='invalid timeout declaration'):
        load_definition(entry, base_root=root, pack_root=root)
    assert not marker.exists()

    _write(root, 'limit.toml', 'version=1\n[fields.LIMIT]\ntype="float"\ndefault=1.0\n')
    definition = load_definition(entry, base_root=root, pack_root=root)
    with pytest.raises(LayoutError, match='finite positive'):
        load_native_bindings(definition, config={'LIMIT': 0})
    assert not marker.exists()

    catalog = load_native_bindings(definition, config={'LIMIT': 0.5})
    assert marker.exists()
    with pytest.raises(LayoutError, match='does not match selected values'):
        Workflow(definition, catalog=catalog, reducers={'messages': add_messages},
                         config={'LIMIT': 1.0})
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages},
                                config={'LIMIT': 0.5})
    assert workflow.timeouts['GRAPH'] == 0.5


def test_timeout_source_cannot_relabel_prompt_resource(fixture_graph):
    root, entry = fixture_graph
    _write(root, 'limit.toml', 'version=1\n[fields.LIMIT]\ntype="float"\ndefault=1.0\n')
    entry.write_text(entry.read_text() + '''\n## Resources

| ID | Kind | Source |
| --- | --- | --- |
| greeting | prompt | [text](limit.toml) |

## Timeouts

| Owner | Source | Key |
| --- | --- | --- |
| GRAPH | [config](limit.toml) | LIMIT |
''')
    with pytest.raises(LayoutError, match='timeout source has conflicting kind'):
        load_definition(entry, base_root=root, pack_root=root)


def test_recorded_stateful_installed_owner_proof_precedes_native_effects(fixture_graph, monkeypatch):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    from api_agent.workflows import provenance, evidence
    calls = []
    monkeypatch.setattr(provenance, 'verified_documents',
                        lambda admitted, packaged_proof=None: calls.append(admitted.definition_id) or {})
    monkeypatch.setattr(evidence, 'publish', lambda payload: None)
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    catalog = load_native_bindings(definition)
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})
    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='installed-proof'))
    assert calls == [definition.definition_id, definition.definition_id]


@pytest.mark.parametrize(('mode', 'message'), [
    ('absent', 'exactly one'),
    ('ambiguous', 'exactly one'),
    ('wrong_name', 'identity mismatch'),
    ('no_inventory', 'no file inventory'),
    ('bad_inventory', 'invalid file inventory'),
    ('empty_inventory', 'no distribution-owned files'),
    ('missing_resource', 'not an installed directory'),
    ('symlink_resource', 'not an installed directory'),
])
def test_installed_library_rejects_invalid_distribution_ownership(fixture_graph, monkeypatch,
                                                                    mode, message):
    root, _entry = fixture_graph
    import shutil
    installed_root = root / 'installed'
    library = installed_root / 'synthetic_workflows'
    library.mkdir(parents=True)
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, library / name)
    pack = _library_pack(root, library, source='../missing-library')
    runtime = root / 'runtime'
    runtime.mkdir()
    if mode == 'symlink_resource':
        (installed_root / 'alias').symlink_to(library, target_is_directory=True)
        manifest = pack / 'pack.toml'
        manifest.write_text(manifest.read_text().replace('resource="synthetic_workflows"',
                                                        'resource="alias"'))
    if mode == 'missing_resource':
        manifest = pack / 'pack.toml'
        manifest.write_text(manifest.read_text().replace('resource="synthetic_workflows"',
                                                        'resource="not-installed"'))

    class Distribution:
        metadata = {'Name': 'different-name' if mode == 'wrong_name' else 'synthetic-workflow'}
        files = None if mode == 'no_inventory' else (
            [Path('synthetic_workflows/../synthetic_workflows/flow.md.html')] if mode == 'bad_inventory' else
            [Path('outside.py')] if mode == 'empty_inventory' else
            [Path('synthetic_workflows/flow.md.html')])
        def locate_file(self, name):
            return installed_root / name

    from api_agent.runtime import resource_pack
    records = [] if mode == 'absent' else [Distribution(), Distribution()] if mode == 'ambiguous' else [Distribution()]
    monkeypatch.setattr(resource_pack.metadata, 'distributions', lambda **kwargs: iter(records))
    with pytest.raises(ResourcePackError, match=message):
        load_launch_context(runtime, pack)


def test_library_present_invalid_source_cannot_fall_back(fixture_graph, monkeypatch):
    root, _entry = fixture_graph
    library = root / 'library'
    library.mkdir()
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        _write(library, name, (root / name).read_text())
    pack = _library_pack(root, library, source='../sibling')
    (root / 'sibling').write_text('not a directory')
    runtime = root / 'runtime'
    runtime.mkdir()
    from api_agent.runtime import resource_pack
    monkeypatch.setattr(resource_pack.metadata, 'distributions',
                        lambda **kwargs: pytest.fail('invalid source unexpectedly fell back'))
    with pytest.raises(ResourcePackError, match='not a regular directory'):
        load_launch_context(runtime, pack)
    (root / 'sibling').unlink()
    (root / 'sibling').symlink_to(root / 'missing-target', target_is_directory=True)
    with pytest.raises(ResourcePackError, match='symlink alias'):
        load_launch_context(runtime, pack)


@pytest.mark.parametrize(('library_table', 'message'), [
    ('source="../library"\ndistribution="synthetic-workflow"', 'requires source, distribution and resource'),
    ('source="../library"\ndistribution="invalid name"\nresource="synthetic_workflows"', 'must be a distribution name'),
])
def test_workflow_library_rejects_malformed_manifest(fixture_graph, library_table, message):
    root, _entry = fixture_graph
    pack = root / 'pack'
    pack.mkdir()
    _write(pack, 'config.toml', 'version=1\n')
    _write(pack, 'pack.toml', '''version=1
id="pack"
config="config.toml"
[workflow]
version = 1
root="base"
entry="flow.md.html"
[workflow.library]
''' + library_table + '\n')
    runtime = root / 'runtime'
    runtime.mkdir()
    with pytest.raises(ResourcePackError, match=message):
        load_launch_context(runtime, pack)


def test_workflow_library_source_symlink_cannot_escape_sibling_parent(fixture_graph):
    root, _entry = fixture_graph
    library = root / 'library'
    library.mkdir()
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        _write(library, name, (root / name).read_text())
    pack = _library_pack(root, library, source='../alias')
    outside = root.parent / 'elsewhere'
    outside.mkdir(exist_ok=True)
    (root / 'alias').symlink_to(outside, target_is_directory=True)
    runtime = root / 'runtime'
    runtime.mkdir()
    with pytest.raises(ResourcePackError, match='symlink alias'):
        load_launch_context(runtime, pack)


def test_workflow_library_rejects_intermediate_source_symlink(fixture_graph, monkeypatch):
    root, _entry = fixture_graph
    import shutil
    library = root / 'actual' / 'child'
    library.mkdir(parents=True)
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, library / name)
    pack = _library_pack(root, library, source='../alias/child')
    (root / 'alias').symlink_to(root / 'actual', target_is_directory=True)
    runtime = root / 'runtime'
    runtime.mkdir()
    from api_agent.runtime import resource_pack
    monkeypatch.setattr(resource_pack.metadata, 'distributions',
                        lambda **kwargs: pytest.fail('symlink source unexpectedly fell back'))
    with pytest.raises(ResourcePackError, match='symlink alias'):
        load_launch_context(runtime, pack)


def test_workflow_library_rejects_intermediate_inventory_symlink(fixture_graph, monkeypatch):
    root, _entry = fixture_graph
    import shutil
    installed_root = root / 'installed'
    actual = installed_root / 'synthetic_workflows' / 'actual'
    actual.mkdir(parents=True)
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, actual / name)
    (installed_root / 'synthetic_workflows' / 'alias').symlink_to(actual, target_is_directory=True)
    pack = _library_pack(root, actual, source='../missing-library',
                         resource='synthetic_workflows')
    runtime = root / 'runtime'
    runtime.mkdir()

    class Distribution:
        metadata = {'Name': 'synthetic-workflow'}
        files = [Path('synthetic_workflows/alias') / name for name in
                 ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html')]

        def locate_file(self, name):
            return installed_root / name

    from api_agent.runtime import resource_pack
    monkeypatch.setattr(resource_pack.metadata, 'distributions',
                        lambda **kwargs: iter([Distribution()]))
    with pytest.raises(ResourcePackError, match='invalid owned file'):
        load_launch_context(runtime, pack)


def test_workflow_library_rejects_intermediate_installed_resource_symlink(fixture_graph, monkeypatch):
    root, _entry = fixture_graph
    import shutil
    installed_root = root / 'installed'
    actual = installed_root / 'actual' / 'synthetic_workflows'
    actual.mkdir(parents=True)
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, actual / name)
    (installed_root / 'alias').symlink_to(installed_root / 'actual', target_is_directory=True)
    pack = _library_pack(root, actual, source='../missing-library',
                         resource='alias/synthetic_workflows')
    runtime = root / 'runtime'
    runtime.mkdir()

    class Distribution:
        metadata = {'Name': 'synthetic-workflow'}
        files = [Path('alias/synthetic_workflows') / name for name in
                 ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html')]

        def locate_file(self, name):
            return installed_root / name

    from api_agent.runtime import resource_pack
    monkeypatch.setattr(resource_pack.metadata, 'distributions',
                        lambda **kwargs: iter([Distribution()]))
    with pytest.raises(ResourcePackError, match='not an installed directory'):
        load_launch_context(runtime, pack)


def test_installed_library_rejects_inventory_file_without_physical_regular_file(fixture_graph, monkeypatch):
    root, _entry = fixture_graph
    import shutil
    installed_root = root / 'installed'
    library = installed_root / 'synthetic_workflows'
    library.mkdir(parents=True)
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, library / name)
    pack = _library_pack(root, library, source='../missing-library')
    runtime = root / 'runtime'
    runtime.mkdir()

    class Distribution:
        metadata = {'Name': 'synthetic-workflow'}
        files = [Path('synthetic_workflows/missing.py')]
        def locate_file(self, name):
            return installed_root / name

    from api_agent.runtime import resource_pack
    monkeypatch.setattr(resource_pack.metadata, 'distributions',
                        lambda **kwargs: iter([Distribution()]))
    with pytest.raises(ResourcePackError, match='invalid owned file'):
        load_launch_context(runtime, pack)


def test_workflow_library_origin_check_rejects_other_logical_base(fixture_graph):
    root, _entry = fixture_graph
    import shutil
    from dataclasses import replace
    library = root / 'library'
    library.mkdir()
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, library / name)
    pack = _library_pack(root, library)
    runtime = root / 'runtime'
    runtime.mkdir()
    context = load_launch_context(runtime, pack)
    definition = load_selected_definition(context)
    catalog = load_native_bindings(definition)
    wrong_context = replace(context, workflow_base_root=runtime)
    with pytest.raises(ResourcePackError, match='does not match selected pack graph'):
        load_workflow(wrong_context, catalog, definition=definition,
                      reducers={'messages': add_messages})


def test_native_host_runtime_import_is_trusted_by_physical_origin(fixture_graph):
    root, entry = fixture_graph
    import api_agent
    runtime_root = Path(api_agent.__file__).resolve().parents[2]
    source = root / 'native.py'
    source.write_text('from api_agent.providers.llm import Message\n' + source.read_text())
    definition = load_definition(entry, base_root=runtime_root, pack_root=root,
                                           entry_root='pack')
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                       thread_id='host-runtime'))
    assert result['visits'] == 1


def test_native_shadow_with_host_prefix_still_requires_admission(fixture_graph):
    root, entry = fixture_graph
    import api_agent
    runtime_root = Path(api_agent.__file__).resolve().parents[2]
    _write(root, 'src/api_agent/extra.py', 'def hidden():\n    return 1\n')
    source = root / 'native.py'
    source.write_text('from api_agent.extra import hidden\n' + source.read_text())
    definition = load_definition(entry, base_root=runtime_root, pack_root=root,
                                           entry_root='pack')
    with pytest.raises(LayoutError, match='local native import must be declared'):
        load_native_bindings(definition)


@pytest.mark.parametrize('explicit', [False, True])
def test_stateful_leaf_accounting_uses_journal_turn_identity(fixture_graph, tmp_path, monkeypatch, explicit):
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text('''from api_agent.providers.llm import (LLMRequest, CompletionRequest,
    LLMResult, Message, TextContent, Usage)
from api_agent.testing.inference_usage import publish
''' + source.read_text().replace('def intake(state, context):', '''def intake(state, context):
    request = LLMRequest('gpt-6-luna', CompletionRequest((Message('user', (TextContent('x'),)),)))
    result = LLMResult(Message('assistant', (TextContent('x'),)), Usage(1, 1, 0, cache_write_tokens=0))
    publish(request, result)'''))
    definition = load_definition(entry, base_root=root, pack_root=root)
    selected_turn = '00000000-0000-4000-8000-000000000012' if explicit else None
    journal = WorkflowJournal(definition, turn_id=selected_turn)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, journal=journal)
    import json
    events = tmp_path / 'usage.jsonl'
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS', str(events))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'synthetic::turn (call)')
    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                               thread_id='accounting', turn_id=selected_turn,
                               turn_kind='approval' if explicit else 'initial'))
    row = json.loads(events.read_text().splitlines()[0])['data']
    envelope = journal.finalize()
    calls = [event for event in envelope['events'] if event['event'] == 'call']
    assert row['turn_id'] == journal.turn_id == envelope['turn_id']
    assert row['turn_kind'] == ('approval' if explicit else 'initial')
    assert calls[0]['call_id'] == row['call_id']
    assert envelope['completeness'] == 'complete'


@pytest.mark.parametrize('journal_source', ['workflow', 'invocation', 'service'])
@pytest.mark.parametrize('recorded', [False, True])
def test_stateful_rejects_mismatched_turn_before_effects(
        fixture_graph, monkeypatch, journal_source, recorded):
    root, entry = fixture_graph
    marker = root / 'node-ran.txt'
    source = root / 'native.py'
    source.write_text('from pathlib import Path\n' + source.read_text().replace(
        'def intake(state, context):',
        f'def intake(state, context):\n    Path({str(marker)!r}).write_text("ran")'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    journal = WorkflowJournal(definition, turn_id='00000000-0000-4000-8000-000000000010')
    workflow = Workflow(
        definition, catalog=load_native_bindings(definition),
        reducers={'messages': add_messages},
        journal=journal if journal_source == 'workflow' else None)
    supplied_journal = journal if journal_source == 'invocation' else None
    services = {'journal': journal} if journal_source == 'service' else None
    if recorded:
        monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
        monkeypatch.setattr('api_agent.workflows.execution._source_manifest',
                            lambda *args: pytest.fail('provenance ran before turn check'))
    with pytest.raises(ValueError, match='turn identity disagrees'):
        asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                                   thread_id='mismatch', services=services,
                                   journal=supplied_journal,
                                   turn_id='00000000-0000-4000-8000-000000000011'))
    assert not marker.exists()
    assert journal.finalize()['events'] == []


def test_stateful_missing_node_event_cannot_complete_journal(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    journal = WorkflowJournal(definition)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                reducers={'messages': add_messages}, journal=journal)
    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                               thread_id='complete'))
    complete = journal.finalize()
    assert complete['completeness'] == 'complete'
    tampered = WorkflowJournal(definition)
    for event in complete['events']:
        if event['event'] != 'node':
            tampered.observe(dict(event))
    assert tampered.finalize()['completeness'] == 'partial'


def test_mixed_workflow_owners_verify_before_native_import(fixture_graph, monkeypatch):
    root, _entry = fixture_graph
    import shutil
    library = root / 'library'
    library.mkdir()
    for name in ('flow.md.html', 'native.py', 'intake.md.html', 'finish.md.html'):
        shutil.copy2(root / name, library / name)
    pack = _library_pack(root, library)
    runtime = root / 'runtime'
    runtime.mkdir()
    marker = root / 'native-loaded'
    native = library / 'native.py'
    native.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("loaded")\n'
                      + native.read_text())
    context = load_launch_context(runtime, pack)
    definition = load_selected_definition(context)
    assert definition.roots == {'base': library, 'pack': pack}
    observed = []
    from api_agent.workflows import provenance, evidence
    def verify(owned, *, packaged_proof=None):
        assert owned.roots == {'base': library, 'pack': pack}
        observed.append((owned.definition_id, packaged_proof, marker.exists()))
        return {'owners': ['base', 'pack']}
    monkeypatch.setattr(provenance, 'verified_documents', verify)
    monkeypatch.setattr(evidence, 'publish', lambda envelope: None)
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING', '1')
    catalog = load_native_bindings(definition)
    workflow = load_workflow(context, catalog, definition=definition,
                             reducers={'messages': add_messages})
    asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0},
                               thread_id='mixed-owner'))
    assert observed[0] == (definition.definition_id, None, False)
    assert observed[1] == (definition.definition_id, None, True)


def test_parent_timeout_binding_rejects_unadmitted_budget():
    from api_agent.providers.progress import bind_timeout_budget
    with pytest.raises(TypeError, match='Expected TimeoutBudget'):
        with bind_timeout_budget(object()):
            pytest.fail('Invalid parent budget must not enter execution')


def test_selected_installed_definition_cannot_escape_owned_inventory(fixture_graph):
    root, entry = fixture_graph
    context = SimpleNamespace(base_root=root, pack_root=root, workflow_entry=entry,
        workflow_root='base', workflow_replacements=(), workflow_library_files=frozenset())
    with pytest.raises(ResourcePackError, match='not owned by installed distribution'):
        load_selected_definition(context)


def test_stateful_pack_rejects_external_conditions(fixture_graph):
    root, entry = fixture_graph
    context = SimpleNamespace(base_root=root, pack_root=root, workflow_entry=entry,
        workflow_root='base', workflow_replacements=())
    definition = load_selected_definition(context)
    catalog = load_native_bindings(definition)
    workflow = load_workflow(context, catalog, conditions=None, definition=definition,
                             reducers={'messages': add_messages})
    assert workflow.definition is definition
    with pytest.raises(ResourcePackError, match='not external conditions'):
        load_workflow(context, catalog, conditions={}, definition=definition,
                      reducers={'messages': add_messages})


def test_presented_stateful_selection_keeps_the_real_loader(fixture_graph):
    from api_agent.workflows.document_format import wrap_document
    from api_agent.workflows.native import (TypedDefinition)
    root, entry = fixture_graph
    bare = load_definition(entry, base_root=root, pack_root=root)
    expected_edge_line = entry.read_text().splitlines()[bare.edges[0]['line'] - 1]
    entry.write_bytes(wrap_document(entry.read_bytes(), root=True,
                                    asset_prefix='.private_resources/viewer/'))
    context = SimpleNamespace(base_root=root, pack_root=root, workflow_entry=entry,
        workflow_root='base', workflow_replacements=())

    selected = load_selected_definition(context)

    assert isinstance(selected, TypedDefinition)
    assert selected.graph_id == bare.graph_id
    assert tuple({key: value for key, value in edge.items() if key != 'line'}
                 for edge in selected.edges) == tuple(
                     {key: value for key, value in edge.items() if key != 'line'}
                     for edge in bare.edges)
    assert selected.nodes == bare.nodes
    assert entry.read_text().splitlines()[selected.edges[0]['line'] - 1] == expected_edge_line
    assert selected.contents[entry] == entry.read_bytes()
    assert selected.definition_id != bare.definition_id


def test_presented_stateful_cli_keeps_validation_and_topology(fixture_graph, capsys):
    from api_agent.workflows.document_format import wrap_document
    root, entry = fixture_graph
    assert workflow_cli(['diagram', '--entry', str(entry)]) == 0
    expected = capsys.readouterr().out
    entry.write_bytes(wrap_document(entry.read_bytes(), root=True,
                                    asset_prefix='.private_resources/viewer/'))

    assert workflow_cli(['diagram', '--entry', str(entry)]) == 0

    assert capsys.readouterr().out == expected
    assert workflow_cli(['validate', '--entry', str(entry)]) == 0
    assert 'Valid workflow: synthetic_flow' in capsys.readouterr().out


@pytest.mark.parametrize('with_journal', [False,True])
def test_stateful_plain_capture_requires_explicit_contract_and_retains_only_fields(fixture_graph,with_journal):
    root, entry = fixture_graph
    definition = load_definition(entry,base_root=root,pack_root=root)
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    boundary = EmissionBoundary(ContentPolicy(True))
    journal = WorkflowJournal(definition,log_content=True,content_policy=ContentPolicy(True), boundary=boundary) if with_journal else None
    contract = {'schema':{'type':'object','properties':{'visits':{'type':'integer'}}},
                'plain_fields':['visits']}
    workflow = Workflow(definition,catalog=load_native_bindings(definition),
        reducers={'messages':add_messages},journal=journal,
        services={'emissions': boundary, 'plain_io_contracts':{'intake':{'input':contract,'output':contract}}})
    result = asyncio.run(workflow.arun({'messages':[HumanMessage(content='unclassified text')],
        'pending':False,'visits':0},thread_id='classified'))
    assert result['visits'] == 1
    if with_journal:
        captures = [row for row in journal.finalize()['events'] if row['event']=='capture']
        assert [row['values'] for row in captures[:2]] == [{'visits':0},{'visits':1}]
        assert len(captures) == 4
        assert all(row['capture_class'] == 'schema_user_io' for row in captures[2:])
        assert 'unclassified text' not in str(captures)


from test_workflow_observations import recording_policy


@pytest.mark.parametrize('observe',[False,True])
@pytest.mark.parametrize('authority',[False,True])
def test_recorded_stateful_graph_publishes_only_resolved_log_permission(fixture_graph,recording_policy,tmp_path,monkeypatch,observe,authority):
    import json
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.workflows.observations import WorkflowObservationFactory
    from api_agent.workflows.evidence import attachment
    resolved,allowed = recording_policy
    root,entry = fixture_graph
    source = root/'native.py'
    source.write_text(source.read_text().replace('    visits: int','    visits: int\n    text: str').replace(
        "'visits': state['visits'] + 1}","'visits': state['visits'] + 1, 'text': state['text']}"))
    events = tmp_path/'recorded.jsonl'
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING','1')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    monkeypatch.setattr('api_agent.workflows.provenance.verified_documents',lambda *args,**kwargs:None)
    definition = load_definition(entry,base_root=root,pack_root=root)
    rows = []
    contract = {'schema':{'type':'object','properties':{'text':{'type':'string'}}},'plain_fields':['text']}
    factory = WorkflowObservationFactory(resolved,boundary=EmissionBoundary(),sink=rows.append)
    workflow = Workflow(definition,catalog=load_native_bindings(definition),reducers={'messages':add_messages},
        config={'LOG_CONTENT':True},services={'observation_factory':factory if observe else None,
            'content_policy':factory.log_policy if authority else None,
            **({'emissions':factory.boundary} if authority else {}),
            'plain_io_contracts':{'intake':{'input':contract,'output':contract}}})
    result = asyncio.run(workflow.arun({'messages':[],'pending':False,'visits':0,
        'text':'private-stateful-sentinel'},thread_id='recorded-policy'))
    assert result['text']=='private-stateful-sentinel' and result['visits']==1
    packet = next(json.loads(line) for line in events.read_text().splitlines() if json.loads(line)['kind']=='workflow_evidence')
    payload = packet['data']
    _,attachment_bytes,_ = attachment(SimpleNamespace(text=json.dumps(payload)),None)
    allowed = allowed and (observe or authority)
    assert ('private-stateful-sentinel' in attachment_bytes.decode()) is allowed
    assert payload['content']==('permitted_scrubbed' if allowed else 'withheld')
    if observe:
        assert [row['event_id'] for row in rows]==[row['event_id'] for row in payload['events']]


@pytest.mark.parametrize('case',[
    pytest.param('visible',id='plain-positive'),
    pytest.param('x'*65536,id='at-limit'),
    pytest.param('x'*65537,id='oversized'),
    pytest.param(['unavailable-private-sentinel'],id='unsupported'),
])
@pytest.mark.parametrize('log',[False,True])
@pytest.mark.parametrize('immutable',[False,True])
def test_recorded_stateful_frozen_capture_contract_is_observational(
        fixture_graph,case,log,tmp_path,monkeypatch,immutable):
    import json
    from types import MappingProxyType
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    from api_agent.telemetry.privacy import EmissionBoundary
    from api_agent.workflows.observations import WorkflowObservationFactory
    from api_agent.workflows.evidence import attachment
    root,entry = fixture_graph
    source = root/'native.py'
    source.write_text(source.read_text().replace('    visits: int','    visits: int\n    text: object').replace(
        "'visits': state['visits'] + 1}","'visits': state['visits'] + 1, 'text': state['text']}"))
    events = tmp_path/'frozen-events.jsonl'
    monkeypatch.setenv('API_AGENT_ALLURE_REPORTING','1')
    monkeypatch.setenv('API_AGENT_INTEGRATION_EVENTS',str(events))
    monkeypatch.setattr('api_agent.workflows.provenance.verified_documents',lambda *a,**k:None)
    resolved = ConfigResult({'LOG_CONTENT':log},{'LOG_CONTENT':ResolvedValue(log,'cli',True)},(),(),())
    rows = []
    factory = WorkflowObservationFactory(resolved,boundary=EmissionBoundary(),sink=rows.append)
    freeze = MappingProxyType if immutable else dict
    schema = freeze({'type':'object','properties':freeze({
        'text':freeze({'type':'array' if isinstance(case,list) else 'string'})})})
    contract = MappingProxyType({'schema':schema,'plain_fields':('text',)})
    definition = load_definition(entry,base_root=root,pack_root=root)
    workflow = Workflow(definition,catalog=load_native_bindings(definition),
        reducers={'messages':add_messages},config={'LOG_CONTENT':log},
        services={'observation_factory':factory,'plain_io_contracts':{
            'intake':{'input':contract,'output':contract}}})
    result = asyncio.run(workflow.arun({'messages':[],'pending':False,'visits':0,'text':case},
        thread_id='frozen-capture'))
    assert result['text']==case and result['visits']==1 and result['pending'] is True
    payload = next(json.loads(line)['data'] for line in events.read_text().splitlines()
        if json.loads(line)['kind']=='workflow_evidence')
    _,data,_ = attachment(SimpleNamespace(text=json.dumps(payload)),None)
    # The explicit intake contract remains restrictive; other nodes capture
    # their own declared JSON state by default.
    captures = [row for row in payload['events'] if row['event']=='capture'][:2]
    live = [row for row in rows if row['event']=='capture'][:2]
    reason = ('unsupported' if isinstance(case,list) else 'payload_limit' if len(case)>65536
              else 'truncated' if log and len(case)>512 else 'captured' if log else 'withheld')
    assert [row['reason'] for row in captures]==[reason,reason]
    assert [row['event_id'] for row in live]==[row['event_id'] for row in captures]
    if reason in ('captured','truncated'):
        assert [row['values']['text'] for row in captures]==[case[:512],case[:512]]
        assert [row['values']['text'] for row in live]==[case,case]
    else:
        assert all('values' not in row for row in captures+live)
        assert 'unavailable-private-sentinel' not in json.dumps(captures)
        assert 'x'*65537 not in data.decode()


def test_stateful_nodes_transfer_declared_grant_and_new_turn_is_fresh(fixture_graph):
    from api_agent.tooling.runtime import OwnerTools
    from test_tool_gate import Owner
    root, entry = fixture_graph
    native = root / 'native.py'
    native.write_text(native.read_text().replace(
        "def intake(state, context):\n", "async def intake(state, context):\n"
        "    prepared = await context.tools['reader'].prepare({'query': 'hello'})\n"
        "    context.services['grant'] = await context.tools['reader'].verify(prepared,\n"
        "        relevance=lambda _: {'relevant': True, 'grounded': True})\n").replace(
        "def finish(state, context):\n", "async def finish(state, context):\n"
        "    await context.tools['runner'].execute(context.services['grant'])\n"))
    (root / 'instance.toml').write_text('id = "sample/reader"\n')
    for name, port, operations in [('intake', 'reader', 'prepare, verify'), ('finish', 'runner', 'execute')]:
        path = root / f'{name}.md.html'
        path.write_text(path.read_text() + '\n## Tools\n\n| Port | Instance | Operations |\n'
                        f'| --- | --- | --- |\n| {port} | [reader](instance.toml) | {operations} |\n')
    definition = load_definition(entry, base_root=root, pack_root=root)
    owner = Owner('synthetic')
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
        reducers={'messages': add_messages}, services={'tools': OwnerTools(definition, owner)})
    async def turns():
        for _ in range(2):
            await workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='conversation')
    asyncio.run(turns())
    assert owner.calls == ['describe', 'validate', 'invoke', 'describe', 'validate', 'invoke']


def test_stateful_parent_lends_provider_without_checkpoint_or_history_to_child_graphs(fixture_graph):
    import json
    from api_agent.providers.llm import LLMResult, Message, TextContent
    from api_agent.workflows.contrived import build
    from test_workflow_trial_decisions import trial_uuid
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text(source.read_text().replace('def intake(state, context):', '''async def intake(state, context):
    for text in ('First child', 'Second child'):
        child = context.services['build_child'](context.services['complete'])
        await child.arun({'user_input': text})
'''))
    requests = []
    async def complete(request):
        requests.append(request)
        tail = json.loads(request.payload.messages[-1].content[0].text)
        original = request.payload.messages[1].content[0].text
        return LLMResult(Message('assistant', (TextContent(json.dumps(dict(
            original_input=original, route='skip', ready=tail['decisions']['ready'],
            final_output=original))),)))
    base = Path(__file__).resolve().parents[2]
    def build_child(borrowed):
        return build(base, llm=borrowed, selection='example',
                     trial=trial_uuid('skip', first_ready=False))
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
        reducers={'messages': add_messages}, services={'complete': complete, 'build_child': build_child})

    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='parent'))

    assert result['visits'] == 1
    assert len(requests) == 4
    assert len(requests[0].payload.messages) == len(requests[2].payload.messages) == 3
    assert requests[2].payload.messages[1].content[0].text == 'Second child'
    for initial, retry in ((requests[0], requests[1]), (requests[2], requests[3])):
        assert retry.payload.messages[:len(initial.payload.messages)] == initial.payload.messages
        assert retry.payload.messages[len(initial.payload.messages)].role == 'assistant'


@pytest.mark.parametrize('log', [False, True])
@pytest.mark.parametrize('awaited', [False, True])
def test_explicit_io_projection_precedes_large_state_admission(fixture_graph, log, awaited):
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    root, entry = fixture_graph
    source = root / 'native.py'
    source.write_text(source.read_text().replace('    visits: int', '    visits: int\n    records: list'))
    definition = load_definition(entry, base_root=root, pack_root=root)
    boundary = EmissionBoundary(ContentPolicy(log), secrets=['registered-credential'])
    journal = WorkflowJournal(definition, log_content=log, content_policy=ContentPolicy(log), boundary=boundary)
    def project(direction, values=None):
        return {'prompt': 'question registered-credential'} if direction == 'input' else {'html': 'x' * 4801}
    projected = []
    async def project_async(direction, values=None):
        await asyncio.sleep(0)
        projected.append(direction)
        return project(direction, values)
    projection = project_async if awaited else project
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
        reducers={'messages': add_messages}, journal=journal,
        services={'emissions': boundary, 'node_io_projections': {'intake': projection, 'finish': projection},
                  'graph_io_projection': projection})
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0,
        'records': ['private-record' * 10000]}, thread_id='projected'))
    assert result['records'] == ['private-record' * 10000]
    packet = journal.finalize()
    assert packet['completeness'] == 'complete'
    from api_agent.workflows.journal import validate_journal
    validate_journal(packet)
    captures = [row for row in packet['events'] if row['event'] == 'capture']
    assert len(captures) == 6
    assert projected == (['input', 'input', 'output', 'input', 'output', 'output'] if awaited else [])
    assert 'private-record' not in str(captures)
    assert 'registered-credential' not in str(captures)
    if log:
        assert all(row['reason'] == 'captured' for row in captures)
        assert [row['values']['html'] for row in captures if row['direction'] == 'output'] == ['x' * 4801] * 3
    else:
        assert all('values' not in row for row in captures)


@pytest.mark.parametrize('scope', ['node', 'graph'])
@pytest.mark.parametrize('invalid', ['return', 'raise'])
@pytest.mark.parametrize('awaited', [False, True])
def test_io_projection_failure_warns_without_failing_workflow(fixture_graph, scope, invalid, caplog, awaited):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    journal = WorkflowJournal(definition)
    def projection(*args):
        if invalid == 'raise':
            raise RuntimeError('private arbitrary callback detail')
        return []
    invoked = []
    async def project_async(*args):
        await asyncio.sleep(0)
        invoked.append(args[0])
        return projection(*args)
    callback = project_async if awaited else projection
    services = ({'node_io_projections': {'intake': callback}} if scope == 'node'
                else {'graph_io_projection': callback})
    workflow = Workflow(definition, catalog=load_native_bindings(definition),
        reducers={'messages': add_messages}, journal=journal, services=services)

    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='invalid-projection'))

    assert result['visits'] == 1
    captures = [row for row in journal.finalize()['events'] if row['event'] == 'capture']
    assert any(row['reason'] == 'unavailable' for row in captures)
    assert 'Workflow I/O capture unavailable: projection failed' in caplog.text
    assert 'private arbitrary callback detail' not in caplog.text
    assert invoked == (['input', 'output'] if awaited else [])


@pytest.mark.parametrize('recorded', [False, True])
@pytest.mark.parametrize('state_name', ['failure', 'partial', 'success', 'cancelled', 'unknown', None, 'invalid'])
@pytest.mark.parametrize('routed', [False, True])
def test_explicit_scope_outcomes_use_returned_state_without_interrupting(fixture_graph, state_name, routed, recorded):
    from api_agent.telemetry.outcomes import ExecutionOutcome, OutcomeCause, OutcomeState
    root, entry = fixture_graph
    _write(root, 'group.md.html', '''# Component: group

## Flow

1.  [node intake](intake.md.html)
2.  [node finish](finish.md.html)
''')
    text = entry.read_text().replace(
        '1.  [node intake](intake.md.html)\n2.  [node finish](finish.md.html)',
        '1.  [component group](group.md.html)').replace(
        '| finish | END | - | - |', '| finish | END | - | - |\n| group | END | - | - |')
    if not routed:
        text = text.replace('| intake | finish | choose | [Python](native.py) |\n| intake | END | choose | [Python](native.py) |', '| intake | finish | - | - |')
    entry.write_text(text)
    definition = load_definition(entry, base_root=root, pack_root=root)
    seen, events = [], []
    journal = WorkflowJournal(definition) if recorded else None
    def outcome(kind, owner, state):
        seen.append((kind, owner, state['pending'], state['visits']))
        if state_name == 'invalid':
            return 'failure'
        if state_name is None:
            return None
        status = OutcomeState(state_name)
        return ExecutionOutcome(status, OutcomeCause.EXECUTION if status in {OutcomeState.FAILURE, OutcomeState.PARTIAL} else None)
    workflow = Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages}, services={'scope_outcome': outcome, 'observe': events.append}, journal=journal)
    if state_name == 'invalid':
        with pytest.raises(ValueError, match='Invalid explicit scope outcome'):
            asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='invalid-scope'))
        return
    result = asyncio.run(workflow.arun({'messages': [], 'pending': False, 'visits': 0}, thread_id='scope'))
    assert result['pending'] and result['visits'] == 1
    if recorded:
        payload = journal.finalize()
        assert payload['completeness'] == 'complete'
        validate_journal(payload)
        if state_name == 'failure':
            from api_agent.workflows.journal import _routes_complete
            for event_kind, field, value in (
                ('node', 'outcome', 'invalid'),
                ('disposition', 'scope_id', 'missing'),
                ('disposition', 'owner', 'different'),
                ('disposition', 'outcome', 'invalid'),
            ):
                altered = [dict(row) for row in payload['events']]
                evidence = next(row for row in altered if row['event'] == event_kind)
                evidence[field] = value
                assert not _routes_complete(payload['projection'], altered)
            for owner in ('intake', 'group'):
                altered = [dict(row) for row in payload['events']]
                end = next(row for row in altered if row['event'] == 'scope_end' and row['owner'] == owner)
                end['outcome'] = 'passed'
                assert not _routes_complete(payload['projection'], altered)
            for remove in [
                lambda row: row['event'] == 'node' and row.get('node') == 'intake',
                lambda row: row['event'] == 'disposition' and row.get('owner') == 'group',
                lambda row: row['event'] == 'edge' and row.get('owner') == 'group' and row.get('target') == 'END',
            ]:
                altered = [dict(row) for row in payload['events'] if not remove(row)]
                for index, row in enumerate(altered, 1):
                    row['sequence'] = index
                assert not _routes_complete(payload['projection'], altered)
    assert seen == [('node', 'intake', False, 1), ('node', 'finish', True, 1), ('component', 'group', True, 1)]
    expected = {'success': 'passed', 'failure': 'failed', 'partial': 'partial', 'cancelled': 'cancelled', 'unknown': 'unknown', None: 'passed'}[state_name]
    assert [(row['owner'], row['outcome']) for row in events if row['event'] == 'scope_end' and row['kind'] in {'node', 'component'}] == [('intake', expected), ('finish', expected), ('group', expected)]


def test_returned_child_failure_does_not_poison_terminal_request(fixture_graph):
    from api_agent.telemetry.outcomes import (ExecutionOutcome, OperationOutcome,
        OutcomeCause, OutcomeState, bind_operation, report_outcome)
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    workflow = Workflow(definition, catalog=load_native_bindings(definition), reducers={'messages': add_messages})
    runtime = object()
    owner = OperationOutcome(runtime, None)
    receipts = []
    observation = SimpleNamespace(trace=runtime, report_outcome=lambda sid, outcome: receipts.append((sid, outcome)))
    failure = ExecutionOutcome(OutcomeState.FAILURE, OutcomeCause.EXECUTION)
    services_token = workflow._services.set({'scope_outcome': lambda kind, name, state: failure})
    journal_token = workflow._journal.set(observation)
    try:
        with bind_operation(owner):
            assert workflow._scope_outcome('node', 'intake', {}, 'child-scope') == 'failed'
            assert receipts == [('child-scope', failure)]
            assert owner.final.state == OutcomeState.UNKNOWN
            report_outcome(ExecutionOutcome(OutcomeState.SUCCESS), runtime=runtime)
            assert owner.final.state == OutcomeState.SUCCESS
    finally:
        workflow._services.reset(services_token)
        workflow._journal.reset(journal_token)
