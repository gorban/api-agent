import pytest

from api_agent.testing import CollectionError, SelectionModel, TestIdentity
from api_agent.testing.search import SearchSelection


def suite():
    return [TestIdentity('repo::a.py::test_alpha', 'repo', 'a.py', 'a.py::test_alpha', {'shared', 'fast'}),
            TestIdentity('repo::a.py::test_beta', 'repo', 'a.py', 'a.py::test_beta', {'slow'}),
            TestIdentity('repo::b.py::test_gamma', 'repo', 'b.py', 'b.py::test_gamma', {'shared'})]


def test_empty_and_invalid_queries_preserve_selection_and_projection():
    state = SelectionModel(suite())
    search = SearchSelection(state, -1)
    before = state.selected_ids.copy(), state.selected_scopes.copy()
    assert search.direction == -1
    assert search.rows() == state.groups()[1:]
    assert search.set_query('alpha')
    projection = search.rows()
    assert not search.set_query('[')
    assert search.query == '[' and search.error == 'Invalid regular expression'
    assert search.rows() == projection
    assert (state.selected_ids, state.selected_scopes) == before
    assert search.set_query('') and search.error is None
    assert search.rows() == state.groups()[1:]


@pytest.mark.parametrize('pattern,group,visible', [
    ('^shared$', 'tag:shared', {0, 2}),
    ('^repo::a.py$', 'file:repo::a.py', {0, 1}),
    ('alpha', None, {0}),
    ('(?i)ALPHA', None, {0}),
    ('ALPHA', None, set()),
    ('no-match', None, set()),
])
def test_regex_matches_full_labels_and_includes_group_children(pattern, group, visible):
    state = SelectionModel(suite())
    search = SearchSelection(state)
    search.set_query(pattern)
    rows = search.rows()
    assert [row.id for row in rows if row.kind == 'test'] == [suite()[i].id for i in sorted(visible)]
    assert [row.id for row in rows if row.kind in {'file', 'tag'}] == ([group] if group else [])
    assert all(row.id != 'all' for row in rows)
    other = [row for row in rows if row.kind == 'other']
    assert len(other) == bool(3 - len(visible))
    if other:
        assert other[0].count == other[0].total == 3 - len(visible)
        assert other[0].selected


def test_union_toggle_overlapping_groups_keeps_intent_and_expands_new_tests():
    state = SelectionModel(suite())
    state.toggle('all')
    search = SearchSelection(state)
    search.set_query('shared|repo::a.py$')
    keys = ['tag:shared', 'file:repo::a.py', suite()[0].id]
    search.toggle_many(keys)
    assert state.selected_ids == {test.id for test in suite()}
    assert state.selected_scopes == {'tag:shared', 'file:repo::a.py'}
    new_file = TestIdentity('new-file', 'repo', 'a.py', 'a.py::new')
    new_tag = TestIdentity('new-tag', 'repo', 'c.py', 'c.py::new', {'shared'})
    unrelated = TestIdentity('unrelated', 'repo', 'd.py', 'd.py::new')
    state.refresh_for_run(suite() + [new_file, new_tag, unrelated])
    assert {new_file.id, new_tag.id} <= state.selected_ids
    assert unrelated.id not in state.selected_ids
    search.toggle_many(keys)
    assert not state.selected_ids


def test_other_is_unique_selected_only_and_removal_keeps_matching_selection():
    state = SelectionModel(suite())
    search = SearchSelection(state)
    search.set_query('beta')
    other = search.rows()[-1]
    assert other.id == 'search:other' and other.label == '2 other tests'
    search.toggle_many([other.id])
    assert state.selected_ids == {suite()[1].id}
    assert len(search.rows()) == 1
    assert search.rows()[0].selected
    search.set_query('no-match')
    search.toggle_many(['search:other'])
    assert search.rows() == ()


def test_empty_and_invalid_toggle_are_atomic_and_individual_does_not_create_scope():
    state = SelectionModel(suite())
    state.toggle('all')
    search = SearchSelection(state)
    search.set_query('alpha')
    search.toggle_many([])
    with pytest.raises(CollectionError):
        search.toggle_many([suite()[0].id, 'all'])
    assert not state.selected_ids
    search.toggle_many([suite()[0].id])
    assert not state.selected_scopes
    search.toggle_many([suite()[0].id])
    assert not state.selected_ids


def test_opening_search_preserves_all_expansion_and_existing_exclusions():
    state = SelectionModel(suite())
    search = SearchSelection(state)
    search.set_query('alpha')
    search.rows()
    assert state.selected_scopes == {'all'}
    state.toggle(suite()[1].id)
    before = state.selected_ids.copy(), state.selected_scopes.copy()
    search.set_query('shared')
    search.rows()
    assert (state.selected_ids, state.selected_scopes) == before
    new = TestIdentity('new', 'repo', 'a.py', 'a.py::new', {'shared'})
    state.refresh_for_run(suite() + [new])
    assert suite()[1].id not in state.selected_ids
    assert new.id not in state.selected_ids


def test_combined_other_and_overlapping_rows_toggle_union_once():
    state = SelectionModel(suite())
    search = SearchSelection(state)
    search.set_query('shared')
    keys = ['tag:shared', suite()[0].id, 'search:other', 'tag:shared']
    search.toggle_many(keys)
    assert not state.selected_ids
    assert search.rows()[-1].kind != 'other'
    search.toggle_many(['tag:shared', suite()[0].id, suite()[2].id])
    assert state.selected_ids == {suite()[0].id, suite()[2].id}
    assert state.selected_scopes == {'tag:shared'}


def test_empty_suite_and_unselected_nonmatching_tests_have_no_other_row():
    empty = SearchSelection(SelectionModel([]))
    assert empty.rows() == ()
    assert empty.set_query('anything')
    assert empty.rows() == ()
    state = SelectionModel(suite())
    state.toggle('all')
    search = SearchSelection(state)
    search.set_query('alpha')
    assert len(search.rows()) == 1
    assert not search.rows()[0].selected
    assert search.rows()[0].count == 0


def test_empty_projected_union_preserves_checked_tests_and_scopes():
    state = SelectionModel(suite())
    selected, scopes = state.selected_ids.copy(), state.selected_scopes.copy()
    state.toggle_members((), ('tag:shared',))
    assert state.selected_ids == selected
    assert state.selected_scopes == scopes
