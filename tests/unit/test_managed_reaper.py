"""Leaderless managed containers are cleaned; anything possibly in use is preserved."""
import json
import logging
import os
from types import SimpleNamespace

import pytest

import api_agent.runtime.prerequisites as p
from api_agent.runtime.ownership import StackScope
from api_agent.runtime.reaper import ReapReport, reap_unused_managed_resources

CURRENT = 'c' * 16
OLD = 'a' * 16
OTHER = 'b' * 16


class Docker:
    """A tiny engine whose only inventory is what each test places in it."""

    def __init__(self):
        self.containers, self.volumes, self.commands, self.pruned = {}, {}, [], []
        self.fail = None
        self.listing = None
        self.mangle = None
        self.counter = 0

    def container(self, scope, *, working_dir, status='exited', labels=None, project=None, service='jaeger',
                  mounts=()):
        self.counter += 1
        identifier = f'{self.counter:064x}'
        merged = {'api-agent.managed': 'true', 'api-agent.scope': scope, 'api-agent.profile': 'otel',
                  'com.docker.compose.project': project or 'sa-' + scope,
                  'com.docker.compose.service': service}
        if working_dir is not None:
            merged['com.docker.compose.project.working_dir'] = str(working_dir)
        merged.update(labels or {})
        self.containers[identifier] = {'id': identifier, 'status': status, 'running': status == 'running',
                                       'labels': merged, 'mounts': list(mounts)}
        return identifier

    def run(self, command, deadline=None):
        self.commands.append(command)
        if self.fail is not None and command[1:3] == self.fail:
            raise p.PrerequisiteError('engine refused')
        arguments = command[1:]
        if arguments[0] == 'ps' and any(value.startswith('volume=') for value in arguments):
            wanted = next(value[len('volume='):] for value in arguments if value.startswith('volume='))
            return '\n'.join(item for item in self.containers if wanted in self.containers[item].get('mounts', ()))
        if arguments[0] == 'ps':
            if self.listing is not None:
                return self.listing
            return '\n'.join(self.containers)
        if arguments[0] == 'inspect':
            rows = [json.dumps(self.containers[item]) for item in arguments[3:]]
            return self.mangle(rows) if self.mangle else '\n'.join(rows)
        if arguments[:2] == ('volume', 'ls'):
            return '\n'.join(name for name, labels in self.volumes.items()
                             if (labels or {}).get('api-agent.managed') == 'true')
        if arguments[:2] == ('volume', 'inspect'):
            return json.dumps([{'Name': name, 'Labels': self.volumes[name]} for name in arguments[2:]])
        if arguments[:2] == ('volume', 'rm'):
            # Like the engine: neither forced, and a volume some container mounts is refused.
            assert '-f' not in arguments and '--force' not in arguments
            for name in arguments[2:]:
                if self.mounted(name):
                    raise p.PrerequisiteError('volume is in use')
                del self.volumes[name]
            return ''
        if arguments[0] == 'stop':
            for item in arguments[3:]:
                self.containers[item].update(status='exited', running=False)
            return ''
        if arguments[0] == 'rm':
            # Like the engine: neither forced nor with volumes, and never a running container.
            assert not {'-f', '-v', '--force', '--volumes'} & set(arguments)
            if any(self.containers[item]['running'] for item in arguments[1:]):
                raise p.PrerequisiteError('rm refused a running container')
            for item in arguments[1:]:
                del self.containers[item]
            return ''
        raise AssertionError(command)

    def volume(self, name, scope, *, project=None, profile='otel', labels=None):
        """An owned volume of ``scope``; ``labels`` replaces the whole label set."""
        self.volumes[name] = labels if labels is not None else {
            'api-agent.managed': 'true', 'api-agent.scope': scope, 'api-agent.profile': profile,
            'com.docker.compose.project': project or 'sa-' + scope}
        return name

    def mounted(self, name):
        return any(name in container.get('mounts', ()) for container in self.containers.values())

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
    """Return the recorded compose directory for a scope's retained snapshot.

    The journal directory is its parent; a journal of ``None`` leaves no journal file.
    """
    ownership = tmp_path / (name or scope_id) / 'artifacts' / 'prerequisites'
    working = ownership / f'{scope_id}-assets-{snapshot}'
    if exists:
        working.mkdir(parents=True)
    if journal is not None:
        ownership.mkdir(parents=True, exist_ok=True)
        p._write_journal(ownership, SimpleNamespace(id=scope_id), journal)
    return working


def live_lease(*container_ids):
    return {'invocation': {'pid': os.getpid(), 'identity': 'boot:start', 'ids': list(container_ids)}}


def reap(docker, scope, **options):
    options.setdefault('deadline', 10_000)
    options.setdefault('now', lambda: 0)
    return reap_unused_managed_resources(run=docker.run, prune_network=docker.prune, executable='docker',
        scope=scope, identity=lambda pid: 'boot:start', **options)


def removed(docker, command_name):
    return [item for command in docker.commands if command[1] == command_name for item in command[2:]]


def test_a_leaderless_scope_is_removed_with_its_network_and_keeps_its_volumes(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, journal={})
    first = docker.container(OLD, working_dir=working)
    second = docker.container(OLD, working_dir=working, status='created', service='collector')
    docker.volume('sa-aaaaaaaaaaaaaaaa_jaeger-badger', OLD)

    report = reap(docker, scope)

    assert docker.containers == {} and docker.pruned == [OLD]
    assert (report.scopes, report.containers, report.volumes, report.networks, report.diagnostic) == (1, 2, 0, 1, '')
    assert removed(docker, 'rm') == [first, second]
    assert docker.volumes and not any(command[1:3] == ('volume', 'rm') for command in docker.commands)


def test_a_running_leaderless_container_is_stopped_before_it_is_removed(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, journal={})
    running = docker.container(OLD, working_dir=working, status='running')
    stopped = docker.container(OLD, working_dir=working, service='collector')

    reap(docker, scope)

    verbs = [command[1] for command in docker.commands if command[1] in {'stop', 'rm'}]
    assert verbs == ['stop', 'rm'] and docker.containers == {}
    assert next(command for command in docker.commands if command[1] == 'stop')[2:] == ('--time', '5', running)
    assert removed(docker, 'rm') == [running, stopped]


def test_a_scope_whose_checkout_is_gone_has_no_journal_and_no_lease(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False), status='running')

    report = reap(docker, scope)

    assert docker.containers == {} and report.containers == 1


def test_a_gone_journal_is_not_proof_when_host_paths_are_not_visible(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False), status='running')

    report = reap(docker, scope, host_paths_visible=False)

    assert docker.containers[container]['running'] and report.scopes == 0


def test_a_journal_directory_that_cannot_be_statted_preserves_the_scope(docker, scope, tmp_path, monkeypatch):
    working = checkout(tmp_path, OLD, journal={})
    container = docker.container(OLD, working_dir=working, status='running')
    real = os.stat

    def refuse(path, *args, **kwargs):
        if str(path).startswith(str(working.parent)):
            raise PermissionError(13, 'denied')
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', refuse)

    report = reap(docker, scope)

    assert docker.containers[container]['running'] and report.scopes == 0


def test_a_container_pinned_by_an_enrolled_reporting_binding_is_preserved(docker, scope, tmp_path, monkeypatch):
    from api_agent.runtime import reporting_registry
    from api_agent.runtime.reporting_metadata import _directory, _write

    working = checkout(tmp_path, OLD, journal={})
    pinned = docker.container(OLD, working_dir=working, status='exited', service='localstack')
    registry = _directory(tmp_path / 'reporting', create=True)
    _write(registry / ('f' * 16 + '.json'), {'state': 'ready', 'ids': [pinned], 'roots': [{'path': '/x', 'identity': [1, 2]}]})
    monkeypatch.setattr(reporting_registry, 'default_registry_directory', lambda: registry)

    report = reap(docker, scope)

    assert pinned in docker.containers and report.scopes == 0


def test_a_canonical_reporting_binding_does_not_pin_its_container(docker, scope, tmp_path, monkeypatch):
    from api_agent.runtime import reporting_registry
    from api_agent.runtime.reporting_metadata import _directory, _write

    working = checkout(tmp_path, OLD, journal={})
    unpinned = docker.container(OLD, working_dir=working, status='exited', service='localstack')
    registry = _directory(tmp_path / 'reporting', create=True)
    _write(registry / ('f' * 16 + '.json'), {'state': 'ready', 'ids': [unpinned], 'roots': []})
    monkeypatch.setattr(reporting_registry, 'default_registry_directory', lambda: registry)

    reap(docker, scope)

    assert unpinned not in docker.containers


def test_files_that_are_not_bindings_in_the_reporting_registry_are_ignored(docker, scope, tmp_path, monkeypatch):
    from api_agent.runtime import reporting_registry
    from api_agent.runtime.reporting_metadata import _directory

    working = checkout(tmp_path, OLD, journal={})
    container = docker.container(OLD, working_dir=working, status='exited')
    registry = _directory(tmp_path / 'reporting', create=True)
    (registry / 'notes.json').write_text('{')
    (registry / ('F' * 16 + '.json')).write_text('{')
    monkeypatch.setattr(reporting_registry, 'default_registry_directory', lambda: registry)

    report = reap(docker, scope)

    assert container not in docker.containers and report.diagnostic == ''


def test_a_missing_reporting_registry_pins_nothing(tmp_path):
    from api_agent.runtime import reporting_registry

    assert reporting_registry.enrolled_container_ids(tmp_path / 'absent') == frozenset()


def test_an_unreadable_reporting_registry_preserves_every_container(docker, scope, tmp_path, monkeypatch):
    from api_agent.runtime import reporting_registry
    from api_agent.runtime.reporting_metadata import _directory

    working = checkout(tmp_path, OLD, journal={})
    container = docker.container(OLD, working_dir=working, status='exited')
    registry = _directory(tmp_path / 'reporting', create=True)
    (registry / ('f' * 16 + '.json')).write_text('{')
    monkeypatch.setattr(reporting_registry, 'default_registry_directory', lambda: registry)

    report = reap(docker, scope)

    assert container in docker.containers and report.diagnostic.startswith('incomplete')


def test_a_live_lease_from_another_process_protects_a_running_scope(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=None, status='running')
    working = checkout(tmp_path, OLD, journal=live_lease(container))
    docker.containers[container]['labels']['com.docker.compose.project.working_dir'] = str(working)

    report = reap(docker, scope)

    assert docker.containers[container]['running'] and report.scopes == 0
    assert not any(command[1] in {'stop', 'rm'} for command in docker.commands)


def test_a_lease_whose_owner_is_gone_does_not_protect_the_scope(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=None, status='running')
    working = checkout(tmp_path, OLD, journal={'invocation': {'pid': os.getpid(),
        'identity': 'previous-boot:start', 'ids': [container]}})
    docker.containers[container]['labels']['com.docker.compose.project.working_dir'] = str(working)

    reap(docker, scope)

    assert docker.containers == {}


@pytest.mark.parametrize('journal', ['{', '[]'])
def test_unreadable_journal_preserves_the_scope(docker, scope, tmp_path, journal):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD), status='running')
    (tmp_path / OLD / 'artifacts' / 'prerequisites' / f'{OLD}.json').write_text(journal)

    report = reap(docker, scope)

    assert docker.containers[container]['running'] and report.scopes == 0
    assert report.diagnostic == 'incomplete: PrerequisiteError'


def test_lock_held_by_another_process_preserves_the_scope(docker, scope, tmp_path, monkeypatch):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD), status='running')
    def busy(*args, **kwargs):
        raise p.PrerequisiteError('Prerequisite deadline exceeded.')
    monkeypatch.setattr(p, 'guarded_cleanup', busy)

    reap(docker, scope)

    assert docker.containers[container]['running']
    assert not any(command[1] in {'stop', 'rm'} for command in docker.commands)


def test_current_and_legacy_scopes_are_never_touched(docker, scope, tmp_path):
    for identifier in (CURRENT, 'd' * 16):
        docker.container(identifier, working_dir=checkout(tmp_path, identifier, exists=False), status='running')

    report = reap(docker, scope)

    assert len(docker.containers) == 2 and report.scopes == 0
    assert [command[1] for command in docker.commands] == ['ps', 'inspect', 'volume']


@pytest.mark.parametrize('layout', ['unknown', 'relative', 'missing', 'other-scope', 'two-directories'])
def test_an_unrecognised_layout_cannot_prove_the_scope_is_unleased(docker, scope, tmp_path, layout):
    working = {'unknown': tmp_path / 'unknown-layout', 'relative': 'relative/compose', 'missing': None,
               'other-scope': checkout(tmp_path, OTHER, exists=False),
               'two-directories': checkout(tmp_path, OLD, name='moved', exists=False)}[layout]
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False), status='running')
    docker.container(OLD, working_dir=working, status='running')

    report = reap(docker, scope)

    assert len(docker.containers) == 2 and report.scopes == 0
    assert not any(command[1] in {'stop', 'rm'} for command in docker.commands)


def test_scope_created_by_several_acquisitions_is_cleaned_as_one(docker, scope, tmp_path):
    for snapshot in ('first111', 'second22'):
        docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}, snapshot=snapshot))

    report = reap(docker, scope)

    assert docker.containers == {} and report.containers == 2


def test_a_container_being_removed_by_someone_else_preserves_its_scope(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, journal={})
    docker.container(OLD, working_dir=working, status='running')
    docker.container(OLD, working_dir=working, status='removing')

    reap(docker, scope)

    assert len(docker.containers) == 2
    assert not any(command[1] in {'stop', 'rm'} for command in docker.commands)


def test_an_unowned_or_misattributed_container_is_skipped_and_blocks_only_its_scope(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, journal={})
    docker.container(OLD, working_dir=working)
    docker.container(OLD, working_dir=working, project='sa-' + OTHER)
    docker.container(OTHER, working_dir=checkout(tmp_path, OTHER, journal={}), labels={'api-agent.managed': 'false'})
    docker.container('not-a-scope', working_dir=working, project='sa-not-a-scope')
    docker.containers[next(reversed(docker.containers))]['labels'] = None
    unattributed = docker.container(OTHER, working_dir=working, project='foreign')
    reapable = docker.container('e' * 16, working_dir=checkout(tmp_path, 'e' * 16, journal={}))

    report = reap(docker, scope)

    assert len(docker.containers) == 5 and unattributed in docker.containers and reapable not in docker.containers
    assert report.scopes == 1 and report.containers == 1


@pytest.mark.parametrize('mangle', [
    lambda rows: 'not-json',
    lambda rows: '\n'.join(rows[:-1]),
    lambda rows: '\n'.join(['[]'] * len(rows)),
    lambda rows: '\n'.join(row.replace('"id": "', '"id": "9') for row in rows),
    lambda rows: '\n'.join(row.replace('"labels"', '"tags"') for row in rows),
    lambda rows: '\n'.join(row.replace('"running": false', '"running": "no"') for row in rows),
    lambda rows: '\n'.join(row.replace('"status": "exited"', '"status": 3') for row in rows),
])
def test_unverifiable_inventory_preserves_everything(docker, scope, tmp_path, mangle):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.mangle = mangle

    report = reap(docker, scope)

    assert len(docker.containers) == 2
    assert not any(command[1] in {'stop', 'rm'} for command in docker.commands)
    assert report.diagnostic and report.scopes == 0


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
    assert 'failed' in caplog.text and 'removed 1 container(s) of 1 scope(s)' in caplog.text


def test_a_failed_stop_leaves_the_container_and_the_network(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False), status='running')
    docker.fail = ('stop', '--time')

    report = reap(docker, scope)

    assert container in docker.containers and docker.pruned == []
    assert report.diagnostic == 'incomplete: PrerequisiteError'


def test_network_prune_failure_is_best_effort(docker, scope, tmp_path):
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    def failing(scope_id, deadline):
        raise p.PrerequisiteError('network inventory')

    report = reap_unused_managed_resources(run=docker.run, prune_network=failing, executable='docker',
        scope=scope, deadline=10_000, now=lambda: 0)

    assert docker.containers == {} and report.diagnostic == 'incomplete: PrerequisiteError'


def test_a_cleanly_idle_engine_issues_only_the_listing(docker, scope):
    report = reap(docker, scope)

    assert docker.commands == [('docker', 'ps', '-a', '--no-trunc', '--filter', 'label=api-agent.managed=true',
                                '--format', '{{.ID}}'),
                               ('docker', 'volume', 'ls', '--quiet', '--filter', 'label=api-agent.managed=true')]
    assert report == ReapReport()


def test_removals_are_batched(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, exists=False)
    for _ in range(70):
        docker.container(OLD, working_dir=working, status='running')

    report = reap(docker, scope)

    assert docker.containers == {}
    assert [len(command) - 4 for command in docker.commands if command[1] == 'stop'] == [32, 32, 6]
    assert [len(command) - 2 for command in docker.commands if command[1] == 'rm'] == [32, 32, 6]
    assert [len(command) - 4 for command in docker.commands if command[1] == 'inspect'] == [70, 70]
    assert report.containers == 70


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
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}))
    def being_removed_meanwhile(command, deadline):
        docker.containers[container]['status'] = 'removing'
        return Docker.run(docker, command, deadline)
    inspections = run_with_second_inspection(docker, being_removed_meanwhile)

    report = reap(docker, scope)

    assert len(inspections) == 2 and container in docker.containers and report.scopes == 0
    assert not any(command[1] == 'rm' for command in docker.commands)


def test_removal_is_confined_to_the_inspected_containers(docker, scope, tmp_path):
    first = docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}))
    def appears(command, deadline):
        result = Docker.run(docker, command, deadline)
        docker.container(OLD, working_dir=None)
        return result
    run_with_second_inspection(docker, appears)

    reap(docker, scope)

    assert first not in docker.containers and len(docker.containers) == 1


def test_scope_whose_inspection_changes_under_the_guard_is_preserved(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}))
    run_with_second_inspection(docker, lambda command, deadline: '')

    report = reap(docker, scope)

    assert container in docker.containers and report.diagnostic == 'incomplete: ValueError'


def test_scope_relabelled_under_the_guard_is_preserved(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}))
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

    report = reap(docker, scope, cancelled=lambda: len(docker.commands) > 3)

    assert len(docker.containers) == 1 and report.scopes == 1 and report.diagnostic == 'incomplete: cancelled'


def test_cancellation_inside_a_scope_stops_the_pass_and_keeps_its_containers(docker, scope, tmp_path):
    container = docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}), status='running')

    report = reap(docker, scope, cancelled=lambda: len(docker.commands) == 3)

    assert container in docker.containers and report.diagnostic == 'incomplete: cancelled'
    assert [command[1] for command in docker.commands] == ['ps', 'inspect', 'inspect']
    assert docker.containers[container]['running']


def test_only_the_scope_without_a_lease_is_reaped(docker, scope, tmp_path):
    leased = docker.container(OLD, working_dir=None, status='running')
    docker.containers[leased]['labels']['com.docker.compose.project.working_dir'] = str(
        checkout(tmp_path, OLD, journal=live_lease(leased)))
    gone = docker.container(OTHER, working_dir=checkout(tmp_path, OTHER, exists=False))

    reap(docker, scope)

    assert leased in docker.containers and gone not in docker.containers


def app_scope():
    return StackScope(CURRENT, checkout='app')


def test_a_scope_under_its_derived_project_name_is_cleaned_like_a_legacy_one(docker, scope, tmp_path):
    working = checkout(tmp_path, OLD, journal={})
    derived = docker.container(OLD, working_dir=working, project='sa-app-aaaaaaaa', labels={'api-agent.checkout': 'app'})
    legacy = docker.container(OLD, working_dir=working, service='collector')

    report = reap(docker, scope)

    assert docker.containers == {} and removed(docker, 'rm') == [derived, legacy] and report.scopes == 1


@pytest.mark.parametrize('case', ['derived name of another scope', 'derived name from another checkout label',
                                  'derived name without a checkout label', 'legacy name of another scope'])
def test_a_project_name_the_labels_do_not_reproduce_preserves_the_whole_scope(docker, scope, tmp_path, case):
    working = checkout(tmp_path, OLD, journal={})
    docker.container(OLD, working_dir=working)
    project, labels = {
        'derived name of another scope': ('sa-app-bbbbbbbb', {'api-agent.checkout': 'app'}),
        'derived name from another checkout label': ('sa-app-aaaaaaaa', {'api-agent.checkout': 'other'}),
        'derived name without a checkout label': ('sa-app-aaaaaaaa', {}),
        'legacy name of another scope': ('sa-' + OTHER, {})}[case]
    mismatched = docker.container(OLD, working_dir=working, project=project, labels=labels)

    report = reap(docker, scope)

    assert len(docker.containers) == 2 and mismatched in docker.containers and report.scopes == 0
    assert not any(command[1] in {'stop', 'rm'} for command in docker.commands)


def own_stack(docker, tmp_path, *, journal, legacy_status='exited'):
    """The caller's current stack plus a leftover container of the same scope under the legacy name."""
    working = checkout(tmp_path, CURRENT, journal=journal)
    current = docker.container(CURRENT, working_dir=working, status='running', project='sa-app-cccccccc',
                               labels={'api-agent.checkout': 'app'})
    leftover = docker.container(CURRENT, working_dir=working, status=legacy_status, service='collector')
    return current, leftover, working.parent


def test_the_callers_current_stack_is_never_touched_and_its_legacy_leftover_is_ignored_by_default(docker, tmp_path):
    current, leftover, _ = own_stack(docker, tmp_path, journal={})

    report = reap(docker, app_scope())

    assert set(docker.containers) == {current, leftover} and report.scopes == 0
    assert [command[1] for command in docker.commands] == ['ps', 'inspect', 'volume']


def test_an_unleased_legacy_leftover_of_the_callers_scope_is_removed_but_not_its_current_stack(docker, tmp_path):
    current, leftover, _ = own_stack(docker, tmp_path, journal={}, legacy_status='running')

    report = reap(docker, app_scope(), own_lock='free')

    assert set(docker.containers) == {current} and report.containers == 1
    assert [command[2:] for command in docker.commands if command[1] == 'stop'] == [('--time', '5', leftover)]


def test_a_live_lease_protects_the_callers_legacy_leftover_when_its_lock_is_free(docker, tmp_path):
    placeholder = '9' * 64
    current, leftover, _ = own_stack(docker, tmp_path, journal=live_lease(placeholder))

    report = reap(docker, app_scope(), own_lock='free')

    assert set(docker.containers) == {current, leftover} and report.scopes == 0


def test_a_caller_holding_the_lock_supplies_the_leased_ids_instead_of_taking_it_again(docker, tmp_path, monkeypatch):
    current, leftover, directory = own_stack(docker, tmp_path, journal={})
    def busy(*args, **kwargs):
        raise AssertionError('the caller already holds this lock')
    monkeypatch.setattr(p, 'guarded_cleanup', busy)

    report = reap(docker, app_scope(), own_lock=(directory, frozenset()))

    assert set(docker.containers) == {current} and report.scopes == 1


def test_leased_ids_from_a_caller_holding_the_lock_protect_the_leftover(docker, tmp_path):
    current, leftover, directory = own_stack(docker, tmp_path, journal={})

    report = reap(docker, app_scope(), own_lock=(directory, frozenset({'1' * 64})))

    assert set(docker.containers) == {current, leftover} and report.scopes == 0


def test_a_held_lock_on_another_directory_does_not_stand_in_for_this_scopes_journal(docker, tmp_path):
    current, leftover, directory = own_stack(docker, tmp_path, journal=live_lease('9' * 64))

    report = reap(docker, app_scope(), own_lock=(tmp_path / 'elsewhere', frozenset()))

    assert set(docker.containers) == {current, leftover} and report.scopes == 0


def test_another_scope_is_never_covered_by_the_callers_held_lock(docker, tmp_path):
    working = checkout(tmp_path, OLD, journal=live_lease('9' * 64))
    container = docker.container(OLD, working_dir=working)

    report = reap(docker, app_scope(), own_lock=(working.parent, frozenset()))

    assert container in docker.containers and report.scopes == 0
