"""Unused managed resources are reaped; anything possibly in use is preserved."""
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from types import SimpleNamespace

import pytest

import api_agent.runtime.prerequisites as p
import api_agent.runtime.reaper as reaper
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.reaper import ReapReport, host_paths_visible, reap_unused_managed_resources

CURRENT = 'c' * 16
OLD = 'a' * 16
OTHER = 'b' * 16
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def stamp(days_ago, nanoseconds='123456789'):
    moment = NOW - timedelta(days=days_ago)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + nanoseconds + 'Z'


class Docker:
    """A tiny engine whose only inventory is what each test places in it."""

    def __init__(self):
        self.containers, self.volumes, self.commands, self.pruned = {}, {}, [], []
        self.fail = None
        self.listing = None
        self.mangle = None
        self.counter = 0

    def container(self, scope, *, working_dir, status='exited', finished_days=0, labels=None, project=None,
                  service='jaeger'):
        self.counter += 1
        identifier = f'{self.counter:064x}'
        merged = {'api-agent.managed': 'true', 'api-agent.scope': scope, 'api-agent.profile': 'otel',
                  'com.docker.compose.project': project or 'sa-' + scope,
                  'com.docker.compose.service': service}
        if working_dir is not None:
            merged['com.docker.compose.project.working_dir'] = str(working_dir)
        merged.update(labels or {})
        finished = '0001-01-01T00:00:00Z' if status == 'created' else stamp(finished_days)
        self.containers[identifier] = {'id': identifier, 'status': status, 'running': status == 'running',
            'finished': finished, 'created': stamp(finished_days + 1), 'labels': merged}
        return identifier

    def volume(self, name, scope, *, labels=None):
        owned = {'api-agent.managed': 'true', 'api-agent.scope': scope, 'api-agent.profile': 'otel',
                 'com.docker.compose.project': 'sa-' + scope}
        self.volumes[name] = owned if labels is None else labels

    def run(self, command, deadline=None):
        self.commands.append(command)
        if self.fail is not None and command[1:3] == self.fail:
            raise p.PrerequisiteError('engine refused')
        arguments = command[1:]
        if arguments[0] == 'ps':
            if self.listing is not None:
                return self.listing
            return '\n'.join(self.containers)
        if arguments[0] == 'inspect':
            rows = [json.dumps({key: value for key, value in self.containers[item].items()})
                    for item in arguments[3:]]
            return self.mangle(rows) if self.mangle else '\n'.join(rows)
        if arguments[0] == 'rm':
            for item in arguments[1:]:
                del self.containers[item]
            return ''
        if arguments[:2] == ('volume', 'ls'):
            project = next(value.rsplit('=', 1)[1] for value in arguments if value.startswith('label=com.docker.compose.project='))
            return '\n'.join(name for name, labels in self.volumes.items()
                             if (labels or {}).get('com.docker.compose.project') == project)
        if arguments[:2] == ('volume', 'inspect'):
            return json.dumps([{'Name': name, 'Labels': self.volumes[name]} for name in arguments[2:]])
        if arguments[:2] == ('volume', 'rm'):
            for name in arguments[2:]:
                del self.volumes[name]
            return ''
        raise AssertionError(command)

    def prune(self, scope_id, deadline):
        self.pruned.append(scope_id)
        return ('n' * 64,)


@pytest.fixture
def docker():
    return Docker()


@pytest.fixture
def scope():
    return StackScope(CURRENT, legacy_ids=('d' * 16,))


def checkout(tmp_path, scope_id, *, name=None, exists=True, journal=None, snapshot='abc12345'):
    """Return the recorded compose directory for a scope's retained snapshot."""
    ownership = tmp_path / (name or scope_id) / 'artifacts' / 'prerequisites'
    working = ownership / f'{scope_id}-assets-{snapshot}'
    if exists:
        working.mkdir(parents=True)
    if journal is not None:
        ownership.mkdir(parents=True, exist_ok=True)
        p._write_journal(ownership, SimpleNamespace(id=scope_id), journal)
    return working


def reap(docker, scope, **options):
    options.setdefault('retention_days', 14)
    options.setdefault('deadline', 10_000)
    options.setdefault('now', lambda: 0)
    return reap_unused_managed_resources(run=docker.run, prune_network=docker.prune, executable='docker',
        scope=scope, wall_clock=lambda: NOW, identity=lambda pid: 'boot:start', **options)


def removed(docker, command_name):
    return [item for command in docker.commands if command[1] == command_name for item in command[2:]]


def test_orphan_scope_is_reaped_with_its_volumes_and_network(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, exists=False)
    first = docker.container(OLD, working_dir=working)
    second = docker.container(OLD, working_dir=working, status='created', service='collector')
    docker.volume('sa-aaaaaaaaaaaaaaaa_jaeger-badger', OLD)

    report = reap(docker, scope)

    assert docker.containers == {} and docker.volumes == {}
    assert docker.pruned == [OLD]
    assert (report.scopes, report.containers, report.volumes, report.networks, report.diagnostic) == (1, 2, 1, 1, '')
    assert removed(docker, 'rm') == [first, second]
    order = [command[1] if command[1] != 'volume' else 'volume-' + command[2] for command in docker.commands]
    assert order.index('rm') < order.index('volume-rm')


def test_scope_with_a_running_container_is_untouched_even_when_orphaned(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, exists=False)
    docker.container(OLD, working_dir=working)
    docker.container(OLD, working_dir=working, status='running')
    docker.volume('sa-aaaaaaaaaaaaaaaa_data', OLD)

    report = reap(docker, scope)

    assert len(docker.containers) == 2 and docker.volumes and docker.pruned == []
    assert report.scopes == 0
    assert not any(command[1] in {'rm', 'volume'} for command in docker.commands)


@pytest.mark.parametrize('status', ['paused', 'restarting', 'removing'])
def test_other_live_states_also_protect_the_scope(docker, scope, tmp_path, status):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False), status=status)

    reap(docker, scope)

    assert len(docker.containers) == 1


def test_live_lease_from_another_checkout_protects_the_scope(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=None)
    working = checkout(tmp_path, OLD, exists=False, journal={'invocation': {'pid': os.getpid(),
        'identity': 'boot:start', 'ids': [container]}})
    docker.containers[container]['labels']['com.docker.compose.project.working_dir'] = str(working)

    report = reap(docker, scope)

    assert container in docker.containers and report.scopes == 0
    assert not any(command[1] == 'rm' for command in docker.commands)


def test_lease_with_a_dead_owner_does_not_protect_the_scope(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=None)
    working = checkout(tmp_path, OLD, exists=False, journal={'invocation': {'pid': os.getpid(),
        'identity': 'previous-boot:start', 'ids': [container]}})
    docker.containers[container]['labels']['com.docker.compose.project.working_dir'] = str(working)

    reap(docker, scope)

    assert docker.containers == {}


@pytest.mark.parametrize('journal', ['{', '[]'])
def test_unreadable_journal_preserves_the_scope(docker, scope, tmp_path, journal):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    ownership = tmp_path / OLD / 'artifacts' / 'prerequisites'
    ownership.mkdir(parents=True)
    (ownership / f'{OLD}.json').write_text(journal)

    report = reap(docker, scope)

    assert container in docker.containers and report.scopes == 0


def test_lock_held_by_another_process_preserves_the_scope(docker, scope, tmp_path, monkeypatch):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    (tmp_path / OLD / 'artifacts' / 'prerequisites').mkdir(parents=True, exist_ok=True)
    def busy(*args, **kwargs):
        raise p.PrerequisiteError('Prerequisite deadline exceeded.')
    monkeypatch.setattr(p, 'guarded_cleanup', busy)

    reap(docker, scope)

    assert container in docker.containers


def test_current_and_legacy_scopes_are_never_reaped_even_when_orphaned_and_aged(docker, scope, tmp_path):
    for identifier in (CURRENT, 'd' * 16):
        docker.container(identifier, working_dir=checkout(tmp_path, identifier, exists=False), finished_days=400)

    report = reap(docker, scope)

    assert len(docker.containers) == 2 and report.scopes == 0
    assert [command[1] for command in docker.commands] == ['ps', 'inspect']


@pytest.mark.parametrize(('stopped_days', 'retention', 'reaped'), [
    (20, 14, True), (14.001, 14, True), (13.999, 14, False), (400, 0, False)])
def test_age_rule_reaps_only_after_the_retention_period(docker, scope, tmp_path, stopped_days, retention, reaped):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD), finished_days=stopped_days)

    reap(docker, scope, retention_days=retention)

    assert (not docker.containers) is reaped


def test_scope_age_is_its_most_recent_stop(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD)
    docker.container(OLD, working_dir=working, finished_days=90)
    docker.container(OLD, working_dir=working, finished_days=2)

    reap(docker, scope)

    assert len(docker.containers) == 2


def test_never_started_container_ages_from_its_creation(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD), status='created', finished_days=30)

    reap(docker, scope)

    assert docker.containers == {}


def test_zero_retention_disables_only_the_age_rule(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD), finished_days=900)
    orphan = docker.container(OTHER, working_dir=checkout(tmp_path, OTHER, exists=False), finished_days=0)

    reap(docker, scope, retention_days=0)

    assert orphan not in docker.containers and len(docker.containers) == 1


def test_aged_scope_with_an_unrecognised_layout_cannot_prove_it_is_unleased(docker, scope, tmp_path):
    elsewhere = tmp_path / 'unknown-layout'
    elsewhere.mkdir()
    docker.container(OLD, working_dir=elsewhere, finished_days=900)
    docker.container(OTHER, working_dir=tmp_path / 'gone' / 'compose', finished_days=900)

    reap(docker, scope)

    assert len(docker.containers) == 2


def test_scope_created_by_several_acquisitions_is_an_orphan_when_every_snapshot_directory_is_gone(docker, scope, tmp_path):
    for snapshot in ('first111', 'second22'):
        docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False, snapshot=snapshot))

    report = reap(docker, scope)

    assert docker.containers == {} and report.containers == 2


def test_scope_with_a_snapshot_directory_still_present_is_not_an_orphan(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False, snapshot='first111'))
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, snapshot='second22'))

    reap(docker, scope)

    assert len(docker.containers) == 2


def test_containers_recording_different_ownership_directories_are_preserved(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False), finished_days=900)
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, name='moved', exists=False), finished_days=900)

    reap(docker, scope)

    assert len(docker.containers) == 2


def test_container_without_a_recorded_directory_preserves_its_scope(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False), finished_days=900)
    docker.container(OLD, working_dir=None, finished_days=900)

    reap(docker, scope)

    assert len(docker.containers) == 2


def test_a_relative_working_directory_is_not_an_orphan_proof(docker, scope):
    docker.container(OLD, working_dir='relative/compose')

    reap(docker, scope)

    assert len(docker.containers) == 1


def test_unverifiable_working_directory_existence_preserves_the_scope(docker, scope, tmp_path, monkeypatch):
    working = checkout(tmp_path, OLD, exists=False)
    docker.container(OLD, working_dir=working)
    real = os.stat
    def denied(path, *args, **kwargs):
        if str(path) == str(working):
            raise PermissionError('private')
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, 'stat', denied)

    reap(docker, scope)

    assert len(docker.containers) == 1


def test_orphan_rule_requires_host_paths_to_be_visible(docker, scope, tmp_path):
    orphan = docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    aged = docker.container(OTHER, working_dir=checkout(tmp_path, OTHER), finished_days=30)

    reap(docker, scope, host_paths_visible=False)

    assert orphan in docker.containers and aged not in docker.containers


def test_volumes_without_complete_ownership_are_never_removed(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.volume('owned', OLD)
    docker.volume('unlabelled', OLD, labels={'com.docker.compose.project': 'sa-' + OLD})
    docker.volume('other-scope', OLD, labels={'api-agent.managed': 'true', 'api-agent.scope': OTHER,
        'api-agent.profile': 'otel', 'com.docker.compose.project': 'sa-' + OLD})
    docker.volume('other-project', OLD, labels={'api-agent.managed': 'true', 'api-agent.scope': OLD,
        'api-agent.profile': 'otel', 'com.docker.compose.project': 'sa-' + OTHER})
    docker.volume('no-profile', OLD, labels={'api-agent.managed': 'true', 'api-agent.scope': OLD,
        'com.docker.compose.project': 'sa-' + OLD})
    docker.volume('null-labels', OLD, labels={})
    docker.volumes['null-labels'] = None
    docker.volume('unmanaged', OLD, labels={'api-agent.managed': 'false', 'api-agent.scope': OLD,
        'api-agent.profile': 'otel', 'com.docker.compose.project': 'sa-' + OLD})
    docker.volume('foreign-project', OTHER)

    report = reap(docker, scope)

    assert set(docker.volumes) == {'unlabelled', 'other-scope', 'other-project', 'no-profile', 'null-labels',
                                   'unmanaged', 'foreign-project'}
    assert report.volumes == 1
    assert removed(docker, 'rm') != []


@pytest.mark.parametrize('listing', ['owned\nowned', '--owned', 'a/b', ''])
def test_invalid_or_empty_volume_listings_remove_no_volume(docker, scope, tmp_path, listing):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.volume('owned', OLD)
    run = docker.run
    def listed(command, deadline=None):
        return listing if command[1:3] == ('volume', 'ls') else run(command, deadline)
    docker.run = listed

    reap(docker, scope)

    assert 'owned' in docker.volumes
    assert not any(command[1:3] == ('volume', 'rm') for command in docker.commands)


@pytest.mark.parametrize('answer', ['not-json', '{}', '[]', '[null]', '[{"Name": "other", "Labels": {}}]'])
def test_unverifiable_volume_inspection_preserves_every_volume_and_the_network(docker, scope, tmp_path, answer):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.volume('owned', OLD)
    run = docker.run
    def inspected(command, deadline=None):
        return answer if command[1:3] == ('volume', 'inspect') else run(command, deadline)
    docker.run = inspected

    report = reap(docker, scope)

    assert 'owned' in docker.volumes and docker.pruned == []
    assert report.diagnostic


@pytest.mark.parametrize('mangle', [
    lambda rows: 'not-json',
    lambda rows: '\n'.join(rows[:-1]),
    lambda rows: '\n'.join(['[]'] * len(rows)),
    lambda rows: '\n'.join(row.replace('"id": "', '"id": "9') for row in rows),
    lambda rows: '\n'.join(row.replace('"labels"', '"tags"') for row in rows),
    lambda rows: '\n'.join(row.replace('"running": false', '"running": "no"') for row in rows),
    lambda rows: '\n'.join(row.replace('"status": "exited"', '"status": 3') for row in rows),
    lambda rows: '\n'.join(row.replace('"finished": "', '"finished": "tomorrow ') for row in rows),
    lambda rows: '\n'.join(row.replace('+', '-').replace('Z"', '"') for row in rows),
])
def test_unverifiable_inventory_preserves_everything(docker, scope, tmp_path, mangle):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False), finished_days=900)
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False), finished_days=900)
    docker.mangle = mangle

    report = reap(docker, scope)

    assert len(docker.containers) == 2
    assert not any(command[1] in {'rm', 'volume'} for command in docker.commands)
    assert report.diagnostic and report.scopes == 0


def test_inventory_with_labels_that_cannot_be_attributed_is_skipped_and_blocks_its_scope(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, exists=False)
    docker.container(OLD, working_dir=working)
    docker.container(OLD, working_dir=working, project='sa-' + OTHER)
    docker.container(OTHER, working_dir=checkout(tmp_path, OTHER, exists=False), labels={'api-agent.managed': 'false'})
    docker.container('not-a-scope', working_dir=working, project='sa-not-a-scope')
    docker.containers[next(reversed(docker.containers))]['labels'] = None
    unattributed = docker.container(OTHER, working_dir=working, project='foreign')
    docker.container('e' * 16, working_dir=checkout(tmp_path, 'e' * 16, exists=False))

    report = reap(docker, scope)

    assert len(docker.containers) == 5 and unattributed in docker.containers
    assert report.scopes == 1 and report.containers == 1


@pytest.mark.parametrize('failing', [('ps', '-a'), ('inspect', '--format')])
def test_listing_failure_never_raises_and_preserves_everything(docker, scope, tmp_path, failing):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.fail = failing

    report = reap(docker, scope)

    assert len(docker.containers) == 1 and report.diagnostic == 'incomplete: PrerequisiteError'


@pytest.mark.parametrize('listing', ['not-an-id', ('1' * 64 + '\n') * 2])
def test_invalid_container_ids_are_not_inspected(docker, scope, tmp_path, listing):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.listing = listing

    report = reap(docker, scope)

    assert [command[1] for command in docker.commands] == ['ps'] and report.diagnostic


def test_removal_failure_is_reported_and_other_scopes_are_still_reaped(docker, scope, tmp_path, caplog):
    first = docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    second = docker.container(OTHER, working_dir=checkout(tmp_path, OTHER, exists=False))
    run = docker.run
    def failing(command, deadline=None):
        if command[1] == 'rm' and first in command:
            docker.commands.append(command)
            raise p.PrerequisiteError('/private/path secret')
        return run(command, deadline)
    docker.run = failing

    with caplog.at_level(logging.INFO, logger='api_agent.runtime.reaper'):
        report = reap(docker, scope)

    assert first in docker.containers and second not in docker.containers
    assert (report.scopes, report.containers, report.diagnostic) == (1, 1, 'incomplete: PrerequisiteError')
    assert 'secret' not in caplog.text and '/private' not in caplog.text
    assert 'failed' in caplog.text and 'removed 1 scope' in caplog.text


def test_network_prune_failure_is_best_effort(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    def failing(scope_id, deadline):
        raise p.PrerequisiteError('network inventory')

    report = reap_unused_managed_resources(run=docker.run, prune_network=failing, executable='docker',
        scope=scope, retention_days=14, deadline=10_000, now=lambda: 0, wall_clock=lambda: NOW)

    assert docker.containers == {} and report.diagnostic == 'incomplete: PrerequisiteError'


def test_a_cleanly_idle_engine_issues_only_the_listing(docker, scope):
    report = reap(docker, scope)

    assert docker.commands == [('docker', 'ps', '-a', '--no-trunc', '--filter', 'label=api-agent.managed=true',
                                '--format', '{{.ID}}')]
    assert report == ReapReport()


def test_only_current_scope_resources_cost_one_listing_and_one_inspection(docker, scope, tmp_path):
    docker.container(CURRENT, working_dir=checkout(tmp_path, CURRENT, exists=False), finished_days=900)

    reap(docker, scope)

    assert [command[1] for command in docker.commands] == ['ps', 'inspect']


def test_removals_are_batched(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, exists=False)
    for _ in range(70):
        docker.container(OLD, working_dir=working)
    for index in range(70):
        docker.volume(f'volume-{index}', OLD)

    report = reap(docker, scope)

    assert docker.containers == {} and docker.volumes == {}
    assert [len(command) - 2 for command in docker.commands if command[1] == 'rm'] == [32, 32, 6]
    assert [len(command) - 3 for command in docker.commands if command[1:3] == ('volume', 'rm')] == [32, 32, 6]
    assert [len(command) - 4 for command in docker.commands if command[1] == 'inspect'] == [70, 70]
    assert report.containers == 70 and report.volumes == 70


def test_inspection_is_batched_for_large_inventories(docker, scope):
    for _ in range(300):
        docker.container(CURRENT, working_dir=None)

    reap(docker, scope)

    assert [len(command) - 4 for command in docker.commands if command[1] == 'inspect'] == [128, 128, 44]


def run_with_second_inspection(docker, action):
    """Run ``action`` when the in-guard (second) inspection is requested."""
    run, inspections = docker.run, []
    def intercepted(command, deadline=None):
        if command[1] == 'inspect':
            inspections.append(command)
            if len(inspections) == 2:
                return action(command, deadline) if action else run(command, deadline)
        return run(command, deadline)
    docker.run = intercepted
    return inspections


def test_scope_state_is_rechecked_under_the_lease_guard(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}), finished_days=30)
    def started_meanwhile(command, deadline):
        docker.containers[container]['status'], docker.containers[container]['running'] = 'running', True
        return Docker.run(docker, command, deadline)
    inspections = run_with_second_inspection(docker, started_meanwhile)

    report = reap(docker, scope)

    assert len(inspections) == 2 and container in docker.containers and report.scopes == 0
    assert not any(command[1] == 'rm' for command in docker.commands)


def test_removal_is_confined_to_the_inspected_containers(docker, scope, tmp_path):
    first = docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}), finished_days=30)
    def appears(command, deadline):
        result = Docker.run(docker, command, deadline)
        docker.container(OLD, working_dir=None)
        return result
    run_with_second_inspection(docker, appears)

    reap(docker, scope)

    assert first not in docker.containers and len(docker.containers) == 1


def test_scope_whose_inspection_changes_under_the_guard_is_preserved(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}), finished_days=30)
    run_with_second_inspection(docker, lambda command, deadline: '')

    report = reap(docker, scope)

    assert container in docker.containers and report.diagnostic == 'incomplete: ValueError'


def test_scope_relabelled_under_the_guard_is_preserved(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}), finished_days=30)
    def relabelled(command, deadline):
        docker.containers[container]['labels']['api-agent.scope'] = OTHER
        return Docker.run(docker, command, deadline)
    run_with_second_inspection(docker, relabelled)

    reap(docker, scope)

    assert container in docker.containers


def test_expired_deadline_stops_before_any_removal(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))

    report = reap(docker, scope, deadline=15, now=lambda: 10 * len(docker.commands))

    assert len(docker.containers) == 1 and report.diagnostic == 'incomplete: deadline'


def test_cancellation_stops_before_any_removal(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))

    report = reap(docker, scope, cancelled=lambda: True)

    assert len(docker.containers) == 1 and report.diagnostic == 'incomplete: cancelled'


def test_cancellation_between_scopes_keeps_completed_work(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.container(OTHER, working_dir=checkout(tmp_path, OTHER, exists=False))

    report = reap(docker, scope, cancelled=lambda: len(docker.commands) > 5)

    assert len(docker.containers) == 1 and report.scopes == 1 and report.diagnostic == 'incomplete: cancelled'


@pytest.mark.parametrize(('environment', 'session', 'marker', 'visible'), [
    ({}, frozenset(), False, True),
    ({'DOCKER_HOST': 'unix:///run/docker.sock'}, frozenset(), False, True),
    ({'DOCKER_HOST': 'ssh://builder'}, frozenset(), False, False),
    ({'CONTAINER_HOST': 'tcp://10.0.0.5:2375'}, frozenset(), False, False),
    ({}, frozenset({'session'}), False, False),
    ({}, frozenset(), True, False)])
def test_host_paths_are_visible_only_to_a_local_non_containerised_session(
        monkeypatch, tmp_path, environment, session, marker, visible):
    monkeypatch.setattr(reaper, 'session_container_identity', lambda: session)
    monkeypatch.setattr(reaper, '_CONTAINER_MARKER', str(tmp_path / '.dockerenv'))
    if marker:
        (tmp_path / '.dockerenv').touch()

    assert host_paths_visible(environment) is visible


def test_session_identity_failure_means_paths_are_not_visible(monkeypatch):
    from api_agent.runtime.containers import ContainerError
    def failing():
        raise ContainerError('unknown')
    monkeypatch.setattr(reaper, 'session_container_identity', failing)

    assert host_paths_visible({}) is False


def test_only_the_scope_whose_directory_is_gone_is_reaped(docker, scope, tmp_path):
    kept = docker.container(OLD, working_dir=checkout(tmp_path, OLD))
    gone = docker.container(OTHER, working_dir=checkout(tmp_path, OTHER, exists=False))

    reap(docker, scope)

    assert kept in docker.containers and gone not in docker.containers
