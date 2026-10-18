"""Settings changes cannot leave a stale picker or suite estimator active."""
import asyncio
from copy import deepcopy
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_historical_cost_tui import Snapshot
from test_test_table import identities
from test_tui import fixture


@pytest.fixture
def configured_picker(tmp_path, monkeypatch):
    app = fixture(tmp_path, 'integration')
    app.selection = SimpleNamespace(tests=identities(2))
    values = {'TEST_COST_HISTORY_DAYS': 7}
    app.session = SimpleNamespace(result=lambda: SimpleNamespace(values=values))
    snapshots = []
    def build(context, tests, config):
        snapshot = Snapshot(config['TEST_COST_HISTORY_DAYS'])
        snapshots.append(snapshot)
        return snapshot
    monkeypatch.setitem(sys.modules, 'api_agent.testing.cost_history',
                        SimpleNamespace(estimator_for=build))
    app.cost_estimator = app.fresh_cost_estimator()
    return app, values, snapshots


def test_start_freezes_new_settings_before_passing_estimator(configured_picker):
    app, values, snapshots = configured_picker
    original = app.cost_estimator
    values['TEST_COST_HISTORY_DAYS'] = 30
    def runner(*, historical_estimator):
        pass

    options = app.historical_runner_options(runner)

    assert options['historical_estimator'] is snapshots[-1] is app.cost_estimator
    assert snapshots[-1].value == 30 and original.value == 7
    assert original.picker_configuration == {'TEST_COST_HISTORY_DAYS': 7}
    assert not app.refresh_cost_configuration()


def test_late_refresh_rebuilds_for_new_settings_without_replacing_selection(configured_picker):
    app, values, snapshots = configured_picker
    original, selected = app.cost_estimator, app.selection
    stale_result = Snapshot(999)
    original.synchronize = lambda: stale_result
    app.refresh_rows = Mock()
    app.run_worker = Mock(side_effect=lambda coroutine, **options: coroutine.close())
    # The consumer is the real handler, with a synthetic picker shell.
    from api_agent.tui.app import WorkbenchScreen
    picker = WorkbenchScreen('tests', 'Select integration tests', '', (), ())
    app._screen_stacks[app._current_mode] = [picker]
    picker.call_after_refresh = Mock()
    values['TEST_COST_HISTORY_DAYS'] = 30

    asyncio.run(app.synchronize_cost_history(original, selected))

    assert app.selection is selected
    assert app.cost_estimator is snapshots[-1] and app.cost_estimator is not stale_result
    assert app.cost_estimator.value == 30
    assert app.cost_estimator.picker_configuration == deepcopy(values)
    app.refresh_rows.assert_not_called()
    picker.call_after_refresh.assert_called_once_with(app.refresh_picker_costs, picker, selected)
    app.run_worker.assert_called_once()


def test_unconfigured_estimator_does_not_force_a_refresh(configured_picker):
    app, _, _ = configured_picker
    app.cost_estimator = Snapshot(1)
    assert not app.refresh_cost_configuration()


def test_start_refreshes_clock_sensitive_predictions_with_unchanged_settings(configured_picker):
    app, _, snapshots = configured_picker
    old = app.cost_estimator
    # The factory represents a fresh clock/window read; cached picker values
    # must not overwrite that read merely because the settings match.
    old.value = 999
    def runner(*, historical_estimator):
        pass

    options = app.historical_runner_options(runner)

    assert options['historical_estimator'] is snapshots[-1] and snapshots[-1] is not old
    assert snapshots[-1].value == 7 and old.value == 999
