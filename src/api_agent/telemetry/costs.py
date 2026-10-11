"""Allocate a suite estimate using provider-supplied list-price proportions."""
from copy import deepcopy
import calendar
import math
from datetime import datetime, timezone

from api_agent.providers.costs import validate_quote
from .estimation import quota_reset_observed


class CostLedger:
    def __init__(self):
        self.calls = {}

    def record(self, call_id, session_id, quote=None):
        if quote is not None:
            quote = deepcopy(validate_quote(quote))
        value = (session_id, quote)
        if call_id in self.calls and self.calls[call_id] != value and self.calls[call_id] != (session_id, None):
            raise ValueError('Conflicting call cost identity')
        self.calls[call_id] = value

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
            cycles = (calendar.monthrange(date.year, date.month)[1] * 86400 / policy['quota_seconds']
                      if policy['period'] == 'calendar_month' else
                      (86400 if policy['period'] == 'day' else 604800) / policy['quota_seconds']
                      if policy['period'] in ('day', 'week') else 1)
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
                'unallocated_cost_usd': None if total is None else max(0.0, total - assigned),
                'test_costs': allocated, 'payment_policy': policy,
                'payment_month': f'{date.year:04d}-{date.month:02d}' if policy is not None and policy['period'] == 'calendar_month' else None,
                'cost_note': 'Estimate assumes exclusive account use; quota percentages may be rounded. Unknown prices/usage prevent proportional allocation.'}
