"""Join immutable workflow call ownership to finalized suite accounting."""
from collections import defaultdict
import math
from datetime import datetime


def _call_facts(call):
    usage = call.get('usage') or {}
    quote = call.get('quote') or {}
    accounting = quote.get('accounting') or {}
    from api_agent.providers.costs import qualified_charge
    charge = (qualified_charge(accounting.get('billing'), usage)
              if accounting.get('billing_mode') in ('api_key', 'metered') else None)
    return {name: usage.get(name) for name in ('input_tokens', 'output_tokens',
            'cached_input_tokens', 'cache_write_tokens', 'reasoning_tokens')} | {
            'list_cost_usd': quote.get('list_cost_usd'),
            'list_cost_basis': 'provider_quote' if quote.get('list_cost_usd') is not None else 'unknown',
            'reported_charge_usd': charge,
            'reported_charge_basis': 'qualified_provider_billing' if charge is not None else 'unavailable',
            'cost_basis': 'provider_quote' if quote.get('list_cost_usd') is not None else 'unknown',
            'selection': call.get('selection'), 'actual_model': call.get('actual_model'),
            'requested_model': accounting.get('requested_model'),
            'catalog_model': accounting.get('catalog_model'),
            'reasoning_effort': call.get('reasoning_effort', accounting.get('reasoning_effort')),
            'service_tier': call.get('service_tier'),
            'serving_revision': call.get('serving_revision'),
            'provider_identity': (accounting.get('compatibility') or {}).get('_COST_PROVIDER_IDENTITY'),
            'outcome': call.get('outcome'), 'turn_id': call.get('turn_id'),
            **({'conversation_id': call['conversation_id']} if 'conversation_id' in call else {})}


class _ScopeFacts(dict):
    """Frozen facts plus unknown observed leaves, without fabricated call evidence."""
    def __init__(self):
        super().__init__()
        self.unknown = defaultdict(set)
        self.invocations = {}
        self.completeness = {}


def join_workflow_accounting(journals, accounting, *, admissions=None):
    """Return a bounded source-of-truth join and per-scope frozen leaf facts."""
    from api_agent.workflows.journal import validate_journal
    admissions = admissions or {}
    by_test = {row['test_id']: row for row in accounting['tests']}
    records = []
    scope_facts = _ScopeFacts()
    selected = dict(journals)
    for test_id, row in by_test.items():
        if any(call.get("conversation_id") is not None for call in row["calls"]):
            selected.setdefault(test_id, [])
    for test_id, captures in selected.items():
        frozen = by_test.get(test_id)
        if frozen is None:
            raise ValueError('Workflow journal has no selected accounting test')
        calls = {row['call_id']: row for row in frozen['calls']}
        if len(calls) != len(frozen['calls']):
            raise ValueError('Duplicate authoritative accounting call')
        observed = defaultdict(list)
        observed_turns = {}
        observed_invocations = {}
        observed_owners = defaultdict(list)
        observed_conversations = {}
        duplicates = set()
        routes = []
        for journal in captures:
            validate_journal(journal)
            identifier = journal['projection']['definition_id']
            # Generated host admissions are exact-case scoped. The flat key is
            # retained only for the trusted caller's legacy admission contract.
            expected = admissions.get((test_id, identifier), admissions.get(identifier))
            binding = 'verified' if expected is not None and journal['projection'] == expected else 'unverified'
            routes.append(dict(invocation_id=journal['invocation_id'],
                               turn_id=journal['turn_id'],
                               **({'execution_context': journal['execution_context']} if 'execution_context' in journal else {}),
                               route_completeness=journal['completeness'],
                               definition_binding=binding))
            for event in journal['events']:
                if event.get('event') == 'call':
                    identifier = event['call_id']
                    if identifier in observed:
                        duplicates.add(identifier)
                    observed[identifier].append(event['visit_id'])
                    observed_owners[identifier].append((journal['invocation_id'], event['visit_id']))
                    observed_invocations[identifier] = journal['invocation_id']
                    observed_turns[identifier] = journal['turn_id']
                    observed_conversations[identifier] = (journal.get('execution_context') or {}).get('conversation_id')
        missing = sorted(set(observed) - set(calls))
        unowned = sorted(set(calls) - set(observed))
        mismatched_turns = sorted(identifier for identifier in set(calls) & set(observed)
                                  if calls[identifier].get('turn_id') != observed_turns[identifier]
                                  or calls[identifier].get('conversation_id') != observed_conversations[identifier])
        status = ('complete' if not missing and not unowned and not duplicates
                  and not mismatched_turns
                  and frozen['calls_complete'] and all(route['route_completeness'] == 'complete' for route in routes)
                  and all(route['definition_binding'] == 'verified' for route in routes)
                  else 'partial')
        scope_facts.completeness[test_id] = dict(calls_complete=frozen['calls_complete'], attribution=status)
        for identifier, visits in observed.items():
            if (identifier not in calls or len(visits) != 1 or identifier in mismatched_turns
                    or any(route['definition_binding'] != 'verified' for route in routes)):
                for invocation_id, visit_id in observed_owners[identifier]:
                    scope_facts.unknown[(test_id, invocation_id, visit_id)].add(identifier)
                continue
            key = (test_id, visits[0], identifier)
            scope_facts[key] = _call_facts(calls[identifier])
            scope_facts.invocations[key] = observed_invocations[identifier]
        for identifier in unowned:
            scope_facts[(test_id, None, identifier)] = _call_facts(calls[identifier])
        allocation = frozen.get('stabilized_estimate') or {}
        records.append(dict(test_id=test_id, routes=routes, attribution=status,
                            allocated_estimate_usd=allocation.get('estimated_cost_usd'),
                            allocation_basis=allocation.get('basis', 'unavailable'),
                            missing_accounting_call_ids=missing, unowned_accounting_call_ids=unowned,
                            mismatched_turn_call_ids=mismatched_turns,
                            duplicate_journal_call_ids=sorted(duplicates),
                            conversations=_conversation_summaries(calls, complete=frozen['calls_complete'], attribution=status),
                            leaf_calls=[dict(call_id=identifier, visit_id=visits[0], invocation_id=observed_invocations[identifier],
                                             **_call_facts(calls[identifier]))
                                        for identifier, visits in sorted(observed.items())
                                        if (test_id, visits[0], identifier) in scope_facts]))
    return dict(version=1, tests=records), scope_facts


def apply_frozen_steps(steps, test_id, scope_facts):
    """Replace draft call/rollup parameters with frozen accounting facts."""
    def visit(step, invocation=None):
        parameters = step.get('parameters', [])
        invocation = next((item['value'] for item in parameters if item.get('name') == 'invocation_id'), invocation)
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
            descendants.extend(visit(child, invocation))
        own = [(call_id, facts) for (owner, visit_id, call_id), facts in scope_facts.items()
               if owner == test_id and scope_id is not None and visit_id == scope_id
               and ((owner, visit_id, call_id) not in getattr(scope_facts, 'invocations', {})
                    or scope_facts.invocations[(owner, visit_id, call_id)] == invocation)]
        if step.get('name', '').startswith(('Conversation ', 'User turn ')):
            context = {item['name']: item['value'] for item in parameters}
            for name, value in getattr(scope_facts, 'completeness', {}).get(test_id, {}).items():
                parameters[:] = [item for item in parameters if item['name'] != name]
                parameters.append(dict(name=name, value=str(value).lower()))
            own = [(call_id, facts) for (owner, _, call_id), facts in scope_facts.items()
                   if owner == test_id and facts.get('conversation_id') == context['conversation_id']
                   and ('turn_id' not in context or facts.get('turn_id') == context['turn_id'])]
        unknown = [(identifier, _call_facts({})) for identifier in
                   (getattr(scope_facts, 'unknown', {}).get((test_id, invocation, scope_id), set())
                    | set(getattr(scope_facts, 'unknown', {}).get((test_id, scope_id), ())))]
        all_calls = list(dict([*own, *descendants, *unknown]).items())
        parameters[:] = [item for item in parameters
                         if not item.get('name', '').startswith(('inclusive_', 'call '))]
        if own and scope_id is not None:
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


def group_execution_steps(steps):
    """Group explicit execution contexts; retain legacy roots without inference."""
    from .workflow_steps import bubble_attention
    roots, conversations, trials = [], {}, {}

    def wrapper(name, parameters):
        return dict(name=name, status='passed', start=0, stop=0,
                    parameters=[dict(name=k, value=str(v)) for k, v in parameters.items()], steps=[])

    flattened = []
    def flatten(step):
        if step['name'].startswith(('Conversation ', 'User turn ', 'Trial ')):
            for child in step['steps']:
                flatten(child)
        else:
            flattened.append(step)
    for step in steps:
        flatten(step)
    for step in sorted(flattened, key=_step_chronology):
        params = {row['name']: row['value'] for row in step.get('parameters', [])}
        conversation = params.get('conversation_id')
        trial = params.get('trial_index')
        if conversation is not None:
            if conversation not in conversations:
                parent = wrapper('Conversation ' + conversation, {'conversation_id': conversation})
                conversations[conversation] = (parent, {})
                roots.append(parent)
            parent, turns = conversations[conversation]
            turn_id = params['turn_id']
            if turn_id not in turns:
                turns[turn_id] = wrapper('User turn ' + str(len(turns) + 1),
                                        {'conversation_id': conversation, 'turn_id': turn_id})
                parent['steps'].append(turns[turn_id])
            turns[turn_id]['steps'].append(step)
        elif trial is not None:
            if trial not in trials:
                trials[trial] = wrapper('Trial ' + trial, {'trial_index': trial})
                roots.append(trials[trial])
            trials[trial]['steps'].append(step)
        else:
            roots.append(step)

    def intervals(step):
        for child in step['steps']:
            intervals(child)
        if step['steps'] and step['name'].startswith(('Conversation ', 'User turn ', 'Trial ')):
            timed = [child for child in step['steps'] if 'start' in child and 'stop' in child]
            if timed:
                step['start'] = min(child['start'] for child in timed)
                step['stop'] = max(child['stop'] for child in timed)
            else:
                step.pop('start', None)
                step.pop('stop', None)
    for root in roots:
        intervals(root)
        bubble_attention(root)
    return roots


_ROLLUP_FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens',
                  'cache_write_tokens', 'reasoning_tokens', 'list_cost_usd',
                  'reported_charge_usd')


def _rollup(calls):
    facts = [_call_facts(call) for call in calls]
    result = {'call_ids': sorted(call['call_id'] for call in calls)}
    for name in _ROLLUP_FIELDS:
        values = [row[name] for row in facts]
        result[name] = (math.fsum(values) if values and all(
            type(value) in (int, float) and math.isfinite(value) for value in values) else None)
    return result


def _conversation_summaries(calls, *, complete=True, attribution="complete"):
    """Persist reusable ID joins and scalar totals, without copying call payloads."""
    grouped = {}
    for call in sorted(calls.values(), key=_call_chronology):
        conversation = call.get('conversation_id')
        if conversation is not None:
            grouped.setdefault(conversation, []).append(call)
    result = []
    for identifier, rows in grouped.items():
        turns = {}
        for call in rows:
            turns.setdefault(call['turn_id'], []).append(call)
        result.append(dict(conversation_id=identifier, calls_complete=complete, attribution=attribution, **_rollup(rows), turns=[
            dict(turn_id=turn_id, calls_complete=complete, attribution=attribution, **_rollup(turn_calls)) for turn_id, turn_calls in turns.items()]))
    return result


def _step_chronology(step):
    parameters = {item['name']: item['value'] for item in step.get('parameters', [])}
    return (float(parameters.get('recorded_started_ms', step.get('start', float('inf')))), parameters.get('invocation_id', ''),
            parameters.get('turn_id', ''), step['name'])


def _call_chronology(call):
    started = call.get('started_at')
    return (datetime.fromisoformat(started).timestamp() if started is not None else float('inf'),
            call.get('turn_id', ''), call['call_id'])


def include_unowned_calls(steps, calls, unowned_ids):
    """Add truthful call leaves for explicit context, or an unattached container."""
    from .workflow_steps import bubble_attention
    unowned = set(unowned_ids)
    additions, unattached = [], []
    for call in sorted(calls, key=_call_chronology):
        if call['call_id'] not in unowned:
            continue
        outcome = call.get('outcome', 'unknown')
        status = 'passed' if outcome in ('completed', 'tool_calls') else 'failed' if outcome == 'failed' else 'broken'
        parameters = {name: call[name] for name in ('conversation_id', 'turn_id') if name in call}
        parameters.update(outcome=outcome, attribution='outside_graph')
        step = dict(name='call ' + call['call_id'], status=status, steps=[],
                    parameters=[dict(name=name, value=str(value)) for name, value in parameters.items()])
        if call.get('started_at') is not None:
            step['parameters'].append(dict(name='recorded_started_ms',
                                           value=str(datetime.fromisoformat(call['started_at']).timestamp() * 1000)))
        if call.get('started_at') is not None and call.get('finished_at') is not None:
            step.update(start=int(datetime.fromisoformat(call['started_at']).timestamp() * 1000),
                        stop=int(datetime.fromisoformat(call['finished_at']).timestamp() * 1000))
        (additions if 'conversation_id' in call else unattached).append(step)
    if unattached:
        container = dict(name='Unattached', status='passed', steps=unattached, parameters=[])
        bubble_attention(container)
        additions.append(container)
    return group_execution_steps([*steps, *additions])
