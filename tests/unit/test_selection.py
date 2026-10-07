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


@pytest.mark.parametrize('selection,expected', [
    ('all', {'new-a', 'new-b', 'new-file'}),
    ('file:repo::a.py', {'new-a'}),
    ('tag:shared', {'new-a', 'new-file'}),
    ('repo::a.py::test_a', set()),
])
def test_recollection_expands_checked_groups_only(selection, expected):
    old = identities()
    selection_state = SelectionModel(old)
    if selection != 'all':
        selection_state.toggle('all')
        selection_state.toggle(selection)
    new = [
        TestIdentity('new-a', 'repo', 'a.py', 'a.py::new', {'shared'}),
        TestIdentity('new-b', 'repo', 'b.py', 'b.py::new', {'new-tag'}),
        TestIdentity('new-file', 'repo', 'new.py', 'new.py::new', {'shared'}),
    ]
    original_selected = selection_state.selected_ids.copy()
    selection_state.refresh_for_run(old + new)
    assert selection_state.selected_ids == original_selected | expected


def test_recollection_does_not_reselect_existing_excluded_tests():
    selection_state = SelectionModel(identities())
    selection_state.toggle('repo::a.py::test_b')
    selection_state.refresh_for_run(identities())
    assert 'repo::a.py::test_b' not in selection_state.selected_ids


def test_individual_selection_does_not_expand_incidentally_checked_tag():
    selection_state = SelectionModel(identities())
    selection_state.toggle('all')
    selection_state.toggle('repo::a.py::test_b')
    assert next(row for row in selection_state.groups() if row.id == 'tag:other').selected
    new = TestIdentity('new-other', 'repo', 'new.py', 'new.py::new', {'other'})
    selection_state.refresh_for_run(identities() + [new])
    assert selection_state.selected_ids == {'repo::a.py::test_b'}


def test_startup_file_selection_leaves_all_unchecked_and_expands_new_file_members():
    selection_state = SelectionModel(identities())
    selection_state.preselect(['repo::a.py'])
    assert not selection_state.groups()[0].selected
    assert {test.file for test in selection_state.selected_tests} == {'a.py'}
    new = TestIdentity('new-file-test', 'repo', 'a.py', 'a.py::new')
    selection_state.refresh_for_run(identities() + [new])
    assert new in selection_state.selected_tests
    assert 'repo::b.py::test_c' not in selection_state.selected_ids
