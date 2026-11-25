"""The container runtime exposes the reaper for a one-time manual cleanup."""
import asyncio
import os
import time
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Input, Static

from api_agent.runtime import prerequisites as p, scope_index
from api_agent.runtime.containers import ContainerError, ContainerRuntime, Volume
from api_agent.runtime.ownership import StackScope
from api_agent.runtime import scope_index
from api_agent.runtime.reaper import ReapReport
from test_managed_reaper import CURRENT, OLD, Docker, checkout
from test_tui import fixture


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: frozenset())
    monkeypatch.setattr('api_agent.runtime.reaper.session_container_identity', lambda: frozenset())
    monkeypatch.setattr('api_agent.runtime.reaper._CONTAINER_MARKER', str(tmp_path / 'no-container-marker'))
    docker = Docker()
    runtime = ContainerRuntime('docker', StackScope(CURRENT), ownership_directory=tmp_path / 'mine')
    run = docker.run

    def routed(command, deadline=None):
        if command[1] == 'network':
            docker.commands.append(command)
            return ''
        return run(command, deadline)

    docker.run = routed
    monkeypatch.setattr(runtime, '_run', lambda arguments, **options: docker.run(('docker', *arguments), options['deadline']))
    return docker, runtime


def test_manual_cleanup_removes_a_leaderless_scope_through_the_runtime(engine, tmp_path):
    docker, runtime = engine
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, journal={}), status='running')
    kept = docker.container(CURRENT, working_dir=checkout(tmp_path, CURRENT, exists=False))

    report = runtime.reap_unused()

    assert (report.scopes, report.containers, report.volumes, report.diagnostic) == (1, 1, 0, '')
    assert list(docker.containers) == [kept]
    assert ('docker', 'network', 'ls', '--filter', 'label=api-agent.network-cleanup-v1=' + OLD,
            '--format', '{{.Name}}') in docker.commands


def idle(docker, tmp_path, days, *, scope_id=OLD, name='sa-aaaaaaaaaaaaaaaa_data'):
    """An unused volume whose scope last wrote its journal ``days`` ago, with its location recorded."""
    ownership = checkout(tmp_path, scope_id, journal={}).parent
    moment = time.time() - days * 86400
    os.utime(ownership / f'{scope_id}.json', (moment, moment))
    scope_index.record(scope_id, ownership)
    return docker.volume(name, scope_id)


@pytest.mark.parametrize(('retention', 'removed'), [(None, True), (14, True), (0, False), (100, False)])
def test_manual_cleanup_honours_the_retention_period_for_unused_volumes(engine, tmp_path, retention, removed):
    docker, runtime = engine
    idle(docker, tmp_path, 30)

    report = runtime.reap_unused(**({} if retention is None else {'retention_days': retention}))

    assert (not docker.volumes) is removed and report.volumes == int(removed)


def test_manual_cleanup_removes_the_volumes_of_a_scope_whose_checkout_is_gone(engine, tmp_path):
    docker, runtime = engine
    idle(docker, tmp_path, 0)
    import shutil
    shutil.rmtree(tmp_path / OLD)
    long_ago = time.time() - 20 * 86400  # last acquired well beyond the retention period
    os.utime(scope_index.index_directory() / f'{OLD}.json', (long_ago, long_ago))

    report = runtime.reap_unused()

    assert docker.volumes == {} and report.volumes == 1


def test_volumes_lists_owned_volumes_with_their_use(engine, tmp_path):
    docker, runtime = engine
    docker.volume('sa-aaaaaaaaaaaaaaaa_data', OLD)
    docker.volume('sa-app-aaaaaaaa_other', OLD, project='sa-app-aaaaaaaa', profile='localstack')
    docker.container(OLD, working_dir=None, mounts=('sa-aaaaaaaaaaaaaaaa_data',))

    assert runtime.volumes() == (Volume('sa-aaaaaaaaaaaaaaaa_data', 'sa-aaaaaaaaaaaaaaaa', 'otel', True),
                                 Volume('sa-app-aaaaaaaa_other', 'sa-app-aaaaaaaa', 'localstack', False))


def test_volume_listing_hides_unexpected_failures_behind_a_safe_message(engine):
    docker, runtime = engine
    docker.fail = ('volume', 'ls')

    with pytest.raises(ContainerError, match='^Volume inventory unavailable.$'):
        runtime.volumes()


def test_volume_listing_keeps_engine_availability_errors(engine, monkeypatch):
    _, runtime = engine
    def unavailable(arguments, **options):
        raise ContainerError('Container engine unavailable.')
    monkeypatch.setattr(runtime, '_run', unavailable)

    with pytest.raises(ContainerError, match='Container engine unavailable'):
        runtime.volumes()


def test_selected_volumes_are_removed_under_the_verification_the_age_rule_needs(engine, tmp_path):
    docker, runtime = engine
    idle(docker, tmp_path, 0)
    keep = docker.volume('sa-aaaaaaaaaaaaaaaa_other', OLD)

    report = runtime.remove_volumes(['sa-aaaaaaaaaaaaaaaa_data', 'sa-aaaaaaaaaaaaaaaa_data'])

    assert set(docker.volumes) == {keep} and report.volumes == 1


def test_the_runtimes_own_volumes_are_removed_under_its_own_journal(engine, tmp_path):
    docker, runtime = engine
    (tmp_path / 'mine').mkdir()
    p._write_journal(tmp_path / 'mine', StackScope(CURRENT), {})
    docker.volume(f'sa-{CURRENT}_data', CURRENT)

    assert runtime.remove_volumes([f'sa-{CURRENT}_data']).volumes == 1


def test_a_live_lease_protects_selected_volumes_of_the_runtimes_scope(engine, tmp_path):
    docker, runtime = engine
    (tmp_path / 'mine').mkdir()
    p._write_journal(tmp_path / 'mine', StackScope(CURRENT), {'invocation': {
        'pid': os.getpid(), 'identity': p.process_identity(os.getpid()), 'ids': ['9' * 64]}})
    docker.volume(f'sa-{CURRENT}_data', CURRENT)

    assert runtime.remove_volumes([f'sa-{CURRENT}_data']).volumes == 0 and docker.volumes


@pytest.mark.parametrize('names', [[], [''], ['-f'], [3], ['ok', '--all']])
def test_volume_removal_needs_valid_selected_names(engine, names):
    with pytest.raises(ContainerError, match='Select valid volumes'):
        engine[1].remove_volumes(names)


@pytest.mark.parametrize('runtime', [ContainerRuntime('docker'), ContainerRuntime('docker', StackScope(CURRENT))])
def test_volume_operations_require_scope_and_ownership_directory(runtime):
    with pytest.raises(ContainerError, match='scope and ownership directory'):
        runtime.volumes()
    with pytest.raises(ContainerError, match='scope and ownership directory'):
        runtime.remove_volumes(['x'])


def test_manual_cleanup_reports_engine_failure_without_raising(engine, tmp_path):
    docker, runtime = engine
    docker.container(OLD, working_dir=checkout(tmp_path, OLD, exists=False))
    docker.fail = ('ps', '-a')

    assert runtime.reap_unused().diagnostic == 'incomplete: PrerequisiteError'


@pytest.mark.parametrize('runtime', [ContainerRuntime('docker'),
                                     ContainerRuntime('docker', StackScope(CURRENT))])
def test_manual_cleanup_requires_scope_and_ownership_directory(runtime):
    with pytest.raises(ContainerError, match='scope and ownership directory'):
        runtime.reap_unused()


@pytest.mark.parametrize('seconds', [0, -1, 301, float('nan'), '5'])
def test_manual_cleanup_deadline_is_bounded(engine, seconds):
    with pytest.raises(ContainerError, match='between zero and 300 seconds'):
        engine[1].reap_unused(cleanup_timeout_seconds=seconds)


async def confirm_reap(app, pilot, answer='y'):
    await app.choose('reap-containers')
    await pilot.pause()
    assert 'Remove unused' in str(app.screen.query_one(Static).content)
    app.screen.query_one(Input).value = answer
    await pilot.click('#reap-submit')
    await pilot.pause()


def reaping_app(tmp_path, runtime, **fields):
    app = fixture(tmp_path, 'cleanup-containers', fields=fields)
    app.container_runtime = runtime
    return app


def test_container_management_removes_unused_resources_and_reports_counts(tmp_path):
    calls = []
    def reap_unused(**options):
        calls.append(options)
        return ReapReport(2, 5, 0, 2)
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=reap_unused)
    async def scenario():
        app = reaping_app(tmp_path, runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            await confirm_reap(app, pilot)
            await pilot.pause()
            assert 'Removed 5 unused container(s) of 2 scope(s), 0 volume(s) and 2 network(s).' in str(
                app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())
    assert calls == [{}]


def test_container_management_says_when_nothing_is_unused(tmp_path):
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=lambda **options: ReapReport())
    async def scenario():
        app = reaping_app(tmp_path, runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            await confirm_reap(app, pilot)
            await pilot.pause()
            assert 'Nothing unused' in str(app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())


@pytest.mark.parametrize(('outcome', 'expected'), [
    (ReapReport(1, 1, 0, 0, 'incomplete: deadline'), 'incomplete: deadline'),
    (ContainerError('Container engine unavailable.'), 'Container engine unavailable.')])
def test_container_management_shows_incomplete_or_failed_cleanup(tmp_path, outcome, expected):
    def reap_unused(**options):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=reap_unused)
    async def scenario():
        app = reaping_app(tmp_path, runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            await confirm_reap(app, pilot)
            await pilot.pause()
            assert expected in str(app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())


@pytest.mark.parametrize('answer', ['', 'yes', 'n'])
def test_unused_resources_are_not_removed_without_a_literal_y(tmp_path, answer):
    calls = []
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=lambda **options: calls.append(options) or ReapReport())
    async def scenario():
        app = reaping_app(tmp_path, runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            await confirm_reap(app, pilot, answer)
            await pilot.pause()
            assert calls == []
    asyncio.run(scenario())


def test_cancelling_the_unused_resources_confirmation_removes_nothing(tmp_path):
    calls = []
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=lambda **options: calls.append(options) or ReapReport())
    async def scenario():
        app = reaping_app(tmp_path, runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.choose('reap-containers')
            await pilot.pause()
            await pilot.click('#reap-cancel')
            await pilot.pause()
            assert calls == []
    asyncio.run(scenario())


def volume_app(tmp_path, volumes, *, removed=None, **runtime):
    items = list(volumes)
    removals = [] if removed is None else removed
    def remove_volumes(names):
        removals.append(names)
        items[:] = [item for item in items if item.name not in names]
        return ReapReport(volumes=len(names))
    namespace = SimpleNamespace(inventory=lambda: (), volumes=lambda: tuple(items), remove_volumes=remove_volumes, **runtime)
    return reaping_app(tmp_path, namespace), removals


UNUSED = Volume('sa-app-aaaaaaaa_data', 'sa-app-aaaaaaaa', 'otel', False)
BUSY = Volume('sa-app-aaaaaaaa_busy', 'sa-app-aaaaaaaa', 'localstack', True)


def test_volumes_are_listed_after_the_containers_and_only_unused_ones_can_be_marked(tmp_path):
    async def scenario():
        app, _ = volume_app(tmp_path, [UNUSED, BUSY])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert 'managed volumes: 2 (1 unused)' in app.container_summary
            table = app.screen.query_one(DataTable)
            assert [table.get_row(row.value)[1:] for row in table.rows] == [
                ['volume', 'sa-app-aaaaaaaa_data', 'sa-app-aaaaaaaa (otel)', 'unused'],
                ['volume', 'sa-app-aaaaaaaa_busy', 'sa-app-aaaaaaaa (localstack)', 'in use']]
            app.toggle_row('volume:sa-app-aaaaaaaa_busy')
            app.toggle_row('volume:unknown')
            assert not app.volume_marked
            app.toggle_row('volume:sa-app-aaaaaaaa_data')
            assert app.volume_marked == {'sa-app-aaaaaaaa_data'}
            assert table.get_row('volume:sa-app-aaaaaaaa_data')[0] == '✓'
            app.toggle_row('volume:sa-app-aaaaaaaa_data')
            assert not app.volume_marked and table.get_row('volume:sa-app-aaaaaaaa_data')[0] == ''
    asyncio.run(scenario())


def test_marked_volumes_are_removed_only_after_a_literal_y(tmp_path):
    async def scenario():
        app, removals = volume_app(tmp_path, [UNUSED, BUSY])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.toggle_row('volume:sa-app-aaaaaaaa_data')
            await app.choose('remove-volumes')
            await pilot.pause()
            app.screen.query_one(Input).value = 'yes'
            await pilot.click('#remove-volumes-submit')
            await pilot.pause()
            assert removals == [] and app.volume_marked == {'sa-app-aaaaaaaa_data'}
            await app.choose('remove-volumes')
            await pilot.pause()
            assert 'Remove these unused volumes and their data?' in str(app.screen.query_one(Static).content)
            app.screen.query_one(Input).value = 'y'
            await pilot.click('#remove-volumes-submit')
            await pilot.pause()
            await pilot.pause()
            assert removals == [('sa-app-aaaaaaaa_data',)] and not app.volume_marked
            assert 'Removed 1 of 1 selected volume(s).' in str(app.screen.query_one('#error', Static).content)
            assert [item.name for item in app.volumes] == [BUSY.name]
    asyncio.run(scenario())


def test_cancelling_the_volume_confirmation_removes_nothing(tmp_path):
    async def scenario():
        app, removals = volume_app(tmp_path, [UNUSED])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.toggle_row('volume:sa-app-aaaaaaaa_data')
            await app.choose('remove-volumes')
            await pilot.pause()
            await pilot.click('#remove-volumes-cancel')
            await pilot.pause()
            assert removals == [] and app.volume_marked == {'sa-app-aaaaaaaa_data'}
    asyncio.run(scenario())


def test_removing_volumes_needs_a_marked_unused_volume(tmp_path):
    async def scenario():
        app, removals = volume_app(tmp_path, [UNUSED])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.choose('remove-volumes')
            await pilot.pause()
            assert 'Select at least one unused volume' in str(app.screen.query_one('#error', Static).content)
            assert removals == []
    asyncio.run(scenario())


@pytest.mark.parametrize(('outcome', 'expected'), [
    (ReapReport(volumes=0), 'Removed 0 of 1 selected volume(s). The rest are referenced'),
    (ReapReport(volumes=1, diagnostic='incomplete: deadline'), 'Cleanup incomplete: deadline.'),
    (ContainerError('Container engine unavailable.'), 'Container engine unavailable.')])
def test_partial_or_failed_volume_removal_is_explained(tmp_path, outcome, expected):
    def remove_volumes(names):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    async def scenario():
        namespace = SimpleNamespace(inventory=lambda: (), volumes=lambda: (UNUSED,), remove_volumes=remove_volumes)
        app = reaping_app(tmp_path, namespace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.toggle_row('volume:sa-app-aaaaaaaa_data')
            await app.choose('remove-volumes')
            await pilot.pause()
            app.screen.query_one(Input).value = 'y'
            await pilot.click('#remove-volumes-submit')
            await pilot.pause()
            await pilot.pause()
            assert expected in str(app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())


def test_an_engine_that_cannot_list_volumes_still_lists_containers(tmp_path):
    def broken():
        raise ContainerError('Volume inventory unavailable.')
    item = SimpleNamespace(id='id', name='name', image='image', status='running', running=True)
    async def scenario():
        app = reaping_app(tmp_path, SimpleNamespace(inventory=lambda: (item,), volumes=broken))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.containers == (item,) and app.volumes == ()
            assert app.container_summary.endswith('Managed containers: 1 running, 0 stopped')
    asyncio.run(scenario())


def test_the_reap_report_counts_volumes(tmp_path):
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=lambda **options: ReapReport(1, 2, 3, 1))
    async def scenario():
        app = reaping_app(tmp_path, runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            await confirm_reap(app, pilot)
            await pilot.pause()
            assert 'Removed 2 unused container(s) of 1 scope(s), 3 volume(s) and 1 network(s).' in str(
                app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())


def test_volumes_alone_make_the_reap_report_non_empty(tmp_path):
    runtime = SimpleNamespace(inventory=lambda: (), reap_unused=lambda **options: ReapReport(0, 0, 2, 0))
    async def scenario():
        app = reaping_app(tmp_path, runtime)
        async with app.run_test() as pilot:
            await pilot.pause()
            await confirm_reap(app, pilot)
            await pilot.pause()
            assert '2 volume(s)' in str(app.screen.query_one('#error', Static).content)
    asyncio.run(scenario())
