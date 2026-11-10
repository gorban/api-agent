"""Compiled plan loading keeps compatibility and provenance, not integrity."""
import importlib
import json
import sys
import asyncio
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from artifact_acceptance_support import QUALIFICATION, build_wheel, install
from artifact_unit_support import build_tool_v3, edited, load_built, v2_built, v3_built  # noqa: F401
from api_agent.api.application import Runtime
from api_agent.config.declarations import ConfigSchema
from api_agent.runtime.resource_pack import LaunchContext
from api_agent.tooling.selection import compose_tools
from api_agent.workflows import artifact
from api_agent.workflows.artifact import ArtifactRejected, CompiledDefinition, CompiledStateDefinition


def _changed(plan, **changes):
    return edited(plan, lambda value: value.update(changes))


def test_compiled_plan_retains_declared_state_and_binding_features(v2_built, v3_built):
    assert isinstance(load_built(v2_built), CompiledDefinition)
    assert isinstance(load_built(v3_built), CompiledStateDefinition)


def test_compiled_plan_retains_conditional_typed_edges(v3_built):
    def conditional(value):
        static = next(row for row in value['state']['edges']
                      if dict(row['$m']).get('Router') == '-')
        static['$m'].append(['When', 'always'])
    loaded = load_built(v3_built, edited(v3_built.plan, conditional))
    assert any(row.get('When') == 'always' for row in loaded.edges)


@pytest.mark.parametrize(
    ('fixture_name', 'key', 'value', 'message'),
    [
        ('v3_built', 'plan_version', 99, 'unsupported plan version'),
        ('v3_built', 'runtime_abi', 99, 'unsupported plan version'),
        ('v2_built', 'workflow_version', 99, 'unsupported workflow version'),
    ],
)
def test_compatibility_rejects_wrong_schema_abi_and_engine(request, fixture_name, key, value, message):
    built = request.getfixturevalue(fixture_name)
    value = json.loads(_changed(built.plan, **{key: value}))

    with pytest.raises(ArtifactRejected, match=message):
        artifact._check_versions(value)


def test_compiled_load_does_not_import_the_declaration_parser(v3_built, monkeypatch):
    monkeypatch.delitem(sys.modules, 'api_agent.workflows.definition', raising=False)
    monkeypatch.delitem(sys.modules, 'api_agent.workflows.document_format', raising=False)

    definition = load_built(v3_built)

    assert definition.origin == 'compiled'
    assert 'api_agent.workflows.definition' not in sys.modules
    assert 'api_agent.workflows.document_format' not in sys.modules


def test_identity_is_over_the_encoded_graph(v3_built):
    value = json.loads(v3_built.plan)
    value['state']['edges'][0]['$m'][1][1] = 'END'

    with pytest.raises(ArtifactRejected, match='identity does not match'):
        load_built(v3_built, artifact.canonical_json(value))


def test_provenance_exposes_plan_source_and_module_hashes(v3_built):
    provenance = artifact.compiled_provenance(load_built(v3_built))

    assert provenance['plan_digest'].startswith('sha256:')
    assert any(row['path'].endswith('flow.md.html') for row in provenance['source_hashes'])
    assert provenance['module_hashes'] == [{
        'module': 'aq_bindings.native',
        'source': 'base/src/aq_bindings/native.py',
        'sha256': provenance['module_hashes'][0]['sha256'],
        'distribution': 'aq-bindings',
    }]


def test_bound_modules_use_normal_python_imports(v3_built, monkeypatch):
    imported = []
    original = importlib.import_module

    def observed(name, package=None):
        imported.append(name)
        return original(name, package)

    monkeypatch.setattr(importlib, 'import_module', observed)
    load_built(v3_built)

    assert imported == ['aq_bindings.native']


def test_compiled_tool_executes_through_production_tool_admission(tmp_path):
    """The compiled graph uses the same OwnerTools and invocation path as production."""
    from api_agent.tooling.runtime import OwnerTools
    from api_agent.workflows.artifact_compile import compile_plan
    from langgraph.graph.message import add_messages
    from test_tool_gate import Owner

    root = tmp_path / 'graph'
    shutil.copytree(QUALIFICATION, root)
    native = root / 'src/aq_bindings/native.py'
    native.write_text(native.read_text().replace(
        'def intake(state, context):', '''async def intake(state, context):
    prepared = await context.tools['reader'].prepare({'query': 'hello'})
    context.services['grant'] = await context.tools['reader'].verify(
        prepared, relevance=lambda _: {'relevant': True, 'grounded': True})''').replace(
        'def finish(state, context):', '''async def finish(state, context):
    await context.tools['runner'].execute(context.services['grant'])'''))
    (root / 'instance.toml').write_text('id="sample/compiled"\n')
    for node, port, operations in [('intake', 'reader', 'prepare, verify'), ('finish', 'runner', 'execute')]:
        path = root / f'{node}.md.html'
        path.write_text(path.read_text() + '\n## Tools\n\n| Port | Instance | Operations |\n'
                        f'| --- | --- | --- |\n| {port} | [tool](instance.toml) | {operations} |\n')
    wheel = build_wheel(tmp_path, 'aq-bindings', {
        'aq_bindings/__init__.py': (root / 'src/aq_bindings/__init__.py').read_bytes(),
        'aq_bindings/native.py': native.read_bytes(),
    })
    plan = compile_plan(root / 'flow.md.html', base_root=root, pack_root=root, wheels=[wheel])
    site = install(wheel, tmp_path / 'site')
    for name in tuple(sys.modules):
        if name == 'aq_bindings' or name.startswith('aq_bindings.'):
            sys.modules.pop(name)
    importlib.invalidate_caches()
    sys.path.insert(0, str(site))
    try:
        definition = artifact.load_artifact(plan)
        owner = Owner('compiled')
        workflow = definition.workflow(reducers={'messages': add_messages},
                                       services={'tools': OwnerTools(definition, owner)})
        result = asyncio.run(workflow.arun({'messages': [], 'mode': 'ok', 'attempts': 0,
                                             'feedback': '', 'trail': [], 'outcome': ''},
                                            thread_id='compiled-tools'))
    finally:
        sys.path.remove(str(site))
        for name in tuple(sys.modules):
            if name == 'aq_bindings' or name.startswith('aq_bindings.'):
                sys.modules.pop(name)

    assert owner.calls == ['describe', 'validate', 'invoke'], (definition.tool_usages, result)


@pytest.mark.parametrize('origin', ['source', 'compiled'])
def test_tool_bound_graph_admits_and_executes_its_installed_factory(tmp_path, origin):
    """Source and compiled graphs share compose_tools admission and runtime acquisition."""
    from langgraph.graph.message import add_messages

    built = build_tool_v3(tmp_path)
    root = built.directory / 'tree'
    context = LaunchContext(root, root, root, 'fixture', 'fixture', root / 'config.toml', {}, {},
                            workflow_entry=root / 'flow.md.html', workflow_base_root=root,
                            workflow_pack_root=root)
    if origin == 'compiled':
        plan_path = built.directory / 'plan.json'
        plan_path.write_bytes(built.plan)
        context = replace(context, workflow_artifact_plan=plan_path,
                          workflow_artifact_graph_id='artifact_qualification')
        definition_loader = lambda _: artifact.load_artifact(plan_path.read_bytes())
    else:
        definition_loader = None
    for name in tuple(sys.modules):
        if name == 'aq_bindings' or name.startswith('aq_bindings.'):
            sys.modules.pop(name)
    importlib.invalidate_caches()
    sys.path.insert(0, str(built.site))
    try:
        tool_plan = compose_tools(context, ConfigSchema({}), environ={},
                                  definition_loader=definition_loader)
        definition = tool_plan.definitions[0]
        assert [name for name, _ in tool_plan.tools[0].admission.dependencies] == ['api_agent', 'xxhash']

        async def execute():
            runtime = Runtime('fixture', tool_plan=tool_plan, services={})
            await runtime.astart()
            try:
                services = {'tools': runtime.tools_for(definition)}
                if origin == 'source':
                    from api_agent.workflows.execution import (Workflow)
                    from api_agent.workflows.native import (load_native_bindings)
                    workflow = Workflow(definition, catalog=load_native_bindings(definition),
                                                reducers={'messages': add_messages}, services=services)
                else:
                    workflow = definition.workflow(reducers={'messages': add_messages}, services=services)
                return await workflow.arun({'messages': [], 'mode': 'ok', 'attempts': 0,
                                            'feedback': '', 'trail': [], 'outcome': ''},
                                           thread_id=origin + '-tools')
            finally:
                await runtime.aclose()

        result = asyncio.run(execute())
    finally:
        sys.path.remove(str(built.site))
        for name in tuple(sys.modules):
            if name == 'aq_bindings' or name.startswith('aq_bindings.'):
                sys.modules.pop(name)

    assert result['outcome'] == 'hello'


def test_local_selection_ignores_artifact_graph_identity_and_uses_source(v3_built, tmp_path, monkeypatch):
    from api_agent.runtime import resource_pack
    plan_path = tmp_path / 'plan.json'
    plan_path.write_bytes(v3_built.plan)
    root = QUALIFICATION
    context = LaunchContext(root, root, root, 'fixture', 'fixture', root / 'config.toml', {}, {},
                            workflow_entry=root / 'flow.md.html', workflow_base_root=root,
                            workflow_artifact_plan=plan_path, workflow_artifact_graph_id='other_graph')
    monkeypatch.syspath_prepend(str(v3_built.site))

    selected = resource_pack.load_selected_definition(context)

    assert selected.origin == 'source'
    assert selected.graph_id != 'other_graph'


def test_compiled_tool_sites_skip_modules_without_a_source_file(tmp_path):
    from types import ModuleType, SimpleNamespace
    from api_agent.tooling.selection import _artifact_sites
    located = ModuleType('aq_located.native')
    located.__file__ = str(tmp_path / 'site/aq_located/native.py')
    namespace = ModuleType('aq_namespace')
    compiled = SimpleNamespace(origin='compiled', modules={'a': located, 'b': namespace})
    source = SimpleNamespace(origin='source', modules={'c': located})

    sites = _artifact_sites([source, compiled])

    assert sites == ((tmp_path / 'site').resolve(),)


def test_local_selection_reloads_source_after_checked_in_artifact_changes(v3_built, tmp_path, monkeypatch):
    from api_agent.runtime import resource_pack
    plan_path = tmp_path / 'plan.json'
    plan_path.write_bytes(v3_built.plan)
    root = QUALIFICATION
    graph_id = json.loads(v3_built.plan)['graph_id']
    context = LaunchContext(root, root, root, 'fixture', 'fixture', root / 'config.toml', {}, {},
                            workflow_entry=root / 'flow.md.html', workflow_base_root=root,
                            workflow_artifact_plan=plan_path, workflow_artifact_graph_id=graph_id)
    monkeypatch.syspath_prepend(str(v3_built.site))
    first = resource_pack.load_selected_definition(context)
    plan_path.write_bytes(b'replaced after first selection')

    second = resource_pack.load_selected_definition(context)

    assert second.origin == first.origin == 'source'
    assert second is not first
