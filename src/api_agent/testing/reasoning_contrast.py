"""Three matched application-HTTP pairs using the ordinary integration owner."""
import asyncio
import math
import time
from uuid import uuid4

from api_agent.providers.inference import result_from_wire
from api_agent.providers.progress import component_timeout
from api_agent.testing.reasoning_heartbeat import post_chat, _drain, _retain_receipts, _select_error

MODEL = 'gpt-6-luna'
PUZZLE = (
    'Write Python 3 code that solves this colored-path puzzle for any finite directed acyclic graph, not just the example. '
    'Define solve(edges, start, goal, red_count, min_blue, max_run). '
    'Each edge is a tuple (source, target, cost, color), where vertex labels are nonempty strings, cost is a nonnegative integer, and color is "R" or "B". '
    'There are no duplicate directed edges. start and goal are distinct vertices. red_count and min_blue are nonnegative integers; max_run is a positive integer. '
    'A valid path from start to goal uses exactly red_count red edges, at least min_blue blue edges, and no run of consecutive edges of one color longer than max_run. '
    'Return None if no valid path exists. '
    'Otherwise return (minimum_total_cost, number_of_paths_with_that_cost, lexicographically_smallest_optimal_path), where the last element is a tuple of vertex labels and the count is an exact integer. '
    'Distinct vertex sequences are distinct paths. '
    'Use a state-based dynamic programming algorithm rather than enumerating all complete paths. '
    'Include all needed standard-library imports. '
    'Under if __name__ == "__main__": construct these example edges and print solve(edges, "S", "T", 3, 2, 2): S>A:4:R, S>B:2:B, S>C:5:R, A>B:1:B, A>D:5:R, A>E:3:B, B>C:2:R, B>D:4:B, B>E:6:R, C>D:1:B, C>E:3:R, C>F:7:B, D>E:2:B, D>F:4:R, D>T:9:B, E>F:1:R, E>T:5:B, F>T:3:R. '
    'Here source>target:cost:color denotes an edge. '
    'Reply with only executable Python code, without Markdown fences or explanation.'
)


def scope(config, *, owner_root=None):
    """Validate the exact contributed choice without loading a provider."""
    from api_agent.testing.model_choices import exact_model
    if config.get('LLM_REASONING_TEST_TOKEN_REPORTING') is not True:
        raise ValueError('Reasoning contrast requires a provider/API route qualified for reasoning-token reporting')
    return exact_model(config, 'LLM_REASONING_TEST_MODEL')


def reasoning_timeout(config):
    """Reject invalid local budgets before any provider is acquired."""
    value = config.get('LLM_REASONING_TEST_TIMEOUT_SECONDS', 150)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('Reasoning contrast timeout must be a positive finite number')
    return value


def valid_call(row, model=MODEL):
    """Conservatively qualify measurements without changing provider outcomes."""
    if not (row.get('status') == 'completed'
            and type(row.get('reasoning_tokens')) is int and row['reasoning_tokens'] >= 0
            and row.get('requested_model') == model and row.get('actual_model') in (None, model)):
        return False
    maximum = row.get('max_output_tokens')
    if row.get('response_text_present', maximum is None) is not True:
        return False
    if maximum is None:
        return True
    usage = row.get('usage')
    return (type(maximum) is int and maximum > 0 and type(usage) is dict
            and type(usage.get('output_tokens')) is int
            and 0 <= usage['output_tokens'] < maximum)


def evaluate(records, *, model=MODEL, efforts=('low', 'high')):
    """The signed three-pair smoke threshold is not a population guarantee."""
    differences = []
    if len(records) != 3:
        return dict(passed=False, differences=differences)
    for pair in records:
        calls = pair.get('calls', [])
        if (len(calls) != 2 or [row.get('effort') for row in calls] != list(efforts)
                or not all(valid_call(row, model) for row in calls)):
            return dict(passed=False, differences=differences)
        differences.append(calls[1]['reasoning_tokens']-calls[0]['reasoning_tokens'])
    return dict(passed=sum(value >= 50 for value in differences) >= 2
                and all(value > -50 for value in differences), differences=differences)


async def run_pairs(client, endpoint, nonce, *, model=MODEL, efforts=('low', 'high'), max_output_tokens=None, timeout_seconds=150, emit=lambda row: None):
    """Send at most six calls, with no retry, replacement or hidden provider."""
    timeout_seconds = reasoning_timeout({'LLM_REASONING_TEST_TIMEOUT_SECONDS': timeout_seconds})
    failures = {}

    async def call(index, marker, effort):
        prompt = f'Run marker: {marker}\n{PUZZLE}'
        identifier = str(uuid4())
        row = dict(pair=index, marker=marker, effort=effort,
                   request_id=identifier, requested_model=model, max_output_tokens=max_output_tokens)
        emit(dict(event='intent', **row))
        started = time.monotonic()
        try:
            async with component_timeout('Reasoning comparison application request', timeout_seconds):
                body = {'session_id':identifier, 'model':model, 'reasoning_effort':effort,
                        'messages':[{'role':'user', 'content':prompt}]}
                if max_output_tokens is not None:
                    body['max_output_tokens'] = max_output_tokens
                response = await post_chat(client, endpoint, nonce, body)
                response.raise_for_status()
                data = response.json()
                if data.pop('session_id') != identifier:
                    raise ValueError('Application request identity mismatch')
                data['text'] = data.pop('response_text')
                result = result_from_wire(data)
                row.update(status=result.outcome,
                    response_text_present=bool(result.payload.content[0].text.strip()),
                    reasoning_tokens=result.usage.reasoning_tokens, usage=data['usage'],
                    actual_model=result.actual_model, serving_revision=result.serving_revision,
                    billing=result.billing, actual_cost_usd=None if result.billing is None else result.billing['usd'],
                    elapsed_seconds=time.monotonic()-started)
        except BaseException as error:
            if isinstance(error, TimeoutError) and isinstance(error.__cause__, asyncio.CancelledError):
                _retain_receipts(error, error.__cause__)
            failures[index, effort] = error
            emit(dict(row, event='call', status='failed' if isinstance(error, Exception) else 'interrupted',
                      failure_type=type(error).__name__, elapsed_seconds=time.monotonic()-started))
            raise
        emit(dict(event='call', **row))
        if not valid_call(row, model):
            raise ValueError('Reasoning comparison received an invalid measurement')
        return row

    tasks = []
    try:
        for index in range(1, 4):
            marker = str(uuid4())
            for effort in efforts:
                work = call(index, marker, effort)
                try:
                    task = asyncio.create_task(work)
                except BaseException:
                    work.close()
                    raise
                tasks.append(task)
        pending = asyncio.gather(*tasks, return_exceptions=True)
        results = await asyncio.shield(pending)
    except BaseException as error:
        interrupted = await _drain(tasks)
        selected = _select_error(error, interrupted)
        _retain_receipts(selected, error, interrupted, *failures.values())
        if selected is not error:
            raise selected
        raise
    for offset, result in enumerate(results):
        if isinstance(result, BaseException):
            key = offset//2+1, efforts[offset%2]
            error = failures.get(key, result)
            _retain_receipts(error, *failures.values())
            raise error
    records = []
    for offset in range(0, 6, 2):
        calls = results[offset:offset+2]
        record = dict(pair=calls[0]['pair'], marker=calls[0]['marker'], calls=calls)
        records.append(record)
        emit(dict(event='pair', **record))
    return records
