"""Join immutable workflow call ownership to finalized suite accounting."""
from collections import defaultdict
import math


def _call_facts(call):
    usage = call.get('usage') or {}
    quote = call.get('quote') or {}
    accounting = quote.get('accounting') or {}
    from api_agent.providers.costs import qualified_charge
    charge = (qualified_charge(accounting.get('billing'), usage)
              if accounting.get('billing_mode') == 'api_key' else None)
    return {name: usage.get(name) for name in ('input_tokens', 'output_tokens',
            'cached_input_tokens', 'cache_write_tokens', 'reasoning_tokens')} | {
            'list_cost_usd': quote.get('list_cost_usd'),
            'list_cost_basis': 'provider_quote' if quote.get('list_cost_usd') is not None else 'unknown',
            'reported_charge_usd': charge,
            'reported_charge_basis': 'qualified_provider_billing' if charge is not None else 'unavailable',
            'cost_basis': 'provider_quote' if quote.get('list_cost_usd') is not None else 'unknown',
            'selection': call.get('selection'), 'actual_model': call.get('actual_model'),
            'outcome': call.get('outcome'), 'turn_id': call.get('turn_id')}


def join_workflow_accounting(journals, accounting, *, admissions=None):
    """Return a bounded source-of-truth join and per-scope frozen leaf facts."""
    from api_agent.workflows.journal import validate_journal
    admissions = admissions or {}
    by_test = {row['test_id']: row for row in accounting['tests']}
    records = []
    scope_facts = {}
    for test_id, captures in journals.items():
        frozen = by_test.get(test_id)
        if frozen is None:
            raise ValueError('Workflow journal has no selected accounting test')
        calls = {row['call_id']: row for row in frozen['calls']}
        observed = defaultdict(list)
        observed_turns = {}
        duplicates = set()
        routes = []
        for journal in captures:
            validate_journal(journal)
            expected = admissions.get(journal['projection']['definition_id'])
            binding = 'verified' if expected is not None and journal['projection'] == expected else 'unverified'
            routes.append(dict(invocation_id=journal['invocation_id'],
                               route_completeness=journal['completeness'],
                               definition_binding=binding))
            for event in journal['events']:
                if event.get('event') == 'call':
                    identifier = event['call_id']
                    if identifier in observed:
                        duplicates.add(identifier)
                    observed[identifier].append(event['visit_id'])
                    observed_turns[identifier] = journal['turn_id']
        missing = sorted(set(observed) - set(calls))
        unowned = sorted(set(calls) - set(observed))
        mismatched_turns = sorted(identifier for identifier in set(calls) & set(observed)
                                  if calls[identifier].get('turn_id') != observed_turns[identifier])
        status = ('complete' if not missing and not unowned and not duplicates
                  and not mismatched_turns
                  and frozen['calls_complete'] and all(route['route_completeness'] == 'complete' for route in routes)
                  and all(route['definition_binding'] == 'verified' for route in routes)
                  else 'partial')
        for identifier, visits in observed.items():
            if (identifier not in calls or len(visits) != 1 or identifier in mismatched_turns
                    or any(route['definition_binding'] != 'verified' for route in routes)):
                continue
            scope_facts[(test_id, visits[0], identifier)] = _call_facts(calls[identifier])
        allocation = frozen.get('stabilized_estimate') or {}
        records.append(dict(test_id=test_id, routes=routes, attribution=status,
                            allocated_estimate_usd=allocation.get('estimated_cost_usd'),
                            allocation_basis=allocation.get('basis', 'unavailable'),
                            missing_accounting_call_ids=missing, unowned_accounting_call_ids=unowned,
                            mismatched_turn_call_ids=mismatched_turns,
                            duplicate_journal_call_ids=sorted(duplicates),
                            leaf_calls=[dict(call_id=identifier, visit_id=visits[0],
                                             **_call_facts(calls[identifier]))
                                        for identifier, visits in sorted(observed.items())
                                        if identifier in calls and len(visits) == 1
                                        and identifier not in mismatched_turns]))
    return dict(version=1, tests=records), scope_facts


def apply_frozen_steps(steps, test_id, scope_facts):
    """Replace draft call/rollup parameters with frozen accounting facts."""
    def visit(step):
        parameters = step.get('parameters', [])
        if step.get('name', '').startswith('call '):
            call_id = step['name'][5:]
            matching = [(identifier, facts) for (owner, _, identifier), facts in scope_facts.items()
                        if owner == test_id and identifier == call_id]
            parameters[:] = ([dict(name=name, value='unknown' if value is None else str(value))
                              for name, value in matching[0][1].items()] if len(matching) == 1
                             else [dict(name='accounting', value='unknown')])
            return []
        scope_id = next((item['value'] for item in parameters if item.get('name') == 'scope_id'), None)
        children = step.get('steps', [])
        descendants = []
        for child in children:
            descendants.extend(visit(child))
        own = [(call_id, facts) for (owner, visit_id, call_id), facts in scope_facts.items()
               if owner == test_id and visit_id == scope_id]
        all_calls = [*own, *descendants]
        parameters[:] = [item for item in parameters
                         if not item.get('name', '').startswith(('inclusive_', 'call '))]
        if own:
            for call_id, facts in own:
                for name, value in facts.items():
                    parameters.append(dict(name='frozen_call_' + call_id + '_' + name,
                                           value='unknown' if value is None else str(value)))
        if all_calls:
            for name in ('input_tokens', 'output_tokens', 'cached_input_tokens',
                         'cache_write_tokens', 'reasoning_tokens', 'list_cost_usd',
                         'reported_charge_usd'):
                values = [facts[name] for _, facts in all_calls]
                amount = math.fsum(values) if all(type(value) in (int, float) and math.isfinite(value)
                                                   for value in values) else None
                parameters.append(dict(name='inclusive_' + name,
                                       value='unknown' if amount is None else str(amount)))
        return all_calls
    for root in steps:
        visit(root)
    return steps
