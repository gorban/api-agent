"""Canonical and compatible credential profile parsing."""
import pytest

from api_agent.auth.profiles import (DEFAULT_PASSWORD_MANAGER, SelectionRequired,
                                         normalize_auth, parse_profiles, select_profiles)


def entry(user):
    return {'vault': 'vault', 'user': user}


READY = {'manager': 'keeper', 'cli_path': 'keeper'}


def canonical(roles, manager=None):
    return {'password_manager': manager or DEFAULT_PASSWORD_MANAGER, 'roles': roles}


def test_default_and_shared_manager_composition():
    assert normalize_auth({'roles': {'LLM User': []}}) == canonical({'LLM User': []})
    profiles = parse_profiles(canonical({'LLM User': [entry('Ada')], 'Other': []}, READY), ('LLM User', 'Other'))
    source = profiles.for_role('LLM User')[0]
    assert (source.manager, source.cli_path, source.vault, source.user) == ('keeper', 'keeper', 'vault', 'Ada')
    assert profiles.for_role('Other') == ()
    custom = parse_profiles(canonical({'LLM User': [entry('A')]}, {'manager': '1password', 'cli_path': '/some op'}), ('LLM User',))
    assert custom.for_role('LLM User')[0].cli_path == '/some op'


def test_legacy_uniform_migration_and_conflict():
    legacy = {'LLM User': [{'manager': 'keeper', 'cli_path': '/keeper', **entry('A')}], 'Other': []}
    assert normalize_auth(legacy) == canonical({'LLM User': [entry('A')], 'Other': []}, {'manager': 'keeper', 'cli_path': '/keeper'})
    assert parse_profiles(legacy, ('LLM User', 'Other')).for_role('LLM User')[0].cli_path == '/keeper'
    mixed = {'LLM User': legacy['LLM User'], 'Other': [{'manager': '1password', 'cli_path': 'op', **entry('B')}]}
    with pytest.raises(ValueError, match='choose one shared password manager'):
        normalize_auth(mixed)


def test_selection_and_index_bounds():
    profiles = parse_profiles(canonical({'LLM User': [entry('A'), entry('B')], 'Other': [entry('X'), entry('Y')]}, READY), ('LLM User', 'Other', 'Empty'))
    with pytest.raises(SelectionRequired):
        select_profiles(profiles, ('LLM User', 'Other'))
    chosen = select_profiles(profiles, ('LLM User', 'Other'), session_index=1)
    assert chosen.for_role('Other').user == 'Y'
    assert select_profiles(profiles, ('Empty',)).attended_roles == ('Empty',)
    with pytest.raises(ValueError, match='out of bounds'):
        select_profiles(profiles, ('LLM User',), configured_index=3)


@pytest.mark.parametrize('payload,message', [
    ([], 'JSON object'),
    ({'password_manager': {'manager': 'bad', 'cli_path': 'x'}, 'roles': {}}, 'Selected password manager is unavailable'),
    ({'roles': {'LLM User': {}}}, 'array'),
    ({'roles': {'LLM User': [None]}}, 'object'),
    ({'roles': {'LLM User': [{'vault': 'v'}]}}, 'exactly vault and user'),
    ({'roles': {'LLM User': [{'vault': '', 'user': 'u'}]}}, 'nonempty'),
    ({'roles': {'Other': []}}, 'unknown role'),
])
def test_invalid(payload, message):
    with pytest.raises(ValueError, match=message):
        parse_profiles(payload, ('LLM User',))


def test_inactive_canonical_roles_are_preserved_without_validation():
    data = {'roles': {'LLM User': [entry('A')], 'Other': [{'unknown': 'kept'}]}}
    assert normalize_auth(data)['roles']['Other'] == [{'unknown': 'kept'}]
    selected = canonical({'LLM User': normalize_auth(data)['roles']['LLM User']}, READY)
    assert parse_profiles(selected, ('LLM User',)).for_role('LLM User')[0].user == 'A'


def test_selection_zero_singleton_and_invalid_indices():
    profiles = parse_profiles(canonical({'LLM User': [entry('A'), entry('B')],
                                        'Other': [entry('X')], 'Empty': []}, READY),
                              ('LLM User', 'Other', 'Empty'))
    assert select_profiles(profiles, ('Other',)).for_role('Other').user == 'X'
    with pytest.raises(ValueError, match='no complete account pair'):
        select_profiles(profiles, ('Empty',), configured_index=9)
    for invalid in (True, -1, 1.0, '1'):
        with pytest.raises(ValueError, match='nonnegative integer'):
            select_profiles(profiles, ('LLM User',), configured_index=invalid)
    with pytest.raises(ValueError, match='complete account pair'):
        select_profiles(profiles, ('LLM User', 'Other'), configured_index=1)
    assert select_profiles(profiles, ('LLM User',), configured_index=0, session_index='stale').for_role('LLM User').user == 'A'
    with pytest.raises(ValueError, match='enabled'):
        select_profiles(profiles, ('Unknown',))


def test_legacy_reserved_role_names_are_not_envelope_fields():
    legacy = {'roles': [{'manager': 'keeper', 'cli_path': 'keeper', **entry('A')}],
              'password_manager': []}
    normalized = normalize_auth(legacy)
    assert normalized['roles'] == {'roles': [entry('A')], 'password_manager': []}
    profiles = parse_profiles(legacy, ('roles', 'password_manager'))
    assert profiles.for_role('roles')[0].user == 'A'
    assert profiles.for_role('password_manager') == ()


def test_blank_draft_manager_blocks_retrieval_but_not_normalization():
    draft = canonical({'LLM User': [entry('A')]})
    assert draft['password_manager'] == {'manager': '', 'cli_path': ''}
    with pytest.raises(ValueError, match='Select a password manager and CLI path'):
        parse_profiles(draft, ('LLM User',))
    assert normalize_auth({'password_manager': {'manager': 'keeper', 'cli_path': ''}, 'roles': {}})['password_manager'] == {
        'manager': 'keeper', 'cli_path': 'keeper'}
    assert normalize_auth({'password_manager': {'manager': '1password', 'cli_path': ''}, 'roles': {}})['password_manager'] == {
        'manager': '1password', 'cli_path': 'op'}


def test_selection_uses_only_complete_shared_pairs():
    profiles = parse_profiles(canonical({'LLM User': [entry('A'), entry('B')],
                                         'Secondary': [entry('X')]}, READY), ('LLM User', 'Secondary'))
    assert select_profiles(profiles, ('LLM User', 'Secondary')).index == 0
    with pytest.raises(ValueError, match='complete account pair'):
        select_profiles(profiles, ('LLM User', 'Secondary'), configured_index=1)
    one_empty = parse_profiles(canonical({'LLM User': [entry('A')], 'Secondary': []}, READY), ('LLM User', 'Secondary'))
    selection = select_profiles(one_empty, ('LLM User', 'Secondary'))
    assert selection.index is None and selection.attended_roles == ('LLM User', 'Secondary')
    assert selection.for_role('LLM User') is None
    with pytest.raises(ValueError, match='not requested'):
        selection.for_role('Unknown')


@pytest.mark.parametrize('payload,message', [
    ({'password_manager': {'manager': 'keeper'}, 'roles': {}}, 'exactly manager and cli_path'),
    ({'password_manager': {'manager': 'keeper', 'cli_path': 4}, 'roles': {}}, 'cli_path must be a string'),
    ({'roles': {}, 'extra': []}, 'only password_manager and roles'),
    ({'password_manager': READY, 'roles': []}, 'roles must be a JSON object'),
    ({'LLM User': {}}, 'must contain an array'),
    ({'LLM User': [{'manager': 'keeper', 'cli_path': 'keeper', 'vault': 'v'}]}, 'requires manager, cli_path, vault and user'),
])
def test_invalid_envelopes_and_legacy_descriptors(payload, message):
    with pytest.raises(ValueError, match=message):
        normalize_auth(payload)


@pytest.mark.parametrize('labels,message', [
    ('LLM User', 'sequence'),
    (['LLM User', ''], 'nonempty role labels'),
    (['LLM User', 'LLM User'], 'duplicate role labels'),
])
def test_invalid_enabled_role_labels(labels, message):
    with pytest.raises(ValueError, match=message):
        parse_profiles({}, labels)


def test_role_lookup_and_selection_options_are_ordered_and_descriptive():
    profiles = parse_profiles(canonical({'LLM User': [entry('A'), entry('B')],
                                         'Other': [entry('X'), entry('Y')]}, READY),
                              ('Other', 'LLM User'))
    with pytest.raises(ValueError, match='Unknown credential role'):
        profiles.for_role('Missing')
    with pytest.raises(SelectionRequired) as exc:
        select_profiles(profiles, ('LLM User', 'Other'))
    assert [(option.role, option.index, option.user) for option in exc.value.options] == [
        ('LLM User', 0, 'A'), ('LLM User', 1, 'B'), ('Other', 0, 'X'), ('Other', 1, 'Y')]
    with pytest.raises(ValueError, match='duplicate role labels'):
        select_profiles(profiles, ('Other', 'Other'))


def test_credential_source_error_identifies_role_and_position(monkeypatch):
    from api_agent.auth.credentials import CredentialError
    from api_agent.auth import profiles as module

    error = CredentialError('unavailable descriptor')

    def fail(_, *, registry=None):
        raise error

    monkeypatch.setattr(module.CredentialSource, 'from_mapping', fail)
    with pytest.raises(ValueError, match="role 'LLM User' entry 0: unavailable descriptor") as exc:
        parse_profiles(canonical({'LLM User': [entry('A')]}, READY), ('LLM User',))
    assert exc.value.__cause__ is error
