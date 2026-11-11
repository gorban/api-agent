"""Allocate a suite estimate using provider-supplied list-price proportions."""
from copy import deepcopy
import math
from datetime import datetime, timezone

from api_agent.providers.costs import validate_quote, period_seconds, qualified_charge
from .estimation import quota_reset_observed, reported_charge


def retain_quota_estimate(summary, artifact, *, selected_count):
    """Keep account-boundary estimates without inventing model-price weights.

    Only a sole selected and recorded owner can receive the whole estimate.
    Call inventory completeness and historical price eligibility stay unchanged.
    """
    amount = summary.get('raw_quota_cost_usd')
    if summary['estimated_cost_usd'] is not None or amount is None:
        return
    policy = artifact['quota']['payment_policy']
    calls = [call for row in artifact['tests'] for call in row['calls']]
    if not calls or policy is None or any(
            (call.get('quote') or {}).get('payment') != policy
            or reported_charge({'calls_complete': True, 'calls': [call]}) is not None
            or any(qualified_charge(attempt['billing'], attempt['usage'] or {}) is not None
                   for attempt in (call.get('transport_attempts') or {}).get('attempts', ()))
            for call in calls):
        return
    summary.update(estimated_cost_usd=amount, cost_basis='quota_payment')
    if selected_count != 1 or len(artifact['tests']) != 1:
        return
    test = artifact['tests'][0]
    own = summary['test_costs'][test['test_id']]
    if not own.get('prompt_count', 0):
        return
    own.update(estimated_cost_usd=amount, basis='quota_payment',
               raw_quota_cost_usd=amount, cost_per_prompt_usd=amount / own['prompt_count'])
    test['raw_quota_cost_usd'] = amount
    test['stabilized_estimate'].update(estimated_cost_usd=amount, basis='quota_payment')
    summary['unallocated_cost_usd'] = 0.0


class CostLedger:
    def __init__(self):
        self.calls = {}
        self.known_subtotals = {}

    def record(self, call_id, session_id, quote=None, *, known_subtotal=None):
        if quote is not None:
            quote = deepcopy(validate_quote(quote))
        value = (session_id, quote)
        if call_id in self.calls and self.calls[call_id] != value and self.calls[call_id] != (session_id, None):
            raise ValueError('Conflicting call cost identity')
        self.calls[call_id] = value
        if known_subtotal is not None:
            from api_agent.providers.costs import valid_amount
            if not valid_amount(known_subtotal):
                raise ValueError('Invalid known cost subtotal')
            self.known_subtotals[call_id] = known_subtotal

    def report(self, usage, prompt_counts, *, now=None):
        date = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        weights, unknown, policies = {}, set(), []
        for session, quote in self.calls.values():
            quote = quote or {'list_cost_usd': None, 'payment': None}
            amount = quote['list_cost_usd']
            weights.setdefault(session, 0.0)
            if amount is None:
                unknown.add(session)
            else:
                weights[session] += amount
            if quote['payment'] is not None and quote['payment'] not in policies:
                policies.append(quote['payment'])
        list_total = None if unknown else math.fsum(weights.values())
        total, basis = list_total, 'list_price'
        policy = policies[0] if len(policies) == 1 else None
        delta = usage.get('pct_point_delta')
        if policy is not None and delta is not None and not quota_reset_observed(usage):
            cycles = (1 if policy['period'] == 'quota' else
                      period_seconds(policy['period'], now=date) / policy['quota_seconds'])
            total, basis = delta / 100 * policy['usd'] / cycles, 'quota_payment'
        if total is None:
            basis = 'unavailable'
        allocated = {}
        for session in weights.keys() | prompt_counts.keys():
            weight = weights.get(session, 0.0)
            amount = (None if total is None or session in unknown or list_total is None
                      else 0.0 if total == 0 or not self.calls
                      else total * weight / list_total if list_total else None)
            prompts = prompt_counts.get(session, 0)
            allocated[session] = {'estimated_cost_usd': amount,
                'cost_per_prompt_usd': None if amount is None or not prompts else amount / prompts,
                'prompt_count': prompts, 'list_cost_usd': None if session in unknown else weight,
                'basis': basis}
        assigned = math.fsum(row['estimated_cost_usd'] for row in allocated.values()
                             if row['estimated_cost_usd'] is not None)
        prompts = sum(prompt_counts.values())
        return {'estimated_cost_usd': total, 'cost_per_prompt_usd': None if total is None or not prompts else total / prompts,
                'raw_quota_cost_usd': total if basis == 'quota_payment' else None,
                'cost_basis': basis, 'list_cost_usd': list_total,
                'known_list_cost_usd': math.fsum(self.known_subtotals.get(call_id, quote['list_cost_usd'] if quote and quote['list_cost_usd'] is not None else 0) for call_id, (_, quote) in self.calls.items()),
                'unallocated_cost_usd': None if total is None else max(0.0, total - assigned),
                'test_costs': allocated, 'payment_policy': policy,
                'payment_month': f'{date.year:04d}-{date.month:02d}' if policy is not None and policy['period'] == 'calendar_month' else None,
                'cost_note': 'Estimate assumes exclusive account use; quota percentages may be rounded. Unknown prices/usage prevent proportional allocation.'}
