"""Deterministic nodes for the per-turn value workflow fixture."""


def intake(inputs, context):
    message = inputs['message']
    return {'topic': message, 'route': 'a' if message.startswith('a') else 'b', 'hint': 'seed',
            'previous': inputs['previous']}


def path_a(inputs, context):
    return {'topic': f'A({inputs["topic"]})', 'extra': f'extra-from-{inputs["topic"]}'}


def path_b(inputs, context):
    return {'topic': f'B({inputs["topic"]})'}


def draft(inputs, context):
    result = {'draft': f'{inputs["topic"]}#{inputs["attempt"]}', 'saw_hint': inputs['hint'],
              'saw_scratch': inputs['scratch']}
    if inputs['attempt'] == 1:
        result.update(hint='h1', scratch='s1')
    return result


def check(inputs, context):
    accepted = inputs['attempt'] >= 2
    return {'accepted': accepted, 'feedback': {'reason': 'accepted' if accepted else 'retry once'}}


def finish(inputs, context):
    return {'answer': inputs['topic'], 'saw_extra': inputs['extra'], 'saw_scratch': inputs['scratch'],
            'saw_hint': inputs['hint']}
