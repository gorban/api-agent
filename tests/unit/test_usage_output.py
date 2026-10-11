from api_agent.telemetry.usage_output import starting_usage_line, final_usage_text
import pytest


def test_usage_lines_measured_zero_and_unavailable():
    report = {'pct_used_before': 10.0, 'pct_used_after': 10.0, 'pct_point_delta': 0.0,
              'prompt_count': 0, 'pct_used_per_prompt': '0.000', 'reset_countdown_hms': '01:02:03'}
    assert starting_usage_line(report) == 'Starting quota used: 10%'
    text = final_usage_text(report)
    assert 'Quota change: 0%' in text
    assert 'Quota points per turn: unavailable' in text
    assert 'Cost attribution assumes' not in text
    assert 'Quota reset countdown (HH:MM:SS): 01:02:03' in text
    assert 'Quota change: unavailable' in final_usage_text({'pct_used_before': 1, 'prompt_count': 2, 'quota_unavailable_reason': 'changed_window'})


def test_output_fallback_per_prompt_and_start_unavailable():
    assert starting_usage_line({}) == ''
    assert 'Quota points per turn: 1.500' in final_usage_text({
        'pct_point_delta': 3.0, 'prompt_count': 2})


def test_cost_is_provider_supplied_not_quota_delta():
    text = final_usage_text({'pct_point_delta': 3.0, 'prompt_count': 2,
                            'estimated_cost_usd': 0.02, 'cost_per_prompt_usd': 0.01})
    assert 'Quota change: 3%' in text
    assert 'This suite cost: $0.020' in text
    assert 'Cost per turn: $0.010' in text
    assert '$3.000' not in text
    assert 'This suite cost: unavailable' in final_usage_text({'pct_point_delta': 3.0})


def test_actual_token_totals_retain_known_counts_without_double_counting_subsets():
    from api_agent.telemetry.usage import SuiteUsage
    from api_agent.telemetry.usage_output import tokens_text, has_tokens
    usage = SuiteUsage('run')
    usage.add_usage(provider='p', model='one', session_id='first', input_tokens=1200,
        output_tokens=10, cache_read_tokens=1000, cache_creation_tokens=0, reasoning_tokens=3)
    usage.add_usage(provider='p', model='two', session_id='second', input_tokens=2,
        output_tokens=4, cache_read_tokens=0, cache_creation_tokens=0, reasoning_tokens=2)
    report = usage.report()
    assert tokens_text(report) == ('Tokens this run:\n'
        '  input: 1,202\n  output: 14\n  cached input: 1,000\n'
        '  cache write: 0\n  reasoning: 5\n  total (input + output): 1,216\n'
        '  Cached input and cache writes are included in input; reasoning is included in output.')
    assert final_usage_text(report).endswith(tokens_text(report))
    assert 'total (input + output): 1,210' in tokens_text(report, 'first')
    assert 'total (input + output): 6' in tokens_text(report, 'second')
    assert has_tokens(report, 'first') and has_tokens(report, 'second')
    assert not has_tokens(report, 'absent')
    assert 'total (input + output): 0' in tokens_text(report, 'absent')


def test_unknown_failed_calls_do_not_erase_reported_spend_or_claim_zero():
    from api_agent.telemetry.usage import SuiteUsage
    from api_agent.telemetry.usage_output import tokens_text, has_tokens
    usage = SuiteUsage('run')
    usage.add_usage(provider='p', model='one', session_id='first', input_tokens=11, output_tokens=5)
    usage.add_usage(provider='p', model='one', session_id='first', input_tokens=None, output_tokens=None)
    usage.record_inference('missing', 'second', {'call_id':'missing', 'usage':None})
    report = usage.report()
    # CSV keeps unavailable aggregates; display retains known lower bounds.
    assert report['usage_rows'][0]['total_tokens'] is None
    assert 'total (input + output): 16 + unknown' in tokens_text(report, 'first')
    assert 'input: 11 + unknown' in tokens_text(report)
    assert has_tokens(report, 'first') and not has_tokens(report, 'second')
    assert 'total (input + output): unavailable' in tokens_text(report, 'second')
    assert report['unmeasured_calls'] == {'second':1}
    assert 'cached input: unavailable' in tokens_text(report)
    legacy = {'usage_rows':[{'session_id':'first', 'input_tokens':2, 'output_tokens':3, 'total_tokens':5}]}
    assert has_tokens(legacy, 'first') and 'total (input + output): 5' in tokens_text(legacy, 'first')


def test_each_count_displays_its_scoped_cost_and_identifies_subset_amounts():
    from api_agent.telemetry.usage_output import tokens_text
    report = {'usage_rows':[{'session_id':'one', 'input_tokens':10, 'output_tokens':5,
        'cache_read_tokens':2, 'cache_creation_tokens':0, 'reasoning_tokens':1, 'total_tokens':15}],
        'category_costs_usd':{'input_tokens':.2,'output_tokens':.8,'cache_read_tokens':.01,
                             'reasoning_tokens':.16,'total_tokens':1},
        'test_costs':{'one':{'category_costs_usd':{'input_tokens':.000002,'output_tokens':.000008,
            'cache_read_tokens':0,'cache_creation_tokens':None,'reasoning_tokens':.0000016,'total_tokens':.00001}}}}
    assert tokens_text(report).splitlines() == ['Tokens this run:',
        '  input: 10 — $0.200', '  output: 5 — $0.800', '  cached input: 2 — $0.010',
        '  cache write: 0', '  reasoning: 1 — $0.160', '  total (input + output): 15 — $1.000',
        '  Cached input and cache writes are included in input; reasoning is included in output.']
    assert tokens_text(report, 'one').splitlines() == ['Tokens this run:',
        '  input: 10 — $0.000002', '  output: 5 — $0.000008', '  cached input: 2 — $0.000',
        '  cache write: 0', '  reasoning: 1 — $0.0000016', '  total (input + output): 15 — $0.00001',
        '  Cached input and cache writes are included in input; reasoning is included in output.']


def test_sub_nanodollar_contributions_remain_distinct_from_zero():
    from api_agent.telemetry.usage_output import tokens_text, money
    assert money(0) == '$0.000'
    assert money(1e-9) == '$0.000000001'
    assert money(2.12345e-12) == '$2.12e-12'
    assert money(None) == money(-1) == 'unavailable'
    report = {'usage_rows':[{'session_id':'one','cache_read_tokens':1}],
              'category_costs_usd':{'cache_read_tokens':2.12345e-12}}
    assert 'cached input: 1 — $2.12e-12' in tokens_text(report)


def test_run_tokens_follow_cost_basis_after_any_unavailable_quota_reason():
    from api_agent.telemetry.usage_output import tokens_text
    report = {'cost_basis':'historical_quota', 'quota_unavailable_reason':'missing_snapshot', 'pct_used_before':1}
    output = final_usage_text(report)
    assert output.endswith('Cost basis: historical_quota\n' + tokens_text(report))
    assert output.index('Quota unavailable: missing_snapshot') < output.index('Cost basis:')
    assert ' · ' not in tokens_text(report)


def test_current_run_cost_and_frozen_historical_average_are_distinct():
    from math import isclose
    from api_agent.telemetry.usage_output import historical_average
    costs = [{'estimated_cost_usd':1, 'advance_prediction':{'estimated_cost_usd':.7}},
             {'estimated_cost_usd':2, 'advance_prediction':{'estimated_cost_usd':.1}},
             {'estimated_cost_usd':0, 'advance_prediction':{'estimated_cost_usd':0}}]
    report = {'estimated_cost_usd':3, 'prompt_count':2, 'test_costs':dict(zip(('one','two','zero'),costs))}
    assert 'This suite cost: $3.000\n  Historical average: $0.800' in final_usage_text(report)
    assert 'Cost per turn: unavailable\n  Historical average: $0.400' in final_usage_text(report)
    assert isclose(historical_average(costs), .8)
    assert historical_average(()) is None
    assert historical_average(({'advance_prediction':None},)) is None
    assert historical_average((costs[0], {})) is None
    assert historical_average(({'advance_prediction':{'estimated_cost_usd':True}},)) is None
    assert historical_average((costs[2],)) == 0


@pytest.mark.parametrize('report,expected', [
    ({'pct_point_delta':0}, 'quota did not change'),
    ({}, 'unavailable'),
    ({'pct_point_delta':False}, 'unavailable'),
    ({'pct_point_delta':1}, 'unavailable'),
    ({'pct_point_delta':0, 'quota_unavailable_reason':'changed_window'}, 'unavailable'),
    ({'pct_point_delta':0, 'before':{'pct_used':99}, 'after':{'pct_used':1}}, 'unavailable'),
])
def test_unknown_cost_explains_only_valid_unchanged_quota_in_suite_and_test_details(report, expected):
    from types import SimpleNamespace
    from api_agent.tui.test_results import test_details
    report = {**report, 'prompt_count':1, 'cost_basis':'unavailable'}
    output = final_usage_text(report)
    assert f'This suite cost: {expected}\n  Historical average: {expected}\nCost per turn: {expected}\n  Historical average: {expected}' in output
    assert 'Cost basis:' not in output
    result = SimpleNamespace(identity=SimpleNamespace(id='test'), duration=1, status='passed', traceback='')
    detailed = test_details(result, report, {'basis':'unavailable'}).plain
    assert f'This test cost: {expected}\n  Historical average: {expected}' in detailed
    assert 'Cost basis:' not in detailed


@pytest.mark.parametrize('average', [0, .6])
def test_zero_quota_preserves_known_historical_and_completed_estimates(average):
    from api_agent.telemetry.usage_output import displayed_cost
    report = {'pct_point_delta':0, 'prompt_count':2, 'estimated_cost_usd':.02,
              'cost_per_prompt_usd':.01, 'cost_basis':'list_price',
              'test_costs':{'test':{'advance_prediction':{'estimated_cost_usd':average}}}}
    output = final_usage_text(report)
    assert 'This suite cost: $0.020' in output and 'Cost per turn: $0.010' in output
    assert f'  Historical average: ${average:.3f}' in output
    assert f'Cost per turn: $0.010\n  Historical average: ${average / 2:.3f}' in output
    assert 'quota did not change' not in output
    assert displayed_cost(0, report) == '$0.000'


def test_zero_tokens_hide_even_known_category_prices_and_unknown_prices_have_no_suffix():
    from api_agent.telemetry.usage_output import tokens_text
    report = {'usage_rows':[{'input_tokens':0, 'output_tokens':5}],
              'category_costs_usd':{'input_tokens':0, 'output_tokens':None}}
    assert '  input: 0\n  output: 5\n' in tokens_text(report)
    assert ' — ' not in tokens_text(report)


def test_reset_notice_preserves_observations_and_token_based_cost():
    from api_agent.telemetry.usage_output import QUOTA_RESET_NOTE
    from api_agent.tui.test_results import test_details
    from types import SimpleNamespace
    report = {'before': {'pct_used':99}, 'after': {'pct_used':1},
              'pct_used_before':99, 'pct_used_after':1, 'pct_point_delta':None,
              'quota_unavailable_reason':'negative_delta', 'estimated_cost_usd':.23,
              'cost_basis':'historical_quota'}
    output = final_usage_text(report)
    assert 'Starting quota used: 99%' in output and 'Final quota used: 1%' in output
    assert 'Quota change: unavailable' in output and QUOTA_RESET_NOTE in output
    assert 'This suite cost: $0.230' in output
    assert output.index(QUOTA_RESET_NOTE) < output.index('Cost basis: historical_quota')
    result = SimpleNamespace(identity=SimpleNamespace(id='test'), duration=1, status='passed', traceback='')
    detailed = test_details(result, report, {'estimated_cost_usd':.23, 'basis':'historical_quota'})
    assert QUOTA_RESET_NOTE in detailed.plain and 'This test cost: $0.230' in detailed.plain
    assert detailed.plain.endswith('PASSED')


def test_pending_and_token_priced_runs_have_no_quota_lines_but_keep_costs():
    from api_agent.telemetry.usage import SuiteUsage
    for report in ({}, SuiteUsage('run').report(), {'cost_basis':'list_price', 'estimated_cost_usd':.02}):
        assert starting_usage_line(report) == ''
        text = final_usage_text(report)
        assert 'quota' not in text.lower() and 'Cost accounting:' not in text
        assert 'This suite cost:' in text and 'Tokens this run:' in text


def test_quota_failure_is_explicit_and_valid_snapshots_override_display():
    from api_agent.telemetry.usage import SuiteUsage, QuotaReadError, QuotaSnapshot
    from datetime import datetime, timezone, timedelta
    usage = SuiteUsage('run')
    usage.observe_snapshot('before', QuotaReadError('http', 401))
    usage.observe_snapshot('after', QuotaReadError('window'))
    report = usage.report()
    assert starting_usage_line(report) == 'Starting quota used: Error: Quota endpoint returned HTTP 401.'
    assert 'Final quota used: Error: Quota response did not include the configured quota window.' in final_usage_text(report)
    now = datetime.now(timezone.utc)
    usage.observe_snapshot('before', QuotaSnapshot(0, 'window', now + timedelta(days=1), now))
    assert starting_usage_line(usage.report()) == 'Starting quota used: 0%'
    assert usage.report()['quota_errors']['before'] == {'code':'http', 'status':401}


def test_completed_quota_errors_use_shared_literal_red_rendering():
    from api_agent.tui.test_results import styled_summary
    report = {'quota_errors':{'before':{'code':'http','status':401}}}
    summary = styled_summary('Integration results: PASS\n' + final_usage_text(report), failed=False)
    assert any(span.style == 'red' and summary.plain[span.start:span.end] == 'Error: Quota endpoint returned HTTP 401.' for span in summary.spans)
    assert summary.plain.startswith('Integration results: PASS')


def test_quota_error_display_fails_closed_on_redacted_or_uncontrolled_metadata():
    for error in ({'code':'[REDACTED]','status':None}, {'code':'secret','status':None}, 'secret'):
        assert starting_usage_line({'quota_errors':{'before':error}}) == 'Starting quota used: Error: Quota request failed or timed out.'


def test_completed_history_replaces_advance_comparison_in_suite_and_test_details():
    from types import SimpleNamespace
    from api_agent.tui.test_results import test_details
    from api_agent.telemetry.usage_output import historical_average
    cost = {'estimated_cost_usd': .0000036,
            'advance_prediction': {'estimated_cost_usd': None},
            'historical_average': {'estimated_cost_usd': .0000036, 'sample_count': 1}}
    report = {'estimated_cost_usd': .0000036, 'estimated_cost_per_prompt_usd': .0000036,
              'prompt_count': 1, 'test_costs': {'t': cost}}
    text = final_usage_text(report)
    assert text.count('Historical average: $0.0000036') == 2
    result = SimpleNamespace(identity=SimpleNamespace(id='t'), duration=8, status='passed', traceback='')
    assert 'Historical average: $0.0000036' in test_details(result, report, cost).plain
    assert historical_average((dict(cost, historical_average=None),)) is None
    assert cost['advance_prediction']['estimated_cost_usd'] is None
