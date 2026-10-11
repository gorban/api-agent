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
    assert 'Estimated cost: $0.020' in text
    assert 'Estimated cost per turn: $0.010' in text
    assert '$3.000' not in text
    assert 'Estimated cost: unavailable' in final_usage_text({'pct_point_delta': 3.0})


def test_actual_token_totals_retain_known_counts_without_double_counting_subsets():
    from api_agent.telemetry.usage import SuiteUsage
    from api_agent.telemetry.usage_output import tokens_text, has_tokens
    usage = SuiteUsage('run')
    usage.add_usage(provider='p', model='one', session_id='first', input_tokens=1200,
        output_tokens=10, cache_read_tokens=1000, cache_creation_tokens=0, reasoning_tokens=3)
    usage.add_usage(provider='p', model='two', session_id='second', input_tokens=2,
        output_tokens=4, cache_read_tokens=0, cache_creation_tokens=0, reasoning_tokens=2)
    report = usage.report()
    assert tokens_text(report) == ('Tokens: input 1,202 · output 14 · cached input 1,000 '
        '· cache write 0 · reasoning 5 · total 1,216')
    assert final_usage_text(report).splitlines()[0] == tokens_text(report)
    assert tokens_text(report, 'first').endswith('total 1,210')
    assert tokens_text(report, 'second').endswith('total 6')
    assert has_tokens(report, 'first') and has_tokens(report, 'second')
    assert not has_tokens(report, 'absent')
    assert tokens_text(report, 'absent').endswith('total 0')


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
    assert tokens_text(report, 'first').endswith('total 16 + unknown')
    assert 'input 11 + unknown' in tokens_text(report)
    assert has_tokens(report, 'first') and not has_tokens(report, 'second')
    assert tokens_text(report, 'second').endswith('total unavailable')
    assert report['unmeasured_calls'] == {'second':1}
    assert 'cached input unavailable' in tokens_text(report)
    legacy = {'usage_rows':[{'session_id':'first', 'input_tokens':2, 'output_tokens':3, 'total_tokens':5}]}
    assert has_tokens(legacy, 'first') and tokens_text(legacy, 'first').endswith('total 5')
