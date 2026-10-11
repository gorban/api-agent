from api_agent.telemetry.usage_output import starting_usage_line, final_usage_text


def test_usage_lines_measured_zero_and_unavailable():
    report = {'pct_used_before': 10.0, 'pct_used_after': 10.0, 'pct_point_delta': 0.0,
              'prompt_count': 0, 'pct_used_per_prompt': '0.000', 'reset_countdown_hms': '01:02:03'}
    assert starting_usage_line(report) == 'Starting quota used: 10.000%'
    text = final_usage_text(report)
    assert 'Quota change: 0.000%' in text
    assert 'Quota points per turn: unavailable' in text
    assert 'other activity invalidates it' in text
    assert 'Quota change: unavailable' in final_usage_text({'prompt_count': 2, 'quota_unavailable_reason': 'changed_window'})


def test_output_fallback_per_prompt_and_start_unavailable():
    assert starting_usage_line({}) == 'Starting quota used: unavailable'
    assert 'Quota points per turn: 1.500' in final_usage_text({
        'pct_point_delta': 3.0, 'prompt_count': 2})


def test_cost_is_provider_supplied_not_quota_delta():
    text = final_usage_text({'pct_point_delta': 3.0, 'prompt_count': 2,
                            'estimated_cost_usd': 0.02, 'cost_per_prompt_usd': 0.01})
    assert 'Quota change: 3.000%' in text
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
    assert tokens_text(report) == ('Tokens this run (estimated costs):\n'
        '  input: 1,202 — unavailable\n  output: 14 — unavailable\n  cached input: 1,000 — unavailable\n'
        '  cache write: 0 — unavailable\n  reasoning: 5 — unavailable\n  total (input + output): 1,216 — unavailable\n'
        '  Cached input and reasoning are included in input/output totals.')
    assert final_usage_text(report).endswith(tokens_text(report))
    assert 'total (input + output): 1,210 — unavailable' in tokens_text(report, 'first')
    assert 'total (input + output): 6 — unavailable' in tokens_text(report, 'second')
    assert has_tokens(report, 'first') and has_tokens(report, 'second')
    assert not has_tokens(report, 'absent')
    assert 'total (input + output): 0 — unavailable' in tokens_text(report, 'absent')


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
    assert 'total (input + output): 16 + unknown — unavailable' in tokens_text(report, 'first')
    assert 'input: 11 + unknown' in tokens_text(report)
    assert has_tokens(report, 'first') and not has_tokens(report, 'second')
    assert 'total (input + output): unavailable — unavailable' in tokens_text(report, 'second')
    assert report['unmeasured_calls'] == {'second':1}
    assert 'cached input: unavailable' in tokens_text(report)
    legacy = {'usage_rows':[{'session_id':'first', 'input_tokens':2, 'output_tokens':3, 'total_tokens':5}]}
    assert has_tokens(legacy, 'first') and 'total (input + output): 5 — unavailable' in tokens_text(legacy, 'first')


def test_each_count_displays_its_scoped_cost_and_identifies_subset_amounts():
    from api_agent.telemetry.usage_output import tokens_text
    report = {'usage_rows':[{'session_id':'one', 'input_tokens':10, 'output_tokens':5,
        'cache_read_tokens':2, 'cache_creation_tokens':0, 'reasoning_tokens':1, 'total_tokens':15}],
        'category_costs_usd':{'input_tokens':.2,'output_tokens':.8,'cache_read_tokens':.01,
                             'reasoning_tokens':.16,'total_tokens':1},
        'test_costs':{'one':{'category_costs_usd':{'input_tokens':.000002,'output_tokens':.000008,
            'cache_read_tokens':0,'cache_creation_tokens':None,'reasoning_tokens':.0000016,'total_tokens':.00001}}}}
    assert tokens_text(report).splitlines() == ['Tokens this run (estimated costs):',
        '  input: 10 — $0.200', '  output: 5 — $0.800', '  cached input: 2 — $0.010',
        '  cache write: 0 — unavailable', '  reasoning: 1 — $0.160', '  total (input + output): 15 — $1.000',
        '  Cached input and reasoning are included in input/output totals.']
    assert tokens_text(report, 'one').splitlines() == ['Tokens this run (estimated costs):',
        '  input: 10 — $0.000002', '  output: 5 — $0.000008', '  cached input: 2 — $0.000',
        '  cache write: 0 — unavailable', '  reasoning: 1 — $0.0000016', '  total (input + output): 15 — $0.00001',
        '  Cached input and reasoning are included in input/output totals.']


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
    report = {'cost_basis':'historical_quota', 'quota_unavailable_reason':'missing_snapshot'}
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
    report = {'estimated_cost_usd':3, 'test_costs':dict(zip(('one','two','zero'),costs))}
    assert 'This suite cost: $3.000\n  Historical average: $0.800' in final_usage_text(report)
    assert isclose(historical_average(costs), .8)
    assert historical_average(()) is None
    assert historical_average(({'advance_prediction':None},)) is None
    assert historical_average((costs[0], {})) is None
    assert historical_average(({'advance_prediction':{'estimated_cost_usd':True}},)) is None
    assert historical_average((costs[2],)) == 0
