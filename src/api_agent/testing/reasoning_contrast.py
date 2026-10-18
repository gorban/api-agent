"""Three matched application-HTTP pairs using the ordinary integration owner."""
import time
from uuid import uuid4

from api_agent.providers.inference import result_from_wire
from api_agent.providers.progress import component_timeout

MODEL = 'gpt-6-luna'
PUZZLE = ('The variables a, b, c, d, e are the integers 1, 2, 3, 4, 5 in some order, '
          'each used once. They satisfy a+b=6, d=e+2, b=2c, and e>b. '
          'What is 10000a+1000b+100c+10d+e? Reply with only the integer.')


def scope(config, *, owner_root=None):
    """Reject unverified routes before the ordinary fixture authenticates."""
    from pathlib import Path
    from api_agent.providers.loading import provider_location
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.llm_providers.gpt.reasoning import reasoning_efforts
    ModelSelectionPolicy({'LLM_ALLOWED_MODELS': config.get('LLM_ALLOWED_MODELS', '.*')}).require_allowed(MODEL)
    base = 'https://api.openai.com/v1'
    if (config.get('GPT_AUTH_MODE', 'subscription') != 'subscription'
            or config.get('GPT_API_MODE', 'responses') != 'responses'
            or config.get('GPT_BASE_URL', base) != base):
        raise ValueError('Reasoning comparison requires official subscription Responses')
    if not {'low', 'medium'} <= set(reasoning_efforts(MODEL, base_url=base) or ()):
        raise ValueError('Low and medium effort support is unverified')
    location = config.get('LLM_PROVIDER')
    if type(location) is not str or not location:
        raise ValueError('Select the included GPT provider')
    if not Path(location.rpartition(':')[0]).expanduser().is_absolute() and owner_root is None:
        raise ValueError('Relative provider selection requires its owner root')
    path, symbol = provider_location(location, owner_root=owner_root)
    if symbol != 'factory' or not path.samefile(Path(__file__).resolve().parents[1]/'llm_providers/gpt/factory.py'):
        raise ValueError('Reasoning comparison requires the included GPT provider')


def valid_call(row):
    return (row.get('status') == 'completed'
            and type(row.get('reasoning_tokens')) is int and row['reasoning_tokens'] >= 0
            and row.get('requested_model') == MODEL and row.get('actual_model') in (None, MODEL))


def evaluate(records):
    """The signed three-pair smoke threshold is not a population guarantee."""
    differences = []
    if len(records) != 3:
        return dict(passed=False, differences=differences)
    for pair in records:
        calls = pair.get('calls', [])
        if (len(calls) != 2 or [row.get('effort') for row in calls] != ['low', 'medium']
                or not all(valid_call(row) for row in calls)):
            return dict(passed=False, differences=differences)
        differences.append(calls[1]['reasoning_tokens']-calls[0]['reasoning_tokens'])
    return dict(passed=sum(value >= 50 for value in differences) >= 2
                and all(value > -50 for value in differences), differences=differences)


async def run_pairs(client, endpoint, nonce, *, emit=lambda row: None):
    """Send at most six calls, with no retry, replacement or hidden provider."""
    records = []
    for index in range(1, 4):
        marker = str(uuid4())
        prompt = f'Run marker: {marker}\n{PUZZLE}'
        calls = []
        for effort in ('low', 'medium'):
            identifier = str(uuid4())
            row = dict(pair=index, marker=marker, effort=effort,
                       request_id=identifier, requested_model=MODEL)
            emit(dict(event='intent', **row))
            started = time.monotonic()
            try:
                async with component_timeout('Reasoning comparison application request', 150):
                    response = await client.post(endpoint+'/app/agent/chat',
                        headers={'Origin':'http://127.0.0.1:8080', 'X-API-Agent-Session':nonce},
                        json={'session_id':identifier, 'model':MODEL, 'reasoning_effort':effort,
                              'messages':[{'role':'user', 'content':prompt}]})
                    response.raise_for_status()
                    data = response.json()
                    if data.pop('session_id') != identifier:
                        raise ValueError('Application request identity mismatch')
                    data['text'] = data.pop('response_text')
                    result = result_from_wire(data)
                    row.update(status=result.outcome,
                        reasoning_tokens=result.usage.reasoning_tokens, usage=data['usage'],
                        actual_model=result.actual_model, serving_revision=result.serving_revision,
                        billing=result.billing, actual_cost_usd=None if result.billing is None else result.billing['usd'],
                        elapsed_seconds=time.monotonic()-started)
            except BaseException as error:
                emit(dict(row, event='call', status='failed' if isinstance(error, Exception) else 'interrupted',
                          failure_type=type(error).__name__, elapsed_seconds=time.monotonic()-started))
                raise
            emit(dict(event='call', **row))
            if not valid_call(row):
                raise ValueError('Reasoning comparison received an invalid measurement')
            calls.append(row)
        record = dict(pair=index, marker=marker, calls=calls)
        records.append(record)
        emit(dict(event='pair', **record))
    return records
