"""Shared acceptance harness for compiled workflow artifacts.

Every candidate implements ``tests/unit/artifact_acceptance_adapter.py``. The
adapter must stay a thin mapping onto public production APIs:

``compile_plan(entry, *, base_root, pack_root, wheels, entry_root=None) -> bytes``
    Build time. May parse declarations. ``wheels`` are the hash-identified
    source wheels that supply every admitted native module.
``with_unsupported_version(plan) -> bytes``
    A plan that differs only by declaring an unsupported schema/runtime version.
``load(plan) -> loaded``
    Production. Bound modules are available through ordinary Python imports.
    Must not import the declaration parser, execute or read declaration source
    files, or unpickle. Imports of compile-time code must be lazy.
``ArtifactRejected``
    The exception type ``load`` raises for every integrity/compatibility failure.

``loaded`` exposes ``definition_id``, ``graph_id``, ``workflow(**options)``
(returning the real engine for the plan's declaration version), ``journal()``,
``resource(logical_path) -> bytes``, ``topology()``, ``tool_usages()`` and
``resources()``. Compiled runs execute in a fresh interpreter; see
``artifact_acceptance_support``.
"""
import importlib
import json
from pathlib import Path

import pytest

from artifact_acceptance_support import QUALIFICATION, ROOT, build_wheel, install, run, source_files

CONTRIVED = ROOT / 'demo' / 'contrived' / 'flow.md.html'
SIMPLE = ROOT / 'demo' / 'simple' / 'flow.md.html'
V3_SCENARIOS = ('complete', 'failure', 'stream', 'cancel', 'fresh')
V2_SCENARIOS = ('repair', 'guard', 'exhaust')


@pytest.fixture(scope='module')
def adapter():
    return importlib.import_module('artifact_acceptance_adapter')


@pytest.fixture(scope='module')
def wheels(tmp_path_factory):
    directory = tmp_path_factory.mktemp('wheels')
    native = QUALIFICATION / 'src' / 'aq_bindings'
    return {
        'v3': build_wheel(directory, 'aq-bindings', {
            'aq_bindings/__init__.py': (native / '__init__.py').read_bytes(),
            'aq_bindings/native.py': (native / 'native.py').read_bytes()}),
        'contrived': build_wheel(directory, 'aq-contrived', {
            'aq_contrived/__init__.py': b'',
            'aq_contrived/functions.py': (ROOT / 'demo/contrived/functions.py').read_bytes()}),
        'simple': build_wheel(directory, 'aq-simple', {
            'aq_simple/__init__.py': b'',
            'aq_simple/functions.py': (ROOT / 'demo/simple/functions.py').read_bytes()}),
    }


def _source_definition(corpus, entry, base_root, pack_root, entry_root=None):
    if corpus == 'v3':
        from api_agent.workflows.stateful import load_stateful_definition
        return load_stateful_definition(entry, base_root=base_root, pack_root=pack_root, entry_root=entry_root)
    from api_agent.workflows.definition import load_definition
    return load_definition(entry, base_root=base_root, pack_root=pack_root, entry_root=entry_root)


def _source_spec(directory, *, corpus, entry, base_root, pack_root, entry_root=None):
    definition = _source_definition(corpus, entry, base_root, pack_root, entry_root)
    spec = {'corpus': corpus, 'entry': str(entry), 'base_root': str(base_root), 'pack_root': str(pack_root),
            'entry_root': entry_root, 'cwd': str(directory), 'forbidden_files': source_files(definition)}
    return definition, spec


def _artifact(adapter, tmp_path_factory, wheels, *, corpus, entry, base_root, pack_root, wheel,
              entry_root=None):
    directory = tmp_path_factory.mktemp(entry.parent.name)
    plan = adapter.compile_plan(entry, base_root=base_root, pack_root=pack_root, wheels=[wheels[wheel]],
                                entry_root=entry_root)
    (directory / 'plan.json').write_bytes(plan)
    definition, spec = _source_spec(directory, corpus=corpus, entry=entry, base_root=base_root,
                                    pack_root=pack_root, entry_root=entry_root)
    spec.update(plan=str(directory / 'plan.json'), site=str(install(wheels[wheel], directory / 'site')))
    return plan, definition, spec


@pytest.fixture(scope='module')
def v3_source(tmp_path_factory):
    _, spec = _source_spec(tmp_path_factory.mktemp('v3-source'), corpus='v3',
                           entry=QUALIFICATION / 'flow.md.html', base_root=QUALIFICATION,
                           pack_root=QUALIFICATION)
    return {**spec, 'mode': 'source', 'site': None}


@pytest.fixture(scope='module')
def v3(adapter, tmp_path_factory, wheels):
    return _artifact(adapter, tmp_path_factory, wheels, corpus='v3', entry=QUALIFICATION / 'flow.md.html',
                     base_root=QUALIFICATION, pack_root=QUALIFICATION, wheel='v3')


@pytest.fixture(scope='module')
def contrived(adapter, tmp_path_factory, wheels):
    plan, definition, spec = _artifact(adapter, tmp_path_factory, wheels, corpus='v2', entry=CONTRIVED,
                                       base_root=ROOT, pack_root=ROOT / 'demo', wheel='contrived')
    draft = next(row for row in definition.layout.tables['Functions'] if row['ID'] == 'first_draft')
    schema = definition.contents[draft['Output']]
    spec['output_schema_path'] = next(item.logical_path for item in definition.resources
                                      if item.content_bytes == schema)
    return plan, definition, spec


def _both(spec, scenario):
    source = run({**spec, 'mode': 'source', 'scenario': scenario, 'site': None})
    compiled = run({**spec, 'mode': 'compiled', 'scenario': scenario})
    return source, compiled


def _assert_production_path(compiled, spec):
    assert compiled['forbidden_modules'] == []
    assert compiled['audit'] == []
    assert compiled['source_modules'] == []
    assert compiled['bindings'], 'no native binding was imported from the installed site'
    site = str(Path(spec['site']).resolve())
    assert {loader for _, loader, _ in compiled['bindings']} == {'SourceFileLoader'}
    assert all(path.endswith('.py') and path.startswith(site) for _, _, path in compiled['bindings'])


def test_plan_is_canonical_and_deterministic(adapter, v3, wheels):
    plan, _, spec = v3

    again = adapter.compile_plan(QUALIFICATION / 'flow.md.html', base_root=QUALIFICATION,
                                 pack_root=QUALIFICATION, wheels=[wheels['v3']])

    assert again == plan
    value = json.loads(plan)
    assert plan in {json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=flag).encode()
                    for flag in (False, True)}


@pytest.mark.parametrize('artifact', ['v3', 'contrived'])
def test_plan_carries_no_declaration_or_native_source(request, artifact):
    plan, definition, _ = request.getfixturevalue(artifact)
    natives = {path for step in definition.layout.steps if step.kind == 'node' for path in [step.source]}

    assert b'# Workflow v' not in plan and b'## Native' not in plan and b'| Registry |' not in plan
    for native in natives:
        for line in definition.contents[native].splitlines():
            if line.strip().startswith((b'def ', b'async def ')):
                assert line.strip() not in plan


@pytest.fixture(scope='module')
def contrived_source(tmp_path_factory):
    _, spec = _source_spec(tmp_path_factory.mktemp('contrived-source'), corpus='v2', entry=CONTRIVED,
                           base_root=ROOT, pack_root=ROOT / 'demo')
    return {**spec, 'mode': 'source', 'site': None}


@pytest.mark.parametrize('corpus,scenario', [*(('v3_source', item) for item in V3_SCENARIOS),
                                             *(('contrived_source', item) for item in V2_SCENARIOS)])
def test_source_traces_are_reproducible(request, corpus, scenario):
    spec = request.getfixturevalue(corpus)

    first = run({**spec, 'scenario': scenario})
    second = run({**spec, 'scenario': scenario})

    assert first['trace'] == second['trace']


def test_audit_trap_detects_source_execution(v3_source):
    source = run({**v3_source, 'scenario': 'complete'})

    assert {'exec', 'compile', 'open'} <= {kind for kind, _ in source['audit']}
    assert 'markdown_it' in source['forbidden_modules']


@pytest.mark.parametrize('scenario', V3_SCENARIOS)
def test_v3_compiled_execution_matches_source(v3, scenario):
    _, definition, spec = v3

    source, compiled = _both(spec, scenario)

    assert compiled['definition_id'] == source['definition_id'] == definition.definition_id
    assert compiled['graph_id'] == source['graph_id']
    assert compiled['trace'] == source['trace']
    _assert_production_path(compiled, spec)


@pytest.mark.parametrize('scenario', V2_SCENARIOS)
def test_v2_contrived_compiled_execution_matches_source(contrived, scenario):
    _, definition, spec = contrived

    source, compiled = _both(spec, scenario)

    assert compiled['definition_id'] == source['definition_id'] == definition.definition_id
    assert compiled['trace'] == source['trace']
    _assert_production_path(compiled, spec)


@pytest.mark.parametrize('entry,tools', [(SIMPLE, 4), (ROOT / 'flow.md.html', 0)], ids=['demo_simple', 'root'])
def test_flow_identity_survives_compilation(adapter, tmp_path_factory, wheels, entry, tools):
    _, definition, spec = _artifact(adapter, tmp_path_factory, wheels, corpus='v2', entry=entry,
                                    base_root=ROOT, pack_root=ROOT / 'demo', wheel='simple', entry_root='base')

    source, compiled = _both(spec, 'identity')

    assert len(definition.tool_usages) == tools
    assert compiled['trace'] == source['trace']
    _assert_production_path(compiled, spec)


def test_rejects_unsupported_version(adapter, v3, tmp_path):
    plan, _, spec = v3
    unsupported = adapter.with_unsupported_version(plan)
    (tmp_path / 'plan.json').write_bytes(unsupported)

    result = run({**spec, 'mode': 'compiled', 'scenario': 'load', 'plan': str(tmp_path / 'plan.json')})

    assert unsupported != plan
    assert result.get('is_artifact_rejected') is True, result
