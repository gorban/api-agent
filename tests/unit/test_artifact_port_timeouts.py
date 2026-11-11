"""Compiled plans carry `<node>.<port>` Timeouts rows and check them as strictly as source does."""
import json

import pytest

from api_agent.workflows.artifact import ArtifactRejected, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.configuration import declared_timeouts
from api_agent.workflows.identity import workflow_identity
from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built  # noqa: F401
from loop_support import CONFIG, NATIVE, graph, load, node, table, write

TOOLS = table('Tools', ('Port', 'Instance', 'Operations'),
              [('search', '[tool](tool.toml)', 'describe, prepare, verify, execute')])
PORT_ROW = ('draft.search', '[config](config.toml)', 'TOOL_SECONDS')


@pytest.fixture
def ported(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    entry = graph(root, [('node', 'draft')], config=CONFIG + '\n[fields.TOOL_SECONDS]\ntype = "float"\ndefault = 0.5\n',
                  sections=table('Timeouts', ('Owner', 'Source', 'Key'), [PORT_ROW]))
    node(root, 'draft', extra=TOOLS)
    write(root, 'tool.toml', 'id = "sample/primary"\n')
    wheel = build_wheel(tmp_path, 'aq-loops', {'aq_loops/__init__.py': b'', 'aq_loops/native.py': NATIVE.encode()})
    plan = compile_plan(entry, base_root=root, pack_root=root, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path), entry


def test_port_row_round_trips_and_matches_the_source_definition(ported):
    built, entry = ported
    source, compiled = load(entry), load_built(built)

    assert [row['Owner'] for row in compiled.layout.tables['Timeouts']] == ['draft.search']
    assert declared_timeouts(compiled) == declared_timeouts(source) == {'draft.search': 0.5}
    values = {'draft.search': 0.5}
    assert (workflow_identity(compiled, timeouts=values, component_passes={})
            == workflow_identity(source, timeouts=values, component_passes={}))


def rows_of(value):
    entries = value['layout']['tables']['$m']
    position = next(i for i, (name, _) in enumerate(entries) if name == 'Timeouts')
    return entries, position, [dict(row['$m']) for row in entries[position][1]]


@pytest.mark.parametrize('change', [
    lambda rows: rows[0].update(Owner='ghost.search'),
    lambda rows: rows[0].update(Owner='draft.absent'),
    lambda rows: rows[0].update(Owner='draft.search.more'),
    lambda rows: rows[0].update(Owner='nobody'),
    lambda rows: rows.append(dict(rows[0])),
    lambda rows: rows[0].update(Key='bad-key'),
    lambda rows: rows[0].pop('Key'),
    lambda rows: rows[0].update(extra=1),
    lambda rows: rows[0].update(Source={'$p': 'base/native.py'}),
    lambda rows: rows[0].update(Source='base/config.toml'),
    lambda rows: rows[0].update(line=0),
    lambda rows: rows[0].update(_owner={'$p': 'base/draft.md.html'}),
    lambda rows: rows[0].update(_owner={'$p': 'base/draft.md.html'}, _owner_id=7),
    lambda rows: rows.__setitem__(0, 1),
])
def test_malformed_timeout_rows_are_rejected(ported, change):
    built, _ = ported

    def mutate(value):
        entries, position, rows = rows_of(value)
        change(rows)
        entries[position][1] = [row if type(row) is not dict else {'$m': sorted(row.items())} for row in rows]

    with pytest.raises(ArtifactRejected, match='invalid timeout rows'):
        load_built(built, edited(built.plan, mutate))


def test_a_timeouts_table_must_be_a_list_of_rows(ported):
    built, _ = ported

    def mutate(value):
        entries, position, _ = rows_of(value)
        entries[position][1] = {'$m': []}

    with pytest.raises(ArtifactRejected, match='invalid timeout rows'):
        load_built(built, edited(built.plan, mutate))


def test_a_port_row_must_name_a_node_port_that_the_plan_declares(ported):
    built, _ = ported

    def undeclare(value):
        value['tool_usages'] = []

    with pytest.raises(ArtifactRejected, match='invalid timeout rows'):
        load_built(built, edited(built.plan, undeclare))
