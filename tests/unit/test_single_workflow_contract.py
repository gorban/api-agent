"""The unreleased workflow formats converge on one public engine contract."""
from pathlib import Path

import pytest

from api_agent.workflows.definition import load_definition, load_native_catalog
from api_agent.workflows.execution import Workflow, NodeFunction
from api_agent.workflows.model import LayoutError
from test_workflow_definition import make_graph
from test_workflow_stateful import fixture_graph


def test_binding_declaration_has_one_release_contract(tmp_path):
    entry = make_graph(tmp_path)

    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    assert definition.version == 1


def test_typed_declaration_uses_the_same_loader_and_engine(fixture_graph):
    root, entry = fixture_graph

    definition = load_definition(entry, base_root=root, pack_root=root)
    catalog = definition.load_catalog()
    from langgraph.graph.message import add_messages
    workflow = Workflow(definition, catalog=catalog, reducers={'messages': add_messages})

    assert definition.version == 1
    assert type(workflow) is Workflow


@pytest.mark.parametrize('heading', ['# Workflow v2', '# Workflow v3', '# LangGraph flow'])
def test_pre_release_headings_are_not_compatibility_formats(tmp_path, heading):
    entry = make_graph(tmp_path)
    entry.write_text(entry.read_text().replace('# Workflow', heading))

    with pytest.raises(LayoutError, match='expected Workflow heading'):
        load_definition(entry, base_root=tmp_path, pack_root=tmp_path)


@pytest.mark.parametrize('options,message', [
    ({'catalog': object()}, 'requires a declared State'),
    ({'reducers': {'field': object()}}, 'requires a declared State'),
    ({'checkpointer': object()}, 'requires a declared State'),
])
def test_checkpoints_require_declared_state(tmp_path, options, message):
    entry = make_graph(tmp_path)
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)

    with pytest.raises(LayoutError, match=message):
        Workflow(definition, **options)


def test_state_routes_cannot_be_replaced_by_external_conditions(fixture_graph):
    root, entry = fixture_graph
    definition = load_definition(entry, base_root=root, pack_root=root)

    with pytest.raises(LayoutError, match='declared routers'):
        Workflow(definition, conditions={})


def test_binding_workflow_does_not_accept_conversation_checkpoints(tmp_path):
    import asyncio
    entry = make_graph(tmp_path)
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    namespace = load_native_catalog(definition, tmp_path / 'native.py')
    workflow = Workflow(definition, {'inspect': NodeFunction(namespace['inspect'])})

    from api_agent.workflows.execution import WorkflowError

    with pytest.raises(WorkflowError, match='checkpoints require a declared State'):
        asyncio.run(workflow.arun({}, thread_id='not-a-checkpoint'))


def test_successful_graph_end_does_not_hide_a_required_observer_failure(tmp_path):
    import asyncio
    entry = make_graph(tmp_path)
    definition = load_definition(entry, base_root=tmp_path, pack_root=tmp_path)
    namespace = load_native_catalog(definition, tmp_path / 'native.py')
    def observe(event):
        if event['event'] == 'scope_end' and event['kind'] == 'graph':
            raise RuntimeError('graph end observer failed')
    workflow = Workflow(definition, {'inspect': NodeFunction(namespace['inspect'])}, services={'observe': observe})

    with pytest.raises(RuntimeError, match='graph end observer failed'):
        asyncio.run(workflow.arun({}))
