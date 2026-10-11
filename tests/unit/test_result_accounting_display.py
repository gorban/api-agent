"""Completed details use actual accounting and the finalized suite estimate."""
import asyncio
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from api_agent.telemetry.usage import SuiteUsage
from api_agent.telemetry.usage_output import tokens_text
from api_agent.tui.test_results import test_details as render_details
from api_agent.tui.test_table import TestTable
from test_tui import fixture
from test_test_table import identities


def accounting(test_id):
    tracker = SuiteUsage('run')
    tracker.add_usage(provider='selected', model='example', session_id=test_id,
        input_tokens=11, output_tokens=5, cache_read_tokens=2,
        cache_creation_tokens=0, reasoning_tokens=1)
    report = tracker.report()
    report['category_costs_usd'] = {'input_tokens':.04,'output_tokens':.06,'total_tokens':.1}
    report['test_costs'] = {test_id:{'category_costs_usd':{'input_tokens':.008,'output_tokens':.012,
        'cache_read_tokens':.001,'reasoning_tokens':.0024,'total_tokens':.02}}}
    return report


@pytest.mark.parametrize('status,color', [('passed','green'),('skipped','dark_orange'),('failed','red')])
def test_shared_details_metadata_precedes_status_or_full_failure_trace(status, color):
    result = SimpleNamespace(identity=SimpleNamespace(id='test.py::test[one]'),
                             duration=1.234, status=status, traceback='')
    usage = accounting(result.identity.id)
    rendered = render_details(result, usage, {'estimated_cost_usd':.02, 'basis':'historical_quota',
        'advance_prediction':{'estimated_cost_usd':.05}})
    assert rendered.plain.startswith(result.identity.id + '\n\nExecution time: 1.234s')
    assert 'This test cost: $0.020' in rendered.plain
    assert 'This test cost: $0.020\n  Historical average: $0.050' in rendered.plain
    assert 'Cost basis: historical_quota\n' + tokens_text(usage, result.identity.id) in rendered.plain
    assert 'input: 11 — $0.008' in rendered.plain and 'output: 5 — $0.012' in rendered.plain
    assert 'total (input + output): 16 — $0.020' in rendered.plain and 'total (input + output): 16 — $0.100' not in rendered.plain
    assert rendered.plain.endswith(tokens_text(usage, result.identity.id) + '\n\n' + status.upper())
    assert rendered.spans[-1].style == color
    result.traceback = 'AssertionError: failed\n  source.py:42\noriginal traceback'
    detailed = render_details(result, usage, {}, historical_tokens=True)
    assert detailed.plain.endswith(result.traceback)
    assert 'This test cost: unavailable' in detailed.plain
    assert '  Historical average: unavailable' in detailed.plain
    assert detailed.plain.index('Earlier compatible runs') < detailed.plain.index('Tokens this run')
    assert not detailed.spans


@pytest.mark.parametrize('mode', ['integration','e2e'])
@pytest.mark.parametrize('current,historical,opens', [(True,False,True),(False,True,True),
                                                    (False,False,False)])
def test_passing_details_open_only_for_current_or_compatible_historical_tokens(tmp_path, monkeypatch, mode, current, historical, opens):
    # This snapshot requires no remote storage, inference or authentication.
    from test_historical_cost_tui import Snapshot
    snapshot = Snapshot()
    tests = identities(1)
    snapshot.predictions = {tests[0].id:{'has_token_history':historical}}
    monkeypatch.setattr('api_agent.testing.cost_history.estimator_for', lambda *args: snapshot)
    async def scenario():
        app = fixture(tmp_path, mode, collector=lambda *a:tests)
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            result = SimpleNamespace(identity=tests[0], duration=1.25, status='passed', traceback='')
            app.unit_result = SimpleNamespace(tests=(result,), cancelled=False)
            app.suite_usage = accounting(tests[0].id) if current else SuiteUsage('run').report()
            app.test_costs = {tests[0].id:{'estimated_cost_usd':.000011}}
            app.unit_rows = {tests[0].id:('PASSED',tests[0].id,'1.250s')}
            app.show_unit_results()
            await pilot.pause()
            assert tokens_text(app.suite_usage) in app.screen.body
            assert app.screen.kind == 'test-results'
            app.screen.query_one(TestTable).focus()
            await pilot.press('enter')
            await pilot.pause()
            if not opens:
                assert app.screen.kind == 'test-results'
                return
            assert app.screen.kind == 'test-failure'
            body = app.screen.query_one('#body', Static).content
            assert body.plain.endswith('PASSED') and body.spans[-1].style == 'green'
            assert 'Execution time: 1.250s' in body.plain
            assert 'This test cost: $0.000011' in body.plain
            assert ('Earlier compatible runs' in body.plain) == historical
            assert tokens_text(app.suite_usage, tests[0].id) in body.plain
            assert app.screen.selected_test_name() == tests[0].id
            # Existing copy fallback and close action also work for passed details.
            copied = []
            monkeypatch.setattr(app, 'copy_to_clipboard', copied.append)
            await app.screen.action_copy_value()
            assert copied == [tests[0].id]
            await pilot.press('c')
            await pilot.pause()
            assert app.screen.kind == 'test-results'
    asyncio.run(scenario())


def test_failure_without_trace_still_opens_metadata_and_error_status(tmp_path):
    async def scenario():
        tests = identities(1)
        app = fixture(tmp_path, 'integration', collector=lambda *a:tests)
        async with app.run_test() as pilot:
            await pilot.pause()
            result = SimpleNamespace(identity=tests[0], duration=2, status='failed', traceback='')
            app.unit_result = SimpleNamespace(tests=(result,), cancelled=False)
            app.unit_rows = {tests[0].id:('FAILED',tests[0].id,'2s')}
            app.show_unit_results()
            await pilot.pause()
            app.screen.query_one(TestTable).focus()
            await pilot.press('enter')
            await pilot.pause()
            assert app.screen.kind == 'test-failure'
            assert app.screen.body.plain.endswith('FAILED')
    asyncio.run(scenario())
