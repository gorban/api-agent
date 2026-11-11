"""Compiled binding plans enforce the same bounded-loop rules as source declarations."""
from dataclasses import replace
import json
from types import MappingProxyType

import pytest

from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built  # noqa: F401
from loop_support import NATIVE, OUTPUTS, edges, graph, limits, load
from test_workflow_loops import retry

from api_agent.workflows import LayoutError, NodeFunction, Workflow
from api_agent.workflows.artifact import ArtifactRejected, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.definition import load_native_catalog
from api_agent.workflows.model import Layout

PLAIN = limits('PLAN_PASSES') + edges(('verify', 'accept', 'verify.accepted'), ('verify', 'failed', 'otherwise'),
                                      ('accept', 'END', 'always'))


def build(tmp_path, *, sections=None):
    root = tmp_path / 'tree'
    root.mkdir()
    entry = retry(root) if sections is None else graph(
        root, [('node', 'draft'), ('node', 'verify'), ('node', 'accept'), ('node', 'failed')], sections=sections)
    wheel = build_wheel(tmp_path, 'aq-loops', {'aq_loops/__init__.py': b'', 'aq_loops/native.py': NATIVE.encode()})
    plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path)


def rows(value, name):
    return next(table for key, table in value['layout']['tables']['$m'] if key == name)


def set_when(row, text):
    for pair in row['$m']:
        if pair[0] == 'When':
            pair[1] = text


def retarget(when):
    """A plan whose draft retry edge carries ``when`` instead of its bounded condition."""
    def change(value):
        row = next(row for row in rows(value, 'Edges') if ['To', 'draft'] in row['$m'])
        set_when(row, when)
    return change


def test_a_bounded_loop_plan_loads(tmp_path):
    definition = load_built(build(tmp_path))

    assert [row['To'] for row in definition.layout.tables['Edges']].count('draft') == 1


@pytest.mark.parametrize('when', ['not verify.accepted', 'is_retry', 'always', 'draft.attempt < 3',
                                  'not verify.accepted or draft.attempt < PLAN_PASSES'])
def test_a_plan_with_an_unbounded_backward_edge_is_rejected(tmp_path, when):
    built = build(tmp_path)

    with pytest.raises(ArtifactRejected, match='backward edge to draft requires a bounded When'):
        load_built(built, edited(built.plan, retarget(when)))


def forged_workflow(tmp_path, when, **extra):
    """A source definition whose draft retry edge was rewritten after admission."""
    entry = retry(tmp_path)
    definition = load(entry)
    native = load_native_catalog(definition, entry.parent / 'native.py')
    registry = {name: NodeFunction(native[name]) for name in OUTPUTS
                if name in native and any(row['ID'] == name for row in definition.layout.tables['Functions'])}
    tables = dict(definition.layout.tables)
    tables['Edges'] = tuple(MappingProxyType({**row, 'When': when, **extra}) if row['To'] == 'draft' else row
                            for row in tables['Edges'])
    forged = replace(definition, layout=Layout(definition.layout.path, definition.layout.steps,
                                               MappingProxyType(tables), definition.layout.files))
    return Workflow(forged, registry, conditions={'is_retry': lambda values: not values['verify']['accepted']})


@pytest.mark.parametrize('when', ['not verify.accepted', 'is_retry'])
def test_an_unbounded_backward_edge_cannot_start_a_workflow(tmp_path, when):
    with pytest.raises(LayoutError, match='backward edge to draft requires a bounded When'):
        forged_workflow(tmp_path, when)


def test_the_unbounded_edge_error_names_the_document_that_declares_it(tmp_path):
    owner = tmp_path / 'group.md.html'

    with pytest.raises(LayoutError) as error:
        forged_workflow(tmp_path, 'is_retry', _owner=owner)

    assert str(error.value).startswith(f'{owner}:')


def test_a_null_application_binding_is_not_a_compiled_plan(tmp_path):
    built = build(tmp_path)
    plan = edited(built.plan, lambda value: value.update(application_binding=None))

    with pytest.raises(ArtifactRejected, match='invalid application binding'):
        load_built(built, plan)


def test_an_empty_limits_table_is_not_a_compiled_plan(tmp_path):
    built = build(tmp_path, sections=PLAIN)
    assert json.loads(built.plan)['layout']['tables']['$m']
    plan = edited(built.plan, lambda value: rows(value, 'Limits').clear())

    with pytest.raises(ArtifactRejected, match='invalid limit rows'):
        load_built(built, plan)

