"""New compiled metadata is admitted before any bound module can execute."""
import importlib

import pytest
from test_stateful_application_admission import application_graph
from test_workflow_stateful import fixture_graph

from api_agent.workflows import artifact
from artifact_unit_support import edited, load_built, v3_built


@pytest.mark.parametrize('binding', [
    7, [], ['factory'], [7, {'$p': 'base/src/aq_bindings/native.py'}],
    ['not-a-registry', {'$p': 'base/src/aq_bindings/native.py'}],
    ['factory', 'base/src/aq_bindings/native.py'],
    ['factory', {'$p': 'base/flow.md.html'}],
    ['factory', {'$p': 'base/unknown.py'}],
])
def test_invalid_application_binding_is_rejected_before_import(v3_built, monkeypatch, binding):
    plan = edited(v3_built.plan, lambda value: value['state'].update(application_binding=binding))
    imported = []
    original = importlib.import_module
    def observed(name, *args, **kwargs):
        imported.append(name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(importlib, 'import_module', observed)

    with pytest.raises(artifact.ArtifactRejected):
        load_built(v3_built, plan)

    assert imported == []


@pytest.mark.parametrize('imports,entry', [
    (7, None), ([[]], None), ([['only']], None),
    ([[{'$p': 'base/flow.md.html'}, 'graph']], None),
    ([[{'$p': 'base/missing.md.html'}, 'graph']], {'$p': 'base/missing.md.html'}),
    ([[{'$p': 'base/flow.md.html'}, 7]], {'$p': 'base/flow.md.html'}),
    ([[{'$p': 'base/flow.md.html'}, 'Bad-Graph']], {'$p': 'base/flow.md.html'}),
    ([], {'$p': 'base/flow.md.html'}),
    ([[{'$p': 'base/missing.md.html'}, 'graph']] * 65, None),
])
def test_invalid_import_metadata_is_rejected_before_import(v3_built, monkeypatch, imports, entry):
    plan = edited(v3_built.plan, lambda value: value.update(imports=imports, declaration_entry=entry))
    def forbidden(*args, **kwargs):
        pytest.fail('Malformed metadata reached a native import')
    monkeypatch.setattr(importlib, 'import_module', forbidden)

    with pytest.raises(artifact.ArtifactRejected, match='import metadata'):
        load_built(v3_built, plan)


def test_compiled_import_keeps_the_selected_declaration_chain(v3_built, tmp_path):
    from artifact_acceptance_support import QUALIFICATION
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.artifact_compile import compile_definition
    entry = tmp_path / 'flow.md.html'
    entry.write_text('# Workflow\n\n## Graph ID\n\n| ID |\n| --- |\n| product |\n\n'
                     '## Import\n\n| Root | Path |\n| --- | --- |\n| base | flow.md.html |\n')
    definition = load_definition(entry, base_root=QUALIFICATION, pack_root=tmp_path, entry_root='pack')

    compiled = load_built(v3_built, compile_definition(definition, [v3_built.wheel]))

    assert compiled.definition_id == definition.definition_id
    assert compiled.imports == ((artifact.ARTIFACT_ROOT / 'base/flow.md.html', definition.imports[0][1]),)
    assert compiled.declaration_entry == compiled.imports[0][0]


def test_compiled_application_factory_is_in_the_runtime_catalog(application_graph, tmp_path, monkeypatch):
    import sys
    from artifact_acceptance_support import build_wheel, install
    from api_agent.runtime.local_tools import selected_application
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.artifact_compile import compile_definition
    root, entry, source = application_graph
    definition = load_definition(entry, base_root=root, pack_root=root)
    wheel = build_wheel(tmp_path, 'application-bindings', {
        'native.py': (root / 'native.py').read_bytes(), 'application.py': source.read_bytes()})
    site = install(wheel, tmp_path / 'site')
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delitem(sys.modules, 'native', raising=False)
    monkeypatch.delitem(sys.modules, 'application', raising=False)

    compiled = artifact.load_artifact(compile_definition(definition, [wheel]))
    catalog = compiled.load_catalog()
    name, path = compiled.application_binding

    assert callable(catalog.bindings[path, name])
    assert selected_application(compiled).definition is compiled
