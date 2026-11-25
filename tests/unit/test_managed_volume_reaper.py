"""Unused managed volumes go only when nothing references them, no lease protects them and their scope is gone or idle."""
from datetime import datetime, timedelta, timezone
import os
import shutil

import pytest

import api_agent.runtime.prerequisites as p
from api_agent.runtime import scope_index
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.reaper import (_absent, host_paths_visible, list_managed_volumes, reap_unused_managed_resources,
                                      remove_managed_volumes, volume_owner)
from test_managed_reaper import CURRENT, OLD, OTHER, Docker, checkout, live_lease

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
VOLUME = 'sa-aaaaaaaaaaaaaaaa_jaeger-badger'


@pytest.fixture
def docker():
    return Docker()


@pytest.fixture
def scope():
    return StackScope(CURRENT, legacy_ids=('d' * 16,))


def idle_for(ownership, scope_id, days):
    moment = (NOW - timedelta(days=days)).timestamp()
    os.utime(ownership / f'{scope_id}.json', (moment, moment))


def stack(docker, tmp_path, *, scope_id=OLD, idle_days=0, exists=True, journal=None, record=True, names=(VOLUME,)):
    """A scope with unused volumes, a journal last written ``idle_days`` ago, and its recorded location."""
    working = checkout(tmp_path, scope_id, journal={} if journal is None else journal)
    ownership = working.parent
    if exists:
        idle_for(ownership, scope_id, idle_days)
    for name in names:
        docker.volume(name, scope_id)
    if record:
        scope_index.record(scope_id, ownership)
    if not exists:
        shutil.rmtree(ownership.parent.parent)  # the whole checkout is gone
        if record:
            index_idle_for(scope_id, 20)  # and it was last acquired well beyond the retention period
    return ownership


def reap(docker, scope, **options):
    options.setdefault('deadline', 10_000)
    options.setdefault('now', lambda: 0)
    return reap_unused_managed_resources(run=docker.run, prune_network=docker.prune, executable='docker',
        scope=scope, wall_clock=lambda: NOW, identity=lambda pid: 'boot:start', **options)


def volume_commands(docker, verb):
    return [command for command in docker.commands if command[1:3] == ('volume', verb)]


def index_idle_for(scope_id, days):
    moment = (NOW - timedelta(days=days)).timestamp()
    os.utime(scope_index.index_directory() / f'{scope_id}.json', (moment, moment))


def test_an_orphaned_scopes_unused_volumes_are_removed_and_its_record_forgotten(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False, names=(VOLUME, 'sa-aaaaaaaaaaaaaaaa_data'))

    report = reap(docker, scope)

    assert docker.volumes == {} and report.volumes == 2 and report.diagnostic == ''
    assert scope_index.directories(OLD) is None
    assert volume_commands(docker, 'rm') == [('docker', 'volume', 'rm', VOLUME, 'sa-aaaaaaaaaaaaaaaa_data')]


@pytest.mark.parametrize(('idle_days', 'retention', 'removed'), [
    (20, 14, True), (14.001, 14, True), (13.999, 14, False), (0, 14, False), (400, 0, False)])
def test_an_idle_scopes_volumes_go_only_after_the_retention_period_from_its_last_journal_write(
        docker, scope, tmp_path, idle_days, retention, removed):
    stack(docker, tmp_path, idle_days=idle_days)

    reap(docker, scope, retention_days=retention)

    assert (not docker.volumes) is removed


def test_an_aged_scope_keeps_its_record_because_its_directory_still_exists(docker, scope, tmp_path):
    ownership = stack(docker, tmp_path, idle_days=30)

    reap(docker, scope)

    assert docker.volumes == {} and scope_index.directories(OLD) == (ownership,)


def test_the_newest_journal_of_several_recorded_directories_is_the_last_use(docker, scope, tmp_path):
    old = stack(docker, tmp_path, idle_days=90)
    other_engine = checkout(tmp_path, OLD, name='second', journal={}).parent
    idle_for(other_engine, OLD, 1)
    scope_index.record(OLD, other_engine)

    reap(docker, scope)

    assert VOLUME in docker.volumes and old.exists()


def test_a_recorded_directory_that_is_gone_says_nothing_about_the_others(docker, scope, tmp_path):
    stack(docker, tmp_path, idle_days=90)
    scope_index.record(OLD, tmp_path / 'gone' / 'artifacts' / 'prerequisites')

    reap(docker, scope)

    assert docker.volumes == {}


@pytest.mark.parametrize(('idle_days', 'retention', 'removed'), [
    (20, 14, True), (14.001, 14, True), (13.999, 14, False), (0, 14, False), (0, 0, True), (400, 0, True)])
def test_a_deleted_checkout_still_waits_out_the_retention_period_from_its_last_acquire(
        docker, scope, tmp_path, idle_days, retention, removed):
    stack(docker, tmp_path, exists=False)
    index_idle_for(OLD, idle_days)

    reap(docker, scope, retention_days=retention)

    assert (not docker.volumes) is removed


def test_a_scope_record_that_vanishes_before_its_time_is_read_preserves_the_volumes(docker, scope, tmp_path, monkeypatch):
    stack(docker, tmp_path, exists=False)
    monkeypatch.setattr(scope_index, 'modified', lambda scope_id: None)

    reap(docker, scope, retention_days=14)

    assert docker.volumes


def test_a_scope_with_a_directory_but_no_journal_is_preserved(docker, scope, tmp_path):
    ownership = stack(docker, tmp_path, idle_days=90)
    (ownership / f'{OLD}.json').unlink()

    report = reap(docker, scope)

    assert VOLUME in docker.volumes and not volume_commands(docker, 'rm') and report.volumes == 0


def test_an_unreadable_journal_time_preserves_the_scope(docker, scope, tmp_path, monkeypatch):
    ownership = stack(docker, tmp_path, idle_days=90)
    real = os.stat
    def denied(path, *args, **kwargs):
        if str(path) == str(ownership / f'{OLD}.json'):
            raise PermissionError('private')
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', denied)

    reap(docker, scope)

    assert VOLUME in docker.volumes


def test_the_orphan_rule_requires_host_paths_to_be_visible(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False)

    reap(docker, scope, host_paths_visible=False)

    assert VOLUME in docker.volumes


def test_an_unverifiable_absence_is_not_an_orphan_proof(docker, scope, tmp_path, monkeypatch):
    ownership = stack(docker, tmp_path, exists=False)
    real = os.stat
    def denied(path, *args, **kwargs):
        if str(path) in {str(ownership), str(ownership / f'{OLD}.json')}:
            raise PermissionError('private')
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', denied)

    reap(docker, scope)

    assert VOLUME in docker.volumes


def test_a_volume_that_a_container_mounts_is_never_removed_even_when_orphaned(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False, names=(VOLUME, 'sa-aaaaaaaaaaaaaaaa_data'))
    docker.container(OLD, working_dir=None, status='exited', mounts=(VOLUME,))

    report = reap(docker, scope)

    assert set(docker.volumes) == {VOLUME} and report.volumes == 1
    assert volume_commands(docker, 'rm') == [('docker', 'volume', 'rm', 'sa-aaaaaaaaaaaaaaaa_data')]


def test_a_volume_referenced_while_the_scope_is_locked_is_not_removed(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False)
    run = docker.run
    checks = []
    def late_container(command, deadline=None):
        if command[1] == 'ps' and any(part.startswith('volume=') for part in command):
            checks.append(command)
            if len(checks) == 2:
                docker.container(OLD, working_dir=None, mounts=(VOLUME,))
        return run(command, deadline)
    docker.run = late_container

    report = reap(docker, scope)

    assert len(checks) == 2 and VOLUME in docker.volumes and report.volumes == 0


def test_a_live_lease_protects_the_scopes_volumes(docker, scope, tmp_path):
    stack(docker, tmp_path, idle_days=90, journal=live_lease('9' * 64))

    report = reap(docker, scope)

    assert VOLUME in docker.volumes and report.volumes == 0


def test_a_lease_whose_owner_is_gone_does_not_protect_the_volumes(docker, scope, tmp_path):
    stack(docker, tmp_path, idle_days=90, journal={'invocation': {
        'pid': os.getpid(), 'identity': 'previous-boot:start', 'ids': ['9' * 64]}})

    reap(docker, scope)

    assert docker.volumes == {}


@pytest.mark.parametrize('journal', ['{', '[]'])
def test_an_unreadable_journal_preserves_the_volumes(docker, scope, tmp_path, journal):
    ownership = stack(docker, tmp_path, idle_days=90)
    (ownership / f'{OLD}.json').write_text(journal)

    report = reap(docker, scope)

    assert VOLUME in docker.volumes and report.diagnostic == 'incomplete: PrerequisiteError'


def test_a_held_lock_preserves_the_volumes(docker, scope, tmp_path, monkeypatch):
    stack(docker, tmp_path, idle_days=90)
    def busy(*args, **kwargs):
        raise p.PrerequisiteError('Prerequisite deadline exceeded.')
    monkeypatch.setattr(p, 'guarded_cleanup', busy)

    reap(docker, scope)

    assert VOLUME in docker.volumes


def test_every_recorded_journal_is_locked_and_any_lease_protects_the_scope(docker, scope, tmp_path):
    stack(docker, tmp_path, idle_days=90)
    leased = checkout(tmp_path, OLD, name='second', journal=live_lease('9' * 64)).parent
    scope_index.record(OLD, leased)

    reap(docker, scope)

    assert VOLUME in docker.volumes


def test_a_scope_with_no_recorded_location_is_preserved(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False, record=False)

    report = reap(docker, scope)

    assert VOLUME in docker.volumes and report.volumes == 0 and report.diagnostic == ''


def test_the_callers_own_and_previous_path_based_scopes_are_never_aged_out(docker, scope, tmp_path):
    for identifier in (CURRENT, 'd' * 16):
        stack(docker, tmp_path, scope_id=identifier, exists=False, names=(f'sa-{identifier}_data',))

    report = reap(docker, scope)

    assert len(docker.volumes) == 2 and report.volumes == 0


@pytest.mark.parametrize('case', ['unmanaged', 'no scope', 'bad scope', 'no project', 'other scope project',
                                  'foreign project', 'no profile', 'name is not project-prefixed', 'bare prefix',
                                  'null labels', 'foreign name'])
def test_volumes_without_complete_ownership_are_never_removed(docker, scope, tmp_path, case):
    stack(docker, tmp_path, exists=False, names=())
    good = {'api-agent.managed': 'true', 'api-agent.scope': OLD, 'api-agent.profile': 'otel',
            'com.docker.compose.project': 'sa-' + OLD}
    name, labels = {
        'unmanaged': ('sa-aaaaaaaaaaaaaaaa_x', {**good, 'api-agent.managed': 'false'}),
        'no scope': ('sa-aaaaaaaaaaaaaaaa_x', {k: v for k, v in good.items() if k != 'api-agent.scope'}),
        'bad scope': ('sa-aaaaaaaaaaaaaaaa_x', {**good, 'api-agent.scope': 'XYZ'}),
        'no project': ('sa-aaaaaaaaaaaaaaaa_x', {k: v for k, v in good.items() if k != 'com.docker.compose.project'}),
        'other scope project': ('sa-bbbbbbbbbbbbbbbb_x', {**good, 'com.docker.compose.project': 'sa-' + OTHER}),
        'foreign project': ('foreign_x', {**good, 'com.docker.compose.project': 'foreign'}),
        'no profile': ('sa-aaaaaaaaaaaaaaaa_x', {k: v for k, v in good.items() if k != 'api-agent.profile'}),
        'name is not project-prefixed': ('data', good),
        'bare prefix': ('sa-aaaaaaaaaaaaaaaa_', good),
        'null labels': ('sa-aaaaaaaaaaaaaaaa_x', None),
        'foreign name': ('other_sa-aaaaaaaaaaaaaaaa_x', good)}[case]
    docker.volumes[name] = labels
    run = docker.run
    def listed(command, deadline=None):
        return name if command[1:3] == ('volume', 'ls') else run(command, deadline)
    docker.run = listed

    report = reap(docker, scope)

    assert name in docker.volumes and report.volumes == 0


@pytest.mark.parametrize('project', ['sa-aaaaaaaaaaaaaaaa', 'sa-app-aaaaaaaa', 'sa-my_app-9-aaaaaaaa'])
def test_legacy_and_derived_project_names_both_own_a_volume(docker, scope, tmp_path, project):
    stack(docker, tmp_path, exists=False, names=())
    docker.volume(project + '_data', OLD, project=project)

    assert reap(docker, scope).volumes == 1


@pytest.mark.parametrize('project', ['sa-app-bbbbbbbb', 'sa-App-aaaaaaaa', 'sa--aaaaaaaa', 'sa-app-aaaaaaaab',
                                     'sa-aaaaaaaa', 'sa-app-aaaaaaaa-x'])
def test_a_project_that_does_not_embed_the_scopes_prefix_does_not_own_a_volume(docker, scope, tmp_path, project):
    stack(docker, tmp_path, exists=False, names=())
    docker.volume(project + '_data', OLD, project=project)

    assert reap(docker, scope).volumes == 0 and project + '_data' in docker.volumes


def test_volume_removal_is_batched_and_never_forced(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False, names=tuple(f'sa-aaaaaaaaaaaaaaaa_v{index}' for index in range(70)))

    report = reap(docker, scope)

    assert docker.volumes == {} and report.volumes == 70
    assert [len(command) - 3 for command in volume_commands(docker, 'rm')] == [32, 32, 6]
    assert [len(command) - 3 for command in volume_commands(docker, 'inspect')] == [70]


def test_a_partially_used_orphan_keeps_its_location_record(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False, names=(VOLUME, 'sa-aaaaaaaaaaaaaaaa_data'))
    docker.container(OLD, working_dir=None, mounts=('sa-aaaaaaaaaaaaaaaa_data',))

    reap(docker, scope)

    assert scope_index.directories(OLD) is not None


def test_containers_removed_for_a_scope_leave_its_journal_location_behind(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, journal={})
    docker.container(OLD, working_dir=working)

    reap(docker, scope)

    assert docker.containers == {} and scope_index.directories(OLD) == (working.parent,)


@pytest.mark.parametrize('listing', ['owned\nowned', '--owned', 'a/b'])
def test_invalid_volume_listings_remove_no_volume(docker, scope, tmp_path, listing):
    stack(docker, tmp_path, exists=False)
    run = docker.run
    docker.run = lambda command, deadline=None: listing if command[1:3] == ('volume', 'ls') else run(command, deadline)

    report = reap(docker, scope)

    assert VOLUME in docker.volumes and report.diagnostic == 'incomplete: ValueError'


@pytest.mark.parametrize('answer', ['not-json', '{}', '[]', '[null]', '[{"Name": "other", "Labels": {}}]'])
def test_unverifiable_volume_inspection_removes_no_volume(docker, scope, tmp_path, answer):
    stack(docker, tmp_path, exists=False)
    run = docker.run
    docker.run = lambda command, deadline=None: answer if command[1:3] == ('volume', 'inspect') else run(command, deadline)

    report = reap(docker, scope)

    assert VOLUME in docker.volumes and report.diagnostic


def test_a_failed_volume_removal_is_reported_and_the_next_scope_is_still_reaped(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False)
    stack(docker, tmp_path, scope_id=OTHER, exists=False, names=('sa-bbbbbbbbbbbbbbbb_data',))
    run = docker.run
    def failing(command, deadline=None):
        if command[1:3] == ('volume', 'rm') and VOLUME in command:
            docker.commands.append(command)
            raise p.PrerequisiteError('/private/path secret')
        return run(command, deadline)
    docker.run = failing

    report = reap(docker, scope)

    assert VOLUME in docker.volumes and 'sa-bbbbbbbbbbbbbbbb_data' not in docker.volumes
    assert report.volumes == 1 and report.diagnostic == 'incomplete: PrerequisiteError'


def test_a_volume_check_that_fails_is_reported_without_removing_anything(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False)
    run = docker.run
    docker.run = lambda command, deadline=None: ('not-an-id' if command[1] == 'ps' and any(
        part.startswith('volume=') for part in command) else run(command, deadline))

    report = reap(docker, scope)

    assert VOLUME in docker.volumes and report.diagnostic == 'incomplete: ValueError'


def test_cancellation_inside_the_volume_pass_stops_it(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False)

    report = reap(docker, scope, cancelled=lambda: len(docker.commands) >= 4)

    assert VOLUME in docker.volumes and report.diagnostic == 'incomplete: cancelled'


def test_an_expired_deadline_ends_the_volume_pass_before_any_removal(docker, scope, tmp_path):
    stack(docker, tmp_path, exists=False)

    report = reap(docker, scope, deadline=35, now=lambda: 10 * len(docker.commands))

    assert VOLUME in docker.volumes and report.diagnostic == 'incomplete: deadline'


@pytest.mark.parametrize(('environment', 'session', 'marker', 'visible'), [
    ({}, frozenset(), False, True),
    ({'DOCKER_HOST': 'unix:///run/docker.sock'}, frozenset(), False, True),
    ({'DOCKER_HOST': 'ssh://builder'}, frozenset(), False, False),
    ({'CONTAINER_HOST': 'tcp://10.0.0.5:2375'}, frozenset(), False, False),
    ({}, frozenset({'session'}), False, False),
    ({}, frozenset(), True, False)])
def test_host_paths_are_visible_only_to_a_local_non_containerised_session(
        monkeypatch, tmp_path, environment, session, marker, visible):
    import api_agent.runtime.reaper as reaper
    monkeypatch.setattr(reaper, 'session_container_identity', lambda: session)
    monkeypatch.setattr(reaper, '_CONTAINER_MARKER', str(tmp_path / '.dockerenv'))
    if marker:
        (tmp_path / '.dockerenv').touch()

    assert host_paths_visible(environment) is visible


def test_session_identity_failure_means_paths_are_not_visible(monkeypatch):
    import api_agent.runtime.reaper as reaper
    from api_agent.runtime.containers import ContainerError
    def failing():
        raise ContainerError('unknown')
    monkeypatch.setattr(reaper, 'session_container_identity', failing)

    assert host_paths_visible({}) is False


def test_absence_is_proven_only_by_the_filesystem_saying_so(tmp_path, monkeypatch):
    (tmp_path / 'file').write_text('x')
    real = os.stat
    def denied(path, *args, **kwargs):
        if str(path).endswith('private'):
            raise PermissionError('private')
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', denied)

    assert _absent(tmp_path / 'missing') and _absent(tmp_path / 'file' / 'below')
    assert not _absent(tmp_path) and not _absent(tmp_path / 'private')


@pytest.mark.parametrize('record', [{}, {'Name': 1, 'Labels': {}}, {'Name': 'x', 'Labels': 'x'}])
def test_records_without_a_name_and_labels_have_no_owner(record):
    assert volume_owner(record) is None


def inventory(docker, scope, **options):
    return list_managed_volumes(run=docker.run, executable='docker', scope=scope, deadline=10_000,
                                now=lambda: 0, **options)


def test_listing_reports_each_owned_volume_with_its_project_profile_and_use(docker, scope, tmp_path):
    docker.volume('sa-app-aaaaaaaa_jaeger-badger', OLD, project='sa-app-aaaaaaaa')
    docker.volume('sa-aaaaaaaaaaaaaaaa_data', OLD, profile='localstack')
    docker.volume('unowned', OLD, labels={'api-agent.managed': 'true'})
    docker.container(OLD, working_dir=None, mounts=('sa-aaaaaaaaaaaaaaaa_data',))

    listed = inventory(docker, scope)

    assert [(item.name, item.scope, item.project, item.profile, item.in_use) for item in listed] == [
        ('sa-app-aaaaaaaa_jaeger-badger', OLD, 'sa-app-aaaaaaaa', 'otel', False),
        ('sa-aaaaaaaaaaaaaaaa_data', OLD, 'sa-aaaaaaaaaaaaaaaa', 'localstack', True)]


def test_listing_failures_are_raised_not_hidden(docker, scope):
    docker.fail = ('volume', 'ls')

    with pytest.raises(p.PrerequisiteError):
        inventory(docker, scope)


def select(docker, scope, tmp_path, names, **options):
    options.setdefault('deadline', 10_000)
    return remove_managed_volumes(run=docker.run, executable='docker', scope=scope, names=names,
        ownership_directory=tmp_path / 'mine' / 'artifacts' / 'prerequisites', now=lambda: 0,
        identity=lambda pid: 'boot:start', **options)


def test_selected_unused_volumes_are_removed_without_the_age_or_orphan_rule(docker, scope, tmp_path):
    stack(docker, tmp_path, idle_days=0, names=(VOLUME, 'sa-aaaaaaaaaaaaaaaa_data'))

    report = select(docker, scope, tmp_path, [VOLUME])

    assert set(docker.volumes) == {'sa-aaaaaaaaaaaaaaaa_data'} and report.volumes == 1


def test_a_selected_volume_that_a_container_mounts_is_refused(docker, scope, tmp_path):
    stack(docker, tmp_path)
    docker.container(OLD, working_dir=None, mounts=(VOLUME,))

    report = select(docker, scope, tmp_path, [VOLUME])

    assert VOLUME in docker.volumes and report.volumes == 0 and not volume_commands(docker, 'rm')


def test_a_live_lease_protects_selected_volumes(docker, scope, tmp_path):
    stack(docker, tmp_path, journal=live_lease('9' * 64))

    report = select(docker, scope, tmp_path, [VOLUME])

    assert VOLUME in docker.volumes and report.volumes == 0


def test_a_selected_volume_of_an_unknown_location_is_preserved(docker, scope, tmp_path):
    stack(docker, tmp_path, record=False)

    report = select(docker, scope, tmp_path, [VOLUME])

    assert VOLUME in docker.volumes and report.volumes == 0


def test_a_selected_volume_without_complete_ownership_is_ignored(docker, scope, tmp_path):
    stack(docker, tmp_path)
    docker.volumes['unowned'] = {'api-agent.managed': 'true'}
    run = docker.run
    docker.run = lambda command, deadline=None: (
        'unowned' if command[1:3] == ('volume', 'ls') else run(command, deadline))

    report = select(docker, scope, tmp_path, ['unowned'])

    assert 'unowned' in docker.volumes and report.volumes == 0 and not volume_commands(docker, 'rm')


def test_the_callers_own_volumes_can_be_removed_under_its_own_journal(docker, scope, tmp_path):
    own = tmp_path / 'mine' / 'artifacts' / 'prerequisites'
    own.mkdir(parents=True)
    p._write_journal(own, StackScope(CURRENT), {})
    docker.volume(f'sa-{CURRENT}_data', CURRENT)

    report = select(docker, scope, tmp_path, [f'sa-{CURRENT}_data'])

    assert docker.volumes == {} and report.volumes == 1


def test_the_callers_own_volumes_are_protected_by_its_own_live_lease(docker, scope, tmp_path):
    own = tmp_path / 'mine' / 'artifacts' / 'prerequisites'
    own.mkdir(parents=True)
    p._write_journal(own, StackScope(CURRENT), live_lease('9' * 64))
    docker.volume(f'sa-{CURRENT}_data', CURRENT)

    report = select(docker, scope, tmp_path, [f'sa-{CURRENT}_data'])

    assert docker.volumes and report.volumes == 0


def test_removal_of_selected_volumes_reports_engine_failures_without_raising(docker, scope, tmp_path):
    stack(docker, tmp_path)
    docker.fail = ('volume', 'rm')

    report = select(docker, scope, tmp_path, [VOLUME])

    assert VOLUME in docker.volumes and report.diagnostic == 'incomplete: PrerequisiteError'
