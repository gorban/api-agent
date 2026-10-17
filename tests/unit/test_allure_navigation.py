"""Result shortcuts preserve test identity, focus and operation ownership."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Input, Static

from api_agent.tui import allure
from api_agent.tui.allure import AllureNavigation
from api_agent.testing.allure_viewer import PreviewSnapshot, ViewerLocation
from test_tui import fixture


class Viewer:
    def __init__(self, *, fail=False, close_fail=False):
        self.calls=[]
        self.fail=fail
        self.close_fail=close_fail
        self.closed=False
    async def show_preview(self,snapshot,result_file):
        self.calls.append((snapshot,result_file))
        if self.fail:
            raise RuntimeError('Preview unavailable')
        return ViewerLocation('http://127.0.0.1:5252/exact','#inspection')
    async def show_history(self,source):
        self.calls.append(source)
        if self.fail:
            raise RuntimeError('History unavailable')
        return ViewerLocation('http://127.0.0.1:5252/history',browser_error='Browser unavailable')
    async def aclose(self):
        if self.close_fail:
            raise RuntimeError('Cleanup pending')
        self.closed=True


def snapshot():
    return PreviewSnapshot({}, {}, {'one':'one-result.json'})


class RetentionHarness(AllureNavigation):
    def __init__(self):
        self.initialize_allure()
        self._inspection_generation = object()
        self._inspection_shutdown = False
        self.refreshed = []
        self.screen = SimpleNamespace(refresh_bindings=lambda: self.refreshed.append(True))


def test_retained_preview_budget_includes_boundary_and_rejects_next_attempt(monkeypatch):
    monkeypatch.setattr(allure, 'MAX_RETAINED_ALLURE_BYTES', 5)
    app = RetentionHarness()
    first = PreviewSnapshot({'a': b'123'}, {}, {'one': 'a'})
    boundary = PreviewSnapshot({'b': b'45'}, {}, {'two': 'b'})
    rejected = PreviewSnapshot({'c': b'6'}, {}, {'three': 'c'})

    for item in (first, boundary, rejected):
        app.receive_allure_snapshot(item, app._inspection_generation)

    assert app.allure_snapshots == {'one': first, 'two': boundary}
    assert app._allure_retained_bytes == 5
    assert app._allure_budget_unavailable == {'three'}
    assert len(app.refreshed) == 3


def test_repeated_attempt_replaces_old_binding_even_when_rejected(monkeypatch):
    monkeypatch.setattr(allure, 'MAX_RETAINED_ALLURE_BYTES', 3)
    app = RetentionHarness()
    first = PreviewSnapshot({'a': b'123'}, {}, {'one': 'a'})
    too_large = PreviewSnapshot({'b': b'1234'}, {}, {'one': 'b'})
    retry = PreviewSnapshot({'c': b'12'}, {}, {'one': 'c'})

    app.receive_allure_snapshot(first, app._inspection_generation)
    app.receive_allure_snapshot(too_large, app._inspection_generation)
    assert app.allure_snapshots == {}
    assert app._allure_retained_bytes == 0
    assert app._allure_budget_unavailable == {'one'}
    app.receive_allure_snapshot(retry, app._inspection_generation)

    assert app.allure_snapshots == {'one': retry}
    assert app._allure_retained_bytes == 2
    assert app._allure_budget_unavailable == set()


def test_final_snapshot_replaces_previews_and_is_counted_once(monkeypatch):
    monkeypatch.setattr(allure, 'MAX_RETAINED_ALLURE_BYTES', 5)
    app = RetentionHarness()
    first = PreviewSnapshot({'a': b'123'}, {}, {'one': 'a'})
    second = PreviewSnapshot({'b': b'45'}, {}, {'two': 'b'})
    final = PreviewSnapshot({'all': b'12345'}, {}, {'one': 'a', 'two': 'b'}, False)

    app.receive_allure_snapshot(first, app._inspection_generation)
    app.receive_allure_snapshot(second, app._inspection_generation)
    app.receive_allure_snapshot(final, app._inspection_generation)
    app.receive_allure_snapshot(final, app._inspection_generation)

    assert app.allure_snapshots == {'one': final, 'two': final}
    assert app._allure_retained_bytes == 5
    assert app._allure_budget_unavailable == set()


def test_stale_snapshot_does_not_change_retention_and_clear_resets_state(monkeypatch):
    monkeypatch.setattr(allure, 'MAX_RETAINED_ALLURE_BYTES', 1)
    app = RetentionHarness()
    retained = PreviewSnapshot({'a': b'1'}, {}, {'one': 'a'})
    rejected = PreviewSnapshot({'b': b'22'}, {}, {'two': 'b'})

    app.receive_allure_snapshot(retained, app._inspection_generation)
    app.receive_allure_snapshot(rejected, app._inspection_generation)
    app.receive_allure_snapshot(retained, object())
    app._inspection_shutdown = True
    app.receive_allure_snapshot(retained, app._inspection_generation)
    assert app.allure_snapshots == {'one': retained}
    assert app._allure_budget_unavailable == {'two'}
    app.clear_allure_snapshots()

    assert app.allure_snapshots == {}
    assert app._allure_budget_unavailable == set()
    assert app._allure_retained_bytes == 0


@pytest.mark.parametrize('status',['PASSED','FAILED','ERROR','SKIPPED','CANCELLED'])
def test_live_results_open_exact_completed_attempt_without_changing_execution(tmp_path,status):
    async def scenario():
        app=fixture(tmp_path)
        viewer=Viewer()
        app.allure_viewer=viewer
        async with app.run_test(size=(110,35)) as pilot:
            await pilot.pause()
            app.mode='integration'
            app.unit_rows={'one':(status,'one','1s'),'two':('RUNNING','two','2s')}
            app.show('test-results','Results',rows=list(app.unit_rows.items()))
            await pilot.pause()
            current=snapshot()
            app.receive_allure_snapshot(current,app._inspection_generation)
            app.receive_allure_snapshot(current,object())
            assert app.screen.check_action('view_allure',()) is True
            await pilot.press('a')
            await pilot.pause()
            assert viewer.calls==[(current,'one-result.json')]
            assert app.screen.kind=='test-results' and app.unit_rows['two'][0]=='RUNNING'
            table=app.screen.query_one(DataTable)
            table.action_extend_range(1)
            assert app.screen.check_action('view_allure',()) is False
            table.action_clear_range()
            assert app.selected_allure_result(app.screen) is None
            table.move_cursor(row=0)
            app.mode='unit'
            assert app.selected_allure_result(app.screen) is None
            app.mode='e2e'
            assert app.selected_allure_result(app.screen)[1]=='one-result.json'
            assert app.screen.check_action('end_inspection',()) is True
            await app.screen.action_end_inspection()
            assert viewer.closed and app.allure_viewer is None
    asyncio.run(scenario())


def test_shortcut_is_hidden_for_pending_and_absent_results(tmp_path):
    async def scenario():
        app=fixture(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode='integration'
            app.unit_rows={'one':('PENDING','one','')}
            app.show('test-results','Results',rows=list(app.unit_rows.items()))
            await pilot.pause()
            await app.open_selected_allure()
            app.receive_allure_snapshot(snapshot(),app._inspection_generation)
            assert app.selected_allure_result(app.screen) is None
            app._allure_opening=True
            app.unit_rows['one']=('PASSED','one','1s')
            assert app.selected_allure_result(app.screen) is None
            app._allure_opening=False
            table=app.screen.query_one(DataTable)
            table.clear()
            assert app.selected_allure_result(app.screen) is None
            app._inspection_shutdown=True
            app.receive_allure_snapshot(PreviewSnapshot({}, {}, {'other':'other-result.json'}),app._inspection_generation)
            assert 'other' not in app.allure_snapshots
    asyncio.run(scenario())


def test_viewer_failure_preserves_results_and_cleanup_can_retry(tmp_path,monkeypatch):
    async def scenario():
        app=fixture(tmp_path)
        notices=[]
        monkeypatch.setattr(app,'notify',lambda text,**kwargs:notices.append(text))
        viewer=Viewer(fail=True,close_fail=True)
        app.allure_viewer=viewer
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode='integration'
            app.unit_rows={'one':('FAILED','one','1s')}
            app.show('test-results','Results',rows=list(app.unit_rows.items()))
            await pilot.pause()
            app.receive_allure_snapshot(snapshot(),app._inspection_generation)
            await app.open_selected_allure()
            assert notices[-1]=='Preview unavailable'
            assert app.unit_rows['one'][0]=='FAILED'
            assert await app.close_allure() is False
            assert app.allure_viewer is viewer
            assert await app.end_inspection() is False
            viewer.close_fail=False
            assert await app.close_allure() is True
            assert app.allure_viewer is None and viewer.closed
            assert await app.close_allure() is True
    asyncio.run(scenario())


@pytest.mark.parametrize('bucket,failed',[(False,False),(True,False),(False,True)])
def test_main_menu_history_and_refresh_are_separate_from_suite_execution(tmp_path,monkeypatch,bucket,failed):
    async def scenario():
        fields={'ALLURE_S3_BUCKET':{'type':'str','default':'reports'}} if bucket else {}
        app=fixture(tmp_path,fields=fields)
        app.context.owner_root=tmp_path
        viewer=Viewer(fail=failed)
        app.allure_viewer=viewer
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode='allure'
            await app.show_allure_history()
            await pilot.pause()
            assert app.screen.kind=='allure'
            source=viewer.calls[0]
            assert (source.storage is not None)==bucket
            assert not app.test_run_active()
            if failed:
                assert 'History unavailable' in str(app.screen.query_one('#error',Static).content)
            else:
                assert 'http://127.0.0.1:5252/history' in str(app.screen.query_one('#body',Static).content)
                app._allure_opening=True
                await app.choose('refresh-allure')
                assert len(viewer.calls)==1
                app._allure_opening=False
                await app.choose('refresh-allure')
                await pilot.pause()
                assert len(viewer.calls)==2
            await app.close_view()
            await pilot.pause()
            assert viewer.closed and app.screen.kind=='menu'
    asyncio.run(scenario())


def test_snapshot_runner_compatibility_and_factory(tmp_path,monkeypatch):
    app=fixture(tmp_path)
    callback=[]
    app.call_from_thread=lambda method,*args:callback.append(args)
    def old_runner():
        pass
    def supported(*,on_snapshot):
        pass
    def flexible(**kwargs):
        pass
    assert app.allure_runner_options(old_runner,1)=={}
    app.allure_runner_options(supported,2)['on_snapshot']('data')
    app.allure_runner_options(flexible,3)['on_snapshot']('data')
    assert callback==[('data',2),('data',3)]
    created=[]
    monkeypatch.setattr('api_agent.testing.allure_viewer.ManagedAllureViewer',lambda port:created.append(port) or Viewer())
    assert app._allure() is app._allure()
    assert created==[5252]


def test_history_menu_mode_configuration_dispatch(tmp_path,monkeypatch):
    async def scenario():
        app=fixture(tmp_path)
        seen=[]
        async def history():
            seen.append('history')
        monkeypatch.setattr(app,'show_allure_history',history)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode='allure'
            await app.configure_operation()
            await pilot.pause()
            assert seen==['history']
    asyncio.run(scenario())


def test_empty_result_screen_and_missing_attempt_have_clear_availability(tmp_path,monkeypatch):
    async def scenario():
        app=fixture(tmp_path)
        notices=[]
        monkeypatch.setattr(app,'notify',lambda text,**kwargs:notices.append(text))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode='integration'
            await app.show('test-results','Empty results')
            assert app.selected_allure_result(app.screen) is None
            app.unit_rows={'one':('ERROR','one','1s')}
            await app.show('test-results','Results',rows=list(app.unit_rows.items()))
            assert app.selected_allure_result(app.screen)==(None,None)
            await app.open_selected_allure()
            assert 'unavailable for this test attempt' in notices[-1]
            assert app.allure_viewer is None
    asyncio.run(scenario())


def test_budget_rejection_keeps_selected_shortcut_with_honest_message(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(allure, 'MAX_RETAINED_ALLURE_BYTES', 1)
        app = fixture(tmp_path)
        notices = []
        monkeypatch.setattr(app, 'notify', lambda text, **kwargs: notices.append(text))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.mode = 'integration'
            app.unit_rows = {'one': ('PASSED', 'one', '1s')}
            await app.show('test-results', 'Results', rows=list(app.unit_rows.items()))
            oversized = PreviewSnapshot({'one-result.json': b'12'}, {}, {'one': 'one-result.json'})
            app.receive_allure_snapshot(oversized, app._inspection_generation)

            assert app.screen.check_action('view_allure', ()) is True
            assert app.selected_allure_result(app.screen) == (None, None)
            await app.open_selected_allure()
            assert 'retained preview memory budget was reached' in notices[-1]
            assert 'If this run finalizes and publishes' in notices[-1]
            assert app.allure_viewer is None
    asyncio.run(scenario())


@pytest.mark.parametrize('operation',['selected','history'])
@pytest.mark.parametrize('failed',[False,True])
def test_late_viewer_completion_does_not_update_a_replaced_operation(tmp_path,monkeypatch,operation,failed):
    async def scenario():
        app=fixture(tmp_path)
        notices=[]
        monkeypatch.setattr(app,'notify',lambda text,**kwargs:notices.append(text))
        class Late(Viewer):
            async def show_preview(self,*args):
                app._allure_generation=object()
                app._allure_opening=True
                if failed:
                    raise RuntimeError('old failure')
                return ViewerLocation('http://127.0.0.1:5252/old')
            async def show_history(self,*args):
                return await self.show_preview()
        viewer=Late()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.allure_viewer=viewer
            app.mode='integration'
            app.unit_rows={'one':('PASSED','one','1s')}
            await app.show('test-results','Results',rows=list(app.unit_rows.items()))
            app.receive_allure_snapshot(snapshot(),app._inspection_generation)
            if operation=='selected':
                await app.open_selected_allure()
            else:
                await app.show_allure_history()
                assert 'old' not in str(app.screen.query_one('#body',Static).content)
                assert not str(app.screen.query_one('#error',Static).content)
            assert notices==[]
            assert app._allure_opening
    asyncio.run(scenario())


@pytest.mark.parametrize('replacement',[False,True])
def test_exported_cleanup_receipt_retries_viewer_and_preserves_replacement(tmp_path,replacement):
    async def scenario():
        app=fixture(tmp_path)
        viewer=Viewer(close_fail=True)
        app.allure_viewer=viewer
        receipt=app.inspection_cleanup_receipt()
        assert viewer in receipt.owners
        with pytest.raises(Exception):
            await receipt.retry()
        assert app.allure_viewer is viewer
        viewer.close_fail=False
        other=Viewer()
        if replacement:
            app.allure_viewer=other
        await receipt.retry()
        assert viewer.closed
        assert app.allure_viewer is (other if replacement else None)
    asyncio.run(scenario())


def test_failed_close_keeps_main_history_available_for_cleanup_retry(tmp_path,monkeypatch):
    async def scenario():
        app=fixture(tmp_path)
        notices=[]
        monkeypatch.setattr(app,'notify',lambda text,**kwargs:notices.append(text))
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.show('allure','History')
            owner=Viewer(close_fail=True)
            app.allure_viewer=owner
            await app.close_view()
            assert app.screen.kind=='allure' and app.allure_viewer is owner
            owner.close_fail=False
            await app.close_view()
            await pilot.pause()
            assert app.screen.kind=='menu' and owner.closed
    asyncio.run(scenario())


def test_closing_during_history_screen_mount_prevents_late_resource_acquisition(tmp_path,monkeypatch):
    async def scenario():
        app=fixture(tmp_path)
        entered=asyncio.Event()
        release=asyncio.Event()
        async with app.run_test() as pilot:
            await pilot.pause()
            original=app.show
            async def blocked(*args,**kwargs):
                entered.set()
                await release.wait()
                await original(*args,**kwargs)
            monkeypatch.setattr(app,'show',blocked)
            operation=asyncio.create_task(app.show_allure_history())
            await entered.wait()
            assert await app.close_allure()
            release.set()
            await operation
            assert app.allure_viewer is None and not app._allure_opening
    asyncio.run(scenario())
