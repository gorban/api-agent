"""Compiled definitions run on the real engines and agree with parsed definitions."""
import asyncio
import json
from types import SimpleNamespace

from langgraph.graph.message import add_messages
import pytest

from artifact_acceptance_scenarios import V3_INITIAL, _reply, _trial
from artifact_acceptance_support import QUALIFICATION, ROOT
from artifact_unit_support import (SAME, inline_built, isolated_imports, load_built, rich_built,  # noqa: F401
                                   tool_built, v2_built, v3_built)

from api_agent.runtime.resource_pack import ResourcePackError, load_workflow
from api_agent.workflows import artifact
from api_agent.workflows.definition import load_definition
from api_agent.workflows.execution import Workflow
from api_agent.workflows.identity import declaration_identity, workflow_identity
from api_agent.workflows.journal import WorkflowJournal, projection, validate_journal
from api_agent.workflows.model import LayoutError
from api_agent.workflows.stateful import load_stateful_catalog, load_stateful_definition


def run(coroutine):
    return asyncio.run(coroutine)


def source_v3(root=QUALIFICATION):
    definition = load_stateful_definition(root / 'flow.md.html', base_root=root, pack_root=root)
    return definition, load_stateful_catalog(definition)


def test_v3_compiled_workflow_matches_the_parsed_workflow_result(v3_built):
    compiled = load_built(v3_built).workflow(reducers={'messages': add_messages})
    definition, catalog = source_v3()
    from api_agent.workflows.stateful import StatefulWorkflow
    parsed = StatefulWorkflow(definition, catalog=catalog, reducers={'messages': add_messages})

    expected = run(parsed.arun(dict(V3_INITIAL), thread_id='t'))
    actual = run(compiled.arun(dict(V3_INITIAL), thread_id='t'))

    assert [message.content for message in actual.pop('messages')] == [
        message.content for message in expected.pop('messages')]
    assert actual == expected and actual['trail'] == ['intake', 'work:0', 'work:1', 'finish']


def test_v3_catalog_exposes_verified_namespaces_and_selected_timeouts(rich_built):
    definition = load_built(rich_built)

    default = definition.load_catalog()
    configured = definition.load_catalog(config={'GRAPH_LIMIT': 2.0})
    workflow = definition.workflow(reducers={'messages': add_messages}, config={'GRAPH_LIMIT': 2.0})

    assert dict(default.timeout_values) == {'GRAPH': 5.5} and dict(configured.timeout_values) == {'GRAPH': 2.0}
    assert default.admitted_modules == {'aq_bindings.helper', 'aq_bindings.native'}
    assert set(default.namespaces) == {definition.schema_source}
    assert workflow.timeouts['GRAPH'] == 2.0
    assert workflow.definition.declared_resources['notes'].content_bytes == b'notes\n'


def test_v3_workflow_rejects_a_catalog_for_other_timeouts(rich_built):
    definition = load_built(rich_built)

    with pytest.raises(LayoutError, match='timeout configuration'):
        definition.workflow(reducers={'messages': add_messages}).__class__(
            definition, catalog=definition.load_catalog(config={'GRAPH_LIMIT': 2.0}),
            reducers={'messages': add_messages})


def test_v3_engine_records_compiled_plan_provenance(v3_built, monkeypatch):
    workflow = load_built(v3_built).workflow(reducers={'messages': add_messages})
    monkeypatch.setattr('api_agent.workflows.stateful.recording_enabled', lambda: True)

    assert run(workflow.arun(dict(V3_INITIAL), thread_id='t'))['trail'] == ['intake', 'work:0', 'work:1', 'finish']


def test_compiled_run_journal_has_a_valid_source_manifest(v3_built):
    definition = load_built(v3_built)
    journal = WorkflowJournal(definition, source_manifest=artifact.compiled_provenance(definition))
    workflow = definition.workflow(reducers={'messages': add_messages}, journal=journal)

    run(workflow.arun(dict(V3_INITIAL), thread_id='recorded'))

    assert validate_journal(journal.finalize()) is not None


def contrived_services(definition, requests):
    async def llm(request):
        requests.append(request)
        return _reply(request)
    row = next(row for row in definition.layout.tables['Functions'] if row['ID'] == 'first_draft')
    return dict(llm=llm, selection='synthetic', trial=_trial('skip'), observe=None, llm_timeout=120,
                output_schema=json.loads(definition.contents[row['Output']]))


def test_v2_compiled_workflow_matches_contrived_source_run(v2_built):
    from api_agent.workflows import contrived
    definition = load_built(v2_built)
    compiled_requests, source_requests = [], []

    async def source_llm(request):
        source_requests.append(request)
        return _reply(request)

    parsed = contrived.build(ROOT, llm=source_llm, selection='synthetic', trial=_trial('skip'))
    compiled = definition.workflow(services=contrived_services(definition, compiled_requests))
    inputs = {'user_input': '  Original λ\n'}

    assert run(compiled.arun(inputs)) == run(parsed.arun(inputs))
    assert [[m.role for m in r.payload.messages] for r in compiled_requests] == [
        [m.role for m in r.payload.messages] for r in source_requests]
    assert compiled.component_passes == parsed.component_passes and compiled.timeouts == parsed.timeouts


def test_v2_registry_binds_each_declared_node_to_its_verified_callable(v2_built):
    definition = load_built(v2_built)

    registry = definition.registry()

    assert {name: function.kind for name, function in registry.items()} == {
        'produce': 'llm', 'validate_format': 'deterministic', 'guard': 'deterministic'}
    assert all(function.invoke.__module__ == 'aq_contrived.functions' for function in registry.values())
    assert Workflow(definition, registry).definition is definition


def test_v2_engine_records_compiled_plan_provenance(v2_built, monkeypatch):
    definition = load_built(v2_built)
    workflow = definition.workflow(services=contrived_services(definition, []))
    monkeypatch.setattr('api_agent.workflows.execution.recording_enabled', lambda: True)

    assert run(workflow.arun({'user_input': 'x'}))


def test_inline_prompts_run_from_compiled_resources(inline_built):
    workflow = load_built(inline_built).workflow()

    assert run(workflow.arun({})) == {'START': {}, 'inspect': {'seen': True}}


def test_compiled_definitions_reproduce_projection_and_identity(v2_built, v3_built, rich_built, inline_built):
    source_v2 = load_definition(ROOT / 'demo/contrived/flow.md.html', base_root=ROOT, pack_root=ROOT / 'demo')
    parsed_v3, catalog = source_v3()
    compiled_v2, compiled_v3 = load_built(v2_built), load_built(v3_built)

    assert projection(compiled_v2) == projection(source_v2)
    assert projection(compiled_v3) == projection(parsed_v3)
    assert declaration_identity(compiled_v2) == declaration_identity(source_v2)
    assert declaration_identity(compiled_v3) == declaration_identity(parsed_v3)
    assert (workflow_identity(compiled_v3, timeouts={}, component_passes={})
            == workflow_identity(parsed_v3, timeouts={}, component_passes={}))
    assert all(item['length'] > 0 for item in projection(compiled_v2)['resources'])
    assert WorkflowJournal(compiled_v3).projection == projection(parsed_v3)


def test_selection_binds_compiled_definitions_through_the_protocol(v2_built, v3_built):
    context = SimpleNamespace(base_root=QUALIFICATION, pack_root=QUALIFICATION, workflow_base_root=None,
                              workflow_entry=None, workflow_root='base', workflow_replacements=(),
                              workflow_pack_root=None)
    state, graph = load_built(v3_built), load_built(v2_built)

    stateful = load_workflow(context, state.load_catalog(), definition=state, reducers={'messages': add_messages})
    classic = load_workflow(SimpleNamespace(**{**vars(context), 'workflow_root': graph.entry_root}),
                            graph.registry(), definition=graph, services=contrived_services(graph, []))

    assert stateful.definition is state and classic.definition is graph
    with pytest.raises(ResourcePackError, match='does not match selected pack graph'):
        load_workflow(SimpleNamespace(**{**vars(context), 'workflow_root': 'pack'}), state.load_catalog(),
                      definition=state)
    with pytest.raises(ResourcePackError, match='does not match selected pack graph'):
        load_workflow(SimpleNamespace(**{**vars(context), 'workflow_replacements': (('x', 'y', 'base'),)}),
                      state.load_catalog(), definition=state)


def test_selection_rejects_a_definition_of_an_unknown_engine_family(v3_built):
    state = load_built(v3_built)
    names = ('origin', 'graph_id', 'definition_id', 'entry', 'entry_root', 'roots', 'replacements', 'resources',
             'layout', 'contents', 'selected_declarations', 'declaration_fragments', 'tool_usages',
             'verify_sources')
    unknown = SimpleNamespace(version=4, **{name: getattr(state, name) for name in names})
    context = SimpleNamespace(base_root=QUALIFICATION, pack_root=QUALIFICATION, workflow_base_root=None,
                              workflow_entry=None, workflow_root='base', workflow_replacements=(),
                              workflow_pack_root=None)

    with pytest.raises(ResourcePackError, match='does not match selected pack graph'):
        load_workflow(context, state.load_catalog(), definition=unknown)
