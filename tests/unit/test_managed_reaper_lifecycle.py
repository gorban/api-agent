"""The reaper runs at prerequisite lease boundaries and never disturbs them."""
import json
import os
import time
from types import SimpleNamespace

import pytest

import api_agent.runtime.prerequisites as p
import api_agent.runtime.reaper as reaper
from api_agent.runtime import scope_index
from api_agent.runtime.compose import ComposeError, plan_compose
from api_agent.runtime.ownership import StackScope
from test_prerequisites import Clock, Engine, trace_fixture_config

LISTING = ('docker', 'ps', '-a', '--no-trunc', '--filter', 'label=api-agent.managed=true',
           '--format', '{{.ID}}')
GONE = 'a' * 16
TEMPLATE_START = '{"id":"{{.Id}}","status"'


class ReapingEngine(Engine):
    """Adds one stopped scope whose checkout was deleted to the ordinary fake engine."""

    def __init__(self, missing_directory):
        super().__init__()
        self.missing_directory = missing_directory
        self.foreign = {}
        self.foreign_volumes = {}
        self.foreign_network = True

    def add_foreign(self, *, running=False):
        identifier = f'{len(self.foreign) + 1:064x}'.replace('0', 'e', 1)
        self.foreign[identifier] = {'id': identifier, 'status': 'running' if running else 'exited', 'running': running,
            'labels': {'api-agent.managed': 'true', 'api-agent.scope': GONE, 'api-agent.profile': 'otel',
                       'com.docker.compose.project': 'sa-' + GONE, 'com.docker.compose.service': 'jaeger',
                       'com.docker.compose.project.working_dir': str(self.missing_directory)}}
        self.foreign_volumes['sa-' + GONE + '_data'] = {'api-agent.managed': 'true', 'api-agent.scope': GONE,
            'api-agent.profile': 'otel', 'com.docker.compose.project': 'sa-' + GONE}
        scope_index.record(GONE, self.missing_directory.parent)
        # Last acquired well beyond the retention period, so the deleted checkout counts as orphaned.
        long_ago = time.time() - 20 * 86400
        os.utime(scope_index.index_directory() / f'{GONE}.json', (long_ago, long_ago))
        return identifier

    def add_own_leftover(self, scope, directory):
        """A stopped container of the caller's own scope under the legacy project name."""
        identifier = f'{len(self.foreign) + 1:064x}'.replace('0', 'e', 1)
        self.foreign[identifier] = {'id': identifier, 'status': 'exited', 'running': False,
            'labels': {'api-agent.managed': 'true', 'api-agent.scope': scope.id, 'api-agent.profile': 'otel',
                       'com.docker.compose.project': 'sa-' + scope.id, 'com.docker.compose.service': 'jaeger',
                       'com.docker.compose.project.working_dir': str(directory / f'{scope.id}-assets-abc12345')}}
        return identifier

    def run(self, command, **kwargs):
        arguments = command[1:]
        if arguments[:1] == ('ps',) and 'label=api-agent.managed=true' in arguments:
            self.commands.append((command, kwargs))
            return '\n'.join(self.foreign)
        if arguments[:2] == ('inspect', '--format') and arguments[2].startswith(TEMPLATE_START):
            self.commands.append((command, kwargs))
            return '\n'.join(json.dumps(self.foreign[item]) for item in arguments[3:])
        if arguments[:2] == ('volume', 'ls'):
            self.commands.append((command, kwargs))
            return '\n'.join(self.foreign_volumes)
        if arguments[:2] == ('volume', 'inspect') and arguments[2] in self.foreign_volumes:
            self.commands.append((command, kwargs))
            return json.dumps([{'Name': name, 'Labels': self.foreign_volumes[name]} for name in arguments[2:]])
        if arguments[:2] == ('volume', 'rm'):
            self.commands.append((command, kwargs))
            assert '-f' not in arguments
            for name in arguments[2:]:
                del self.foreign_volumes[name]
            return ''
        if arguments[:1] == ('ps',) and any(value.startswith('volume=') for value in arguments):
            self.commands.append((command, kwargs))
            return ''
        if arguments[:1] == ('stop',) and arguments[3] in self.foreign:
            self.commands.append((command, kwargs))
            for item in arguments[3:]:
                self.foreign[item].update(status='exited', running=False)
            return ''
        if arguments[:1] == ('rm',) and arguments[1] in self.foreign:
            self.commands.append((command, kwargs))
            for item in arguments[1:]:
                del self.foreign[item]
            return ''
        network = 'sa-' + GONE + '_default'
        if arguments[:2] == ('network', 'ls') and any(value.endswith('=' + GONE) for value in arguments):
            self.commands.append((command, kwargs))
            return network if self.foreign_network else ''
        if arguments[:3] == ('network', 'inspect', network):
            self.commands.append((command, kwargs))
            return json.dumps([{'Name': network, 'Id': 'f' * 64, 'Created': '2026-01-01T00:00:00Z',
                'Labels': {'api-agent.network-cleanup-v1': GONE, 'api-agent.managed': 'true',
                           'api-agent.scope': GONE, 'com.docker.compose.project': 'sa-' + GONE,
                           'com.docker.compose.network': 'default'}}])
        if arguments[:3] == ('network', 'rm', 'f' * 64):
            self.commands.append((command, kwargs))
            self.foreign_network = False
            return ''
        return super().run(command, **kwargs)


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(reaper, 'host_paths_visible', lambda environment: True)
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / 'pack')
    clock = Clock()
    engine = ReapingEngine(tmp_path / 'deleted' / 'artifacts' / 'prerequisites' / f'{GONE}-assets-abc12345')

    def create(config=None, **kwargs):
        manager = p.PrerequisiteManager(plan_compose(trace_fixture_config(config or {'TRACE_MODE': 'OTEL'}), scope),
            scope, tmp_path / 'artifacts' / 'prerequisites', command_runner=engine.run,
            cancelled=lambda: engine.cancel, readiness_probe=lambda *args: True,
            now=clock.now, sleep=clock.sleep, identity=lambda pid: 'boot:start', **kwargs)
        engine.owner = manager
        return manager
    return create, engine, scope


def commands(engine):
    return [command for command, _ in engine.commands]


def volume_removals(engine):
    """Storage is never removed by a lease boundary."""
    return [command for command in commands(engine) if command[1:3] in {('volume', 'rm'), ('volume', 'prune')}
            or '-v' in command or '--volumes' in command]


def test_acquire_reaps_before_starting_services(lifecycle):
    create, engine, _ = lifecycle
    manager = create()
    container = engine.add_foreign()

    manager.acquire()

    seen = commands(engine)
    assert container not in engine.foreign and not engine.foreign_network
    up = next(index for index, command in enumerate(seen) if 'up' in command)
    assert seen.index(LISTING) < up
    assert ('docker', 'rm', container) in seen
    assert engine.foreign_volumes == {} and seen.index(('docker', 'rm', container)) < seen.index(
        ('docker', 'volume', 'rm', 'sa-' + GONE + '_data'))


def test_acquire_removes_the_volumes_of_a_scope_whose_checkout_is_gone_but_not_a_live_ones(lifecycle):
    create, engine, scope = lifecycle
    manager = create()
    engine.add_foreign()

    manager.acquire()

    assert engine.foreign_volumes == {}
    assert [command for command in commands(engine) if command[1:3] == ('volume', 'rm')] == [
        ('docker', 'volume', 'rm', 'sa-' + GONE + '_data')]
    assert scope_index.directories(scope.id) == (manager.directory,)


def test_release_reaps_volumes_after_the_lease_ends(lifecycle):
    create, engine, _ = lifecycle
    manager = create()
    manager.acquire()
    engine.add_foreign()
    engine.foreign.clear()

    manager.release()

    assert engine.foreign_volumes == {}


def test_acquire_stops_a_running_leaderless_container_before_removing_it(lifecycle):
    create, engine, _ = lifecycle
    manager = create()
    container = engine.add_foreign(running=True)

    manager.acquire()

    seen = commands(engine)
    stop, remove = (seen.index(command) for command in (('docker', 'stop', '--time', '5', container),
                                                       ('docker', 'rm', container)))
    assert stop < remove and container not in engine.foreign


def test_acquire_removes_the_legacy_named_leftover_of_its_own_scope_under_its_held_lock(lifecycle):
    create, engine, scope = lifecycle
    manager = create()
    leftover = engine.add_own_leftover(scope, manager.directory)

    lease = manager.acquire()

    assert leftover not in engine.foreign
    assert all(engine.records[item]['State']['Running'] for item in lease.owned_ids)
    assert ('docker', 'rm', leftover) in commands(engine)


def test_release_removes_the_legacy_named_leftover_of_its_own_scope_once_the_lock_is_free(lifecycle):
    create, engine, scope = lifecycle
    manager = create()
    manager.acquire()
    leftover = engine.add_own_leftover(scope, manager.directory)

    manager.release()

    assert leftover not in engine.foreign and engine.records == {}


def test_release_under_a_caller_held_lock_leaves_its_own_scopes_leftover_alone(lifecycle):
    create, engine, scope = lifecycle
    manager = create()
    manager.acquire()
    leftover = engine.add_own_leftover(scope, manager.directory)

    manager.release(_prelocked=True)

    assert leftover in engine.foreign


def test_release_reaps_after_the_lease_ends(lifecycle):
    create, engine, _ = lifecycle
    manager = create()
    manager.acquire()
    engine.commands.clear()
    container = engine.add_foreign()

    manager.release()

    seen = commands(engine)
    stop = next(index for index, command in enumerate(seen) if command[1] == 'stop')
    assert seen.index(LISTING) > stop
    assert container not in engine.foreign and manager.lease is None


def test_acquire_and_release_each_issue_one_listing_when_nothing_is_unused(lifecycle):
    create, engine, _ = lifecycle
    manager = create()

    manager.acquire()
    manager.release()

    assert commands(engine).count(LISTING) == 2
    assert not volume_removals(engine)


def test_repeated_acquire_and_idle_release_do_not_reap_again(lifecycle):
    create, engine, _ = lifecycle
    manager = create()
    manager.acquire()
    manager.acquire()
    manager.release()
    manager.release()

    assert commands(engine).count(LISTING) == 2


def test_disabled_plan_never_queries_the_engine(lifecycle):
    create, engine, _ = lifecycle
    manager = create({'TRACE_MODE': ''})

    manager.acquire()
    manager.release()

    assert not engine.commands


def test_pending_rollback_blocks_acquire_without_reaping(lifecycle):
    create, engine, _ = lifecycle
    manager = create()
    engine.fail_up = True
    with pytest.raises(p.PrerequisiteError, match='up failed'):
        manager.acquire()
    engine.commands.clear()
    manager._rollback = (None, None, 'x', ())

    with pytest.raises(p.PrerequisiteError, match='rollback remains pending'):
        manager.acquire()

    assert LISTING not in commands(engine)


def test_a_failed_release_does_not_reap(lifecycle):
    create, engine, _ = lifecycle
    manager = create()
    manager.acquire()
    engine.commands.clear()
    engine.fail_stop = True

    with pytest.raises(p.PrerequisiteError, match='stop failed'):
        manager.release()

    assert LISTING not in commands(engine)


def test_cancelled_release_skips_reaping(lifecycle):
    create, engine, _ = lifecycle
    manager = create()
    manager.acquire()
    engine.commands.clear()
    engine.cancel = True

    manager.release()

    assert LISTING not in commands(engine)


@pytest.mark.parametrize('failing', [LISTING[1:3], ('inspect', '--format')])
def test_reaper_failure_never_fails_or_blocks_acquire_and_release(lifecycle, failing):
    create, engine, _ = lifecycle
    manager = create()
    engine.add_foreign()
    run = manager.runner
    def broken(command, **kwargs):
        if tuple(command[1:3]) == failing and any('managed' in part or 'status' in part for part in command):
            raise p.PrerequisiteError('private engine diagnostic')
        return run(command, **kwargs)
    manager.runner = broken

    lease = manager.acquire()
    manager.release()

    assert lease.owned_ids and manager.lease is None and engine.foreign


@pytest.mark.parametrize(('allowed', 'expected'), [(None, 30), (3, 3)])
def test_reaper_runs_within_its_own_bounded_budget(lifecycle, allowed, expected):
    create, engine, _ = lifecycle
    manager = create()

    manager.acquire(**({} if allowed is None else {'_deadline': manager.now() + allowed}))

    listing = next(kwargs for command, kwargs in engine.commands if command == LISTING)
    assert listing['deadline'] == expected


def test_retention_and_host_visibility_come_from_the_plan_and_the_engine(lifecycle, monkeypatch):
    create, _, _ = lifecycle
    seen = []
    monkeypatch.setattr(reaper, 'reap_unused_managed_resources', lambda **kwargs: seen.append(kwargs))
    monkeypatch.setattr(reaper, 'host_paths_visible', lambda environment: dict(environment) == {'DOCKER_HOST': 'ssh://far'})
    manager = create({'TRACE_MODE': 'OTEL', 'MANAGED_CONTAINER_RETENTION_DAYS': 3},
                     engine_environment={'DOCKER_HOST': 'ssh://far'})

    manager.acquire()

    assert [item['retention_days'] for item in seen] == [3] and seen[0]['host_paths_visible'] is True
    assert seen[0]['scope'] is manager.scope and seen[0]['executable'] == 'docker'
    assert seen[0]['own_lock'] == (manager.directory, frozenset())


@pytest.mark.parametrize(('config', 'expected'), [({}, 14), ({'MANAGED_CONTAINER_RETENTION_DAYS': 0}, 0),
                                                   ({'MANAGED_CONTAINER_RETENTION_DAYS': 30}, 30)])
def test_plan_carries_the_declared_retention(tmp_path, config, expected):
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / 'pack')

    plan = plan_compose(trace_fixture_config({'TRACE_MODE': 'OTEL', **config}), scope)

    assert plan.retention_days == expected


@pytest.mark.parametrize('value', [-1, True, '14', 1.5, None])
def test_invalid_retention_is_refused(tmp_path, value):
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / 'pack')

    with pytest.raises(ComposeError, match='MANAGED_CONTAINER_RETENTION_DAYS'):
        plan_compose(trace_fixture_config({'TRACE_MODE': 'OTEL', 'MANAGED_CONTAINER_RETENTION_DAYS': value}), scope)


def test_new_containers_are_labelled_with_their_checkout_name(lifecycle, tmp_path):
    create, engine, scope = lifecycle
    manager = create()

    manager.acquire()

    override = json.loads((manager.directory / f'{scope.id}-labels.json').read_text())
    labels = [service['labels'] for service in override['services'].values()]
    assert labels and all(item['api-agent.checkout'] == 'pack' for item in labels)
    assert all(item[p.CONFIG_LABEL] for item in labels)


def test_scope_without_a_checkout_name_adds_no_label(lifecycle):
    create, _, scope = lifecycle
    manager = create()
    manager.scope = StackScope(scope.id)

    manager.acquire()

    override = json.loads((manager.directory / f'{scope.id}-labels.json').read_text())
    assert all('api-agent.checkout' not in service['labels'] for service in override['services'].values())


@pytest.mark.parametrize(('name', 'label'), [('demo-pack', 'demo-pack'), ('my checkout!', 'my_checkout_'),
                                             ('ünï.code', '_n_.code'), ('x' * 100, 'x' * 63)])
def test_checkout_label_is_a_sanitised_basename_of_the_pack_root(tmp_path, name, label):
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / name)

    assert scope.checkout == label


def test_root_directory_has_no_checkout_name(tmp_path):
    assert StackScope.from_roots(tmp_path / 'base', '/').checkout == ''
    assert StackScope('b' * 16).checkout == ''


def test_checkout_name_does_not_change_scope_identity_or_ownership_labels(tmp_path):
    scope = StackScope.from_roots(tmp_path / 'base', tmp_path / 'pack')

    assert scope == StackScope(scope.id) and StackScope(scope.id).project == 'sa-' + scope.id
    assert scope.project == f'sa-pack-{scope.id[:8]}'
    assert scope.labels('otel') == {'api-agent.managed': 'true', 'api-agent.scope': scope.id,
                                    'api-agent.profile': 'otel'}
