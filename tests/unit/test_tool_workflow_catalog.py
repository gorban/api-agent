"""Static case associations admit only explicitly declared graphs and variants."""
import pytest
from pathlib import Path

from api_agent.config.declarations import ConfigSchema
from api_agent.runtime.resource_pack import load_launch_context
from api_agent.tooling.selection import compose_tools

from api_agent.testing.workflow_selection import parse_catalog, selected_graphs
from api_agent.tooling.models import OperationSelection, ToolSelectionError


def declaration():
    return {'version': 1,
            'graphs': {'primary': {'root': 'base', 'entry': 'flow.md.html'}},
            'test_cases': {'base::tests/test_flow.py::test_case[first]': ['primary']},
            'variants': {'finite': {'graph': 'primary', 'replacements': [
                {'target_id': 'inspect', 'entry': 'alternate.md.html', 'root_id': 'pack'}]}}}


def test_exact_case_union_includes_only_requested_finite_variant(tmp_path):
    catalog = parse_catalog(declaration(), {'base': tmp_path, 'pack': tmp_path})

    with pytest.raises(ToolSelectionError, match='no static graph association'):
        selected_graphs(catalog, OperationSelection('tests', ('base::tests/test_flow.py::test_case[second]',)))
    selected = selected_graphs(catalog, OperationSelection('tests', ('base::tests/test_flow.py::test_case[first]',), ('finite',)))

    assert selected == (('primary', 'finite'), ())
    assert selected_graphs(catalog, OperationSelection('collection')) == ((), ())


@pytest.mark.parametrize('change', [
    lambda data: data.update(version=True),
    lambda data: data['graphs']['primary'].update(root=[]),
    lambda data: data['graphs']['primary'].update(entry='../outside.md.html'),
    lambda data: data['graphs']['primary'].update(entry='./flow.md.html'),
    lambda data: data['graphs']['primary'].update(profile=[]),
    lambda data: data['variants']['finite'].update(graph=[]),
    lambda data: data['variants']['finite']['replacements'].append(
        {'target_id': 'inspect', 'entry': 'second.md.html', 'root_id': 'pack'}),
    lambda data: data['test_cases'].update({'base::tests/test_flow.py': ['primary']}),
    lambda data: data['test_cases'].update({'base::tests/test_flow.py::test_unknown': ['invented']}),
    lambda data: data['test_cases'].update({'base::tests/test_flow.py::test_unknown': ['primary', 'primary']}),
    lambda data: data.update(graphs=[]),
    lambda data: data['graphs']['primary'].update(unexpected=True),
    lambda data: data['graphs']['primary'].update(root='missing'),
    lambda data: data['graphs']['primary'].update(replacements={}),
    lambda data: data['graphs'].update({'NotCanonical': {'root': 'base', 'entry': 'flow.md.html'}}),
    lambda data: data['variants']['finite'].update(profile='missing'),
    lambda data: data['variants']['finite'].update(profile=[]),
    lambda data: data.update(profiles={'shared': {'version': 1, 'kind': 'shared-primary',
        'primary': 'one', 'secondary': ['one']}}),
])
def test_catalog_rejects_ambiguous_or_unbounded_associations(tmp_path, change):
    data = declaration()
    change(data)

    with pytest.raises(ToolSelectionError):
        parse_catalog(data, {'base': tmp_path, 'pack': tmp_path})


def test_worker_cannot_request_an_undeclared_variant(tmp_path):
    catalog = parse_catalog(declaration(), {'base': tmp_path, 'pack': tmp_path})

    with pytest.raises(ToolSelectionError, match='Unknown trusted workflow variant'):
        selected_graphs(catalog, OperationSelection('tests', variants=('invented',)))


@pytest.mark.parametrize('case', [
    'test_studio_request.py::test_studio_request',
    'test_studio_selection.py::test_studio_selection',
    'test_reasoning_contrast.py::test_reasoning_contrast',
])
def test_bundled_direct_http_tests_do_not_activate_an_application_graph(case):
    context = load_launch_context(Path(__file__).resolve().parents[2])
    selected = compose_tools(context, ConfigSchema({}), environ={}, selection=(
        'api-agent::tests/integration/' + case,))

    assert not selected.definitions
    assert not selected.tools
    assert 'API_ENDPOINT' not in selected.schema.fields


def test_catalog_bounds_declaration_identity_count(tmp_path, monkeypatch):
    from api_agent.testing import workflow_selection
    monkeypatch.setattr(workflow_selection, 'MAX_GRAPHS', 0)

    with pytest.raises(ToolSelectionError, match='finite identity budget'):
        parse_catalog(declaration(), {'base': tmp_path, 'pack': tmp_path})


def test_graphless_collected_cases_and_unit_mode_do_not_require_static_graphs(tmp_path):
    catalog = parse_catalog(declaration(), {'base': tmp_path, 'pack': tmp_path})

    assert selected_graphs(catalog, OperationSelection('unit', ('unmapped',))) == ((), ())
    assert selected_graphs(catalog, OperationSelection('tests', ('unmapped',), graphless=('unmapped',))) == ((), ())


def test_exact_case_profile_is_retained_with_its_declared_graph(tmp_path):
    data = declaration()
    data['profiles'] = {'shared': {'version': 1, 'kind': 'shared-primary',
        'primary': 'sample/one', 'secondary': ['sample/two']}}
    data['test_cases']['base::tests/test_flow.py::test_case[first]'] = {'graphs': ['primary'], 'profile': 'shared'}
    catalog = parse_catalog(data, {'base': tmp_path, 'pack': tmp_path})

    selected = selected_graphs(catalog, OperationSelection('tests', ('base::tests/test_flow.py::test_case[first]',)))

    assert selected == (('primary',), ('shared',))
