from hashlib import sha256
import pytest
import re
from api_agent.runtime.ownership import StackScope, accepted_projects, checkout_label, checkout_slug, derived_project


def test_canonical_root_identity(tmp_path):
    scope=StackScope.from_roots(tmp_path / 'base' / '..',tmp_path / 'pack')
    assert sha256(f'{tmp_path.resolve()}\n{(tmp_path / "pack").resolve()}'.encode()).hexdigest()[:16] in scope.legacy_ids
    assert scope.project == f'sa-pack-{scope.id[:8]}'
    assert scope == StackScope.from_roots(tmp_path,tmp_path / 'pack')
    assert scope != StackScope.from_roots(tmp_path,tmp_path / 'other')


def test_labels_require_all_ownership_evidence():
    scope=StackScope('test')
    labels={**scope.labels('langfuse'),'com.docker.compose.project':scope.project,'com.docker.compose.service':'api'}
    assert scope.owns(labels)
    assert not scope.owns(None)
    for key in labels:
        broken={**labels,key:'wrong'}
        if key == 'com.docker.compose.service': broken[key]=' '
        assert not scope.owns(broken)
        assert not scope.owns({k:v for k,v in labels.items() if k != key})
    for profile in ('otel','localstack'):
        assert scope.labels(profile)['api-agent.profile']==profile
    with pytest.raises(ValueError,match='Unsupported'):
        scope.labels('unknown')


def test_physical_aliases_keep_scope_without_resolve_equality(tmp_path, monkeypatch):
    from pathlib import Path
    base, pack = tmp_path / 'base', tmp_path / 'pack'
    base.mkdir(); pack.mkdir()
    alias_base, alias_pack = tmp_path / 'alias-base', tmp_path / 'alias-pack'
    alias_base.symlink_to(base, target_is_directory=True)
    alias_pack.symlink_to(pack, target_is_directory=True)
    original = Path.resolve
    monkeypatch.setattr(Path, 'resolve', lambda path: path.absolute() if path in {alias_base, alias_pack} else original(path))
    assert base.resolve() != alias_base.resolve() and base.samefile(alias_base)
    assert pack.resolve() != alias_pack.resolve() and pack.samefile(alias_pack)
    scope = StackScope.from_roots(base, pack)
    assert scope == StackScope.from_roots(alias_base, alias_pack)
    other = tmp_path / 'worktree'; other.mkdir()
    assert scope != StackScope.from_roots(base, other)
    legacy = scope.legacy_ids[0]
    assert scope.is_legacy({**scope.labels('otel'), 'api-agent.scope': legacy,
                            'com.docker.compose.project': 'sa-' + legacy})
    assert not scope.is_legacy(None)
    assert not scope.is_legacy(scope.labels('otel'))


SCOPE = '0123456789abcdef'


@pytest.mark.parametrize(('name', 'slug'), [
    ('demo-pack', 'demo-pack'), ('My Checkout!', 'my_checkout'), ('UPPER.case', 'upper-case'),
    ('x' * 100, 'x' * 40), ('...', 'checkout'), ('___', 'checkout'), ('-a-', 'a')])
def test_checkout_slug_is_a_lowercase_bounded_compose_name_fragment(name, slug):
    assert checkout_slug(checkout_label(name)) == slug


def test_derived_project_names_the_checkout_and_the_first_eight_hex_of_the_scope():
    assert derived_project(SCOPE, 'Demo-Pack') == 'sa-demo-pack-01234567'
    assert derived_project(SCOPE, 'Demo-Pack') == derived_project(SCOPE, 'Demo-Pack')
    assert derived_project(SCOPE, '') == 'sa-' + SCOPE


@pytest.mark.parametrize('name', ['app', 'My App', 'x' * 200, '...', 'ünï.code', 'a'])
def test_derived_projects_are_valid_bounded_compose_project_names(tmp_path, name):
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / name)

    assert re.fullmatch(r'[a-z0-9][a-z0-9_-]*', scope.project) and len(scope.project) <= 3 + 40 + 1 + 8


def test_same_named_checkouts_have_distinct_projects_and_one_slug(tmp_path):
    first = StackScope.from_roots(tmp_path / 'base', tmp_path / 'one' / 'app')
    second = StackScope.from_roots(tmp_path / 'base', tmp_path / 'two' / 'app')

    assert first.checkout == second.checkout == 'app'
    assert first.project != second.project
    assert first.project.startswith('sa-app-') and second.project.startswith('sa-app-')


def test_a_scope_without_a_checkout_keeps_the_legacy_project_name():
    assert StackScope(SCOPE).project == 'sa-' + SCOPE


def test_accepted_projects_are_the_legacy_name_and_the_one_derived_from_the_labels():
    assert accepted_projects(SCOPE, 'app') == {'sa-' + SCOPE, 'sa-app-01234567'}
    assert accepted_projects(SCOPE, '') == accepted_projects(SCOPE, None) == {'sa-' + SCOPE}
    assert accepted_projects(SCOPE, 7) == {'sa-' + SCOPE}


def owned_labels(scope, **changes):
    labels = {**scope.labels('otel'), 'api-agent.checkout': 'app', 'com.docker.compose.project': scope.project,
              'com.docker.compose.service': 'jaeger'}
    return {**labels, **changes}


@pytest.mark.parametrize('project', ['sa-' + SCOPE, 'sa-app-01234567'])
def test_ownership_accepts_the_legacy_and_the_derived_project(project):
    scope = StackScope(SCOPE, checkout='app')

    assert scope.owns(owned_labels(scope, **{'com.docker.compose.project': project}))


@pytest.mark.parametrize(('changes', 'why'), [
    ({'com.docker.compose.project': 'sa-app-89abcdef'}, 'project derived from another scope'),
    ({'com.docker.compose.project': 'sa-other-01234567'}, 'derived name that does not match the checkout label'),
    ({'com.docker.compose.project': 'sa-App-01234567'}, 'unnormalised derived name'),
    ({'com.docker.compose.project': 'sa-89abcdef01234567'}, 'legacy name of another scope'),
    ({'api-agent.checkout': 'other'}, 'checkout label that does not produce the derived name'),
    ({'api-agent.checkout': ''}, 'derived name without a checkout label'),
    ({'api-agent.scope': '89abcdef01234567'}, 'label says another scope than the project')])
def test_ownership_rejects_a_project_name_the_labels_do_not_reproduce(changes, why):
    scope = StackScope(SCOPE, checkout='app')

    assert not scope.owns(owned_labels(scope, **changes)), why


def test_a_label_override_names_the_stack_without_changing_its_identity(tmp_path):
    state = tmp_path / 'state-3f9a'
    plain = StackScope.from_roots(tmp_path / 'base', state)
    named = StackScope.from_roots(tmp_path / 'base', state, label='my-pack-tools')

    assert plain.checkout == 'state-3f9a'
    assert named.checkout == 'my-pack-tools'
    assert named.id == plain.id and named == plain
    assert named.project == 'sa-my-pack-tools-' + named.id[:8]


def test_attempts_sharing_a_label_still_get_distinct_projects(tmp_path):
    first = StackScope.from_roots(tmp_path / 'base', tmp_path / 'attempt-a', label='my-pack-tools')
    second = StackScope.from_roots(tmp_path / 'base', tmp_path / 'attempt-b', label='my-pack-tools')

    assert first.checkout == second.checkout and first.project != second.project


def test_a_pack_directory_named_demo_gives_a_demo_project(tmp_path):
    scope = StackScope.from_roots(tmp_path, tmp_path / 'demo')

    assert scope.project == f'sa-demo-{scope.id[:8]}'
