"""The parser-free model, its protocols and the decoupled import graph."""
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType, SimpleNamespace

import pytest

from artifact_acceptance_support import QUALIFICATION, ROOT

PARSER = ('api_agent.workflows.definition', 'api_agent.workflows.document_format',
          'api_agent.workflows.layout', 'markdown_it')


def imported_after(statement):
    code = ('import sys\n' + statement + '\n'
            'print(sorted(name for name in sys.modules if name in %r or name.startswith("markdown_it.")))' % (PARSER,))
    completed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True,
                               env={'PYTHONPATH': str(ROOT / 'src'), 'PATH': ''})
    return eval(completed.stdout)


@pytest.mark.parametrize('module', ['api_agent.workflows', 'api_agent.workflows.model', 'api_agent.workflows.execution',
                                    'api_agent.workflows.stateful', 'api_agent.workflows.journal',
                                    'api_agent.workflows.identity', 'api_agent.workflows.contrived'])
def test_engines_journal_and_loader_import_without_the_parser(module):
    assert imported_after(f'import {module}') == []


def test_package_attributes_load_their_modules_lazily():
    assert imported_after('import api_agent.workflows as w; w.Workflow; w.LayoutError') == []
    assert 'markdown_it' in imported_after('import api_agent.workflows as w; w.load_layout')


def test_unknown_package_attribute_is_rejected():
    import api_agent.workflows as workflows

    with pytest.raises(AttributeError, match='no attribute'):
        workflows.not_a_symbol


def test_layout_and_definition_modules_reexport_the_model_names():
    from api_agent.workflows import definition, layout, model

    assert layout.Layout is model.Layout and layout.LayoutError is model.LayoutError
    assert definition.Resource is model.Resource and definition.ToolUsage is model.ToolUsage
    assert definition.Replacement is model.Replacement and definition._freeze is model.freeze


def test_freeze_makes_nested_values_read_only():
    from api_agent.workflows.model import freeze

    frozen = freeze({'a': [1, {'b': [2]}]})

    assert isinstance(frozen, MappingProxyType) and frozen['a'][1]['b'] == (2,)


def test_resource_reports_declared_size_when_bytes_are_withheld():
    from api_agent.workflows.model import Resource

    assert Resource('declaration', 'base/x', None, 'a' * 64, 7).size == 7
    assert Resource('prompt', 'base/y', b'abc', 'b' * 64).size is None


def test_compiled_plans_cannot_claim_source_provenance():
    from api_agent.workflows.model import LayoutError, require_source_provenance

    require_source_provenance(SimpleNamespace(origin='source'))
    require_source_provenance(SimpleNamespace())
    with pytest.raises(LayoutError, match='unavailable for a compiled plan'):
        require_source_provenance(SimpleNamespace(origin='compiled'))


def test_parsed_definitions_satisfy_their_protocols():
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.model import Definition, GraphDefinition, StateDefinition
    from api_agent.workflows.stateful import load_stateful_definition

    v2 = load_definition(ROOT / 'demo/contrived/flow.md.html', base_root=ROOT, pack_root=ROOT / 'demo')
    v3 = load_stateful_definition(QUALIFICATION / 'flow.md.html', base_root=QUALIFICATION,
                                  pack_root=QUALIFICATION)

    assert isinstance(v2, GraphDefinition) and not isinstance(v2, StateDefinition)
    assert isinstance(v3, StateDefinition) and not isinstance(v3, GraphDefinition)
    assert [(item.version, item.origin) for item in (v2, v3)] == [(2, 'source'), (3, 'source')]
    assert all(isinstance(item, Definition) for item in (v2, v3))
    assert not isinstance(SimpleNamespace(), Definition)


def test_parsed_v3_definition_loads_its_catalog_through_the_protocol():
    from api_agent.workflows.stateful import load_stateful_definition

    definition = load_stateful_definition(QUALIFICATION / 'flow.md.html', base_root=QUALIFICATION,
                                          pack_root=QUALIFICATION)

    catalog = definition.load_catalog()

    assert catalog.definition_id == definition.definition_id
    assert catalog.admitted_modules == {'<stateful_catalog>'}


def test_parsed_v2_definition_names_why_a_callable_is_not_its_binding():
    from api_agent.workflows.definition import load_definition, load_native_catalog

    definition = load_definition(ROOT / 'demo/contrived/flow.md.html', base_root=ROOT, pack_root=ROOT / 'demo')
    step = next(step for step in definition.layout.steps if step.id == 'first_draft')
    catalog = load_native_catalog(definition, step.source)
    clone = type(catalog['guard'])(catalog['guard'].__code__, {})

    def foreign(inputs, context):
        return None

    assert definition.native_rejection(catalog['produce'], step) is None
    assert 'declared source link' in definition.native_rejection(foreign, step)
    assert 'admitted definition' in definition.native_rejection(clone, step)


def test_load_workflow_binds_any_protocol_definition_and_rejects_others(tmp_path):
    from api_agent.runtime.resource_pack import ResourcePackError, load_workflow
    from api_agent.workflows.stateful import load_stateful_definition

    definition = load_stateful_definition(QUALIFICATION / 'flow.md.html', base_root=QUALIFICATION,
                                          pack_root=QUALIFICATION)
    context = SimpleNamespace(base_root=QUALIFICATION, pack_root=QUALIFICATION, workflow_base_root=None,
                              workflow_entry=QUALIFICATION / 'flow.md.html', workflow_root='base',
                              workflow_replacements=(), workflow_pack_root=None)
    catalog = definition.load_catalog()

    from langgraph.graph.message import add_messages

    workflow = load_workflow(context, catalog, definition=definition, reducers={'messages': add_messages})
    with pytest.raises(ResourcePackError, match='does not match selected pack graph'):
        load_workflow(context, catalog, definition=SimpleNamespace(version=3))
    with pytest.raises(ResourcePackError, match='declared routers'):
        load_workflow(context, catalog, {}, definition=definition)

    assert workflow.definition is definition


def test_logical_resolver_maps_admitted_paths_and_inline_prompts_to_resource_paths():
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.identity import logical_resolver

    definition = load_definition(ROOT / 'demo/contrived/flow.md.html', base_root=ROOT, pack_root=ROOT / 'demo')
    resolve = logical_resolver(definition)
    by_logical = {item.logical_path: item for item in definition.resources}

    for path, data in definition.contents.items():
        assert by_logical[resolve(path)].content_bytes == data
    with pytest.raises(ValueError, match='Missing or ambiguous'):
        resolve(Path('/elsewhere/file'))


def contrived_registry(replace=lambda catalog: {}):
    from api_agent.workflows.definition import load_definition, load_native_catalog
    from api_agent.workflows.execution import NodeFunction

    definition = load_definition(ROOT / 'demo/contrived/flow.md.html', base_root=ROOT, pack_root=ROOT / 'demo')
    source = next(step.source for step in definition.layout.steps if step.id == 'first_draft')
    catalog = load_native_catalog(definition, source)
    functions = {'produce': catalog['produce'], 'validate_format': catalog['validate_format'],
                 'guard': catalog['guard'], **replace(catalog)}
    kinds = {'produce': 'llm'}
    return definition, {name: NodeFunction(function, kinds.get(name, 'deterministic'))
                        for name, function in functions.items()}


def test_workflow_accepts_only_loader_owned_bindings_of_its_definition():
    from api_agent.workflows.execution import Workflow
    from api_agent.workflows.model import LayoutError

    def foreign(inputs, context):
        return {}

    definition, registry = contrived_registry()
    _, impostor = contrived_registry(lambda catalog: {'guard': type(foreign)(catalog['guard'].__code__, {})})
    _, wrong_file = contrived_registry(lambda catalog: {'guard': foreign})

    assert Workflow(definition, registry).definition is definition
    with pytest.raises(LayoutError, match='declared source link'):
        Workflow(definition, wrong_file)
    with pytest.raises(LayoutError, match='native callable does not belong to the admitted definition'):
        Workflow(definition, impostor)


def test_stateful_workflow_requires_bindings_from_admitted_catalog_modules():
    from dataclasses import replace

    from api_agent.workflows.model import LayoutError
    from api_agent.workflows.stateful import StatefulWorkflow, load_stateful_definition
    from langgraph.graph.message import add_messages

    definition = load_stateful_definition(QUALIFICATION / 'flow.md.html', base_root=QUALIFICATION,
                                          pack_root=QUALIFICATION)
    catalog = definition.load_catalog()

    StatefulWorkflow(definition, catalog=catalog, reducers={'messages': add_messages})
    with pytest.raises(LayoutError, match='missing admitted native binding'):
        StatefulWorkflow(definition, catalog=replace(catalog, admitted_modules=frozenset({'elsewhere'})),
                         reducers={'messages': add_messages})
