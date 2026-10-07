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
