"""Limits declared by Bindings-grammar documents bound loops through configuration."""
import pytest

from api_agent.workflows import LayoutError
from api_agent.workflows.identity import declaration_identity, workflow_identity
from loop_support import CONFIG, build, edges, graph, limits, load, run, write

FLOW = [('node', 'draft'), ('node', 'verify'), ('node', 'accept'), ('node', 'failed')]


def retry_graph(tmp_path, sections=None, flow=FLOW):
    return graph(tmp_path, flow, sections=limits('PLAN_PASSES') if sections is None else sections)


def test_root_limit_is_graph_wide_and_resolves_its_default(tmp_path):
    definition = load(retry_graph(tmp_path))

    assert [(row['Key'], row.get('_owner_id')) for row in definition.limits] == [('PLAN_PASSES', None)]
    workflow, _, _ = build(tmp_path / 'flow.md.html')
    assert dict(workflow.limit_values) == {'PLAN_PASSES': 2}


@pytest.mark.parametrize('value', [0, 1, 99])
def test_configuration_overrides_the_declared_default_including_zero(tmp_path, value):
    entry = retry_graph(tmp_path)

    workflow, _, _ = build(entry, config={'PLAN_PASSES': value})

    assert dict(workflow.limit_values) == {'PLAN_PASSES': value}


@pytest.mark.parametrize('value', [-1, 100, True, 2.0, '2'])
def test_invalid_override_is_rejected_before_any_node_runs(tmp_path, value):
    entry = retry_graph(tmp_path)

    with pytest.raises(LayoutError, match='limit must be an integer from 0 to 99') as caught:
        build(entry, config={'PLAN_PASSES': value})

    assert str(entry) in str(caught.value)


def test_component_and_node_documents_declare_scoped_limits(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), ('node', 'accept')],
                  sections=limits('PLAN_PASSES'),
                  components={'group': (['draft', 'verify'], limits('REPLAN_PASSES'))})
    write(tmp_path, 'accept.md.html', (tmp_path / 'accept.md.html').read_text() + limits('NO_PASSES'))

    definition = load(entry)

    rows = {row['Key']: row.get('_owner_id') for row in definition.limits}
    assert rows == {'PLAN_PASSES': None, 'REPLAN_PASSES': 'group', 'NO_PASSES': 'accept'}


@pytest.mark.parametrize('rows,message', [
    (limits('plan_passes'), 'invalid limit key'),
    (limits('PLAN_PASSES', 'PLAN_PASSES'), 'duplicate limit key'),
    (limits('RATIO'), 'limit key must name an integer configuration field'),
    (limits('MISSING'), 'limit key must name an integer configuration field'),
    (limits('NO_DEFAULT'), 'limit requires a declared integer default'),
    (limits('NEGATIVE'), 'limit must be an integer from 0 to 99'),
    (limits('HUGE'), 'limit must be an integer from 0 to 99'),
    (limits('PLAN_PASSES', source='native.py'), 'limit source must be TOML'),
])
def test_invalid_limit_declarations_are_located_at_load(tmp_path, rows, message):
    entry = retry_graph(tmp_path, rows)

    with pytest.raises(LayoutError, match=message) as caught:
        load(entry)

    assert f'{entry}:' in str(caught.value)


def test_component_limit_errors_name_the_declaring_document(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), ('node', 'accept')],
                  sections=limits('PLAN_PASSES'),
                  components={'group': (['draft', 'verify'], limits('PLAN_PASSES'))})

    with pytest.raises(LayoutError, match='duplicate limit key') as caught:
        load(entry)

    assert str(tmp_path / 'group.md.html') + ':' in str(caught.value)


def test_a_limit_key_cannot_also_be_a_component_pass_key(tmp_path):
    entry = graph(tmp_path, [('component', 'group'), ('node', 'accept')],
                  sections=limits('PLAN_PASSES'),
                  components={'group': (['draft', 'verify'],
                                       '## Components\n\n| ID | Max passes | Guard |\n| --- | --- | --- |\n'
                                       '| group | [PLAN_PASSES](config.toml) | verify |\n')})

    with pytest.raises(LayoutError, match='conflicts with a component Max passes key'):
        load(entry)


def test_limits_are_predicate_constants_in_edges(tmp_path):
    entry = retry_graph(tmp_path, limits('PLAN_PASSES') + edges(
        ('verify', 'accept', 'PLAN_PASSES > 0'), ('verify', 'failed', 'otherwise'),
        ('accept', 'END', 'always')))

    assert run(entry)[1] == ['draft', 'verify', 'accept']
    assert run(entry, config={'PLAN_PASSES': 0})[1] == ['draft', 'verify', 'failed']


def test_a_component_limit_is_a_constant_only_for_edges_inside_its_scope(tmp_path):
    flow = [('component', 'group'), ('node', 'accept'), ('node', 'failed')]
    components = {'group': (['draft', 'verify'], limits('REPLAN_PASSES'))}
    inside = graph(tmp_path, flow, sections=edges(
        ('group', 'accept', 'REPLAN_PASSES > 0'), ('group', 'failed', 'otherwise')), components=components)

    assert load(inside).limits

    outside = graph(tmp_path, flow, sections=edges(
        ('accept', 'END', 'REPLAN_PASSES > 0'), ('accept', 'failed', 'otherwise')), components=components)
    with pytest.raises(LayoutError, match='condition'):
        load(outside)


def identity(tmp_path, values, rows=None):
    definition = load(retry_graph(tmp_path, rows))
    return definition, workflow_identity(definition, timeouts={}, component_passes={}, limits=values)


def test_limit_values_are_part_of_the_workflow_identity(tmp_path):
    _, two = identity(tmp_path, {'PLAN_PASSES': 2})
    _, three = identity(tmp_path, {'PLAN_PASSES': 3})
    _, zero = identity(tmp_path, {'PLAN_PASSES': 0})

    assert len({two['settings_sha256'], three['settings_sha256'], zero['settings_sha256']}) == 3
    assert two['declaration_sha256'] == three['declaration_sha256'] == zero['declaration_sha256']


def test_declared_limits_change_the_declaration_identity_but_absent_ones_do_not(tmp_path):
    def declared(name, sections, **kwargs):
        (tmp_path / name).mkdir()
        return declaration_identity(load(graph(tmp_path / name, FLOW, sections=sections, **kwargs)))

    config = CONFIG.replace('PLAN_PASSES', 'ATTEMPTS')
    ids = {declared('with', limits('PLAN_PASSES')), declared('without', ''),
           declared('renamed', limits('ATTEMPTS'), config=config)}

    assert len(ids) == 3


def test_limit_coverage_must_match_the_declaration(tmp_path):
    definition = load(retry_graph(tmp_path))

    with pytest.raises(ValueError, match='limit coverage'):
        workflow_identity(definition, timeouts={}, component_passes={}, limits={})
    with pytest.raises(ValueError, match='limit coverage'):
        workflow_identity(definition, timeouts={}, component_passes={}, limits={'PLAN_PASSES': 100})
