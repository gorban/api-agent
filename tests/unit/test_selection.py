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


def test_range_toggle_uses_union_of_overlapping_groups_and_preserves_scope():
    state = SelectionModel(identities())
    keys = ['tag:shared', 'file:repo::a.py', identities()[0].id]
    state.toggle_many(keys)
    assert state.selected_ids == set()
    state.toggle(identities()[0].id)
    state.toggle_many(keys)
    assert state.selected_ids == {test.id for test in identities()}
    assert {'tag:shared', 'file:repo::a.py'} <= state.selected_scopes
    new = TestIdentity('new', 'repo', 'a.py', 'a.py::new')
    state.refresh_for_run(identities() + [new])
    assert new.id in state.selected_ids
    state.toggle_many(keys)
    assert not state.selected_ids


def test_range_toggle_empty_and_invalid_keys_are_atomic():
    state = SelectionModel(identities())
    before = state.selected_ids.copy()
    state.toggle_many(())
    assert state.selected_ids == before
    with pytest.raises(CollectionError, match='Unknown selection group or test: missing'):
        state.toggle_many((identities()[0].id, 'missing'))
    assert state.selected_ids == before


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


@pytest.mark.parametrize('selector,expanded', [
    ('repo::only.py::test_one', set()),
    ('repo::only.py', {'repo::only.py::test_two'}),
    ('tag:shared', {'repo::other.py::test_two'}),
])
def test_startup_full_coverage_does_not_infer_all_intent(selector, expanded):
    original = TestIdentity('repo::only.py::test_one', 'repo', 'only.py', 'only.py::test_one', {'shared'})
    selection_state = SelectionModel([original])
    selection_state.preselect([selector])
    assert 'all' not in selection_state.selected_scopes
    additions = [TestIdentity('repo::only.py::test_two', 'repo', 'only.py', 'only.py::test_two'),
                 TestIdentity('repo::other.py::test_two', 'repo', 'other.py', 'other.py::test_two', {'shared'}),
                 TestIdentity('repo::unrelated.py::test_three', 'repo', 'unrelated.py', 'unrelated.py::test_three')]
    selection_state.refresh_for_run([original, *additions])
    assert selection_state.selected_ids == {original.id} | expanded


def test_qualified_tag_refresh_stays_within_repository():
    base = TestIdentity('base::a.py::test_one', 'base', 'a.py', 'a.py::test_one', {'shared'})
    pack = TestIdentity('pack::a.py::test_one', 'pack', 'a.py', 'a.py::test_one', {'shared'})
    selection_state = SelectionModel([base, pack])
    selection_state.preselect(['base::tag:shared'])
    assert selection_state.selected_scopes == {'base::tag:shared'}
    additions = [TestIdentity('base::b.py::test_new', 'base', 'b.py', 'b.py::test_new', {'shared'}),
                 TestIdentity('pack::b.py::test_new', 'pack', 'b.py', 'b.py::test_new', {'shared'})]
    selection_state.refresh_for_run([base, pack, *additions])
    assert selection_state.selected_ids == {base.id, additions[0].id}


def test_qualified_tag_scope_clears_when_last_member_is_deselected():
    old = TestIdentity('base::a.py::test_one', 'base', 'a.py', 'a.py::test_one', {'shared'})
    selection_state = SelectionModel([old])
    selection_state.preselect(['base::tag:shared'])
    selection_state.toggle(old.id)
    assert not selection_state.selected_scopes
    new = TestIdentity('base::b.py::test_new', 'base', 'b.py', 'b.py::test_new', {'shared'})
    selection_state.refresh_for_run([old, new])
    assert not selection_state.selected_ids


def test_startup_exclusion_survives_recollection():
    state = SelectionModel(identities())
    state.preselect(['tag:shared', '-repo::a.py::test_a'])
    added = TestIdentity('repo::new.py::new', 'repo', 'new.py', 'new.py::new', {'shared'})
    state.refresh_for_run(identities() + [added])
    assert state.selected_ids == {'repo::b.py::test_c', added.id}


def test_exclusion_only_keeps_repository_default_through_refresh():
    base = TestIdentity('base::a.py::test_a', 'base', 'a.py', 'a.py::test_a')
    owner = TestIdentity('owner::a.py::test_b', 'owner', 'a.py', 'a.py::test_b', {'skip'})
    keep = TestIdentity('owner::a.py::test_c', 'owner', 'a.py', 'a.py::test_c')
    state = SelectionModel([base, owner, keep])
    state.set_defaults({'owner'})
    state.preselect(['-skip'])
    new = TestIdentity('owner::new.py::test_d', 'owner', 'new.py', 'new.py::test_d')
    state.refresh_for_run([base, owner, keep, new])
    assert state.selected_ids == {keep.id, new.id}


def test_bare_file_namespace_retains_scope_for_refresh():
    state = SelectionModel(identities())
    state.preselect(['file:a.py'])
    added = TestIdentity('repo::a.py::new', 'repo', 'a.py', 'a.py::new')
    state.refresh_for_run(identities() + [added])
    assert state.selected_ids == {identities()[0].id, identities()[1].id, added.id}


def test_toggling_cannot_restore_startup_exclusion():
    state = SelectionModel(identities())
    state.preselect(['-tag:other'])
    state.toggle('repo::a.py::test_b')
    assert 'repo::a.py::test_b' not in state.selected_ids


def test_excluded_member_does_not_prevent_group_toggle():
    state = SelectionModel(identities())
    state.preselect(['a.py', '-tag:other'])
    state.toggle('file:repo::a.py')
    assert not state.selected_ids
    state.toggle('file:repo::a.py')
    assert state.selected_ids == {identities()[0].id}


@pytest.mark.parametrize('file,node,tags', [('shared', 'shared::one', set()),
                                              ('a.py', 'a.py::one', {'shared'})])
def test_qualified_group_scope_does_not_expand_another_repository(file, node, tags):
    base = TestIdentity('base::shared::one', 'base', file, node, tags)
    other = TestIdentity('shared::other.py::one', 'shared', 'other.py', 'other.py::one')
    added = TestIdentity('base::shared::two', 'base', base.file, 'shared::two', base.tags)
    unrelated = TestIdentity('shared::other.py::two', 'shared', 'other.py', 'other.py::two')
    state = SelectionModel([base, other])
    state.preselect(['base::shared'])
    state.refresh_for_run([base, other, added, unrelated])
    assert state.selected_ids == {base.id, added.id}


@pytest.mark.parametrize('group', ['tag:shared', 'file:a.py'])
def test_fully_excluded_group_keeps_startup_scope_for_new_members(group):
    old = TestIdentity('repo::a.py::old', 'repo', 'a.py', 'a.py::old', {'shared'})
    unrelated = TestIdentity('repo::b.py::keep', 'repo', 'b.py', 'b.py::keep')
    added = TestIdentity('repo::a.py::new', 'repo', 'a.py', 'a.py::new', {'shared'})
    state = SelectionModel([old, unrelated])
    state.preselect([group, unrelated.id, '-' + old.id])
    state.refresh_for_run([old, unrelated, added])
    assert state.selected_ids == {unrelated.id, added.id}


def test_reset_defaults_keeps_excluded_tests_unselected():
    state = SelectionModel(identities())
    state.preselect(['-tag:other'])
    state.set_defaults()
    assert state.selected_ids == {identities()[0].id, identities()[2].id}


@pytest.mark.parametrize('selector', ['repo', 'repo:repo'])
def test_startup_repository_group_expands_new_repository_tests(selector):
    original = TestIdentity('repo::a.py::one', 'repo', 'a.py', 'a.py::one')
    other = TestIdentity('other::b.py::one', 'other', 'b.py', 'b.py::one')
    added = TestIdentity('repo::new.py::two', 'repo', 'new.py', 'new.py::two')
    state = SelectionModel([original, other])
    state.preselect([selector])
    state.refresh_for_run([original, other, added])
    assert state.selected_ids == {original.id, added.id}


def test_qualified_file_group_expands_only_its_repository():
    base = TestIdentity('base::a.py::one', 'base', 'a.py', 'a.py::one')
    other = TestIdentity('other::a.py::one', 'other', 'a.py', 'a.py::one')
    added = TestIdentity('base::a.py::two', 'base', 'a.py', 'a.py::two')
    unrelated = TestIdentity('other::a.py::two', 'other', 'a.py', 'a.py::two')
    state = SelectionModel([base, other])
    state.preselect(['file:base::a.py'])
    state.refresh_for_run([base, other, added, unrelated])
    assert state.selected_ids == {base.id, added.id}
