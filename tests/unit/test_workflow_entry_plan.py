"""Compiled plans and diagrams carry START entry rows and component results like their source."""
import asyncio
from html import unescape

import pytest

from api_agent.workflows.artifact import ArtifactRejected, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.identity import declaration_identity
from api_agent.workflows.viewer_projection import project_view
from api_agent.workflows.viewer_render import _projection_source
from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built  # noqa: F401

import scripted_support as scripted
import test_workflow_component_result as result
import test_workflow_entry as entry


def build(directory, source, nodes):
    root = directory / 'tree'
    root.mkdir()
    flow = source(root)
    wheel = build_wheel(directory, 'aq-entry', {'aq_entry/__init__.py': b'',
                                                'aq_entry/native.py': scripted.native_source(nodes).encode()})
    plan = compile_plan(flow, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, directory / 'site'), wheel, directory), flow


@pytest.fixture(scope='module')
def entered(tmp_path_factory):
    return build(tmp_path_factory.mktemp('entry'), entry.graph, entry.NODES)


@pytest.fixture(scope='module')
def resulted(tmp_path_factory):
    return build(tmp_path_factory.mktemp('result'), result.graph, result.NODES)


def run(compiled, script):
    native = compiled.modules['aq_entry.native']
    native.SCRIPT.update(script)
    asyncio.run(compiled.workflow().arun({}))
    return native.CALLS


def rows(value):
    return dict(value['layout']['tables']['$m'])['Edges']


def set_cell(row, key, new):
    for pair in row['$m']:
        if pair[0] == key:
            pair[1] = new
            return
    row['$m'].append([key, new])


def entry_row(value, target):
    return next(row for row in rows(value) if dict(row['$m'])['From'] == 'START' and dict(row['$m'])['To'] == target)


@pytest.mark.parametrize(('mode', 'ran'), [('b', ['b', 'c']), ('c', ['c']), ('other', ['a', 'b', 'c'])])
def test_a_compiled_plan_enters_a_component_like_its_source(entered, mode, ran):
    plan, source = entered

    compiled = load_built(plan)

    assert [row['To'] for row in compiled.layout.tables['Edges'] if row['From'] == 'START'] == ['b', 'c', 'a']
    assert declaration_identity(compiled) == declaration_identity(scripted.load(source))
    assert run(compiled, {'pre': [{'mode': mode}]}) == ['pre', *ran, 'post']


def test_a_compiled_plan_resolves_component_results_like_its_source(resulted):
    plan, source = resulted

    compiled = load_built(plan)

    assert declaration_identity(compiled) == declaration_identity(scripted.load(source))
    assert run(compiled, {'check': [{'verdict': 'bad'}], 'fix': [{'verdict': 'ok'}]}) == ['check', 'fix', 'done']


@pytest.mark.parametrize(('change', 'message'), [
    (lambda value: set_cell(entry_row(value, 'b'), 'To', 'END'), 'START must name a direct child'),
    (lambda value: set_cell(entry_row(value, 'b'), 'To', 'post'), 'START must name a direct child'),
    (lambda value: set_cell(entry_row(value, 'b'), 'To', 'nowhere'), 'START must name a direct child'),
    (lambda value: set_cell(entry_row(value, 'b'), '_owner_id', 'pre'), 'START must name a direct child'),
    (lambda value: set_cell(entry_row(value, 'b'), '_owner_id', 3), 'START must name a direct child'),
    (lambda value: entry_row(value, 'b')['$m'].remove(['_owner_id', 'group']), 'START must name a direct child'),
])
def test_plan_decoding_rejects_invalid_entry_rows(entered, change, message):
    plan, _ = entered

    with pytest.raises(ArtifactRejected, match=message):
        load_built(plan, edited(plan.plan, change))


def test_the_projection_and_diagram_draw_the_entry_rows_inside_the_component(tmp_path):
    projection = project_view(scripted.load(entry.graph(tmp_path)))

    edges = [(edge.owner, edge.target, edge.condition, edge.implicit)
             for edge in projection.edges if edge.source == 'START']
    document = next(item for item in projection.documents if item.stable_id == 'group')
    text = unescape(_projection_source(projection, document)[0].decode())

    assert edges == [('GRAPH', 'pre', 'always', True), ('group', 'b', 'pre.mode == "b"', False),
                     ('group', 'c', 'pre.mode == "c"', False), ('group', 'a', 'otherwise', False)]
    assert 'circle "START" as start' in text
    assert [line.rsplit(' : ', 1)[1] for line in text.splitlines() if line.startswith('start -->')] == [
        'pre.mode == "b"', 'pre.mode == "c"', 'otherwise']


def test_a_graph_level_entry_row_is_drawn_in_the_root_diagram(tmp_path):
    nodes = {**entry.NODES, 'a': ({'mode': ('START', 'mode', '"none"', entry.STR)}, {'tag': entry.STR})}
    flow = entry.graph(tmp_path, '', extra='', nodes=nodes, flow=['a', 'b', 'c'], sections=entry.edges(
        ('START', 'c', 'START.mode == "c"'), ('START', 'a', 'otherwise')))

    projection = project_view(scripted.load(flow))
    document = next(item for item in projection.documents if item.stable_id == 'scripted')
    text = unescape(_projection_source(projection, document)[0].decode())

    assert [edge.condition for edge in projection.edges if edge.owner == 'GRAPH' and edge.source == 'START'] == [
        'START.mode == "c"', 'otherwise']
    assert any(line.endswith(': START.mode == "c"') for line in text.splitlines())
