"""Interactive full-unit admission uses the freshly collected identities."""
import asyncio
from unittest.mock import Mock

import pytest
from textual.widgets import Input, Static

from api_agent.testing.collection import TestIdentity as Identity
from api_agent.testing.lifecycle import TestRunLifecycle
from api_agent.testing.runner import UnitRunResult
from api_agent.testing.selection import SelectionModel
from test_tui import fixture


def identity(name):
    return Identity(f'base::test.py::test_{name}', 'base', 'test.py', f'test.py::test_{name}')


@pytest.mark.parametrize('selection', ['group', 'individual', 'expanded'])
@pytest.mark.parametrize('response', ['y', 'n', 'Y', 'escape'])
def test_full_selection_requires_literal_y_after_recollection(tmp_path, selection, response):
    async def run():
        tests = [identity('one'), identity('two')]
        app = fixture(tmp_path, collector=lambda *args: tests)
        app.mode = 'unit'
        app.selection = SelectionModel(tests[:1] if selection == 'expanded' else tests)
        if selection == 'individual':
            app.selection.selected_scopes.clear()
        app.test_run = TestRunLifecycle()
        app.collection_cancel = app.test_run.cancel_event
        app.unit_runner = Mock(return_value=UnitRunResult(0, (), (), ''))
        async with app.run_test(size=(100, 40)) as pilot:
            task = asyncio.create_task(app._run_units())
            for _ in range(100):
                await pilot.pause(.02)
                if app.screen.__class__.__name__ == 'QuitDialog':
                    break
            message = str(app.screen.query_one('#quit-message', Static).content)
            assert 'Full-suite execution in the TUI is slower.' in message
            assert './launch.sh --tui-mode unit --no-std-in' in message
            assert 'focused partial selections' in message
            assert not app.unit_runner.called
            if response == 'escape':
                await pilot.press('escape')
            else:
                answer = app.screen.query_one('#quit-answer', Input)
                answer.value = response
                answer.focus()
                await pilot.pause()
                await pilot.press('enter')
            await asyncio.wait_for(task, 5)
            assert app.unit_runner.called is (response == 'y')
            assert not app.test_run.active
    asyncio.run(run())


@pytest.mark.parametrize('case', ['partial', 'new_unselected', 'noninteractive'])
def test_non_full_or_noninteractive_run_does_not_show_warning(tmp_path, case):
    async def run():
        first, second = identity('one'), identity('two')
        fields = {'NO_STD_IN': {'type': 'bool', 'default': False, 'acknowledge_default': False}}
        app = fixture(tmp_path, fields=fields, collector=lambda *args: [first, second])
        app.mode = 'unit'
        app.selection = SelectionModel([first] if case == 'new_unselected' else [first, second])
        if case == 'partial':
            app.selection.selected_ids.remove(second.id)
            app.selection.selected_scopes.clear()
        if case == 'new_unselected':
            app.selection.selected_scopes.clear()
        app.test_run = TestRunLifecycle()
        app.collection_cancel = app.test_run.cancel_event
        app.unit_runner = Mock(return_value=UnitRunResult(0, (), (), ''))
        async with app.run_test() as pilot:
            if case == 'noninteractive':
                app.session.set_value('NO_STD_IN', 'True', persist=False)
            await asyncio.wait_for(app._run_units(), 5)
            # Let the asynchronously mounted results screen settle before teardown.
            await pilot.pause()
            assert app.unit_runner.called
            assert app.screen.__class__.__name__ != 'QuitDialog'
    asyncio.run(run())


def test_cancel_during_full_unit_confirmation_does_not_execute(tmp_path):
    async def run():
        tests = [identity('one')]
        app = fixture(tmp_path, collector=lambda *args: tests)
        app.mode = 'unit'
        app.selection = SelectionModel(tests)
        app.test_run = TestRunLifecycle()
        app.collection_cancel = app.test_run.cancel_event
        app.unit_runner = Mock(return_value=UnitRunResult(0, (), (), ''))
        async with app.run_test() as pilot:
            task = asyncio.create_task(app._run_units())
            for _ in range(100):
                await pilot.pause(.02)
                if app.screen.__class__.__name__ == 'QuitDialog':
                    break
            app.collection_cancel.set()
            await asyncio.wait_for(task, 5)
            assert not app.unit_runner.called
            assert not app.test_run.active
    asyncio.run(run())


def test_direct_cancellation_removes_full_unit_confirmation(tmp_path):
    async def run():
        tests = [identity('one')]
        app = fixture(tmp_path, collector=lambda *args: tests)
        app.mode = 'unit'
        app.selection = SelectionModel(tests)
        app.test_run = TestRunLifecycle()
        app.collection_cancel = app.test_run.cancel_event
        app.unit_runner = Mock(return_value=UnitRunResult(0, (), (), ''))
        async with app.run_test() as pilot:
            task = asyncio.create_task(app._run_units())
            for _ in range(100):
                await pilot.pause(.02)
                if app.screen.__class__.__name__ == 'QuitDialog':
                    break
            assert app.screen.__class__.__name__ == 'QuitDialog'
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await pilot.pause()
            assert app.screen.__class__.__name__ != 'QuitDialog'
            assert not app.unit_runner.called
            assert not app.test_run.active
    asyncio.run(run())


@pytest.mark.parametrize('size', [(100, 40), (80, 24)])
@pytest.mark.parametrize('outcome,notice,selection_retained', [
    (True, 'Value copied.', False),
    (False, 'Value sent to terminal clipboard.', False),
    ('failure', 'Could not copy the value.', True),
])
def test_full_unit_warning_copy_feedback_is_visible_above_modal(tmp_path, monkeypatch, outcome, notice, selection_retained, size):
    from unittest.mock import AsyncMock
    from textual.selection import Selection
    from textual.widgets._toast import Toast
    from api_agent.tui.clipboard import ClipboardError

    async def scenario():
        tests = [identity('one')]
        app = fixture(tmp_path, collector=lambda *args: tests)
        app.mode = 'unit'
        app.selection = SelectionModel(tests)
        app.test_run = TestRunLifecycle()
        app.collection_cancel = app.test_run.cancel_event
        app.unit_runner = Mock(return_value=UnitRunResult(0, (), (), ''))
        copy = AsyncMock(return_value=outcome)
        if outcome == 'failure':
            copy.side_effect = ClipboardError('synthetic failure')
        monkeypatch.setattr('api_agent.tui.app.copy_text', copy)
        async with app.run_test(size=size, notifications=True) as pilot:
            task = asyncio.create_task(app._run_units())
            for _ in range(100):
                await pilot.pause(.02)
                if (app.screen.__class__.__name__ == 'QuitDialog'
                        and app.screen.query('#quit-message')
                        and app.screen.query('#quit-answer')
                        and app.screen.query('#quit-submit')):
                    break
            dialog = app.screen
            assert dialog.__class__.__name__ == 'QuitDialog'
            body = dialog.query_one('#quit-message', Static)
            dialog.selections = {body: Selection(None, None)}
            selected = dialog.get_selected_text()
            assert './launch.sh --tui-mode unit --no-std-in' in selected

            try:
                await pilot.press('ctrl+c')
                await pilot.pause()

                copy.assert_awaited_once_with(app, selected)
                assert dialog.get_selected_text() == (selected if selection_retained else None)
                assert app.screen is dialog
                assert not app.unit_runner.called
                toast = dialog.query_one(Toast)
                assert notice in str(toast.render())
                assert toast.screen is dialog and toast.visible and toast.region.area > 0
                front, _ = dialog.get_widget_at(toast.region.x + 1, toast.region.y + 1)
                assert front is toast or toast in front.ancestors
            finally:
                await pilot.press('escape')
                await asyncio.wait_for(task, 5)
                await pilot.pause()
            assert not app.unit_runner.called
    asyncio.run(scenario())
