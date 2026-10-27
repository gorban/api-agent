"""Exit feedback waits for real reader completion rather than cancelled waiters."""
import asyncio
from contextvars import ContextVar
import threading

import pytest
from textual.app import App

from api_agent.tui.background_reads import BackgroundReads
from test_tui import fixture


async def entered(event):
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(.001)


def test_reader_keeps_context_arguments_and_result():
    async def scenario():
        reads = BackgroundReads()
        await reads.close(lambda message: pytest.fail(message))
        value = ContextVar('reader_test_value', default=0)
        value.set(3)
        assert await reads.run('configuration review', lambda *, add: value.get() + add, add=4) == 7
        await reads.close(lambda message: pytest.fail(message))
        assert not reads.pending
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [False, True])
def test_cancelled_waiter_keeps_reader_until_visible_drain(failure):
    async def scenario():
        reads = BackgroundReads()
        started, release, stopped = threading.Event(), threading.Event(), threading.Event()
        messages = []
        def read():
            started.set()
            assert release.wait(3)
            if failure:
                raise OSError('unavailable source')
            return 1
        waiter = asyncio.create_task(reads.run('cost history synchronization', read, cancel=stopped.set))
        try:
            await entered(started)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert stopped.is_set() and len(reads.pending) == 1
            def output(message):
                messages.append(message)
                release.set()
            await reads.close(output)
            assert messages == [
                'Finishing background work: cost history synchronization. Waiting for current reads to finish.',
                'Background work finished.']
            assert not reads.pending
        finally:
            release.set()
    asyncio.run(scenario())


def test_fast_reader_exit_is_quiet_and_named_cancellation_is_scoped():
    async def scenario():
        reads = BackgroundReads()
        started, release = threading.Event(), threading.Event()
        stops = []
        def read():
            started.set()
            assert release.wait(3)
        waiter = asyncio.create_task(reads.run('configuration review', read, cancel=lambda: stops.append('config')))
        try:
            await entered(started)
            reads.cancel('cost history synchronization')
            assert stops == []
            closing = asyncio.create_task(reads.close(lambda message: pytest.fail(message)))
            await asyncio.sleep(0)
            release.set()
            await closing
            await waiter
            assert stops == ['config']
        finally:
            release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [False, True])
def test_application_restores_terminal_before_reader_feedback(tmp_path, monkeypatch, capsys, failure):
    async def scenario():
        app = fixture(tmp_path)
        started, release = threading.Event(), threading.Event()
        def read():
            started.set()
            assert release.wait(3)
        waiter = asyncio.create_task(app.background_reads.run('configuration review', read))
        async def terminal_run(self, **kwargs):
            import sys
            print('terminal restored', file=sys.stderr)
            if failure:
                raise ValueError('application failed')
            return 130
        monkeypatch.setattr(App, 'run_async', terminal_run)
        try:
            await entered(started)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            running = asyncio.create_task(app.run_async(headless=True))
            output = ''
            async with asyncio.timeout(3):
                while not output.endswith('Waiting for current reads to finish.\n'):
                    output += capsys.readouterr().err
                    await asyncio.sleep(.01)
            assert output.startswith('terminal restored\nFinishing background work: configuration review.')
            assert not running.done()
            release.set()
            if failure:
                with pytest.raises(ValueError, match='application failed'):
                    await running
            else:
                assert await running == 130
            assert capsys.readouterr().err == 'Background work finished.\n'
            assert not app.background_reads.pending
        finally:
            release.set()
    asyncio.run(scenario())


def test_repeated_cancellation_does_not_abandon_owned_reader(tmp_path, monkeypatch, capsys):
    async def scenario():
        app = fixture(tmp_path)
        started, release = threading.Event(), threading.Event()
        def read():
            started.set()
            assert release.wait(3)
        waiter = asyncio.create_task(app.background_reads.run('configuration review', read))
        async def terminal_run(self, **kwargs):
            return 0
        monkeypatch.setattr(App, 'run_async', terminal_run)
        try:
            await entered(started)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            running = asyncio.create_task(app.run_async())
            async with asyncio.timeout(3):
                while 'Waiting for current reads' not in capsys.readouterr().err:
                    await asyncio.sleep(.01)
            running.cancel()
            await asyncio.sleep(0)
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done() and app.background_reads.pending
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert not app.background_reads.pending
        finally:
            release.set()
    asyncio.run(scenario())


def test_menu_cancels_old_history_and_reentry_uses_a_new_snapshot(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import sys
    from test_historical_cost_tui import Snapshot
    from test_test_table import identities
    async def scenario():
        releases = [threading.Event(), threading.Event()]
        starts = [threading.Event(), threading.Event()]
        stops = [threading.Event(), threading.Event()]
        snapshots = [Snapshot(1), Snapshot(2)]
        for i, snapshot in enumerate(snapshots):
            def read(index=i):
                starts[index].set()
                assert releases[index].wait(5)
                return snapshots[index]
            snapshot.synchronize = read
            snapshot.cancel_synchronization = stops[i].set
        remaining = iter(snapshots)
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=lambda *args, **options: next(remaining)))
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2))
        try:
            async with app.run_test() as pilot:
                await entered(starts[0])
                await app.close_view()
                assert app.screen.kind == 'menu' and stops[0].is_set()
                assert not stops[1].is_set()
                app.mode = 'integration'
                app.show_selection()
                await entered(starts[1])
                releases[0].set()
                await pilot.pause()
                assert app.cost_estimator is snapshots[1]
        finally:
            for release in releases:
                release.set()
    asyncio.run(scenario())


@pytest.mark.parametrize('covered', [False, True])
@pytest.mark.parametrize('reenter_before_collection', [False, True])
def test_queued_history_does_not_start_after_picker_closes(tmp_path, monkeypatch, covered, reenter_before_collection):
    from types import SimpleNamespace
    from unittest.mock import Mock
    import sys
    from test_historical_cost_tui import Snapshot
    from test_test_table import identities
    async def scenario():
        snapshots = []
        def estimate(*args, **options):
            snapshot = Snapshot(1)
            snapshot.synchronize = Mock(return_value=snapshot)
            snapshots.append(snapshot)
            return snapshot
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=estimate))
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2))
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            app.show_selection()
            queued = app.cost_estimator
            if covered:
                from api_agent.tui.app import SettingHelpDialog
                app.push_screen(SettingHelpDialog('History', 'Covered picker'))
            app.show_menu()
            if reenter_before_collection:
                app.mode = 'integration'
                app.show('collecting', 'Collecting integration tests')
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen.kind == ('collecting' if reenter_before_collection else 'menu')
            queued.synchronize.assert_not_called()
            app.mode = 'integration'
            app.show_selection()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.cost_estimator is snapshots[-1] is not queued
            snapshots[-1].synchronize.assert_called_once()
    asyncio.run(scenario())


def test_active_history_failure_preserves_picker(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    import sys
    from test_historical_cost_tui import Snapshot
    from test_test_table import identities
    async def scenario():
        snapshot = Snapshot(1)
        snapshot.synchronize = Mock(side_effect=OSError('source unavailable'))
        monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                            SimpleNamespace(estimator_for=lambda *args, **options: snapshot))
        app = fixture(tmp_path, 'integration', collector=lambda *args: identities(2))
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            snapshot.synchronize.assert_called_once()
            assert app.screen.kind == 'tests'
            assert app.cost_estimator is snapshot
            assert not app.background_reads.pending
    asyncio.run(scenario())
