"""Compiled plans carry Limits rows and bounded loops through compile and load."""
import asyncio
import json

import pytest

from api_agent.workflows.artifact import ArtifactRejected, plan_digest
from api_agent.workflows.artifact_compile import compile_plan
from api_agent.workflows.identity import workflow_identity
from artifact_acceptance_support import build_wheel, install
from artifact_unit_support import Built, edited, isolated_imports, load_built  # noqa: F401
from loop_support import NATIVE, edges, graph, limits, load
from test_workflow_loops import RETRY, retry


def built(tmp_path, entry):
    wheel = build_wheel(tmp_path, 'aq-loops', {'aq_loops/__init__.py': b'', 'aq_loops/native.py': NATIVE.encode()})
    plan = compile_plan(entry, base_root=entry.parent, pack_root=entry.parent, wheels=[wheel])
    return Built(plan, plan_digest(plan), install(wheel, tmp_path / 'site'), wheel, tmp_path)


@pytest.fixture
def loop_plan(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    entry = retry(root)
    return built(tmp_path, entry), entry


def run(compiled, update, config=None):
    values = asyncio.run(compiled.workflow(config=config).arun(update))
    return values, compiled.modules['aq_loops.native'].CALLS


def tables(plan):
    return dict(json.loads(plan)['layout']['tables']['$m'])


def test_compiled_plan_round_trips_limits_and_runs_the_loop(loop_plan):
    plan, entry = loop_plan

    compiled = load_built(plan)

    assert [row['Key'] for row in compiled.limits] == ['PLAN_PASSES']
    values, calls = run(compiled, {'accept_at': 2})
    assert calls == ['draft', 'verify', 'draft', 'verify', 'accept'] and values['draft'] == {'n': 2}
    compiled = load_built(plan)
    assert run(compiled, {'accept_at': 9})[1] == ['draft', 'verify', 'draft', 'verify', 'failed']


def test_compiled_limits_honor_configuration_overrides(loop_plan):
    plan, _ = loop_plan

    compiled = load_built(plan)

    assert run(compiled, {'accept_at': 99}, config={'PLAN_PASSES': 3})[1].count('draft') == 3


def test_compiled_identity_matches_the_source_definition(loop_plan):
    plan, entry = loop_plan
    source, compiled = load(entry), load_built(plan)
    values = {'PLAN_PASSES': 2}

    assert (workflow_identity(compiled, timeouts={}, component_passes={}, limits=values)
            == workflow_identity(source, timeouts={}, component_passes={}, limits=values))


def test_compiled_plan_without_limits_carries_no_limits_table(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    plain = built(tmp_path, graph(root, [('node', 'draft')]))

    assert 'Limits' not in tables(plain.plan)
    assert load_built(plain).limits == ()


@pytest.mark.parametrize('change,message', [
    (lambda rows: rows.append(dict(rows[0])), 'invalid limit rows'),
    (lambda rows: rows[0].pop('Key'), 'invalid limit rows'),
    (lambda rows: rows[0].update(extra=1), 'invalid limit rows'),
    (lambda rows: rows[0].update(Key='lower'), 'invalid limit rows'),
    (lambda rows: rows[0].update(Source={'$p': 'base/native.py'}), 'invalid limit rows'),
    (lambda rows: rows[0].update(Source='base/config.toml'), 'invalid limit rows'),
    (lambda rows: rows[0].update(line=0), 'invalid limit rows'),
    (lambda rows: rows.__setitem__(0, 1), 'invalid limit rows'),
])
def test_malformed_limit_rows_are_rejected(loop_plan, change, message):
    plan, _ = loop_plan

    def mutate(value):
        entries = value['layout']['tables']['$m']
        position = next(i for i, (name, _) in enumerate(entries) if name == 'Limits')
        rows = [dict(row['$m']) for row in entries[position][1]]
        change(rows)
        entries[position][1] = [row if type(row) is not dict else {'$m': sorted(row.items())} for row in rows]

    with pytest.raises(ArtifactRejected, match=message):
        load_built(plan, edited(plan.plan, mutate))


def test_component_declared_limits_keep_their_owner_through_the_plan(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    entry = graph(root, [('component', 'group'), ('node', 'accept')], components={
        'group': (['draft', 'verify'], limits('PLAN_PASSES') + edges(RETRY, ('verify', 'END', 'otherwise')))})
    plan = built(tmp_path, entry)

    compiled = load_built(plan)

    assert [(row['Key'], row['_owner_id']) for row in compiled.limits] == [('PLAN_PASSES', 'group')]
    assert run(compiled, {'accept_at': 99})[1] == ['draft', 'verify', 'draft', 'verify', 'accept']

    def foreign_owner(value):
        entries = value['layout']['tables']['$m']
        position = next(i for i, (name, _) in enumerate(entries) if name == 'Limits')
        row = dict(entries[position][1][0]['$m'])
        row['_owner_id'] = 7
        entries[position][1] = [{'$m': sorted(row.items())}]

    with pytest.raises(ArtifactRejected, match='invalid limit rows'):
        load_built(plan, edited(plan.plan, foreign_owner))


def test_a_limits_table_must_be_a_list_of_rows(loop_plan):
    plan, _ = loop_plan

    def mutate(value):
        entries = value['layout']['tables']['$m']
        position = next(i for i, (name, _) in enumerate(entries) if name == 'Limits')
        entries[position][1] = {'$m': []}

    with pytest.raises(ArtifactRejected, match='invalid limit rows'):
        load_built(plan, edited(plan.plan, mutate))
