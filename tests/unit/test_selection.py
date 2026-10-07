import pytest
from api_agent.testing import CollectionError, SelectionModel, TestIdentity


def identities():
    return [TestIdentity('repo::a.py::test_a', 'repo', 'a.py', 'a.py::test_a', frozenset({'shared'})),
            TestIdentity('repo::a.py::test_b', 'repo', 'a.py', 'a.py::test_b', frozenset({'other'})),
            TestIdentity('repo::b.py::test_c', 'repo', 'b.py', 'b.py::test_c', frozenset({'shared'}))]


def test_overlapping_groups_recompute_counts():
    tests = identities()
    selection_state = SelectionModel(tests + [tests[0]])
    assert len(selection_state.selected_tests) == 3
    assert [row.kind for row in selection_state.groups()] == ['all', 'tag', 'tag', 'file', 'file', 'test', 'test', 'test']
    selection_state.toggle('tag:shared')
    rows = {row.id: row for row in selection_state.groups()}
    assert not rows['all'].selected and rows['all'].count == 1
    assert rows['file:repo::a.py'].count == 1
    selection_state.toggle('file:repo::a.py')
    assert len(selection_state.selected_ids) == 2
    selection_state.toggle('tag:shared')
    assert selection_state.groups()[0].selected
    selection_state.toggle(selection_state.groups()[0])
    assert not selection_state.selected_tests
    selection_state.toggle(tests[0].id)
    assert selection_state.selected_tests == (tests[0],)


def test_unknown_and_empty_selection():
    selection_state = SelectionModel([])
    assert selection_state.groups()[0].total == 0
    assert not selection_state.groups()[0].selected
    with pytest.raises(CollectionError):
        selection_state.toggle('missing')
